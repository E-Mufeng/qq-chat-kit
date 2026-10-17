"""本地调试 CLI：零 QQ 依赖，验证代聊效果与多线防串号。

用法：
  pip install -r requirements.txt
  cp .env.example .env   # 填 LLM_API_KEY 等
  python debug_cli.py

命令：
  p <id>          切换当前对话对象（如 p A / p B / p C，或任意备注）
  <文本>          以"当前对象"身份说一句话（触发代聊生成）
  /style 稳健|会撩|直接   切换风格
  /stage          显示当前对象的关系阶段与阶段准则
  /stage <阶段名> 手动设定阶段（初识/破冰/升温/暧昧/确认/冷却/淘汰）
  /rag <一句话>   试跑本地语料检索，看能捞出什么素材
  /check <一句话> 只做分寸校验（判断这句话在当前阶段能不能发）
  /reset          清除当前对象记忆（防串号清理演练）
  /who            列出所有对象
  /test           运行多线隔离（串号）自检，确定性、无需联网
  /exit           退出

说明：填了真实 LLM_API_KEY 即进入"在线代聊"模式；否则为"离线模拟"模式（用模板回复，
仅用于走通流程与串号验证）。
"""
from __future__ import annotations

import asyncio
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import memory_store

try:
    import stage_policy
except Exception:
    stage_policy = None

try:
    from llm_client import get_reply
except Exception:
    get_reply = None

# 判定"是否真配了 LLM"。
# ⚠️ 原实现用了 not _KEY.startswith("sk-")，这是致命错：OpenAI / DeepSeek / 硅基流动
#    的正式 key 全部以 sk- 开头，会导致填了真 key 反而退化成离线模拟，永远打不到 LLM。
# 正确判定：key 非空且不是示例占位值。
_PLACEHOLDERS = {"", "sk-", "sk-your-key-here", "your-key-here",
                 "changeme", "none", "null", "ollama"}
_KEY = (os.getenv("LLM_API_KEY", "") or "").strip()
LIVE = bool(get_reply) and _KEY.lower() not in _PLACEHOLDERS

STATE = {"pid": "A", "style": os.getenv("GT_STYLE", "会撩")}


def _run(coro):
    return asyncio.run(coro)


async def _mock_reply(pid: str, text: str) -> dict:
    """离线模板回复：证明流程通，不消耗 API。"""
    mem = memory_store.get_partner(pid)
    name = mem["display_name"] if mem else pid
    return {
        "reply": f"（离线模拟）[{name}] 你说：{text} —— 这是一条像人话的占位回复",
        "stage": "初识", "emotional_state": "未知",
        "last_topic": text[:8], "key_event": "", "dont_repeat": "",
    }


async def _chat(pid: str, text: str) -> dict:
    if LIVE:
        return await get_reply(pid, text, STATE["style"])
    return await _mock_reply(pid, text)


def _cross_test() -> None:
    """确定性串号自检：A/B/C 各装完全不同数据，断言彼此不出现在对方上下文。"""
    for pid in ("A", "B", "C"):
        memory_store.forget_partner(pid)
    memory_store.upsert_partner("A", display_name="小鹿", stage="暧昧",
                                last_topic="上周露营", interests="登山摄影")
    memory_store.append_turn("A", "user", "我们上周露营拍的照片好看吗")
    memory_store.upsert_partner("B", display_name="阿橙", stage="升温",
                                last_topic="咖啡探店", interests="手冲咖啡")
    memory_store.append_turn("B", "user", "那家手冲咖啡店周末去吗")
    memory_store.upsert_partner("C", display_name="柚子", stage="破冰",
                                last_topic="电影宇宙", interests="科幻片")
    memory_store.append_turn("C", "user", "最近的科幻片你看了没")

    # 每个对象上下文里，绝不能出现其他对象独有的关键字
    markers = {"A": "露营", "B": "手冲咖啡", "C": "科幻片"}
    names = {"A": "小鹿", "B": "阿橙", "C": "柚子"}
    ok = True
    print("\n===== 多线隔离（串号）自检 =====")
    for pid in ("A", "B", "C"):
        ctx = memory_store.build_context(pid)
        leaked = [f"{other}={m}" for other, m in markers.items()
                  if other != pid and (m in ctx or names[other] in ctx)]
        if leaked:
            ok = False
            print(f"  [FAIL] {pid} 上下文泄露了：{leaked}")
        else:
            print(f"  [PASS] {pid} 上下文纯净（只含自身数据）")
    print("结果：", "全部通过，不会串号 ✅" if ok else "存在串号风险 ❌")


