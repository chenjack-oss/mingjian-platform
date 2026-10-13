"""
手动对话脚本（不进冒烟测试，只用于人工验收四档行为）

跑法：
    python tests/chat_cli.py direct   "用一句话解释什么是 RAG"
    python tests/chat_cli.py auto     "2026 年苏州 AI 应用开发岗位薪资行情"
    python tests/chat_cli.py research "调研苏州 AI 岗位现状，写一份带来源链接的报告"
    python tests/chat_cli.py rag      "知识库里有什么"

这个脚本存在的理由：
"四档到底有没有按预期工作"这件事，靠单测断言不出来 ——
direct 是不是真的没调工具、auto 是不是该查才查、research 有没有写文档，
都得真跑一遍看工具调用清单。所以留一个能直接打真人问题的入口。

验收判据（2026-09-27 实测基线，qwen-plus / qwen-flash）：
    direct   ~7s    tools=[]                                 零工具、直接答
    auto     8~30s  tools=[] 或 ['internet_search']           按需检索，不反问
    research ~67s   tools=['internet_search','write_document'] 成稿且引用可溯源
    rag      —      tools=[]                                  诚实降级 rag_not_wired
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from src import context  # noqa: E402
from src.agent.core import run_agent  # noqa: E402


async def main() -> None:
    tier = sys.argv[1] if len(sys.argv) > 1 else "auto"
    query = sys.argv[2] if len(sys.argv) > 2 else "用一句话解释什么是 RAG"

    user_id, session_id = "u_cli", "s_cli"
    thread_id = f"u:{user_id}:s:{session_id}:{tier}"
    context.bind(user_id=user_id, session_id=session_id, thread_id=thread_id)

    t0 = time.time()
    chunks: list[str] = []
    tools: list[str] = []

    async for ev in run_agent(query=query, history=[], tier=tier,
                              thread_id=thread_id):
        kind = ev.get("type")
        if kind == "token":
            chunks.append(ev.get("text", ""))
        elif kind == "tool":
            tools.append(ev.get("name", ""))
        elif kind == "error":
            print("[ERROR]", str(ev)[:400])

    text = "".join(chunks)
    print(f"\n===== tier={tier}  耗时={time.time() - t0:.1f}s  工具={tools} =====")
    print(text[:2000] if text else "（无输出）")


if __name__ == "__main__":
    asyncio.run(main())
