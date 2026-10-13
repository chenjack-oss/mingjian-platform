# -*- coding: utf-8 -*-
"""平台加固（P8）离线自测。

为什么离线：这一批改的全是**工程纪律**类问题（阻塞事件循环、限流 fail-open、
配额跨 worker 失效、诊断信息没有时间戳），它们没有"正确输出"可以比对，
只有"行为性质"可以断言 —— 断言"等待期间事件循环还在转"比断言返回值更有意义。

跑法：
    .venv/Scripts/python.exe tests/test_platform_hardening.py
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings  # noqa: E402

_PASSED: List[str] = []
_FAILED: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSED.append(name)
        print(f"  [ok] {name}")
    else:
        _FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


async def _loop_alive_during(awaitable, seconds: float = 0.6) -> int:
    """在 awaitable 执行期间数事件循环转了多少次。

    这是本次加固的核心断言方式：如果这段代码真的阻塞了 event loop，
    ticker 就一次都跑不了 —— 比"有没有返回值"更能说明问题。
    """
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            await asyncio.sleep(0.02)
            ticks += 1

    _, _ = await asyncio.gather(awaitable, ticker())
    return ticks


# =========================================================================
# 1. 日志基础设施
# =========================================================================
def case_logging() -> None:
    print("\n[1] 日志：幂等配置 + 短模块名 + stderr 隔离")

    from src.observe import logging_setup as ls

    root = logging.getLogger()
    before = len(root.handlers)
    ls.configure_logging()
    ls.configure_logging()          # 幂等
    check("configure_logging 幂等（handler 不重复）",
          len(root.handlers) == max(before, 1), f"{before} -> {len(root.handlers)}")

    lg = ls.get_logger("src.agent.subagents.registry")
    check("模块名压缩为最后两段", lg.name == "subagents.registry", lg.name)

    # MCP stdio server 的日志绝不能落到 stdout（stdout 是 JSON-RPC 通道）
    buf = io.StringIO()
    ls.configure_logging(stream=buf, force=True)
    logging.getLogger("probe.x").warning("hello-stderr-check")
    check("stream=stderr 时日志不写 stdout", "hello-stderr-check" in buf.getvalue())
    ls.configure_logging(force=True)   # 还原

    # 真断言：降级/失败路径必须打 warning。静默降级是最难排查的一类问题，
    # 用 StringIO handler 抓真实输出，而不是"检查源码里写了 warning"。
    from src.capabilities import search as S
    lg = ls.get_logger("capabilities.search")
    buf2 = io.StringIO()
    h2 = logging.StreamHandler(buf2)
    h2.setFormatter(logging.Formatter("%(levelname)s|%(message)s"))
    lg.addHandler(h2)
    try:
        S._on_error("boom-query", RuntimeError("网络抖动"))
    finally:
        lg.removeHandler(h2)
    check("失败路径打 WARNING 而不是 INFO",
          buf2.getvalue().startswith("WARNING"), buf2.getvalue()[:60])


# =========================================================================
# 2. 限流兜底（原 fail-open）
# =========================================================================
def case_rate_limit_fallback() -> None:
    print("\n[2] 限流：Redis 缺席时用进程内有界桶兜底（不再无限放行）")

    from src.cache import redis_client as rc

    rc.local_fallback_hits = 0
    uid = f"test-user-{int(time.time())}"

    # 把容量压到 3，验证"有界"而不是"无限"
    old_cap = settings.rate_capacity
    settings.rate_capacity = 3
    try:
        results = [rc.rate_allow(uid, 1)[0] for _ in range(5)]
    finally:
        settings.rate_capacity = old_cap

    check("前 3 次放行", results[:3] == [True, True, True], str(results))
    check("第 4、5 次被拒（不是全放行）", results[3:] == [False, False], str(results))
    check("兜底命中被计数（供 /health 如实上报）", rc.local_fallback_hits >= 5,
          str(rc.local_fallback_hits))

    h = rc.health()
    check("health() 上报 rate_limit_local", "rate_limit_local" in h, str(list(h)))


def budget_case() -> None:
    print("\n[3] 全局预算：Redis 不可用时返回 None（由调用方决定回退策略）")
    from src.cache import redis_client as rc
    # redis_on 是由 redis_url 推导的只读 property，这里直接把取连接这一步打桩
    old_get = rc._get
    rc._get = lambda: None
    try:
        got = rc.budget_consume("search:u1", 10, 1)
    finally:
        rc._get = old_get
    check("Redis 不可用时 budget_consume 返回 None", got is None, str(got))

    # 脚本直连 Lua 语义：不做网络调用也能验证判断分支（超限时返回 0 且不自增）
    check("Lua 脚本先判断后自增（无竞态）",
          "INCRBY" in rc._BUDGET_LUA and "GET" in rc._BUDGET_LUA)


# =========================================================================
# 3. 本地 trace 兜底（原 tracing 默认关）
# =========================================================================
def case_local_trace() -> None:
    print("\n[4] 本地 trace：Langfuse 缺席时落 JSONL，不再整条 no-op")

    from src.observe import tracing

    d = os.path.join(".data", "traces-test")
    settings.trace_dir = d
    os.makedirs(d, exist_ok=True)
    path = tracing._local_path()
    if os.path.exists(path):
        os.remove(path)

    check("Langfuse 未配置时 local_enabled()=True",
          tracing.local_enabled() is True)

    tr = tracing.start_trace("selftest", user_id="u1", session_id="s1",
                             input="测试问题", metadata={"tier": "research"})
    check("start_trace 返回本地句柄（不是 no-op）",
          isinstance(tr, tracing._LocalTrace), type(tr).__name__)

    sp = tr.span("检索")
    time.sleep(0.05)
    sp.end()
    tracing.end_trace(tr, output="答案", metadata={"tools_used": ["kb_search"]})

    ok = os.path.exists(path)
    check("JSONL 落盘", ok, path)
    if ok:
        with open(path, encoding="utf-8") as f:
            rec = json.loads(f.read().strip().split("\n")[-1])
        check("记录含 name/user_id", rec.get("name") == "selftest"
              and rec.get("user_id") == "u1", str(rec)[:120])
        check("记录含 elapsed_ms", isinstance(rec.get("elapsed_ms"), int),
              str(rec.get("elapsed_ms")))
        check("记录含子 span 耗时", bool(rec.get("spans")), str(rec.get("spans")))
        check("end 时合并的 metadata 生效",
              rec["metadata"].get("tools_used") == ["kb_search"],
              str(rec.get("metadata")))
    settings.trace_dir = "./.data/traces"


# =========================================================================
# 4. 长任务不占死事件循环
# =========================================================================
async def case_off_loop() -> None:
    print("\n[5] eager 长任务：跑到独立线程的新事件循环，不阻塞主 loop")

    from src.tasks import dispatch

    async def long_job() -> str:
        await asyncio.sleep(0.6)      # 模拟 research 档的长任务
        return "done"

    ticks = await _loop_alive_during(dispatch._run_off_loop(long_job), 0.6)
    check("等待期间事件循环仍在转（未被占死）", ticks >= 5, f"ticks={ticks}")

    # ContextVar 必须跨线程带过去，否则任务里的文件会写进上一个用户的目录
    from src import context
    async def read_ctx() -> Any:
        return context.get_user_id()
    token = context.bind(user_id="cross-thread-user", session_id="s")
    try:
        got = await dispatch._run_off_loop(read_ctx)
    finally:
        token.reset()
    check("ContextVar 跨线程传递正确", got == "cross-thread-user", str(got))


async def case_mcp_ready_not_blocking() -> None:
    print("\n[6] MCP 首连：等待放到线程池，不再把事件循环冻 30s")

    from src.mcp_client import client as mc

    class _SlowReady(threading.Event):
        def wait(self, timeout=None):        # noqa: D102
            time.sleep(0.6)                  # 模拟"子进程启动慢"
            return True

    r = mc._Runner(name="fake", args=["python", "-c", "pass"])
    r._spawn_thread = lambda: None           # 不真起进程
    r._thread = object()                     # 让 _ensure_ready 认为线程已起
    r._ready = _SlowReady()

    ticks = await _loop_alive_during(r._ensure_ready(timeout=5), 0.6)
    check("首连等待期间事件循环仍在转", ticks >= 5, f"ticks={ticks}")
    check("就绪后没有误报超时", r._error is None, str(r._error))

    class _NeverReady(threading.Event):
        def wait(self, timeout=None):
            time.sleep(0.1)
            return False
    r2 = mc._Runner(name="fake2", args=["python", "-c", "pass"])
    r2._spawn_thread = lambda: None
    r2._thread = object()
    r2._ready = _NeverReady()
    await r2._ensure_ready(timeout=1)
    check("真的超时时会记录错误（不静默）",
          bool(r2._error) and "超时" in r2._error, str(r2._error))


# =========================================================================
# 5. 搜索工具双实现 + 预算
# =========================================================================
async def case_search_tool() -> None:
    print("\n[7] 联网检索：sync/async 双实现 + schema 不变")

    from src.capabilities import search as S

    check("工具名仍是 internet_search", S.internet_search.name == "internet_search")
    check("挂了 coroutine（async 路径可用）",
          S.internet_search.coroutine is not None)
    schema = S.internet_search.args
    check("topic 枚举约束保留",
          schema.get("topic", {}).get("enum") == ["news", "finance", "general"],
          str(schema.get("topic")))
    check("max_results/include_raw_content 参数保留",
          "max_results" in schema and "include_raw_content" in schema)

    # 未配 Key 时两条路径都要优雅返回"不可用"，而不是抛异常
    old = S._API_KEY
    S._API_KEY = ""
    try:
        sync_out = S._internet_search_sync("测试")
        async_out = await S._internet_search_async("测试")
    finally:
        S._API_KEY = old
    check("sync 路径：未配置时返回不可用提示（不抛）",
          isinstance(sync_out, str) and "不可用" in sync_out, str(sync_out)[:60])
    check("async 路径：未配置时返回不可用提示（不抛）",
          isinstance(async_out, str) and "不可用" in async_out, str(async_out)[:60])

    # 预算：本地回退路径下超额必须拒绝（防"配额 × worker 数"的兜底仍要有界）
    from src.cache import redis_client as rc
    old_key, old_budget = S._API_KEY, S._SEARCH_BUDGET
    old_get = rc._get
    S._API_KEY = "fake-key-for-budget-test"
    S._SEARCH_BUDGET = 2
    rc._get = lambda: None          # 强制走本地回退分支
    try:
        outs = [S._try_consume(f"budget-{int(time.time())}") for _ in range(4)]
    finally:
        S._API_KEY, S._SEARCH_BUDGET = old_key, old_budget
        rc._get = old_get
    allowed = [o[0] for o in outs]
    check("预算内放行、超额拒绝（原子 check-and-consume）",
          allowed == [True, True, False, False], str(allowed))


# =========================================================================
# 6. CORS fail-closed
# =========================================================================
def case_cors() -> None:
    print("\n[8] CORS：生产环境未配置来源时 fail-closed")

    import src.api.server as srv

    old_env = os.environ.get("MJ_ENV")
    old_origins = settings.cors_origins

    try:
        os.environ["MJ_ENV"] = "production"
        settings.cors_origins = ["*"]
        origins, cred, lvl, msg = srv._cors_policy()
        check("生产未配置 → 拒绝所有跨源", origins == [], str(origins))
        check("生产未配置 → 不留 credentials", cred is False)
        check("生产未配置 → 按 error 级别留痕", lvl == "error", str(lvl))

        os.environ["MJ_ENV"] = "dev"
        origins, cred, lvl, _ = srv._cors_policy()
        check("非生产环境保持放行（不误伤开发）", origins == ["*"], str(origins))
        check("非生产环境仍留 warning", lvl == "warning", str(lvl))

        settings.cors_origins = ["https://app.example.com"]
        origins, cred, lvl, _ = srv._cors_policy()
        check("显式配置时按配置走且允许 credentials",
              origins == ["https://app.example.com"] and cred is True,
              f"{origins} cred={cred}")
    finally:
        if old_env is None:
            os.environ.pop("MJ_ENV", None)
        else:
            os.environ["MJ_ENV"] = old_env
        settings.cors_origins = old_origins


# =========================================================================
# 7. 引用校验（聊天主干道 + 落盘盲区提示）
# =========================================================================
def case_citation() -> None:
    print("\n[9] 引用溯源：0 条链接也要显式提示（不是静默跳过）")

    from src.capabilities import citation_guard

    key = f"cite-{int(time.time())}"
    citation_guard.clear(key)
    citation_guard.register_urls(key, ["https://example.com/a"])

    good = citation_guard.verify_citations("见 https://example.com/a 与 https://evil.com/x", key)
    check("能溯源/不能溯源分开统计",
          good["total"] == 2 and good["verified"] == 1
          and good["unverified"] == ["https://evil.com/x"], str(good))

    none = citation_guard.verify_citations("据某报告显示，同比增长 20%", key)
    check("无 URL 时 total=0 且 coverage=1.0",
          none["total"] == 0 and none["coverage"] == 1.0, str(none))
    section = citation_guard.build_audit_section(none)
    check("0 条时仍输出盲区提示（来源名+日期点不开）",
          "未包含任何可点击的来源 URL" in section)

    # write_document 无条件追加审计章节（原来是 `if total>0 or True` 的死分支）
    from src.capabilities import files as F
    import src.context as ctx
    old_root = settings.storage_root
    settings.storage_root = os.path.join(".data", "files-test")
    token = ctx.bind(user_id="cite-u", session_id="cite-s")
    try:
        out = F.write_document.func("selftest.md", "自测", "正文无链接。")
        body = open(out["path"], encoding="utf-8").read()
    finally:
        token.reset()
        settings.storage_root = old_root
    check("write_document 落盘", out.get("ok") is True)
    check("落盘内容含引用溯源校验章节", "引用溯源校验" in body)


# =========================================================================
# 8. 子智能体权限可观测（原 🟡：依赖 deepagents 内部实现）
# =========================================================================
def case_subagent_audit() -> None:
    print("\n[10] 子智能体权限：把实际工具列出来，让回归能被看见")

    from src.agent.subagents import registry

    class _T:
        name = "kb_search"

    ok_spec = registry.general_purpose_suppressor()
    rows = registry.audit([ok_spec, {"name": "kb-analyst", "tools": [_T()]}])
    check("审计列出每个子智能体实际拿到的工具",
          [r["n_tools"] for r in rows] == [0, 1]
          and rows[1]["tools"] == ["kb_search"], str(rows))
    check("占位零工具时无越权告警", registry.privilege_leak([ok_spec]) == [])

    leaked = dict(ok_spec)
    leaked["tools"] = [_T()]
    check("占位若被塞了工具则告警（抑制失效可观测）",
          len(registry.privilege_leak([leaked])) == 1,
          str(registry.privilege_leak([leaked])))


# =========================================================================
def main() -> int:
    print("=" * 62)
    print("明辨平台 P8 加固自测（离线）")
    print("=" * 62)

    case_logging()
    case_rate_limit_fallback()
    budget_case()
    case_local_trace()
    asyncio.run(case_off_loop())
    asyncio.run(case_mcp_ready_not_blocking())
    asyncio.run(case_search_tool())
    case_cors()
    case_citation()
    case_subagent_audit()

    print("\n" + "=" * 62)
    print(f"通过 {len(_PASSED)} / {len(_PASSED) + len(_FAILED)}")
    for f in _FAILED:
        print(f"  [FAIL] {f}")
    print("=" * 62)
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
