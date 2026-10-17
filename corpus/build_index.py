"""语料切片 + 建索引（SQLite FTS5 + jieba 预分词）。

为什么用 SQLite FTS5 而不是内存向量库：
- workbuddy2api 没有 embeddings 端点（/v1/embeddings 返回 404），做不了真语义检索；
- FTS5 自带 bm25() 排序，是成熟的全文检索实现，磁盘常驻、内存占用恒定；
- 语料目标 1GB+（约数百万 chunk），内存稀疏矩阵会吃掉数 GB，FTS5 不会；
- 支持增量追加（同一 chunk 用 hash 去重）。

中文分词：FTS5 的 unicode61 对中文不切词，所以写入前先用 jieba 切成空格分隔的
token 串，查询时同样处理 —— 这是中文 FTS5 的通行做法。

用法：
    python corpus/build_index.py            # 增量构建
    python corpus/build_index.py --rebuild  # 全量重建
    python corpus/build_index.py --stats    # 只看统计
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import jieba
except ImportError:
    print("缺少 jieba，请先： pip install jieba")
    raise

CORPUS_ROOT = Path(os.getenv("GT_CORPUS_ROOT", "corpus_data"))
TEXT = CORPUS_ROOT / "text"
DB = CORPUS_ROOT / "corpus.db"

CHUNK_SIZE = 500      # 每片目标字数
CHUNK_OVERLAP = 100   # 重叠，避免切断语义
MIN_CHUNK = 60        # 短于此丢弃
MAX_PER_SOURCE = int(os.getenv("GT_MAX_CHUNKS_PER_SOURCE", "600000"))

jieba.setLogLevel(60)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    source UNINDEXED,
    title,
    body,
    tokens,
    chash UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 0'
);
"""

# 停用词：降低"的/了/是"这类高频虚词对 bm25 的干扰
_STOP = set("""的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己 这 那 他 她 它 我们 你们 他们
个 中 为 与 及 对 从 以 被 把 而 且 或 等 更 最 已 并 但 又 还 可 就 只 才 再 让 给 使 由 于 之 者 所 如 若 因 此 其 该 各 每 些 这 那
啊 吧 呢 吗 哦 嗯 哈 呀 啦 嘛 呗 咯 喔 嘿 唉 哎 唔 呣 呐 咧 喽 噜 噢 嚯 呃 嘞
可以 就是 不是 这个 那个 什么 怎么 为什么 因为 所以 但是 然后 如果 虽然 于是 并且 或者 以及 对于 关于 通过 进行 存在 表示 由于 因此""".split())
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def cut(text: str) -> str:
    """jieba 分词 → 去停用词/标点 → 空格串。"""
    # HMM 关掉：新词发现对检索增益很小，但慢一倍以上（实测 210 片/s → ~400 片/s）
    words = []
    for w in jieba.cut(text, HMM=False):
        w = w.strip()
        if not w or w in _STOP or len(w) < 2 and not ("\u4e00" <= w <= "\u9fff"):
            continue
        if _PUNCT.fullmatch(w):
            continue
        words.append(w)
    return " ".join(words)


def iter_blocks(text: str):
    """先按 ## 标题切大块，再按长度滑窗切片。"""
    parts = re.split(r"\n(?=## )", text)
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p.startswith("## "):
            nl = p.find("\n")
            title = p[3:nl].strip() if nl > 0 else p[3:].strip()
            body = p[nl + 1:].strip() if nl > 0 else ""
        else:
            title, body = "", p
        yield title, body


def sliding(body: str):
    if len(body) <= CHUNK_SIZE:
        yield body
        return
    step = CHUNK_SIZE - CHUNK_OVERLAP
    i = 0
    while i < len(body):
        piece = body[i:i + CHUNK_SIZE]
        if len(piece) >= MIN_CHUNK:
            yield piece
        i += step


def index_stats() -> tuple[int, int]:
    if not DB.exists():
        return 0, 0
    c = sqlite3.connect(DB)
    try:
        n = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        src = c.execute("SELECT COUNT(DISTINCT source) FROM chunks").fetchone()[0]
        return n, src
    finally:
        c.close()


