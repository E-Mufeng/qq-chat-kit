"""端到端验证：真实 LLM 代聊 + 多线隔离 + 记忆持久化。

跑完自动清理测试对象记忆。
"""
import asyncio

import memory_store
from llm_client import get_reply

SCRIPT = [
    ("A", "在吗"),
    ("B", "今天加班到十点，累死了"),
    ("A", "周末有空吗，想去爬山"),
    ("B", "你周末一般干嘛"),
    ("A", "上次说的地方你还记得吗"),
]


async def main():
    for pid in ("A", "B", "C"):
        memory_store.forget_partner(pid)

    print("=" * 70)
    print("端到端代聊（真实 LLM：本机 workbuddy2api）")
    print("=" * 70)
    for pid, text in SCRIPT:
        data = await get_reply(pid, text)
        if data.get("_error"):
            print(f"\n[{pid}] 对方：{text}")
            print(f"  ❌ {data['_error']}")
            continue
        print(f"\n[{pid}] 对方：{text}")
        print(f"  代聊：{data.get('reply','')}")
        print(f"  记忆：阶段={data.get('stage')} 情绪={data.get('emotional_state')} "
              f"话题={data.get('last_topic')}")

    print()
    print("=" * 70)
    print("多线隔离复查（A/B 上下文不得互串）")
    print("=" * 70)
    ctx_a = memory_store.build_context("A")
    ctx_b = memory_store.build_context("B")
    ok = True
    # 判据要用"只可能来自另一对象"的原文，不能用双方都可能提到的泛词（如"周末"）
    for marker in ("加班到十点", "最离谱的是哪件", "吐槽特权"):
        if marker in ctx_a:
            print(f"  ❌ A 上下文泄露 B 的原文「{marker}」")
            ok = False
    for marker in ("在吗", "想去爬山", "爬哪座", "记得啊"):
        if marker in ctx_b:
            print(f"  ❌ B 上下文泄露 A 的原文「{marker}」")
            ok = False
    if ok:
        print("  ✅ 隔离层正确：A/B 上下文互不混入对方原文")

    # 额外：内容层串味检测（LLM 是否把 A 的话题带进了给 B 的回复）
    print()
    print("  [内容层提示] 若 B 的回复里出现 A 独有话题（如'爬山'），说明 LLM 自行复用素材，")
    print("               这不是 memory 串号，但会埋下后续混淆隐患 → 需要发前自检。")

    print("\n---- A 的上下文 ----")
    print(ctx_a)
    print("\n---- B 的上下文 ----")
    print(ctx_b)

    for pid in ("A", "B", "C"):
        memory_store.forget_partner(pid)
    print("\n（测试对象记忆已清理）")


asyncio.run(main())
