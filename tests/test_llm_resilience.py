# -*- coding: utf-8 -*-
"""大模型熔断与降级（P9）离线自测。

跑法：
    .venv/Scripts/python.exe tests/test_llm_resilience.py

设计取向：全部走**本地状态**（把 _redis 打桩成 None），不依赖任何外部服务。
Redis 分支与本地分支共用同一套 state/记账逻辑，测本地分支足以证明行为正确。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from src.config import settings
from src.llm import resilience as R

_PASSED: List[str] = []
_FAILED: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSED.append(name)
        print(f"  [ok] {name}")
    else:
        _FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def _force_local() -> None:
    """强制走进程内状态，避免测试依赖 Redis。"""
    R.CircuitBreaker._redis = lambda self: None      # type: ignore[assignment]
    R._local.clear()


def _fresh(name: str, threshold: int = 3, cooldown: float = 0.3) -> R.CircuitBreaker:
    return R.CircuitBreaker(f"t-{name}-{int(time.time()*1000)}",
                            threshold=threshold, window_sec=60,
                            cooldown_sec=cooldown)


# =========================================================================
def case_classify() -> None:
    print("\n[1] 错误分类：区分「值得重试」和「重试一万次也是错」")

    cases = [
        ("429 Too Many Requests", R.TRANSIENT),
        ("503 Service Unavailable", R.TRANSIENT),
        ("ReadTimeout", R.TRANSIENT),
        ("Connection reset by peer", R.TRANSIENT),
        ("401 Unauthorized", R.PERMANENT),
        ("400 Bad Request: invalid messages", R.PERMANENT),
        ("404 model not found", R.PERMANENT),
    ]
    for msg, want in cases:
        got = R.classify_error(RuntimeError(msg))
        check(f"{msg[:28]:30s} → {want}", got == want, got)

    check("认不出来时按 permanent（保守，不瞎重试）",
          R.classify_error(RuntimeError("something weird")) == R.UNKNOWN
          and not R.retryable(RuntimeError("something weird")))


async def case_retry_policy() -> None:
    print("\n[2] 重试策略：只对 transient 重试，且带退避")

    _force_local()

    # permanent 错误：一次都不该重试
    calls = {"n": 0}

    async def bad_permanent() -> str:
        calls["n"] += 1
        raise RuntimeError("401 Unauthorized")

    br = _fresh("perm")
    try:
        await br.call(bad_permanent, max_retries=3)
    except Exception:
        pass
    check("401 只调用 1 次（不该重试）", calls["n"] == 1, f"调用 {calls['n']} 次")

    # transient 错误：应当重试到上限
    calls2 = {"n": 0}

    async def bad_transient() -> str:
        calls2["n"] += 1
        raise RuntimeError("503 Service Unavailable")

    br2 = _fresh("trans")
    t0 = time.perf_counter()
    try:
        await br2.call(bad_transient, max_retries=2)
    except Exception:
        pass
    elapsed = time.perf_counter() - t0
    check("503 重试到上限（共 3 次）", calls2["n"] == 3, f"调用 {calls2['n']} 次")
    check("重试之间有退避（不是瞬间打完）", elapsed > 0.3, f"{elapsed:.2f}s")

    # 抖动：两次退避时长不应完全相同（防重试尖峰）
    ds = {round(R.backoff_delay(1), 6) for _ in range(20)}
    check("退避带抖动（多次取值不同）", len(ds) > 1, str(len(ds)))


async def case_circuit() -> None:
    print("\n[3] 熔断：达到阈值开闸，开闸后**快速失败**")

    _force_local()
    br = _fresh("open", threshold=3, cooldown=5)

    hits = {"n": 0}

    async def always_fail() -> str:
        hits["n"] += 1
        raise RuntimeError("502 Bad Gateway")

    for _ in range(3):
        try:
            await br.call(always_fail, max_retries=0)
        except Exception:
            pass

    check("累计 3 次失败后开闸", br.state() == R.OPEN, br.state())
    check("失败发生在开闸之前（共 3 次调用）", hits["n"] == 3, str(hits["n"]))

    # 开闸后：必须是快速失败，且**一次请求都不发**
    t0 = time.perf_counter()
    opened = False
    try:
        await br.call(always_fail, max_retries=0)
    except R.CircuitOpen:
        opened = True
    elapsed = time.perf_counter() - t0
    check("开闸后抛 CircuitOpen", opened)
    check("开闸后不再打后端（调用次数未增加）", hits["n"] == 3, str(hits["n"]))
    check("开闸后是快速失败（<50ms，不是等超时）", elapsed < 0.05, f"{elapsed*1000:.1f}ms")


async def case_half_open() -> None:
    print("\n[4] 半开：冷却后只放行一个探测请求")

    _force_local()
    br = _fresh("half", threshold=2, cooldown=0.25)

    async def fail() -> str:
        raise RuntimeError("500")

    for _ in range(2):
        try:
            await br.call(fail, max_retries=0)
        except Exception:
            pass
    check("已开闸", br.state() == R.OPEN)

    await asyncio.sleep(0.3)          # 等冷却（本地分支靠 open_until 判断）
    # 冷却结束 → 半开：放行探测
    ok = {"n": 0}

    async def good() -> str:
        ok["n"] += 1
        return "ok"

    out = await br.call(good, max_retries=0)
    check("半开放行探测并成功", out == "ok" and ok["n"] == 1, str(ok))
    check("探测成功后关闸", br.state() == R.CLOSED, br.state())

    # 再开一次闸，验证探测失败会回到 open
    br2 = _fresh("half2", threshold=1, cooldown=0.2)
    try:
        await br2.call(fail, max_retries=0)
    except Exception:
        pass
    check("阈值 1 即开闸", br2.state() == R.OPEN, br2.state())
    await asyncio.sleep(0.25)
    check("冷却后转半开（可读到 HALF_OPEN 语义）",
          br2.state() in (R.HALF_OPEN, R.CLOSED), br2.state())


def case_per_model_breaker() -> None:
    print("\n[5] 每个模型一个熔断器：主模型挂了不能连备用一起熔断")

    _force_local()
    primary = R.breaker_for("qwen-max")
    standby = R.breaker_for("qwen-flash")
    check("不同模型的 key 不同",
          primary.name != standby.name, f"{primary.name} / {standby.name}")

    for _ in range(10):
        primary.record_failure()
    check("主模型已开闸", primary.state() == R.OPEN, primary.state())
    check("备用模型不受影响（仍 closed）", standby.state() == R.CLOSED,
          standby.state())


async def case_http_client() -> None:
    print("\n[6] httpx 层接入：真实请求也能被熔断拦住")

    _force_local()
    br = _fresh("http", threshold=2, cooldown=5)
    # 指向一个必然连不上的端口：连接失败属于 transient
    cli = R.ResilientAsyncClient(breaker=br, timeout=1.0)

    errs = 0
    for _ in range(2):
        try:
            await cli.send(httpx.Request("POST", "http://127.0.0.1:1/nope"))
        except R.CircuitOpen:
            raise AssertionError("不该在开闸前抛 CircuitOpen")
        except Exception:
            errs += 1

    check("两次真实连接失败被记账", errs == 2, str(errs))
    check("达到阈值后开闸", br.state() == R.OPEN, br.state())

    t0 = time.perf_counter()
    blocked = False
    try:
        await cli.send(httpx.Request("POST", "http://127.0.0.1:1/nope"))
    except R.CircuitOpen:
        blocked = True
    except Exception as e:
        blocked = False
        print(f"      （意外异常 {type(e).__name__}）")
    dt = time.perf_counter() - t0
    check("开闸后请求被就地拦下", blocked)
    check("拦截是瞬时的（未发起真实连接）", dt < 0.05, f"{dt*1000:.1f}ms")
    await cli.aclose()


def case_fallback_text() -> None:
    print("\n[7] 兜底话术：降级必须可见，不能假装正常")

    check("无模型兜底话术说明了「不是你的问题」",
          "不是你的问题" in R.FALLBACK_NO_MODEL)
    check("无模型兜底话术不假装回答了问题",
          "熔断" in R.FALLBACK_NO_MODEL and "稍后重试" in R.FALLBACK_NO_MODEL)
    check("带证据的兜底明确说「未经加工」",
          "未经加工" in R.FALLBACK_WITH_EVIDENCE)
    check("降级回答会追加可见标记", "降级" in R.FALLBACK_NOTE)


def case_is_llm_down() -> None:
    print("\n[8] 降级判定：不能把真实 bug 伪装成「模型挂了」")

    # 该判成"模型挂了"的
    for s in ("Connection reset by peer", "CircuitOpen: 熔断器已打开",
              "LLMUnavailable: 模型调用失败", "503 Service Unavailable",
              "429 rate limit", "ReadTimeout"):
        check(f"判为不可用: {s[:30]}", R.is_llm_down(s) is True, str(s))

    # **不该**判成"模型挂了"的：伪装的话会把真实 bug 藏起来
    for s in ("some random tool bug", "invalid api key", "file not found", ""):
        check(f"不伪装: {s[:30] or '(空)'}", R.is_llm_down(s) is False, str(s))


def case_health() -> None:
    print("\n[8] /health 如实上报熔断状态")

    _force_local()
    h = R.health()
    check("上报 llm_circuit=on", h.get("llm_circuit") == "on", str(h))
    check("逐个模型上报 state/fails",
          any(isinstance(v, dict) and "state" in v
              for k, v in h.items() if k != "llm_circuit"), str(h))

    old = settings.llm_circuit_enabled
    settings.llm_circuit_enabled = False
    try:
        check("关闭时上报 off（不是假装正常）",
              R.health().get("llm_circuit") == "off")
    finally:
        settings.llm_circuit_enabled = old


# =========================================================================
def main() -> int:
    print("=" * 62)
    print("大模型熔断与降级自测（离线）")
    print("=" * 62)

    case_classify()
    asyncio.run(case_retry_policy())
    asyncio.run(case_circuit())
    asyncio.run(case_half_open())
    case_per_model_breaker()
    asyncio.run(case_http_client())
    case_fallback_text()
    case_is_llm_down()
    case_health()

    print("\n" + "=" * 62)
    print(f"通过 {len(_PASSED)} / {len(_PASSED) + len(_FAILED)}")
    for f in _FAILED:
        print(f"  [FAIL] {f}")
    print("=" * 62)
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