def build(rebuild: bool = False, only: str = "", no_dedup: bool = False) -> None:
    if rebuild and DB.exists():
        DB.unlink()
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(_SCHEMA)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=OFF")
    c.execute("PRAGMA cache_size=-262144")  # 256MB

    seen: set[str] = set()
    if not rebuild and not no_dedup:
        # 增量：已入库的 chunk hash 全部载入，重复的跳过
        for (h,) in c.execute("SELECT chash FROM chunks"):
            seen.add(h)
    elif no_dedup:
        print("  (--no-dedup：跳过已入库 hash 载入，假定本次源均为新增)")

    # 每源配置：max_chunks=上限，sample=抽样步长（跳过的不分词，只切片 → 大幅提速）
    mf: dict = {}
    for p in sorted(CORPUS_ROOT.glob("manifest*.json")):
        try:
            mf.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    max_by_src = {k: int(v.get("max_chunks", MAX_PER_SOURCE) or MAX_PER_SOURCE)
                  for k, v in mf.items()}
    sample_by_src = {k: max(1, int(v.get("sample", 1) or 1)) for k, v in mf.items()}

    sources = sorted(p for p in TEXT.iterdir() if p.is_dir()) if TEXT.exists() else []
    only_set = {x.strip() for x in only.split(",") if x.strip()}
    if only_set:
        sources = [s for s in sources if s.name in only_set]

    t0 = time.time()
    total_new = 0
    for sdir in sources:
        name = sdir.name
        # chinese-poetry 抽取产物是 *.json（实为纯文本），一并纳入
        files = sorted(list(sdir.rglob("*.txt")) + list(sdir.rglob("*.json")))
        if not files:
            print(f"  {name}: 无 .txt/.json，跳过")
            continue
        wrote = 0
        stride = sample_by_src.get(name, 1)
        seen_n = 0
        batch: list[tuple] = []
        for f in files:
            try:
                raw = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for title, body in iter_blocks(raw):
                for piece in sliding(body):
                    seen_n += 1
                    if stride > 1 and seen_n % stride:
                        continue  # 均匀抽样：跳过的只切片不分词，省掉最耗时的环节
                    h = hashlib.md5((name + piece[:200]).encode("utf-8")).hexdigest()[:16]
                    if h in seen:
                        continue
                    seen.add(h)
                    batch.append((name, title, piece, cut((title + " " + piece)[:1200]), h))
                    wrote += 1
                    if len(batch) >= 2000:
                        c.executemany(
                            "INSERT INTO chunks(source,title,body,tokens,chash) VALUES(?,?,?,?,?)", batch)
                        batch.clear()
            cap = max_by_src.get(name, MAX_PER_SOURCE)
            if wrote >= cap:
                print(f"  {name}: 达到单源上限 {cap}，停止")
                break
        if batch:
            c.executemany(
                "INSERT INTO chunks(source,title,body,tokens,chash) VALUES(?,?,?,?,?)", batch)
            batch.clear()
        # 记录已处理文件的 hash，供增量跳过
        for f in files:
            fh = hashlib.md5(str(f).encode("utf-8")).hexdigest()[:16]
            c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", ("f:" + fh, str(f)))
        c.commit()
        total_new += wrote
        extra = f"（抽样 1/{stride}，扫过 {seen_n} 片）" if stride > 1 else ""
        print(f"  {name}: 新增 {wrote} 片（累计 {total_new}）{extra}", flush=True)

    c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
              ("built_at", time.strftime("%Y-%m-%d %H:%M:%S")))
    c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
              ("text_bytes", str(sum(f.stat().st_size for s in sources for f in s.rglob("*.txt")))))
    c.commit()
    n = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    c.close()
    print(f"\n完成：索引共 {n} 片，本次新增 {total_new}，用时 {time.time() - t0:.0f}s")
    print(f"索引文件：{DB}  ({DB.stat().st_size / 1048576:.0f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--no-dedup", action="store_true")
    a = ap.parse_args()
    if a.stats:
        n, s = index_stats()
        print(f"索引：{n} 片，来自 {s} 个语料源，DB {DB.stat().st_size / 1048576:.0f} MB" if DB.exists() else "索引不存在")
    else:
        build(a.rebuild, a.only, a.no_dedup)
