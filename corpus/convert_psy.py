#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把"高对口度"素材转成 corpus/text/<source>/*.txt，供 build_index 统一索引。

覆盖四类：
  1. proto-refs    原型仓库（powerycy/goutoujunshi）的 references/**/*.md + SKILL.md
                   —— 密度最高、最贴场景，索引权重最高。
  2. eq-chat       中文暧昧/高情商对话（user/girl）
  3. cei-eq        中文情商语料（instruction/output）
  4. emo-support   中文情绪支持回复（剥掉 [思考过程] 只留最终回复）
  5. baai-psych    BAAI IndustryCorpus2 心理学中文分片（parquet → 纯文本）

用法：
  python corpus/convert_psy.py                 # 全部
  python corpus/convert_psy.py --only proto-refs,eq-chat
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import os
import sys
from pathlib import Path

# 语料根目录：可用环境变量 GT_CORPUS_ROOT 覆盖（不配则用当前目录下的 corpus_data）。
CORPUS_ROOT = Path(os.getenv("GT_CORPUS_ROOT", "corpus_data"))
RAW = CORPUS_ROOT / "raw" / "psy"
TEXT = CORPUS_ROOT / "text"
# 本仓库根目录（convert_psy.py 位于 corpus/ 下）；如需引用其他仓库可用 GT_PROTO_REPO 覆盖。
REPO_ROOT = Path(__file__).resolve().parent.parent
PROTO_REPO = Path(os.getenv("GT_PROTO_REPO", REPO_ROOT))

# 中文字之间的空格（分词语料）→ 去掉，还原连贯中文
_CJK_SP = re.compile(r"(?<=[\u4e00-\u9fff])[ ]+(?=[\u4e00-\u9fff])")

THINK_RE = re.compile(r"^\[思考过程\].*?(?=\n\s*\S)", re.S)
TAG_RE = re.compile(r"\[(思考过程|分析|内心独白)\][^\n]*\n?")


def _w(src: str, blocks: list[tuple[str, str]]) -> int:
    """blocks: [(title, body)] → text/<src>/NNNNN.txt，返回写入字节数。"""
    d = TEXT / src
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for i in range(0, len(blocks), 200):
        chunk = blocks[i:i + 200]
        parts = []
        for t, b in chunk:
            parts.append(f"## {t}\n{b}")
        p = d / f"{i // 200:05d}.txt"
        s = "\n\n".join(parts)
        p.write_text(s, encoding="utf-8")
        n += len(s.encode("utf-8"))
    return n


# ---------------------------------------------------------------- 1. 原型仓库
def conv_proto() -> int:
    if not PROTO_REPO.exists():
        print("  [跳过] 原型仓库不存在:", PROTO_REPO)
        return 0
    files = sorted(PROTO_REPO.rglob("*.md"))
    blocks: list[tuple[str, str]] = []
    for f in files:
        rel = f.relative_to(PROTO_REPO).as_posix()
        # 跳过纯工程类文件，保留知识/实战/方法论
        if rel.split("/")[0] in (".github",):
            continue
        if rel in ("CHANGELOG.md", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md",
                   "SECURITY.md", "LICENSE.zh-CN.md", "README_EN.md"):
            continue
        try:
            body = f.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            continue
        if len(body) < 200:
            continue
        blocks.append((rel, body))
    n = _w("proto-refs", blocks)
    print(f"  proto-refs         {len(blocks):>5} 篇 → {n/1024:.0f} KB")
    return n


# ---------------------------------------------------------------- 2. 暧昧对话
def _load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def conv_eqchat() -> int:
    p = RAW / "eqchat.json"
    if not p.exists():
        return 0
    data = _load_json(p)
    blocks = []
    for i, r in enumerate(data):
        u = (r.get("user") or "").strip()
        g = (r.get("girl") or "").strip()
        if not u or not g:
            continue
        blocks.append((f"暧昧对话 {i}", f"对方：{u}\n回复：{g}"))
    n = _w("eq-chat", blocks)
    print(f"  eq-chat            {len(blocks):>5} 轮 → {n/1024:.0f} KB")
    return n


