# -*- coding: utf-8 -*-
"""QPS / 并发延迟探测（本地单机口径）

设计原则（与项目其它测量脚本一致）：
1. **口径必须随数字一起记录**：机器配置、档位、并发数、是否命中缓存、模型名。
   脱离口径的 QPS 是废数字 —— 面试时宁可说"没测"，也不报一个没口径的数。
2. **只测稳态**：先跑 2 次预热（冷启动首连/图装配不算数），再开始计时。
3. **记录劣化拐点**：不是只报一个 QPS，而是看并发升到多少开始出现
   超时 / 5xx / 明显劣化 —— 拐点比峰值更有工程价值。
4. **失败也要分类**：429（限流）/ timeout / 5xx，分不清就无法定位瓶颈。

用法：
    python scripts/probe_qps.py --base http://127.0.0.1:8000 --key mj_xxx \
        --tier direct --concurrency 1,5,10,20 --requests 20
"""
import argparse
import asyncio
import json
import statistics
import time
from collections import Counter

import httpx

PROMPT = "用一句话解释什么是向量检索（第{i}次）"


def pct(vals, q):
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = int(round((len(vals) - 1) * q))
    return vals[k]


async def one(client, base, key, sid, tier, i, sem, timeout):
    payload = {"content": PROMPT.format(i=i), "tier": tier}
    headers = {} if key in ("", "anonymous") else {"X-API-Key": key}
    async with sem:
        t0 = time.perf_counter()
        try:
            r = await client.post(
                f"{base}/v1/sessions/{sid}/messages",
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            dt = (time.perf_counter() - t0) * 1000
            if r.status_code == 200:
                meta = r.json().get("meta") or {}
                return ("ok", dt, float(meta.get("elapsed_sec", 0)) * 1000)
            return (f"http_{r.status_code}", dt, 0)
        except httpx.TimeoutException:
            return ("timeout", (time.perf_counter() - t0) * 1000, 0)
        except Exception as e:
            return (f"err_{type(e).__name__}", (time.perf_counter() - t0) * 1000, 0)


async def run_level(base, key, sid, tier, conc, n, timeout):
    sem = asyncio.Semaphore(conc)
    async with httpx.AsyncClient(trust_env=False) as client:
        # 预热：冷启动的图装配 / MCP 首连不计入
        await asyncio.gather(*[one(client, base, key, sid, tier, -1, sem, timeout)
                               for _ in range(min(2, conc))])
        t0 = time.perf_counter()
        results = await asyncio.gather(
            *[one(client, base, key, sid, tier, i, sem, timeout) for i in range(n)])
        wall = time.perf_counter() - t0

    kinds = Counter(r[0] for r in results)
    lat = [r[1] for r in results if r[0] == "ok"]
    srv = [r[2] for r in results if r[0] == "ok" and r[2] > 0]
    ok = kinds.get("ok", 0)
    return {
        "concurrency": conc,
        "requests": n,
        "success": ok,
        "failed": n - ok,
        "failure_breakdown": dict(kinds),
        "wall_sec": round(wall, 2),
        "qps": round(ok / wall, 3) if wall > 0 else 0,
        "latency_ms": {
            "p50": round(pct(lat, 0.5), 1),
            "p95": round(pct(lat, 0.95), 1),
            "max": round(max(lat), 1) if lat else 0,
            "mean": round(statistics.mean(lat), 1) if lat else 0,
        },
        "server_reported_ms": {
            "p50": round(pct(srv, 0.5), 1) if srv else 0,
            "p95": round(pct(srv, 0.95), 1) if srv else 0,
        },
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--key", required=True)
    ap.add_argument("--tier", default="direct")
    ap.add_argument("--concurrency", default="1,5,10,20")
    ap.add_argument("--requests", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--prompt", default="用一句话解释什么是向量检索（第{i}次）")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    global PROMPT
    PROMPT = a.prompt

    hdrs = {} if a.key in ("", "anonymous") else {"X-API-Key": a.key}
    async with httpx.AsyncClient(trust_env=False) as c:
        r = await c.post(f"{a.base}/v1/sessions", json={"title": "qps"},
                         headers=hdrs, timeout=30)
        r.raise_for_status()
        sid = r.json()["session"]["id"]
        health = (await c.get(f"{a.base}/health", timeout=30)).json()

    print(f"档位={a.tier}  会话={sid}")
    print("health:", json.dumps(health, ensure_ascii=False)[:300])

    rows = []
    for conc in [int(x) for x in a.concurrency.split(",")]:
        row = await run_level(a.base, a.key, sid, a.tier, conc, a.requests, a.timeout)
        rows.append(row)
        print(f"  并发 {conc:>3}: QPS={row['qps']:<7} 成功{row['success']}/{row['requests']} "
              f"p50={row['latency_ms']['p50']}ms p95={row['latency_ms']['p95']}ms "
              f"max={row['latency_ms']['max']}ms 失败={row['failure_breakdown']}")

    out = {"tier": a.tier, "base": a.base, "health": health, "levels": rows}
    txt = json.dumps(out, ensure_ascii=False, indent=2)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(txt)
        print("saved", a.out)
    print("\n--- JSON ---\n" + txt)


if __name__ == "__main__":
    asyncio.run(main())
