"""QQ 私聊代聊 + 自动扩列插件。

安全边界（已修复 3 个 P0）：

P0-2 管理命令只对 SUPERUSERS 生效。陌生人发 /who 一律**静默忽略、不回任何消息**，
      否则对方一条命令就能拿到你全部对象的昵称/阶段/话题，并暴露你在跑 bot。

P0-3 代聊对象白名单。默认只代聊白名单内的 QQ 号；
      QQ_ALLOW_ALL=true 才对所有人开放（危险，默认 false）。
      扩列时你亲自批准的好友会自动写入白名单。

P0-4 开场白差异化。原模板含未替换的 "xx" 占位符且只做 random.choice，
      等于群发同一条——正好撞腾讯"同消息多收件人"检测。现按对方昵称/附言
      生成专属锚点，每条都不同。

多线隔离仍由 memory_store 按 partner_id 保证，本插件不跨对象传数据。
"""
import asyncio
import datetime
import json
import os
import random
from pathlib import Path

from nonebot import on_message, on_request
from nonebot.adapters.onebot.v11 import Bot, FriendRequestEvent, PrivateMessageEvent
from nonebot.rule import Rule

from llm_client import get_reply
import memory_store
import stage_policy

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WHITELIST_FILE = DATA_DIR / "whitelist.json"
HANDLER_LOG = DATA_DIR / "handler.log"


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(text: str) -> None:
    """处理过程落盘（UTF-8），便于事后复盘与 /review 查看。"""
    try:
        with open(HANDLER_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{_stamp()}] {text}\n")
    except Exception:
        pass


async def _report_to_owner(bot, text: str) -> None:
    """审核/跳过等情况：落盘 + 私聊回执给号主(SUPERUSERS)，让号主在 QQ 上即可看到处理。"""
    _log(text)
    for su in SUPERUSERS:
        try:
            await bot.send_private_msg(user_id=int(su), message=text)
        except Exception:
            pass

AUTO_REPLY = os.getenv("QQ_AUTO_REPLY", "true").lower() == "true"
AUTO_KUOLIE = os.getenv("QQ_AUTO_KUOLIE", "true").lower() == "true"
REPLY_DELAY = float(os.getenv("QQ_REPLY_DELAY", "3") or 3)
# 危险开关：true = 对任何私聊者都代聊。默认 false，必须走白名单。
ALLOW_ALL = os.getenv("QQ_ALLOW_ALL", "false").lower() == "true"

DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------- UID 集合解析（兼容 JSON 数组与逗号分隔） ----------

def _load_uid_set(raw: str) -> set[str]:
    raw = (raw or "").strip()
    if not raw:
        return set()
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return {str(x).strip() for x in v if str(x).strip()}
        if isinstance(v, str):
            return {x.strip() for x in v.split(",") if x.strip()}
    except Exception:
        pass
    return {x.strip() for x in raw.split(",") if x.strip()}


SUPERUSERS = _load_uid_set(os.getenv("SUPERUSERS", ""))
# .env 里额外配的初始白名单（可选）
ENV_WHITELIST = _load_uid_set(os.getenv("QQ_WHITELIST", ""))


# ---------- 白名单 ----------

def _load_whitelist() -> set[str]:
    s = set(ENV_WHITELIST)
    try:
        data = json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            s |= {str(x) for x in data}
    except Exception:
        pass
    return s


