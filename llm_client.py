"""LLM 编排层：整合 记忆(memory_store) + 知识路由(knowledge_loader) + 两层 prompt。

每次调用流程：
1. 取该对象记忆上下文（严格单对象，防串号）
2. 路由相关参考知识（RAG-lite）
3. 调 LLM 产出 JSON：reply(对外你本人口吻) + 记忆字段
4. 持久化：更新对象档案 + 追加对话轮
返回解析后的 dict；失败降级为纯文本回复。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from openai import AsyncOpenAI

# 本模块在 import 时就读取 LLM_* 环境变量，所以必须自己先加载项目 .env。
# 否则任何直接 `from llm_client import get_reply` 的入口（debug_cli、脚本、测试）
# 都会拿到默认值 sk- 而 401 —— 这个隐式依赖必须在本层解决，不能指望调用方。
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from goutou_prompt import build_system_prompt, load_user_profile
import memory_store
import knowledge_loader
import stage_policy
import rag_retriever

client = AsyncOpenAI(
    base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
    api_key=os.getenv("LLM_API_KEY", "sk-"),
    timeout=float(os.getenv("LLM_TIMEOUT", "90")),
    max_retries=int(os.getenv("LLM_MAX_RETRIES", "1")),
)
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
STYLE = os.getenv("GT_STYLE", "会撩")
USE_JSON = os.getenv("GT_JSON", "true").lower() == "true"

# RAG：本地语料检索（见 corpus/），关掉则只靠策略文档
RAG_ON = os.getenv("GT_RAG", "true").lower() == "true"
RAG_K = int(os.getenv("GT_RAG_K", "6"))
RAG_CHARS = int(os.getenv("GT_RAG_CHARS", "2000"))


def _extract_json(raw: str) -> dict | None:
    """宽容解析 LLM 输出：允许 markdown 代码块包裹、前后多余文字。

    实测 workbuddy2api 上开 response_format=json_object 反而会让部分模型返回空串，
    因此默认走 GT_JSON=false + prompt 约束，这里负责把各种脏输出捞成 dict。
    """
    s = (raw or "").strip()
    if not s:
        return None
    # 去掉 ```json ... ``` / ``` ... ``` 包裹
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    # 截取第一个 { 到最后一个 }
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    try:
        return json.loads(s)
    except Exception:
        return None


async def _create(messages: list[dict]) -> str:
    """单次 LLM 调用，返回原始文本。"""
    kwargs: dict = {"model": MODEL, "messages": messages,
                    "temperature": 0.8, "max_tokens": 500}
    if USE_JSON:
        kwargs["response_format"] = {"type": "json_object"}
    resp = await client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message
    raw = (msg.content or "").strip()
    # 部分推理模型会把正文落在 reasoning_content
    if not raw:
        raw = (getattr(msg, "reasoning_content", "") or "").strip()
    return raw


def _err(uid: str, msg: str) -> dict:
    print(f"[llm_client][ERROR] uid={uid} {msg}")
    return {"_error": msg, "reply": ""}


# —— 去 AI 味：底层硬拦截 ——
# 命中即视为不合格，触发重写。聚焦"明显 AI 痕迹"，尽量不误伤真人正常表达。
_AI_TELL_PATTERNS = [
    (r"首先|其次|综上所述|总的来说|总而言之|一方面", "结构化枚举（首先/其次/综上）"),
    (r"作为一个\s*AI|作为\s*AI|我是\s*AI|人工智能|大模型", "自称 AI / 暴露模型身份"),
    (r"我建议你|建议你可以|建议你尝试|建议你去|我建议", "助手腔：主动给建议"),
    (r"从心理学角度|从专业角度|从情感角度|从科学角度", "伪专家腔"),
    (r"需要注意的是|值得注意的是|值得一提的是|请记住|请放心", "说教 / 提醒腔"),
    (r"以下是|如上所述|如前所述|下面为你", "报告体"),
    (r"我来帮你|我可以帮你|希望这对你有帮助|希望帮到你|对你有所帮助|希望你能", "客服收尾 / 帮助腔"),
    (r"如果你需要|如果有需要|随时可以问|还有什么我可以帮|需要我", "支持机器人腔"),
    (r"加油哦|为你加油|抱抱你|给你一个拥抱|摸摸头", "尬聊共情腔"),
    (r"很高兴认识你|很荣幸|非常理解|深有同感|感同身受", "过度礼貌套话"),
    (r"请问|请您", "敬语 / 客服腔（请问 / 您）"),
    (r"我理解你的感受|我能感受到|我能想象|你的心情我", "咨询师镜像腔"),
    (r"让我们一起|我们不妨", "教练腔"),
    (r"【[^】]*】", "用【】写旁白 / 标注"),
    (r"\*\*[^*]+\*\*", "用 ** 加粗（小红书 / 报告体）"),
    (r"(?:^|\n)\s*(?:\d+[、.]\s|\d+\))", "出现编号清单 1.2.3."),
    (r"(?:^|\n)\s*[-•·]\s", "出现项目符号清单"),
    (r"哈哈哈哈+", "哈哈哈堆叠过多"),
    (r"！{2,}", "感叹号堆叠 ！！"),
    # —— 被质疑是机器人时的典型送人头破绽（越想证明越假）——
    (r"验证|出题|秒答|在线答|考考你|证明[下给你看]|我证明", "主动验证/出题/证明（机器人破绽）"),
    (r"我(?:是|叫)?\s*(?:机器人|程序|代码)|是\s*机器人|机器人\s*[吗吧]|我是\s*人机|就是\s*(?:人机|机器人)", "暴露身份（机器人/程序/人机）"),
    (r"问得好|说得好|提得(好|对)|你问得", "过度认同式开场（问得好/说得好）"),
]

# 软项：宁可放行也不轻易拒发（长句 / 表情多有时是真人风格）
_AI_SOFT = ("回复过长", "表情堆砌")


def _ai_tells(reply: str) -> list[str]:
    """返回 reply 命中的 AI 痕迹标签列表（空 = 干净）。"""
    if not reply:
        return []
    hits: list[str] = []
    for pat, label in _AI_TELL_PATTERNS:
        if re.search(pat, reply):
            hits.append(label)
    if len(reply) > 260:
        hits.append("回复过长（>260字）像小作文")
    emoji = re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", reply)
    if len(emoji) > 4:
        hits.append(f"表情堆砌（{len(emoji)}个）")
    return hits


async def get_reply(uid: str, text: str, style: str | None = None) -> dict:
    """返回 dict：{reply, stage, emotional_state, last_topic, key_event, dont_repeat}。

    流程：取阶段 → RAG 检索 → 组 prompt → 调 LLM → 收敛阶段 → 分寸校验（越界重写一次）
          → 持久化。任一步失败都不伪造回复，直接报错由上层拒发。
    """
    use_style = style or STYLE
    profile = load_user_profile()
    partner_ctx = memory_store.build_context(uid)

    # 1) 当前阶段（决定说辞热度；新对象一律「初识」）
    cur = (memory_store.get_partner(uid) or {}).get("stage") or "初识"
    if cur not in stage_policy.STAGE_ORDER:
        cur = "初识"

    # 1.5) 深夜降一档热度（只影响本轮，不改真实阶段）
    eff_stage, why = stage_policy.night_adjust(cur)
    if why:
        print(f"[stage] uid={uid} {why}")

    # 2) RAG：按阶段捞素材，作为"辅导层/底层架构参考"（替代 LoRA 微调）。
    #    只供模型吸收思路与表达质感，严禁 1:1 摘抄原句；真实回复必须现编、随语境应变。
    #    索引没建就明说，不静默降级。
    rag_ctx = ""
    if RAG_ON:
        if rag_retriever.available():
            rag_ctx = rag_retriever.retrieve(text, stage=eff_stage, k=RAG_K, max_chars=RAG_CHARS)
            if not rag_ctx:
                print(f"[llm_client] uid={uid} RAG 无命中，本轮只用策略文档")
        else:
            print("[llm_client] RAG 索引不存在（先跑 corpus/fetch_corpus.py + build_index.py）")
    # 文档涨到 49 篇后改成小节级路由，k 提高到 4，但总字数预算不变
    relevant = knowledge_loader.load_relevant(text, k=4, max_chars=1800)

    system = build_system_prompt(profile, partner_ctx, relevant, use_style, eff_stage, rag_ctx)
    user_msg = f"对方说：{text}"
    base = [{"role": "system", "content": system}, {"role": "user", "content": user_msg}]

    # 3) 首次生成
    try:
        raw = await _create(base)
    except Exception as e:
        return _err(uid, f"LLM 调用失败：{type(e).__name__}: {e}")

    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        return _err(uid, f"LLM 未返回可解析 JSON，原文前 200 字：{raw[:200]}")

    # 4) 阶段收敛：一次最多前进一档，防止一步跳到暧昧
    proposed = parsed.get("stage") or cur
    if why and proposed == eff_stage:
        # 模型看到的是"深夜降档后"的阶段，它照着填了 —— 这不是真实回退，改回 cur
        proposed = cur
    final_stage, note = stage_policy.clamp_stage(cur, proposed)
    if note:
        print(f"[stage] uid={uid} {note}")
    parsed["stage"] = final_stage

    # 5) 分寸校验：阶段没到却暧昧/低俗 → 带着违规项重写一次
    #    校验取「真实阶段」与「深夜降档后」中更保守的那个
    check_stage = stage_policy.earlier(final_stage, eff_stage)
    reply = (parsed.get("reply") or "").strip()
    hits = stage_policy.check_boundary(check_stage, reply)
    if hits:
        print(f"[boundary] uid={uid}({check_stage}) " + " | ".join(hits))
    if stage_policy.is_blocked(hits):
        print(f"[boundary] uid={uid} 越界，带违规项重写一次")
        fb = (
            "你上一条回复在当前关系阶段（" + check_stage + "）越界了，必须改：\n"
            + "\n".join(f"- {h}" for h in hits)
            + "\n按【当前关系阶段】里的「可以做/绝对不可以」重写一条同样意思但不过界的回复，"
              "保持口语短句，只输出 JSON。"
        )
        try:
            raw2 = await _create(base + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": fb},
            ])
        except Exception as e:
            return _err(uid, f"越界重写时 LLM 调用失败：{type(e).__name__}: {e}")
        parsed2 = _extract_json(raw2)
        if not isinstance(parsed2, dict):
            return _err(uid, f"越界重写后无法解析 JSON，原文前 200 字：{raw2[:200]}")
        st2, note2 = stage_policy.clamp_stage(cur, parsed2.get("stage") or final_stage)
        parsed2["stage"] = st2
        hits2 = stage_policy.check_boundary(
            stage_policy.earlier(st2, eff_stage), (parsed2.get("reply") or "").strip())
        if stage_policy.is_blocked(hits2):
            return _err(uid, "重写后仍越界，拒发：" + " | ".join(hits2))
        print(f"[boundary] uid={uid} 重写通过，采用新回复")
        parsed = parsed2

    # 6) 去 AI 味硬拦截：命中明显 AI 痕迹 → 带违规项重写（最多 2 轮）
    tells = _ai_tells((parsed.get("reply") or "").strip())
    if tells:
        print(f"[deai] uid={uid} 命中 AI 痕迹：{' | '.join(tells)}")
        ctx = json.dumps(parsed, ensure_ascii=False)
        fb = (
            "你上一条回复有明显的 AI / 客服 / 情感导师味，必须重写成真人随手发的聊天：\n"
            + "\n".join(f"- {t}" for t in tells)
            + "\n去掉所有结构化、建议腔、总结腔、共情套话、敬语、清单、加粗；像朋友在微信上随口回。"
              "保持口语短句，只输出 JSON。"
        )
        for _ in range(2):
            try:
                raw3 = await _create(base + [
                    {"role": "assistant", "content": ctx},
                    {"role": "user", "content": fb},
                ])
            except Exception as e:
                return _err(uid, f"去AI重写时 LLM 调用失败：{type(e).__name__}: {e}")
            parsed3 = _extract_json(raw3)
            if not isinstance(parsed3, dict):
                return _err(uid, f"去AI重写后无法解析 JSON，原文前 200 字：{raw3[:200]}")
            new_tells = _ai_tells((parsed3.get("reply") or "").strip())
            if not new_tells:
                print(f"[deai] uid={uid} 重写通过")
                parsed = parsed3
                break
            tells = new_tells
            ctx = json.dumps(parsed3, ensure_ascii=False)
            fb = ("还是有明显 AI 味，继续重写，必须去掉：\n"
                  + "\n".join(f"- {t}" for t in tells)
                  + "\n像真人随手发，只输出 JSON。")
        else:
            hard = [t for t in tells if not t.startswith(_AI_SOFT)]
            if hard:
                return _err(uid, "去AI重写后仍不达标，拒发：" + " | ".join(hard))
            print(f"[deai] uid={uid} 仅软项残留，放行")

    # 7) 持久化：更新对象档案 + 追加对话轮（严格单对象）
    _persist(uid, text, parsed)
    return parsed


def _persist(uid: str, user_text: str, data: dict) -> None:
    updates = {}
    if data.get("stage"):
        updates["stage"] = data["stage"]
    if data.get("emotional_state"):
        updates["emotional_state"] = data["emotional_state"]
    if data.get("last_topic"):
        updates["last_topic"] = data["last_topic"]
    if data.get("dont_repeat"):
        p = memory_store.get_partner(uid) or {}
        existing = p.get("dont_repeat", [])
        if data["dont_repeat"] not in existing:
            existing.append(data["dont_repeat"])
        # 截断：否则长期运行会让上下文无上限膨胀（key_events 同样只留最近 10 条）
        updates["dont_repeat"] = existing[-10:]
    if data.get("key_event"):
        p = memory_store.get_partner(uid) or {}
        events = p.get("key_events", [])
        events.append(data["key_event"])
        updates["key_events"] = events[-10:]  # 控体积
    if updates:
        memory_store.upsert_partner(uid, **updates)
    memory_store.append_turn(uid, "user", user_text)
    memory_store.append_turn(uid, "assistant", data.get("reply", ""))
