"""关键词→文档路由（RAG-lite），零依赖，按需注入相关参考，控上下文体积。

- 扫描 knowledge/ 下所有 .md，构建内存索引。
- 对每条 incoming 消息，按子串/二元组重叠打分，取 top-k 相关文档片段注入。
- 保证每次只带最相关的少量参考，避免 43 篇全量灌入炸掉上下文。
"""
from __future__ import annotations

import os
import re

_KNOWLEDGE_DIR = os.path.join(os.path.dirname(__file__), "knowledge")
_docs: list[tuple[str, str]] = []  # (filename, content)


def _load() -> None:
    global _docs
    if _docs:
        return
    if not os.path.isdir(_KNOWLEDGE_DIR):
        return
    for fn in sorted(os.listdir(_KNOWLEDGE_DIR)):
        if fn.endswith(".md"):
            with open(os.path.join(_KNOWLEDGE_DIR, fn), encoding="utf-8") as f:
                _docs.append((fn, f.read()))


def _tokens(text: str) -> list[str]:
    """粗略中文感知分词：去空白→按标点切→取长度>=2 的片段与二元组。"""
    text = re.sub(r"\s+", "", text.lower())
    parts = re.split(r"[，。、！？；：,.\!\?\;\:\n]+", text)
    toks = [p for p in parts if len(p) >= 2]
    grams: set[str] = set()
    for p in toks:
        for i in range(len(p) - 1):
            grams.add(p[i:i + 2])
    return toks + list(grams)


def _sections(name: str, content: str) -> list[tuple[str, str]]:
    """文档 → 小节列表 [(标题路径, 正文)]。

    按 Markdown 标题切分；无标题的长文按空行切段，避免整篇灌入把上下文撑爆。
    """
    lines = content.splitlines()
    out: list[tuple[str, str]] = []
    heads: list[str] = []
    buf: list[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if len(body) >= 40:
            out.append((" / ".join(heads) if heads else name, body))
        buf.clear()

    for ln in lines:
        if re.match(r"^#{1,6}\s", ln):
            flush()
            lv = len(ln) - len(ln.lstrip("#"))
            title = ln.lstrip("#").strip()
            heads = heads[:lv - 1] + [title]
            continue
        if not ln.strip() and len("\n".join(buf)) > 900:
            flush()
            continue
        buf.append(ln)
    flush()
    if not out and content.strip():
        out.append((name, content.strip()))
    return out


_sections_cache: list[tuple[str, str, str]] = []  # (doc名, 标题路径, 正文)


def _ensure_sections() -> list[tuple[str, str, str]]:
    global _sections_cache
    if _sections_cache:
        return _sections_cache
    _load()
    for name, content in _docs:
        for head, body in _sections(name, content):
            _sections_cache.append((name, head, body))
    return _sections_cache


def load_relevant(message: str, k: int = 4, max_chars: int = 1800) -> str:
    """返回与 message 最相关的 top-k **文档小节**（不是整篇）。

    文档从 10 篇涨到 49 篇后，整篇截断会把真正的答案段切掉 ——
    改成小节级检索：先按标题层级切片，再对小节打分，最后按预算拼接。
    """
    secs = _ensure_sections()
    if not secs:
        return ""
    q_toks = _tokens(message)
    if not q_toks:
        return ""
    scored: list[tuple[int, int, str, str, str]] = []
    for name, head, body in secs:
        low = (head + body).lower()
        score = sum(1 for t in q_toks if t and t in low)
        if score == 0:
            continue
        # 小节越长命中越多，用长度做温和惩罚，避免长篇霸榜
        scored.append((score * 1000 // (100 + len(body) // 20), len(body), name, head, body))
    if not scored:
        return ""
    scored.sort(key=lambda x: (x[0], -x[1]), reverse=True)

    out: list[str] = []
    used = 0
    for _s, _len, name, head, body in scored:
        room = max_chars - used
        if room < 150:
            break
        piece = body if len(body) <= room else body[:room] + "…"
        out.append(f"# 参考：{name} › {head}\n{piece}")
        used += len(piece)
        if len(out) >= k:
            break
    return "\n\n".join(out)


def list_docs() -> list[str]:
    _load()
    return [n for n, _ in _docs]


if __name__ == "__main__":
    print("已索引文档：")
    for d in list_docs():
        print(" -", d)
    print("\n测试路由（query='今天好累 想睡觉'）：")
    print(load_relevant("今天好累 想睡觉", k=2)[:500])
