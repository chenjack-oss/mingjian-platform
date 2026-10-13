# -*- coding: utf-8 -*-
"""P6.0 MCP 并发探针：子智能体并行派发之前，必须先回答"会不会串帧"。

为什么这个探针排在所有子智能体工作之前
--------------------------------------
LangGraph 在 async 路径下会把**同一轮里的多个工具调用并发执行**
（`src/agent/core.py` 走的是 `graph.astream(stream_mode="messages")`）。
而我们每个 MCP server 只有**一个常驻 stdio 会话**（`src/mcp_client/client.py`
的 `_Runner`），`_submit()` 把调用丢给 runner 自己的 loop 时**没有任何排队/Lock**
——全文件只有 `_runners_lock`，那是保护 runner 创建的，不保护调用。

单智能体时代一次通常只调一个工具，风险低；**多子智能体之后概率显著抬高**。

怎么判定"串了"
--------------
用 `query_order` 做探针：它的返回体里带着请求的 `id`（store.py:257-267）。
如果并发下响应错位（A 的请求拿到 B 的结果），返回里的 `id` 就会和请求的不一致 ——
这是**唯一能证明串帧的证据**，比"没报错"可靠得多。

用法：
    python scripts/probe_mcp_concurrency.py            # 默认 30 次 / 并发 10
    python scripts/probe_mcp_concurrency.py 50 20      # 次数 / 并发度
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = "✅", "❌"
_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(f"  {PASS if cond else FAIL} {name}" + (f"  {detail}" if detail else ""))


def pct(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * p))]


async def _get_tool(prefix: str, name: str):
    from src.mcp_client.client import langchain_tools
    tools = await langchain_tools(prefix=prefix)
    for t in tools:
        if getattr(t, "name", "") == name:
            return t
    return None


async def run_batch(tool, ids, conc):
    """返回 (延迟列表, 错配列表, 错误列表)"""
    lat, mismatch, errors = [], [], []

    async def one(i, oid):
        t0 = time.time()
        try:
            r = await tool.ainvoke({"order_id": oid})
        except Exception as e:
            errors.append(f"{oid}: {type(e).__name__}: {e}")
            return
        lat.append((time.time() - t0) * 1000)
        if not isinstance(r, dict):
            errors.append(f"{oid}: 返回非 dict -> {str(r)[:80]}")
            return
        if r.get("error"):
            errors.append(f"{oid}: {r.get('kind')} {str(r.get('message'))[:80]}")
            return
        got = r.get("id")
        if got != oid:
            mismatch.append(f"请求 {oid} 却拿到 {got}")

    if conc <= 1:
        for i, oid in enumerate(ids):
            await one(i, oid)
    else:
        sem = asyncio.Semaphore(conc)

        async def wrapped(i, oid):
            async with sem:
                await one(i, oid)

        await asyncio.gather(*[wrapped(i, o) for i, o in enumerate(ids)])
    return lat, mismatch, errors


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    conc = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    print(f"\n=== MCP 并发探针（{n} 次调用）===")
    tool = await _get_tool("query_", "query_order")
    if tool is None:
        print("  ❌ 拿不到 query_order 工具（business server 没起来？）")
        return 1
    check("query_order 工具可用", True)

    ids = [f"SO-{900000 + i}" for i in range(n)]

    # 预热一次，把首次连接的开销排除掉
    await tool.ainvoke({"order_id": ids[0]})

    print(f"\n-- A. 串行 {n} 次 --")
    t0 = time.time()
    lat_s, mis_s, err_s = await run_batch(tool, ids, 1)
    wall_s = time.time() - t0
    print(f"  墙钟 {wall_s*1000:.0f}ms  p50={pct(lat_s,0.5):.1f}ms  p95={pct(lat_s,0.95):.1f}ms")

    print(f"\n-- B. 并发 {n} 次（并发度 {conc}）--")
    t0 = time.time()
    lat_c, mis_c, err_c = await run_batch(tool, ids, conc)
    wall_c = time.time() - t0
    print(f"  墙钟 {wall_c*1000:.0f}ms  p50={pct(lat_c,0.5):.1f}ms  p95={pct(lat_c,0.95):.1f}ms")

    print(f"\n-- 判定 --")
    check("串行无错配", not mis_s, f"{mis_s[:2]}")
    check("串行无错误", not err_s, f"{err_s[:2]}")
    check("并发无错配（**这是关键**）", not mis_c, f"{mis_c[:3]}")
    check("并发无错误", not err_c, f"{err_c[:3]}")

    if lat_s and lat_c:
        speedup = wall_s / wall_c if wall_c else 0
        print(f"\n  并发加速比（墙钟）：{speedup:.2f}×")
        print(f"  并发 p95 / 串行 p95：{pct(lat_c,0.95)/max(pct(lat_s,0.95),0.01):.2f}×")

    # rag server（慢，模型已热的话 1~3s）也并发试一轮，确认慢路径同样没问题
    rag = await _get_tool("kb_", "kb_lookup")
    if rag is not None:
        print(f"\n-- C. rag server 并发 {conc} 次 kb_lookup（慢路径 + 大返回体）--")
        base = ["H3C 默认登录账号", "VLAN 配置步骤", "端口镜像怎么配",
                "设备恢复出厂设置", "固件升级方法", "无线 SSID 配置"]
        qs = [f"{base[i % len(base)]} #{i}" for i in range(conc)]
        lat_r, err_r = [], []

        async def one(q):
            t0 = time.time()
            try:
                r = await rag.ainvoke({"query": q})
            except Exception as e:
                err_r.append(f"{q}: {type(e).__name__}: {e}")
                return
            lat_r.append((time.time() - t0) * 1000)
            if isinstance(r, dict) and r.get("error"):
                err_r.append(f"{q}: {r.get('kind')}")

        await asyncio.gather(*[one(q) for q in qs])
        print(f"  p50={pct(lat_r,0.5):.0f}ms  p95={pct(lat_r,0.95):.0f}ms  n={len(lat_r)}")
        check("rag 并发 6 次无错误", not err_r, f"{err_r[:3]}")

    ok = sum(1 for _, c in _results if c)
    print(f"\n合计 {ok}/{len(_results)} 通过")
    if ok != len(_results):
        print("\n🔴 **并发不安全**：下一步必须给 _Runner 加 per-runner asyncio.Lock，")
        print("   或者每个 server 起多个进程做连接池。在此之前不要开并行派发。")
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
