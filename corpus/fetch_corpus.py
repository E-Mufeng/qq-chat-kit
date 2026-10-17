"""语料采集器：把远程中文语料拉到 CORPUS_ROOT/raw，并抽取为纯文本 CORPUS_ROOT/text。

设计原则：
- 纯标准库（urllib / bz2 / zipfile），不引入额外依赖。
- 单源失败不影响其它源，全部记录到 manifest.json。
- 支持 --only 指定源、--speed-test 只测速不下全量。
- 断点续传：已存在且大小匹配则跳过。

用法：
    python corpus/fetch_corpus.py --speed-test
    python corpus/fetch_corpus.py
    python corpus/fetch_corpus.py --only zhwiki-1,chinese-poetry
"""
from __future__ import annotations

import argparse
import bz2
import json
import os
import re
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

CORPUS_ROOT = Path(os.getenv("GT_CORPUS_ROOT", "corpus_data"))
RAW = CORPUS_ROOT / "raw"
TEXT = CORPUS_ROOT / "text"
MANIFEST = CORPUS_ROOT / "manifest.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# kind: zip = GitHub 仓库压缩包，取其中的文本类文件
#       wiki = 维基百科 xml.bz2 dump，走 wikiextractor 抽取
SOURCES: list[dict] = [
    {
        "name": "zhwiki-1",
        "kind": "wiki",
        "url": "https://dumps.wikimedia.org/zhwiki/latest/zhwiki-latest-pages-articles1.xml-p1p187712.bz2",
        "desc": "中文维基百科正文分片1（常识/话题知识底座）",
    },
    {
        "name": "zhwiki-2",
        "kind": "wiki",
        "url": "https://dumps.wikimedia.org/zhwiki/latest/zhwiki-latest-pages-articles2.xml-p187713p630160.bz2",
        "desc": "中文维基百科正文分片2",
    },
    {
        "name": "zhwiki-3",
        "kind": "wiki",
        "url": "https://dumps.wikimedia.org/zhwiki/latest/zhwiki-latest-pages-articles3.xml-p630161p1389648.bz2",
        "desc": "中文维基百科正文分片3",
    },
    {
        "name": "chinese-poetry",
        "kind": "zip",
        "url": "https://github.com/chinese-poetry/chinese-poetry/archive/refs/heads/master.zip",
        "desc": "全量古诗词（唐诗宋词元曲等），文艺表达/接话素材",
    },
    {
        "name": "chinese_chatbot_corpus",
        "kind": "zip",
        "url": "https://github.com/codemayq/chinese_chatbot_corpus/archive/refs/heads/master.zip",
        "desc": "中文聊天语料（含豆瓣多轮对话、微博、PTT 等）",
    },
    {
        "name": "ChineseNlpCorpus",
        "kind": "zip",
        "url": "https://github.com/SophonPlus/ChineseNlpCorpus/archive/refs/heads/master.zip",
        "desc": "中文 NLP 语料集合（对话/评论/情感等多域）",
    },
    {
        # 对口度最高：110 万条中文多任务语料，含大量闲聊 / 情感 / 角色扮演
        "name": "firefly-1.1M",
        "kind": "raw",
        "url": "https://huggingface.co/datasets/YeungNLP/firefly-train-1.1M/resolve/main/firefly-train-1.1M.jsonl",
        "desc": "110 万条中文多任务语料（闲聊/情感/对话为主，1.1GB）",
        "max_chunks": 1500000,
    },
    {
        "name": "coig-cqia",
        "kind": "raw",
        "url": "https://huggingface.co/datasets/m-a-p/COIG-CQIA/resolve/main/COIG-CQIA-full.jsonl",
        "desc": "高质量中文真实语料（豆瓣/知乎/小红书/对话等）",
        "max_chunks": 500000,
    },
]

# zip 里值得抽取的文本后缀
TEXT_EXT = {".txt", ".md", ".json", ".jsonl", ".csv", ".tsv"}


def _http_head_size(url: str) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
    with urllib.request.urlopen(req, timeout=45) as r:
        return int(r.headers.get("Content-Length", 0))


