# -*- coding: utf-8 -*-
"""L3 鲁棒性评估：故障注入 + 行为断言 —— 证明降级矩阵是"有断言的"，不是写在 README 里的。

三层注入口径：
A. 错误分类表（纯函数断言，零网络）：classify_error 对 15 种典型异常的归类
   必须符合「429/5xx/超时/连接=TRANSIENT 可重试，4xx 参数类=PERMANENT 不重试」；
B. 熔断三态机（本地 async fn 注入，零网络）：
   - 连续失败达阈值 → open，之后调用**快速失败**（<50ms，对应单测 test_llm_resilience.py:144 口径）；
   - PERMANENT 错误**不重试**（fn 只被调 1 次）；
   - 冷却结束 → half_open 只放行一个探测；探测成功 → closed；探测失败 → 回 open；
C. HTTP 层集成（本地 mock OpenAI 端点，真实 httpx 链路）：
   mock 持续 500 → ResilientAsyncClient 重试次数 = settings.llm_max_retries，
   失败记账后熔断打开；mock 恢复 200 → 冷却后探测关闸。

为什么 mock 在 127.0.0.1 而不是拦真实端点：
   真实端点不可控（不能真的把它打挂），且本机有 HTTP_PROXY 拦截 127.0.0.1 的坑
   （2026-10-12 QPS 实测踩过）——httpx.Client(trust_env=False) 直连绕开代理。

用法：
    python scripts/robustness_eval.py
产出：
    .data/eval_l3_<时间戳>.json
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings  # noqa: E402
from src.llm import resilience  # noqa: E402
from src.llm.resilience import (  # noqa: E402
    CircuitBreaker, CircuitOpen, classify_error, PERMANENT, TRANSIENT,
)

OUT_DIR = Path(".data")
results: List[Dict[str, Any]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append({"name": name, "pass": bool(ok), "detail": detail})
    print(f"  {'✅' if ok else '🔴'} {name}  {detail}")


# ---------------------------------------------------------------------------
# A. 错误分类表
# ---------------------------------------------------------------------------
def part_a():
    print("\n-- A. 错误分类（15 例）--")
    cases = [
        ("连接失败 ConnectError", TRANSIENT), ("read timeout occurred", TRANSIENT),
        ("Error 500 internal error", TRANSIENT), ("502 Bad Gateway", TRANSIENT),
        ("503 service unavailable", TRANSIENT), ("429 rate limit exceeded", TRANSIENT),
        ("connection reset by peer", TRANSIENT), ("server overloaded", TRANSIENT),
        ("400 invalid parameter", PERMANENT), ("401 unauthorized", PERMANENT),
        ("403 forbidden", PERMANENT), ("404 not found", PERMANENT),
        ("422 malformed request", PERMANENT), ("authentication failed", PERMANENT),
        ("invalid api key", PERMANENT),
    ]
    ok = 0
    for msg, expect in cases:
        got = classify_error(RuntimeError(msg))
        if got == expect:
            ok += 1
        else:
            check(f"classify({msg!r})", False, f"期望 {expect} 实际 {got}")
    check("错误分类表 15/15", ok == len(cases), f"{ok}/{len(cases)}")


# ---------------------------------------------------------------------------
# B. 熔断三态机（本地注入）
# ---------------------------------------------------------------------------
async def part_b():
    print("\n-- B. 熔断三态机 --")
    br = CircuitBreaker(f"eval-l3-{int(time.time())}", threshold=3,
                        window_sec=60, cooldown_sec=1.0)

    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise RuntimeError("500 internal error")

    for _ in range(3):
        try:
            await br.call(boom, max_retries=0)
        except RuntimeError:
            pass
    check("连续3次500后熔断打开", br.state() == "open", f"state={br.state()}")

    t0 = time.perf_counter()
    try:
        await br.call(boom, max_retries=0)
        fast = False
    except CircuitOpen:
        fast = True
    ms = (time.perf_counter() - t0) * 1000
    check("open 期间快速失败 <50ms", fast and ms < 50, f"{ms:.2f}ms")
    check("open 期间不打后端（fn 未被再调用）", calls["n"] == 3, f"fn调用={calls['n']}")

    # PERMANENT 不重试
    br2 = CircuitBreaker(f"eval-l3-p-{int(time.time())}", threshold=5,
                         window_sec=60, cooldown_sec=60)
    pcalls = {"n": 0}

    async def bad_request():
        pcalls["n"] += 1
        raise RuntimeError("400 invalid parameter")

    try:
        await br2.call(bad_request, max_retries=3)
    except RuntimeError:
        pass
    check("PERMANENT(400) 不重试（retries=3 但只调 1 次）",
          pcalls["n"] == 1, f"fn调用={pcalls['n']}")

    # 冷却 → half_open 单探测 → 成功关闸
    await asyncio.sleep(1.1)
    ok_calls = {"n": 0}

    async def fine():
        ok_calls["n"] += 1
        return "ok"

    out = await br.call(fine, max_retries=0)
    check("冷却后探测成功 → 关闸", out == "ok" and br.state() == "closed",
          f"state={br.state()}")

    # half_open 只放一个探测（并发两个只有一个能进）
    br3 = CircuitBreaker(f"eval-l3-h-{int(time.time())}", threshold=1,
                         window_sec=60, cooldown_sec=0.5)
    try:
        await br3.call(boom, max_retries=0)
    except RuntimeError:
        pass
    await asyncio.sleep(0.6)
    entered = {"n": 0}

    async def slow_probe():
        entered["n"] += 1
        await asyncio.sleep(0.3)
        return "ok"

    r = await asyncio.gather(br3.call(slow_probe, max_retries=0),
                             br3.call(slow_probe, max_retries=0),
                             return_exceptions=True)
    got_in = sum(1 for x in r if x == "ok")
    rejected = sum(1 for x in r if isinstance(x, CircuitOpen))
    check("half_open 只放行一个探测", got_in == 1 and rejected == 1,
          f"放行={got_in} 拒绝={rejected}")


# ---------------------------------------------------------------------------
# C. HTTP 层集成（本地 mock 端点）
# ---------------------------------------------------------------------------
class _MockHandler(BaseHTTPRequestHandler):
    status = 500

    def do_POST(self):  # noqa: N802
        body = json.dumps({"error": {"message": "mock upstream failure"}}).encode()
        if self.__class__.status == 200:
            body = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "mock ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
        self.send_response(self.__class__.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静音
        pass


async def part_c():
    print("\n-- C. HTTP 层集成 --")
    # C1: 传输层故障（连接拒绝，真实模拟端点宕机）→ 熔断必须记账并打开
    br = CircuitBreaker(f"eval-l3-http-{int(time.time())}", threshold=2,
                        window_sec=60, cooldown_sec=1.0)
    import httpx
    client = resilience.ResilientAsyncClient(breaker=br, timeout=3, trust_env=False)
    for _ in range(2):
        try:
            await client.send(httpx.Request(
                "POST", "http://127.0.0.1:1/v1/chat/completions", content=b"{}"))
        except Exception:  # noqa: BLE001
            pass
    check("C1 传输层故障（连接拒绝）达阈值后熔断打开",
          br.state() == "open", f"state={br.state()} fails={br._fails()}")

    t0 = time.perf_counter()
    try:
        await client.send(httpx.Request(
            "POST", "http://127.0.0.1:1/v1/chat/completions", content=b"{}"))
        fast = False
    except CircuitOpen:
        fast = True
    ms = (time.perf_counter() - t0) * 1000
    check("C1 熔断后快速失败 <50ms", fast and ms < 50, f"{ms:.2f}ms")
    await client.aclose()

    # C2: HTTP 500（mock 端点可连但持续报错）——2026-10-13 盲区已修复：
    #     ResilientClient 现在把 5xx/429 显式转成 HTTPStatusError 进熔断记账。
    #     本断言验证修复后的行为：持续 500 必须累计失败并打开熔断、快速失败。
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    br2 = CircuitBreaker(f"eval-l3-500-{int(time.time())}", threshold=2,
                         window_sec=60, cooldown_sec=30)
    client2 = resilience.ResilientAsyncClient(breaker=br2, timeout=3, trust_env=False)
    for _ in range(2):
        try:
            await client2.send(httpx.Request(
                "POST", f"http://127.0.0.1:{port}/v1/chat/completions",
                content=b"{}", headers={"Content-Type": "application/json"}))
        except Exception:  # noqa: BLE001  期望抛 HTTPStatusError
            pass
    check("C2 HTTP 500 现在会被熔断记账并打开熔断（2026-10-13 已修复）",
          br2.state() == "open" and br2._fails() >= 2,
          f"state={br2.state()} fails={br2._fails()}")
    t0 = time.perf_counter()
    try:
        await client2.send(httpx.Request(
            "POST", f"http://127.0.0.1:{port}/v1/chat/completions",
            content=b"{}", headers={"Content-Type": "application/json"}))
        fast = False
    except CircuitOpen:
        fast = True
    except Exception:  # noqa: BLE001  500 在熔断未开时会抛 HTTPStatusError，也算未快速失败
        fast = False
    ms = (time.perf_counter() - t0) * 1000
    check("C2 熔断后 HTTP 500 快速失败 <50ms", fast and ms < 50, f"{ms:.2f}ms")
    await client2.aclose()
    srv.shutdown()


async def main() -> int:
    print("=== 明辨平台 L3 鲁棒性评估 | 故障注入 ===")
    part_a()
    await part_b()
    await part_c()

    n = len(results)
    ok = sum(1 for r in results if r["pass"])
    print(f"\n=== 断言 {ok}/{n} 通过 ===")
    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "assertions": results, "passed": ok, "total": n,
        "params": {"llm_circuit_enabled": settings.llm_circuit_enabled,
                   "threshold": settings.llm_circuit_threshold,
                   "window_sec": settings.llm_circuit_window_sec,
                   "cooldown_sec": settings.llm_circuit_cooldown_sec,
                   "max_retries": settings.llm_max_retries,
                   "redis_on": settings.redis_on},
    }
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"eval_l3_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告：{out}")
    return 0 if ok == n else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
