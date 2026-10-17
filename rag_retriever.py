"""RAG 检索层：从千万级本地语料里捞出跟"这条消息"最相关的少量素材。

用法（llm_client 里调用）：
    from rag_retriever import retrieve
    ctx = retrieve("今天加班到十点累死了", stage="破冰")

设计：
- 查询串 = 对方原话 + 当前阶段的主题词（阶段不同，捞的素材不同）。
- 检索走 SQLite FTS5 的 bm25()，再按「语料源权重」重排（对话语料 > 诗词 > 百科）。
- 索引不存在时返回空串，由调用方回落到 knowledge_loader（不静默兜底，日志会说）。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import jieba
except ImportError:
    jieba = None  # 类型: ignore

# 不配置 GT_CORPUS_ROOT（或指向的目录没有 corpus.db）时，available() 返回 False，
# 调用方自动回落到 knowledge/ 策略文档，不会报错。
CORPUS_ROOT = Path(os.getenv("GT_CORPUS_ROOT", ""))
DB = CORPUS_ROOT / "corpus.db"

# 语料源权重：越贴近"聊天应对"越高；百科量大但泛，压低避免刷屏
SOURCE_WEIGHT = {
    # —— 第一梯队：贴场景的方法论与对话成品 ——
    "proto-refs": 3.0,               # 原型仓库 52 篇：阶段/边界/话术/心理学，密度最高
    "eq-chat": 2.6,                  # 中文暧昧对话成品（user/girl 成对）
    "cei-eq": 2.2,                   # 中文高情商表达 4 万条
    "emo-support": 2.0,              # 中文情绪支持回复
    "psyqa": 2.3,                    # 真实心理问答 2.2 万条（提问+回复+干预策略）
    # —— 第二梯队：对话/情感语料 ——
    "firefly-1.1M": 1.8,
    "coig-cqia": 1.7,
    "chinese_chatbot_corpus": 1.6,
    "ChineseNlpCorpus": 1.5,
    # —— 第三梯队：专业知识底座 ——
    "baai-psych": 1.3,               # BAAI 医学_健康_心理_中医（量大，作知识底座）
    "psy-books": 1.5,                # 心理咨询/治疗教材（有文本层的 PDF）
    "books": 1.4,                    # 中文心理学电子书（人格/社会/沟通/话术/读心）
    "chinese-poetry": 1.0,
    "zhwiki-1": 0.7,
    "zhwiki-2": 0.7,
    "zhwiki-3": 0.7,
}
DEFAULT_WEIGHT = 0.8

# 每个阶段额外拼接的检索主题词 —— 阶段不同，捞出来的素材类型就不同
STAGE_QUERY: dict[str, str] = {
    "初识": "开场 第一句 破冰 陌生人 怎么开始聊天 加好友 第一印象",
    "破冰": "找话题 共同兴趣 聊不下去 怎么接话 让对方愿意聊",
    "升温": "暧昧 调侃 推拉 昵称 专属感 让对方在意你 幽默",
    "暧昧": "调情 双关 邀约 见面 表达好感 挑逗 分寸",
    "确认": "表白 确定关系 认真 表达心意 承诺",
    "冷却": "对方冷淡 回复慢 降低需求感 要不要主动 收",
    "淘汰": "体面 收尾 不再联系 止损",
}

_STOP = set("""的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己 这 那 他 她 它 我们 你们 他们
啊 吧 呢 吗 哦 嗯 哈 呀 啦 嘛 呗 咯 哦 嘿 唉 哎 可以 就是 不是 这个 那个 什么 怎么""".split())

_db_ok: bool | None = None


def available() -> bool:
    """索引是否就绪。"""
    global _db_ok
    if _db_ok is None:
        _db_ok = DB.exists() and sqlite3.connect(str(DB)).execute(
            "SELECT 1 FROM chunks LIMIT 1").fetchall() != []
    return _db_ok


def _cut(text: str, limit: int = 32) -> list[str]:
    if jieba is None:
        return []
    out: list[str] = []
    for w in jieba.cut(text):
        w = w.strip()
        if len(w) < 2 or w in _STOP or not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", w):
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _match_expr(words: list[str]) -> str:
    """OR 连接；FTS5 对纯中文词可直接加引号。"""
    uniq = list(dict.fromkeys(words))
    return " OR ".join(f'"{w}"' for w in uniq)


def _search(words: list[str], limit: int) -> list[tuple]:
    expr = _match_expr(words)
    c = sqlite3.connect(str(DB))
    try:
        rows = c.execute(
            "SELECT source, title, body, bm25(chunks, 0.0, 6.0, 1.0, 3.0, 0.0) AS s "
            "FROM chunks WHERE chunks MATCH ? ORDER BY s LIMIT ?",
            (expr, limit),
        ).fetchall()
    finally:
        c.close()
    return rows


def retrieve(query: str, stage: str = "初识", k: int = 6, max_chars: int = 2000) -> str:
    """返回拼接好的参考资料文本；无索引或无命中返回空串。

    排序以「对方原话里的词命中了多少个」为主，而不是 bm25 ——
    实测只按 bm25 时，阶段主题词会把大量无关百科段落捞进来（"学吉他"→业余无线电）。
    """
    if not available() or jieba is None:
        return ""
    core = _cut(query)                                   # 对方原话的词（主判据）
    stage_w = _cut(STAGE_QUERY.get(stage, ""), limit=10)  # 阶段主题词（只做加分）

    # 对方原话能切出的实词 < 2 个（"在吗""嗯嗯"这类），没有可检索的信息，
    # 硬凑阶段词只会捞回噪音 —— 宁可不注入。
    if len(core) < 2:
        return ""
    words = core
    try:
        rows = _search(words, k * 8)
        # 只用原话检索不到，再放宽到「原话 + 阶段词」捞一次
        if not rows and stage_w and words is core:
            rows = _search(core + stage_w, k * 8)
    except Exception as e:
        print(f"[rag] 检索失败：{type(e).__name__}: {e}")
        return ""

    core_u = list(dict.fromkeys(core))
    stage_u = list(dict.fromkeys(stage_w))
    scored = []
    for source, title, body, s in rows:
        blob = (title or "") + (body or "")
        hc = sum(1 for w in core_u if w in blob)
        hs = sum(1 for w in stage_u if w in blob)
        # 硬门槛：必须至少命中 1 个"对方原话"的词，且总命中 >= 2
        if (core_u and hc == 0) or (hc + hs) < 2:
            continue
        w = SOURCE_WEIGHT.get(source, DEFAULT_WEIGHT)
        # 主序=覆盖率×源权重，次序=bm25（-s 越大越相关）
        scored.append(((hc * 3 + min(hs, 2)) * w, -s, source, title, body))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    picked: list[str] = []
    seen: set[str] = set()
    used = 0
    for _score, _bm, source, title, body in scored:
        key = hashlib.md5(body[:80].encode("utf-8")).hexdigest()[:12]
        if key in seen:
            continue
        seen.add(key)
        head = f"[{source}]" + (f" {title}" if title else "")
        piece = f"{head}\n{body}".strip()
        if used + len(piece) > max_chars:
            room = max_chars - used
            if room < 120:
                break
            piece = piece[:room] + "…"
        picked.append(piece)
        used += len(piece)
        if len(picked) >= k or used >= max_chars:
            break

    if not picked:
        return ""
    return "【语料参考】（仅作素材与表达参考，不要照抄，要化成你自己的话）\n" + "\n\n".join(picked)


def stats() -> dict:
    if not available():
        return {"ready": False}
    c = sqlite3.connect(str(DB))
    n = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    by_src = dict(c.execute("SELECT source, COUNT(*) FROM chunks GROUP BY source").fetchall())
    c.close()
    return {"ready": True, "chunks": n, "by_source": by_src,
            "db_mb": round(DB.stat().st_size / 1048576)}


if __name__ == "__main__":
    print(stats())
    for q, st in [("今天加班到十点累死了", "升温"),
                  ("在吗", "初识"),
                  ("你喜欢什么类型的电影", "破冰"),
                  ("最近好想你", "暧昧")]:
        print(f"\n=== [{st}] {q} ===")
        print(retrieve(q, st, k=3, max_chars=700) or "(无命中)")