def _fmt(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def speed_test(seconds: int = 8) -> None:
    """对每个源下 8 秒，估算可达性与带宽，不落全量文件。"""
    print(f"{'源':<26}{'声明大小':>12}{'8s实测':>12}{'估计速率':>14}  状态")
    print("-" * 90)
    for s in SOURCES:
        name, url = s["name"], s["url"]
        try:
            sz = _http_head_size(url)
        except Exception as e:
            print(f"{name:<26}{'-':>12}{'-':>12}{'-':>14}  HEAD失败 {type(e).__name__}")
            continue
        got, t0 = 0, time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                while time.time() - t0 < seconds:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    got += len(chunk)
        except Exception as e:
            print(f"{name:<26}{_fmt(sz):>12}{_fmt(got):>12}{'-':>14}  下载失败 {type(e).__name__}")
            continue
        el = time.time() - t0
        rate = got / el if el > 0 else 0
        eta = f"{sz / rate / 60:.1f}分钟" if rate > 0 and sz else "-"
        print(f"{name:<26}{_fmt(sz):>12}{_fmt(got):>12}{_fmt(rate) + '/s':>14}  预计全量 {eta}")


def download(name: str, url: str, expect: int = 0) -> Path | None:
    """下载到 RAW/name.<ext>；支持断点续传，返回最终路径。"""
    RAW.mkdir(parents=True, exist_ok=True)
    ext = os.path.splitext(url.split("?")[0])[1] or ".bin"
    dest = RAW / f"{name}{ext}"
    part = dest.with_suffix(dest.suffix + ".part")

    pos = part.stat().st_size if part.exists() else 0
    if dest.exists() and (expect == 0 or dest.stat().st_size >= expect):
        print(f"    [跳过] 已存在 {dest.name} ({_fmt(dest.stat().st_size)})")
        return dest

    headers = {"User-Agent": UA}
    if pos:
        headers["Range"] = f"bytes={pos}-"
        print(f"    [续传] 从 {_fmt(pos)} 继续")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            total = int(r.headers.get("Content-Length", 0)) or expect
            if r.status == 200 and pos:  # 服务器不支持 Range，从头写
                pos = 0
            mode = "ab" if pos and r.status == 206 else "wb"
            t0 = time.time()
            done = pos
            with open(part, mode) as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if int(time.time() - t0) % 10 == 0:
                        pct = f"{done / total * 100:.1f}%" if total else "-"
                        print(f"    ... {_fmt(done)} / {_fmt(total)} {pct}", flush=True)
    except Exception as e:
        print(f"    [失败] {name}: {type(e).__name__}: {e}")
        return None

    if part.exists():
        part.replace(dest)
    size = dest.stat().st_size if dest.exists() else 0
    print(f"    [完成] {dest.name} {_fmt(size)} 用时 {time.time() - t0:.0f}s")
    return dest if size else None


# ---------------- 抽取 ----------------

_WS = re.compile(r"[ \t\u3000]+")
_BLANK = re.compile(r"\n{3,}")


def _clean(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _WS.sub(" ", s)
    s = _BLANK.sub("\n\n", s)
    return s.strip()


def _extract_json_text(obj) -> str:
    """从任意 JSON 结构里捞出中文长文本串。"""
    out: list[str] = []
    if isinstance(obj, str):
        if len(obj) >= 4 and re.search(r"[\u4e00-\u9fff]", obj):
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.append(_extract_json_text(v))
    elif isinstance(obj, list):
        for v in obj:
            out.append(_extract_json_text(v))
    return "\n".join(x for x in out if x)


def extract_zip(name: str, zpath: Path) -> int:
    outdir = TEXT / name
    outdir.mkdir(parents=True, exist_ok=True)
    written = 0
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            p = Path(info.filename)
            if p.suffix.lower() not in TEXT_EXT:
                continue
            if info.file_size > 200 * 1024 * 1024:  # 单文件 >200MB 跳过
                continue
            try:
                raw = z.read(info).decode("utf-8", errors="ignore")
            except Exception:
                continue
            if p.suffix.lower() in (".json", ".jsonl"):
                try:
                    data = json.loads(raw)
                except Exception:
                    data = None
                    lines = []
                    for ln in raw.splitlines():
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            lines.append(_extract_json_text(json.loads(ln)))
                        except Exception:
                            lines.append(ln)
                    data = "\n".join(lines)
                text = _extract_json_text(data) if not isinstance(data, str) else data
            else:
                text = raw
            text = _clean(text)
            if len(text) < 40:
                continue
            safe = re.sub(r"[^\w.\-]", "_", p.name)
            with open(outdir / safe, "w", encoding="utf-8") as f:
                f.write(text)
            written += len(text.encode("utf-8"))
    return written


def extract_raw(name: str, path: Path) -> int:
    """jsonl / txt 语料：逐行取出中文字段，按批写成 .txt（每条样本独立成段）。"""
    outdir = TEXT / name
    outdir.mkdir(parents=True, exist_ok=True)
    written = 0
    idx = 0
    buf: list[str] = []
    nline = 0
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            nline += 1
            if path.suffix.lower() in (".jsonl", ".json"):
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                piece = _clean(_extract_json_text(obj))
            else:
                piece = _clean(line)
            if len(piece) < 20:
                continue
            buf.append(piece)
            if len(buf) >= 3000:
                written += _flush(outdir / f"part_{idx:04d}.txt", buf)
                buf, idx = [], idx + 1
    if buf:
        written += _flush(outdir / f"part_{idx:04d}.txt", buf)
    print(f"    共读 {nline} 行，产出 {idx + 1} 个分片")
    return written


def extract_wiki(name: str, bzpath: Path) -> int:
    """用 wikiextractor（成熟开源方案，CLI 调用）把 xml.bz2 抽成纯文本。

    输出：TEXT/name/wiki_NN.txt，每个文件是一批条目的「标题 + 正文」。
    """
    import subprocess

    outdir = TEXT / name
    outdir.mkdir(parents=True, exist_ok=True)
    tmp = CORPUS_ROOT / "tmp" / name
    tmp.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "wikiextractor.WikiExtractor",
        str(bzpath),
        "-o", str(tmp),
        "-b", "100M",
        "--json",
        "--no-templates",
        "--processes", "1",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"wikiextractor rc={r.returncode}: {(r.stderr or '')[-400:]}")

    written = 0
    idx = 0
    buf: list[str] = []
    for f in sorted(tmp.rglob("wiki_*")):
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                title = (obj.get("title") or "").strip()
                body = _clean(obj.get("text") or "")
                if len(body) < 60:
                    continue
                buf.append(f"## {title}\n{body}\n")
                if len(buf) >= 2000:
                    written += _flush(outdir / f"wiki_{idx:04d}.txt", buf)
                    buf, idx = [], idx + 1
    if buf:
        written += _flush(outdir / f"wiki_{idx:04d}.txt", buf)
    return written


def _flush(path: Path, chunks: list[str]) -> int:
    s = "\n\n".join(chunks)
    with open(path, "w", encoding="utf-8") as f:
        f.write(s)
    return len(s.encode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed-test", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--no-extract", action="store_true")
    ap.add_argument("--manifest", default="manifest.json",
                    help="并发跑多个采集进程时用不同文件名，避免互相覆盖")
    args = ap.parse_args()

    global MANIFEST
    MANIFEST = CORPUS_ROOT / args.manifest

    CORPUS_ROOT.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    TEXT.mkdir(parents=True, exist_ok=True)

    if args.speed_test:
        speed_test()
        return 0

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    targets = [s for s in SOURCES if not only or s["name"] in only]

    manifest: dict = {}
    if MANIFEST.exists():
        try:
            manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

    for s in targets:
        print(f"\n===== {s['name']} | {s['desc']} =====")
        rec = manifest.get(s["name"], {})
        try:
            expect = _http_head_size(s["url"])
        except Exception:
            expect = 0
        rec["url"] = s["url"]
        rec["kind"] = s["kind"]
        rec["desc"] = s.get("desc", "")
        rec["max_chunks"] = s.get("max_chunks", 600000)
        rec["expect_bytes"] = expect
        t0 = time.time()
        path = download(s["name"], s["url"], expect)
        if not path:
            rec["status"] = "download_failed"
            manifest[s["name"]] = rec
            MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            continue
        rec["raw_bytes"] = path.stat().st_size
        rec["raw_path"] = str(path)

        if args.no_extract:
            rec["status"] = "downloaded"
            manifest[s["name"]] = rec
            MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            continue

        print(f"    抽取纯文本 ...")
        try:
            if s["kind"] == "zip":
                nbytes = extract_zip(s["name"], path)
            elif s["kind"] == "raw":
                nbytes = extract_raw(s["name"], path)
            else:
                nbytes = extract_wiki(s["name"], path)
            rec["text_bytes"] = nbytes
            rec["status"] = "ok"
            print(f"    [抽取完成] {_fmt(nbytes)}")
        except Exception as e:
            rec["status"] = f"extract_failed: {type(e).__name__}: {e}"
            print(f"    [抽取失败] {type(e).__name__}: {e}")
        rec["elapsed_s"] = round(time.time() - t0)
        manifest[s["name"]] = rec
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== 汇总 =====")
    tot_raw = tot_txt = 0
    for k, v in manifest.items():
        print(f"  {k:<26} {v.get('status'):<28} raw={_fmt(v.get('raw_bytes', 0)):>10} text={_fmt(v.get('text_bytes', 0)):>10}")
        tot_raw += v.get("raw_bytes", 0)
        tot_txt += v.get("text_bytes", 0)
    print(f"  合计：原始 {_fmt(tot_raw)}，纯文本 {_fmt(tot_txt)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
