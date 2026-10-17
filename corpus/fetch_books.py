#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按“远程 zip + HTTP Range”方式只抽取 Mxode/Chinese-Psychology-Books 里
**可提取文本** 的成员（.mobi/.epub/.txt/.md/.html），避免整包 10GB 全量下载。

依赖成熟开源库：remotezip（按 Range 读远端 zip 成员）。

用法：
    python corpus/fetch_books.py                 # 按 plan 拉取全部
    python corpus/fetch_books.py --only 心理学.zip
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

from remotezip import RemoteZip

CORPUS_ROOT = Path(os.getenv("GT_CORPUS_ROOT", "corpus_data"))
RAW = CORPUS_ROOT / "raw"
BOOKS = RAW / "books"
PLAN = RAW / "books_plan.json"

BASE = "https://huggingface.co/datasets/Mxode/Chinese-Psychology-Books/resolve/main/"
KEEP = {"txt", "epub", "mobi", "md", "html", "htm"}


def _fmt(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def scan(zips: list[str] | None = None) -> dict:
    """扫远端 zip 的中央目录，产出 {zip名: [{f, sz, csz}]}。"""
    names = zips or ["Others.zip", "人格心理学.zip", "发展心理学.zip", "变态心理学.zip",
                     "实验心理学.zip", "心理咨询.zip", "心理学(1).zip", "心理学.zip",
                     "心理学教材.zip", "心理学电子书.zip", "心理学研究方法.zip",
                     "心理学经典著作.zip", "心理测量学.zip", "心理统计学.zip",
                     "教育心理学.zip", "普通心理学.zip", "社会心理学.zip", "管理心理学.zip"]
    plan: dict = {}
    for n in names:
        try:
            z = RemoteZip(BASE + urllib.parse.quote(n))
            items = [{"f": i.filename, "sz": i.file_size, "csz": i.compress_size}
                     for i in z.infolist()
                     if not i.is_dir() and i.filename.lower().rsplit(".", 1)[-1] in KEEP]
        except Exception as e:
            print(f"  [扫描失败] {n}: {type(e).__name__}: {e}")
            continue
        s = sum(i["sz"] for i in items)
        print(f"  {n:<20} 可取 {len(items):>4} 个  {_fmt(s):>10}")
        plan[n] = items
    PLAN.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    print("plan →", PLAN)
    return plan


def fetch(plan: dict) -> int:
    BOOKS.mkdir(parents=True, exist_ok=True)
    got = 0
    for zipname, items in plan.items():
        print(f"\n===== {zipname} ({len(items)} 个成员) =====")
        try:
            z = RemoteZip(BASE + urllib.parse.quote(zipname))
        except Exception as e:
            print(f"  [打开失败] {type(e).__name__}: {e}")
            continue
        for k, it in enumerate(items, 1):
            src = it["f"]
            # 用 basename 落盘，同名加序号，规避深层目录与非法字符
            base = src.replace("\\", "/").split("/")[-1]
            safe = "".join(c for c in base if c not in ':*?"<>|\n\r\t')
            dest = BOOKS / safe
            if dest.exists() and dest.stat().st_size >= it["sz"] * 0.95:
                got += 1
                continue
            try:
                data = z.read(src)
            except Exception as e:
                print(f"  [失败] {safe[:50]}: {type(e).__name__}")
                continue
            tmp = dest.with_suffix(dest.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            got += 1
            if k % 20 == 0:
                print(f"  ... {k}/{len(items)}  累计 {got}", flush=True)
    print(f"\n落盘 {got} 个文件 → {BOOKS}")
    return got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="只扫描，更新 plan")
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)
    if args.scan or not PLAN.exists():
        plan = scan([x for x in args.only.split(",") if x] or None)
    else:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
        if args.only:
            want = {x for x in args.only.split(",") if x}
            plan = {k: v for k, v in plan.items() if k in want}
    t0 = time.time()
    fetch(plan)
    print(f"用时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
