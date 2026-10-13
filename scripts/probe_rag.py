"""
P5 联调：明辨 → MCP → Agentic RAG 全链路

跑法：
    python scripts/probe_rag.py

验证三件事：
    1. rag 档不再返回"未接入"降级，而是真的去查知识库
    2. kb_lookup 冷/热延迟（冷 = 首次要加载 BGE-M3 权重，热 = 权重已常驻）
    3. 答案里带得上引用来源（source），不是模型自己编的
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db import engine                      # noqa: E402
from src.mcp_client import client as mcp       # noqa: E402
from src.routing import executor               # noqa: E402

QUERY = "知识库里，路由器 Web 登录页面的默认用户名和密码是什么？"


async def main() -> int:
    engine.init_db()

    print("=== 1. kb_wired 探测 ===")
    wired = await executor.kb_wired()
    print(f"  kb_wired = {wired}")

    print("\n=== 2. kb_lookup 冷/热延迟（同一问题连查 3 次）===")
    for i in range(3):
        r, ms = await mcp.raw_call("kb_lookup",
                                   {"query": "路由器登录", "top_k": 3}, server="rag")
        n = len((r or {}).get("items", []))
        print(f"  第{i + 1}次：{ms:8.1f}ms  命中 {n} 条"
              + ("（冷启动：含 BGE-M3 权重加载）" if i == 0 else ""))

    print("\n=== 3. rag 档端到端 ===")
    t0 = time.time()
    res = await executor.execute(QUERY, [], "rag", "probe-rag-thread")
    print(f"  耗时 {time.time() - t0:.1f}s")
    print(f"  tools_used = {res['tools_used']}")
    print(f"  meta       = {res['meta']}")
    print(f"  answer     = {res['content'][:600]}")

    print("\n=== 4. 关掉 rag server 后应诚实降级 ===")
    import src.config as cfg
    cfg.settings.mcp_servers = "business"
    from src import agent
    agent.core._mcp_tools_cache = None
    print(f"  kb_wired = {await executor.kb_wired()}")
    res2 = await executor.execute(QUERY, [], "rag", "probe-rag-thread-2")
    print(f"  degradation = {res2['meta'].get('degradation')}")
    print(f"  content 前 80 字 = {res2['content'][:80]}")

    mcp.close_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