def _cur_stage(pid: str) -> str:
    p = memory_store.get_partner(pid)
    return (p or {}).get("stage") or "初识"


def _print_stage(pid: str) -> None:
    st = _cur_stage(pid)
    print(f"\n当前对象 {pid} 的阶段：{st}")
    if stage_policy:
        print(stage_policy.stage_block(st))
    print()


def _cmd_rag(query: str) -> None:
    try:
        import rag_retriever
    except Exception as e:
        print(f"  rag_retriever 不可用：{e}")
        return
    st = _cur_stage(STATE["pid"])
    print(f"  [rag] 阶段={st} 索引状态={rag_retriever.stats()}")
    out = rag_retriever.retrieve(query, stage=st, k=5, max_chars=1200)
    print(out or "  （无命中：索引未建，或语料里没有相关内容）")


def _cmd_check(text: str) -> None:
    if not stage_policy:
        print("  stage_policy 不可用")
        return
    st = _cur_stage(STATE["pid"])
    hits = stage_policy.check_boundary(st, text)
    if not hits:
        print(f"  [通过] 在「{st}」阶段可以发：{text}")
        return
    for h in hits:
        print("  " + h)
    print(f"  结论：{'必须拦截（不会发出去）' if stage_policy.is_blocked(hits) else '放行，但注意分寸'}")


async def _main() -> None:
    print(f"狗头军师调试 CLI | 模式：{'在线代聊' if LIVE else '离线模拟'}")
    print("输入 p <id> 切换对象；直接打字=以当前对象身份发言；/test 串号自检；/exit 退出\n")
    while True:
        try:
            line = input(f"[{STATE['pid']}·{_cur_stage(STATE['pid'])}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line == "/exit":
            break
        if line == "/test":
            _cross_test()
            continue
        if line == "/who":
            for p in memory_store.list_partners():
                print(f"  - {p['display_name'] or p['partner_id']} | {p['stage']} | {p['last_topic']}")
            continue
        if line.startswith("/reset"):
            memory_store.forget_partner(STATE["pid"])
            print(f"  已清除 [{STATE['pid']}] 的记忆")
            continue
        if line.startswith("/style"):
            _, _, s = line.partition(" ")
            if s in ("稳健", "会撩", "直接"):
                STATE["style"] = s
                print(f"  风格→{s}")
            continue
        if line.startswith("/rag"):
            _, _, q = line.partition(" ")
            _cmd_rag(q.strip())
            continue
        if line.startswith("/check"):
            _, _, q = line.partition(" ")
            _cmd_check(q.strip())
            continue
        if line.startswith("/stage"):
            _, _, s = line.partition(" ")
            s = s.strip()
            if not s:
                _print_stage(STATE["pid"])
            elif s in stage_policy.STAGE_ORDER:
                memory_store.upsert_partner(STATE["pid"], stage=s)
                print(f"  阶段→{s}")
            else:
                print(f"  未知阶段 {s}，可选：{'/'.join(stage_policy.STAGE_ORDER)}")
            continue
        if line.startswith("p "):
            STATE["pid"] = line[2:].strip() or STATE["pid"]
            print(f"  当前对象→{STATE['pid']}")
            continue
        # 默认：当前对象发言
        data = await _chat(STATE["pid"], line)
        if data.get("_error"):
            print(f"  [拒发，不会发出去] {data['_error']}")
            continue
        print(f"代聊：{data.get('reply','')}")
        print(f"  └ 阶段={data.get('stage') or '-'}"
              f" 情绪={data.get('emotional_state') or '-'}"
              f" 话题={data.get('last_topic') or '-'}")


if __name__ == "__main__":
    _run(_main())