# ---------------------------------------------------------------- 3. 情商语料
def conv_cei() -> int:
    p = RAW / "cei.json"
    if not p.exists():
        return 0
    data = _load_json(p)
    blocks = []
    for i, r in enumerate(data):
        ins = (r.get("instruction") or "").strip()
        out = (r.get("output") or "").strip()
        if not ins or not out:
            continue
        blocks.append((f"高情商表达 {i}", f"对方：{ins}\n回复：{out}"))
    n = _w("cei-eq", blocks)
    print(f"  cei-eq             {len(blocks):>5} 条 → {n/1024:.0f} KB")
    return n


# ---------------------------------------------------------------- 4. 情绪支持
def conv_emosupport() -> int:
    p = RAW / "emotional_support_zh.jsonl"
    if not p.exists():
        return 0
    blocks = []
    with io.open(p, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            ins = (r.get("instruction") or "").strip()
            out = (r.get("output") or "").strip()
            if not ins or not out:
                continue
            # 剥掉思考过程，只留最终对外回复
            out = THINK_RE.sub("", out).strip()
            out = TAG_RE.sub("", out).strip()
            if len(out) < 8:
                continue
            blocks.append((f"情绪支持 {i}", f"对方：{ins}\n回复：{out}"))
    n = _w("emo-support", blocks)
    print(f"  emo-support        {len(blocks):>5} 条 → {n/1024:.0f} KB")
    return n


# ---------------------------------------------------------------- 5. BAAI 心理
def conv_baai(max_docs: int = 260000) -> int:
    p = RAW / "baai_psych_rank00069.parquet"
    if not p.exists():
        print("  [跳过] BAAI 分片未下载完")
        return 0
    try:
        import pyarrow.parquet as pq
    except ImportError:
        print("  [跳过] 缺 pyarrow；请先 pip install pyarrow")
        return 0
    pf = pq.ParquetFile(str(p))
    cols = pf.schema_arrow.names
    textcol = None
    for c in ("text", "content", "contents", "data", "raw"):
        if c in cols:
            textcol = c
            break
    if textcol is None:
        print("  [跳过] parquet 无正文字段，列 =", cols)
        return 0
    blocks = []
    total = 0
    for batch in pf.iter_batches(batch_size=4000, columns=[textcol]):
        col = batch.column(textcol).to_pylist()
        for t in col:
            if not t:
                continue
            t = str(t).strip()
            if len(t) < 120:
                continue
            blocks.append(("心理学语料", t[:6000]))
            total += len(t)
            if len(blocks) >= max_docs:
                break
        if len(blocks) >= max_docs:
            break
    n = _w("baai-psych", blocks)
    print(f"  baai-psych         {len(blocks):>5} 篇 → {n/1048576:.1f} MB")
    return n


# ---------------------------------------------------------------- 6. 心理学电子书
def _html_to_text(html: str) -> str:
    """html → 纯文本（保留段落结构）。"""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for t in soup(["script", "style"]):
            t.decompose()
        return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()
    except Exception:
        s = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        s = re.sub(r"(?s)<[^>]+>", "\n", s)
        s = re.sub(r"&nbsp;", " ", s)
        return re.sub(r"\n{3,}", "\n\n", s).strip()


def _read_txt(p: Path) -> str:
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            return p.read_text(encoding=enc, errors="strict")
        except Exception:
            continue
    return p.read_text(encoding="utf-8", errors="replace")


def _read_epub(p: Path) -> str:
    from ebooklib import epub
    from ebooklib import ITEM_DOCUMENT
    bk = epub.read_epub(str(p))
    parts = []
    for it in bk.get_items_of_type(ITEM_DOCUMENT):
        try:
            t = _html_to_text(it.get_content().decode("utf-8", "replace"))
        except Exception:
            continue
        if len(t) > 30:
            parts.append(t)
    return "\n\n".join(parts)


def _read_mobi(p: Path) -> str:
    import mobi
    tmp, fp = mobi.extract(str(p))
    try:
        raw = Path(fp).read_bytes().decode("utf-8", "replace")
        return _html_to_text(raw)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def conv_books(max_mb: int = 40) -> int:
    """RAW/books 下 .mobi/.epub/.txt/.html → text/books。"""
    d = CORPUS_ROOT / "raw" / "books"
    if not d.exists():
        print("  [跳过] 无 raw/books")
        return 0
    blocks = []
    ok = fail = 0
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in (".part",):
            continue
        try:
            if ext in (".txt", ".md", ".html", ".htm"):
                t = _html_to_text(p.read_text(encoding="utf-8", errors="replace")) \
                    if ext in (".html", ".htm") else _read_txt(p)
            elif ext == ".epub":
                t = _read_epub(p)
            elif ext == ".mobi":
                t = _read_mobi(p)
            else:
                continue
        except Exception as e:
            fail += 1
            print(f"    [失败] {p.name[:40]}: {type(e).__name__}")
            continue
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
        if len(t) < 800:
            fail += 1
            continue
        ok += 1
        # 长书按 3000 字一片切开，保证检索粒度
        for j in range(0, len(t), 3000):
            piece = t[j:j + 3000].strip()
            if len(piece) >= 200:
                blocks.append((f"{p.stem}#{j//3000}", piece))
    n = _w("books", blocks)
    print(f"  books              {ok:>5} 本(成功) / {fail} 跳过 → {n/1048576:.1f} MB，{len(blocks)} 片")
    return n


# ---------------------------------------------------------------- 7. 心理问答 PsyQA
def conv_psyqa() -> int:
    p = RAW / "psyqa_full.json"
    if not p.exists():
        print("  [跳过] psyqa_full.json 未下载")
        return 0
    data = _load_json(p)
    blocks = []
    for i, r in enumerate(data):
        q = (r.get("question") or "").strip()
        desc = (r.get("description") or "").strip()
        ans = r.get("answers") or []
        if not q or not ans:
            continue
        a = ans[0] if isinstance(ans[0], dict) else {"answer_text": str(ans[0])}
        at = (a.get("answer_text") or "").strip()
        lab = a.get("labels") or a.get("label") or ""
        if isinstance(lab, (list, tuple)):
            lab = "/".join(str(x) for x in lab)
        if len(at) < 20:
            continue
        body = f"提问：{q}"
        if desc:
            body += f"\n补充：{desc[:400]}"
        body += f"\n回复：{at}"
        if lab:
            body += f"\n策略：{lab}"
        blocks.append((f"心理问答 {i}", body))
    n = _w("psyqa", blocks)
    print(f"  psyqa              {len(blocks):>5} 条 → {n/1024:.0f} KB")
    return n


# ---------------------------------------------------------------- 8. 心理咨询教材 PDF
def conv_pdfbooks() -> int:
    """从 RAW/psy 下有文本层的 PDF 抽文本（扫描版自动跳过）→ text/psy-books。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            print("  [跳过] 缺 pypdf")
            return 0
    import io
    import zipfile

    def pdfs():
        for p in sorted(RAW.glob("*.pdf")):
            yield p.name, p.read_bytes()
        z = RAW / "psy_counseling.zip"
        if z.exists():
            with zipfile.ZipFile(z) as zf:
                for i in zf.infolist():
                    if i.filename.lower().endswith(".pdf"):
                        yield i.filename.split("/")[-1], zf.read(i)

    blocks = []
    for name, data in pdfs():
        try:
            r = PdfReader(io.BytesIO(data))
        except Exception as e:
            print(f"    [打不开] {name[:40]}: {type(e).__name__}")
            continue
        chunks = []
        for pg in r.pages:
            try:
                t = pg.extract_text() or ""
            except Exception:
                t = ""
            t = re.sub(r"[ \t\u3000]+", " ", t).strip()
            if t:
                chunks.append(t)
        if len("".join(chunks)) < 2000:
            print(f"    [扫描版·跳过] {name[:50]}")
            continue
        full = "\n".join(chunks)
        for j in range(0, len(full), 3000):
            piece = full[j:j + 3000].strip()
            if len(piece) >= 200:
                blocks.append((f"{Path(name).stem}#{j//3000}", piece))
        print(f"    [有文本层] {name[:50]} {len(full)} 字")
    n = _w("psy-books", blocks)
    print(f"  psy-books          {len(blocks):>5} 片 → {n/1024:.0f} KB")
    return n


# ---------------------------------------------------------------- 9. 知乎真实问答
# 只保留跟"关系 / 情绪 / 沟通"沾边的问题，避免百科式长答把索引灌水
ZHIHU_KW = (
    "恋爱 情侣 男朋友 女朋友 男友 女友 暗恋 暧昧 表白 追求 喜欢 心动 相亲 约会 "
    "分手 复合 前任 异地 婚姻 夫妻 离婚 家庭 婆媳 育儿 亲子 "
    "情绪 抑郁 焦虑 自卑 孤独 压力 内耗 敏感 心理 精神 情绪价值 安全感 依恋 "
    "沟通 聊天 话术 情商 社交 人际 朋友 同事 领导 关系 边界 拒绝 吵架 矛盾 冷暴力 "
    "搭讪 高情商 会说话 冷读 微表情 读心 攻心 女人 男人 女生 男生 追女 追男 魅力"
).split()


def conv_zhihu(min_len: int = 300, max_len: int = 4000) -> int:
    """Zhihu-KOL parquet → text/zhihu（只留关系/情绪/沟通类真实问答）。"""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        print("  [跳过] 缺 pyarrow")
        return 0
    d = CORPUS_ROOT / "raw" / "psy" / "zhihu"
    files = sorted(d.glob("*.parquet")) if d.exists() else []
    if not files:
        print("  [跳过] 无 zhihu parquet")
        return 0
    blocks = []
    seen = 0
    for p in files:
        pf = pq.ParquetFile(str(p))
        for batch in pf.iter_batches(batch_size=2000):
            for r in batch.to_pylist():
                seen += 1
                q = (r.get("INSTRUCTION") or "").strip()
                a = (r.get("RESPONSE") or "").strip()
                if len(a) < min_len:
                    continue
                if not any(k in q for k in ZHIHU_KW):
                    continue
                a = re.sub(r"\n{3,}", "\n\n", a)[:max_len]
                blocks.append((f"知乎问答 {len(blocks)}", f"问题：{q}\n回答：{a}"))
    n = _w("zhihu", blocks)
    print(f"  zhihu              扫描 {seen} 条 → 命中 {len(blocks)} 条 → {n/1048576:.1f} MB")
    return n


# ---------------------------------------------------------------- 10. 真实中文闲聊
def conv_lccc(max_turns: int = 600000) -> int:
    """LCCC 中文开放域多轮对话 → text/lccc（自然接话/日常对话节奏）。"""
    p = RAW / "lccc_base_train.jsonl"
    if not p.exists():
        print("  [跳过] lccc_base_train.jsonl 未下载")
        return 0
    blocks = []
    with io.open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if isinstance(r, list):
                dlg = r
            else:
                dlg = r.get("dialog") or r.get("conversation") or r.get("dialogue")
            if not isinstance(dlg, list) or len(dlg) < 2:
                continue
            # LCCC 是分词后空格隔开的，合并中文字间空格还原成连贯中文
            turns = [_CJK_SP.sub("", str(x).strip()) for x in dlg if str(x).strip()]
            if len(turns) < 2:
                continue
            body = "\n".join(f"对方：{t}" if i % 2 == 0 else f"回复：{t}"
                             for i, t in enumerate(turns[:12]))
            if len(body) < 20:
                continue
            blocks.append((f"日常对话 {len(blocks)}", body))
            if len(blocks) >= max_turns:
                break
    n = _w("lccc", blocks)
    print(f"  lccc               {len(blocks):>5} 组 → {n/1048576:.1f} MB")
    return n


CONV = {
    "proto-refs": conv_proto,
    "eq-chat": conv_eqchat,
    "cei-eq": conv_cei,
    "emo-support": conv_emosupport,
    "baai-psych": conv_baai,
    "books": conv_books,
    "psyqa": conv_psyqa,
    "psy-books": conv_pdfbooks,
    "zhihu": conv_zhihu,
    "lccc": conv_lccc,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    want = [s for s in args.only.split(",") if s] or list(CONV)
    print("转换目标 →", ", ".join(want))
    tot = 0
    for name in want:
        fn = CONV.get(name)
        if not fn:
            print("  未知源:", name)
            continue
        try:
            tot += fn()
        except Exception as e:
            print(f"  [失败] {name}: {type(e).__name__}: {e}")
    print(f"\n合计写入 {tot/1048576:.2f} MB → {TEXT}")


if __name__ == "__main__":
    sys.exit(main())