def _save_whitelist(uids: set[str]) -> None:
    WHITELIST_FILE.write_text(
        json.dumps(sorted(uids - set(ENV_WHITELIST)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _add_whitelist(uid: str) -> None:
    s = _load_whitelist()
    if uid in s:
        return
    s.add(uid)
    _save_whitelist(s)


def _remove_whitelist(uid: str) -> None:
    s = _load_whitelist()
    if uid not in s:
        return
    s.discard(uid)
    _save_whitelist(s)


def _allowed(uid: str) -> bool:
    """是否允许对该 QQ 号代聊。"""
    if ALLOW_ALL:
        return True
    if uid in SUPERUSERS:
        return True
    return uid in _load_whitelist()


# ---------- 开场白（P0-4：差异化，带对方专属锚点） ----------

OPENERS = [
    "{anchor}，头像这地方我也想去，是刚去的？",
    "刚刷到你，{anchor}这名字有点意思哈",
    "你打招呼这句挺有意思，通过了就别光躺着不说话哈",
    "{anchor}——这缘分不给个好友位说不过去吧",
    "{anchor}？刚通过就冒个泡，不介意吧",
]


def _build_opener(nick: str, comment: str) -> str:
    """每条开场白都带对方专属锚点（昵称/好友请求附言），绝不群发同一条。"""
    anchor = (nick or "").strip()
    if not anchor:
        c = (comment or "").strip()
        anchor = f"「{c[:10]}」" if c else ""
    if not anchor:
        anchor = "你这个号"
    return random.choice(OPENERS).format(anchor=anchor)


async def _nick_of(bot: Bot, uid: str) -> str:
    """取对方昵称，失败返回空串（不中断主流程）。"""
    try:
        info = await bot.call_api("get_stranger_info", user_id=int(uid), no_cache=False)
        return (info.get("nickname") or "").strip()
    except Exception:
        return ""


# ---------- 匹配器 ----------

def _private_only(event) -> bool:
    """只处理私聊：群消息根本不进 handler（比在 handler 里 isinstance 过滤更干净）。"""
    return isinstance(event, PrivateMessageEvent)


msg_matcher = on_message(priority=5, block=False, rule=Rule(_private_only))
req_matcher = on_request(priority=5, block=False)

_STYLE_MAP: dict[str, str] = {}


@msg_matcher.handle()
async def handle_private(bot: Bot, event: PrivateMessageEvent) -> None:
    text = event.get_plaintext().strip()
    if not text:
        return
    uid = str(event.user_id)

    # ===== 命令区：仅 SUPERUSERS；陌生人一律静默返回（不发任何消息） =====
    if text.startswith("/"):
        if uid not in SUPERUSERS:
            return

        if text.startswith("/style"):
            parts = text.split()
            if len(parts) == 2 and parts[1] in ("稳健", "会撩", "直接"):
                _STYLE_MAP[uid] = parts[1]
                await msg_matcher.finish(f"已切换为【{parts[1]}】风格")
            return

        if text.startswith("/reset"):
            memory_store.forget_partner(uid)
            await msg_matcher.finish("已清除与你的记忆（防串号清理）")
            return

        if text.startswith("/who"):
            partners = memory_store.list_partners()
            txt = "\n".join(
                f"- {p['display_name'] or p['partner_id']} | {p['stage']} | {p['last_topic']}"
                for p in partners
            ) or "（暂无对象）"
            await msg_matcher.finish(f"当前对象：\n{txt}")
            return

        if text.startswith("/review"):
            try:
                lines = HANDLER_LOG.read_text(encoding="utf-8").splitlines()[-25:]
            except Exception:
                lines = []
            body = "\n".join(lines) if lines else "（暂无处理记录）"
            await msg_matcher.finish("最近处理记录：\n" + body[-1500:])
            return

        if text.startswith("/test"):
            payload = text[len("/test"):].strip()
            if not payload:
                await msg_matcher.finish(
                    "用法：/test <对方说的话>\n"
                    "会用狗头军师逻辑（RAG+LLM+去AI味+分寸校验）生成拟回并回执给你，"
                    "不实际发送给任何人，也不污染真实对象记忆。")
            res = await get_reply("test_partner", payload, _STYLE_MAP.get(uid))
            if res.get("_error"):
                await msg_matcher.finish("[test 拒发] " + res["_error"])
            reply = res.get("reply", "")
            stage = res.get("stage") or "初识"
            topic = res.get("last_topic") or "（暂无）"
            await msg_matcher.finish(
                f"【狗头军师·自测拟回】\n对方说：{payload}\n拟回：{reply}\n"
                f"（阶段：{stage}｜话题：{topic}）")
            return

        if text.startswith("/allow"):
            parts = text.split()
            if len(parts) == 2 and parts[1].isdigit():
                _add_whitelist(parts[1])
                await msg_matcher.finish(f"已允许代聊：{parts[1]}")
            return

        if text.startswith("/deny"):
            parts = text.split()
            if len(parts) == 2 and parts[1].isdigit():
                _remove_whitelist(parts[1])
                await msg_matcher.finish(f"已停止代聊：{parts[1]}")
            return

        if text.startswith("/wl"):
            wl = sorted(_load_whitelist())
            await msg_matcher.finish("代聊白名单：\n" + ("\n".join(wl) or "（空）"))
            return

        if text.startswith("/stage"):
            # 查看/手动修正关系阶段（阶段决定说辞热度，修错了会聊跑人）
            parts = text.split()
            cur = (memory_store.get_partner(uid) or {}).get("stage") or "初识"
            if len(parts) == 1:
                await msg_matcher.finish(f"当前阶段：{cur}")
                return
            if parts[1] in stage_policy.STAGE_ORDER:
                memory_store.upsert_partner(uid, stage=parts[1])
                await msg_matcher.finish(f"阶段：{cur} → {parts[1]}")
            else:
                await msg_matcher.finish("可选阶段：" + "/".join(stage_policy.STAGE_ORDER))
            return

        return

    # ===== 代聊白名单（P0-3）=====
    if not _allowed(uid):
        msg = (f"[跳过] 对方 {uid} 不在代聊白名单，未处理。\n"
               f"对方原话：{text}\n"
               f"→ 在 QQ 给机器人发「/allow {uid}」即可让狗头军师开始代聊并回执给你。")
        print(msg)
        await _report_to_owner(bot, msg)
        return

    # ===== 代聊生成 =====
    data = await get_reply(uid, text, _STYLE_MAP.get(uid))
    if data.get("_error"):
        msg = f"[拒发] 对方 {uid} 原话：{text}\n原因：{data['_error']}"
        print(msg)
        await _report_to_owner(bot, msg)
        return
    reply = data.get("reply", "")
    if not reply:
        return

    if not AUTO_REPLY:
        stage = data.get("stage") or "初识"
        topic = data.get("last_topic") or "（暂无）"
        msg = (
            f"【狗头军师·审核拟回】\n"
            f"对方 {uid}：{text}\n"
            f"拟回：{reply}\n"
            f"（阶段：{stage}｜话题：{topic}）\n"
            f"—审核模式不会实际发出；确认 OK 后改 QQ_AUTO_REPLY=true 即自动代发。"
        )
        print(msg)
        await _report_to_owner(bot, msg)
        return

    await asyncio.sleep(random.uniform(1, min(REPLY_DELAY, 10)))
    await msg_matcher.finish(reply)


@req_matcher.handle()
async def handle_friend_request(bot: Bot, event: FriendRequestEvent) -> None:
    if not isinstance(event, FriendRequestEvent):
        return
    if not AUTO_KUOLIE:
        return

    uid = str(event.user_id)
    try:
        await bot.call_api("set_friend_add_request", flag=event.flag, approve=True)
    except Exception:
        return

    nick = await _nick_of(bot, uid)
    opener = _build_opener(nick, getattr(event, "comment", "") or "")

    # 初始化该对象记忆（阶段=初识），防止后续串号
    memory_store.upsert_partner(uid, stage="初识", display_name=nick)
    # 你已亲自批准该好友请求 → 视为授权代聊，自动入白名单
    _add_whitelist(uid)

    await asyncio.sleep(random.uniform(2, 6))
    try:
        await bot.send_private_msg(user_id=int(uid), message=opener)
    except Exception:
        pass
