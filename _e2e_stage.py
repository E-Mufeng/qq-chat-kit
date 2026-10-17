"""端到端验证：阶段策略是否真的管住了说辞热度。

三个场景：
  A. 新对象「初识」说"在吗"        → 期望：有信息量、不暧昧、不叫宝贝
  B. 新对象「初识」说"你长得真好看"  → 期望：不油、不评价外貌
  C. 已到「暧昧」的对象说"最近好想你" → 期望：可以有点张力

注意：AsyncOpenAI client 是模块级单例，绑定首次创建的事件循环，
所以整个脚本必须只 asyncio.run 一次（真实 bot 也是单 loop，无此问题）。
"""
from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

import memory_store
import stage_policy
import rag_retriever
from llm_client import get_reply


async def show(tag: str, uid: str, text: str, want_stage: str | None = None) -> None:
    if want_stage:
        memory_store.upsert_partner(uid, stage=want_stage)
    print(f"\n{'=' * 70}\n[{tag}] 阶段={want_stage or '初识'} 对方说：{text}")
    data = await get_reply(uid, text)
    if data.get("_error"):
        print(f"  [拒发] {data['_error']}")
        return
    reply = data.get("reply", "")
    print(f"  代聊：{reply}")
    st = data.get("stage")
    print(f"  阶段={st} 情绪={data.get('emotional_state')} 话题={data.get('last_topic')}")
    hits = stage_policy.check_boundary(st or "初识", reply)
    print("  分寸校验：" + ("通过 ✅" if not hits else "；".join(hits)))


async def main() -> int:
    for uid in ("T_A", "T_B", "T_C"):
        memory_store.forget_partner(uid)

    print("RAG 索引状态：", rag_retriever.stats())

    await show("A 初识", "T_A", "在吗")
    await show("B 初识", "T_B", "你长得真好看，加个好友呗")
    await show("C 暧昧", "T_C", "最近好想你", want_stage="暧昧")

    for uid in ("T_A", "T_B", "T_C"):
        memory_store.forget_partner(uid)
    print("\n测试对象已清理")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
