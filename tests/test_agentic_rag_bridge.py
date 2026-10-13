# -*- coding: utf-8 -*-
"""Agentic RAG 服务化接入（P7）的离线自测。

为什么是"离线"：不依赖 8082 上真跑着服务，用 httpx.MockTransport 把 HTTP 层换掉，
就能在任何机器（含 CI / 面试现场）验证**平台侧**这层逻辑：
归一化、降级、并发闸门、ContextVar 隔离、契约解析。

跑法：
    .venv/Scripts/python.exe tests/test_agentic_rag_bridge.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from src.capabilities import agentic_rag
from src.agent.subagents.contracts import coerce

_PASSED: List[str] = []
_FAILED: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSED.append(name)
        print(f"  [ok] {name}")
    else:
        _FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


# ---------- 假的 Agentic RAG 响应 ----------
def _payload(level: str = "confident") -> Dict[str, Any]:
    return {
        "answer": "该设备默认账号为 admin，密码见机身标签。参见[1]。",
        "citations": [{
            "ref": 1, "doc_id": "abc#chunk_0001", "source": "H3C-NR-手册.pdf",
            "snippet": "默认账号 admin", "score": 1.0, "relevance": 0.83,
            "title": "快速入门",
        }],
        "reasoning_trace": [{"step": 1, "node": "generator", "detail": "生成完成"}],
        "iterations": 1,
        "elapsed_ms": 15200,
        "session_id": "mingjian",
        "answer_level": level,
        "clarify_count": 0,
        "tokens": {"total_tokens": 1200},
    }


def _install(handler) -> None:
    """把模块里的单例 client 换成 MockTransport 版本。

    连 loop 一起记：_ensure_client 见到 loop 对不上会重建真实 client
    （那是换事件循环时的保护逻辑），测试里必须让它认为"这就是当前 loop 的 client"。

    🔴 base_url 用占位 http://test（不是 _BASE_URL）：
    现在 _BASE_URL 默认留空 = HTTP 通道未启用，用空串建 client 会报
    "unknown url type"。MockTransport 不关心真实地址，占位即可。
    """
    agentic_rag._client = httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(agentic_rag._TIMEOUT),
    )
    try:
        agentic_rag._client_loop = asyncio.get_running_loop()
    except RuntimeError:
        agentic_rag._client_loop = None


# ---------- 用例 ----------
async def case_normalize() -> None:
    print("\n[1] 归一化：Agentic RAG 响应 → 平台证据结构 + 探针写入")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_payload())

    _install(handler)
    agentic_rag.reset_probe()
    r = await agentic_rag.query("默认账号是什么", top_k=5)

    check("found=True", r["found"] is True)
    check("answer 非空", bool(r["answer"]))
    check("answer_level 透传", r["answer_level"] == "confident", r.get("answer_level"))
    check("citations 带 source/relevance",
          r["citations"][0]["source"] == "H3C-NR-手册.pdf"
          and r["citations"][0]["relevance"] == 0.83)
    p = agentic_rag.last_probe() or {}
    check("探针写入 channel=http", p.get("channel") == "http")
    check("探针 confidence=0.83", p.get("confidence") == 0.83, str(p.get("confidence")))
    check("探针 citations=1", p.get("citations") == 1)


async def case_ctxvar_isolation() -> None:
    print("\n[2] ContextVar 隔离：两个并发请求的探针不串台")

    def handler(request: httpx.Request) -> httpx.Response:
        # 按问题内容返回不同等级，用来区分两个请求
        body = request.content.decode("utf-8")
        lvl = "refuse" if "弱" in body else "confident"
        return httpx.Response(200, json=_payload(lvl))

    _install(handler)
    agentic_rag.reset_probe()

    async def one(q: str) -> str:
        agentic_rag.reset_probe()
        await agentic_rag.query(q)
        p = agentic_rag.last_probe() or {}
        await asyncio.sleep(0.05)  # 制造交错
        return str(p.get("answer_level"))

    a, b = await asyncio.gather(one("强证据问题"), one("弱证据问题"))
    check("两路各自读到自己的等级", {a, b} == {"confident", "refuse"}, f"{a} / {b}")


async def case_unavailable() -> None:
    print("\n[3] 不可达：抛 AgenticRagUnavailable，工具层转成契约可识别的 error")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install(handler)
    agentic_rag.reset_probe()
    try:
        await agentic_rag.query("随便问点什么")
        check("应抛异常", False, "没有抛")
    except agentic_rag.AgenticRagUnavailable as e:
        check("抛 AgenticRagUnavailable", True)
        check("提示里含不可用原因", "不可达" in str(e), str(e))

    out = await agentic_rag.agentic_rag_query.ainvoke({"question": "随便问点什么"})
    check("工具返回 error=True", out.get("error") is True)
    check("kind=unavailable → degraded=not_wired",
          coerce(out).degraded == "not_wired", str(coerce(out).degraded))


async def case_no_retry_on_504() -> None:
    print("\n[4] 504 不重试：服务端已满负荷，重试等于在峰值上加压")

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(504, json={"detail": "请求超时"})

    _install(handler)
    try:
        await agentic_rag.query("会超时的问题")
    except agentic_rag.AgenticRagUnavailable:
        pass
    check("只发了 1 次请求（未重试）", calls["n"] == 1, f"实际 {calls['n']} 次")


async def case_concurrency_gate() -> None:
    print("\n[5] 客户端并发闸门：在飞请求数不超过 agentic_rag_concurrency")

    inflight = {"now": 0, "max": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_payload())

    _install(handler)

    async def one() -> None:
        # 直接走闸门内部：模拟一次真实调用占用
        async with agentic_rag._ensure_sem():
            inflight["now"] += 1
            inflight["max"] = max(inflight["max"], inflight["now"])
            await asyncio.sleep(0.05)
            inflight["now"] -= 1

    await asyncio.gather(*[one() for _ in range(agentic_rag._MAX_CONCURRENCY + 3)])
    check(f"最大并发 ≤ {agentic_rag._MAX_CONCURRENCY}",
          inflight["max"] <= agentic_rag._MAX_CONCURRENCY,
          f"实际最大 {inflight['max']}")


async def case_contract_fields() -> None:
    print("\n[6] 契约：answer_level / confidence 能从信封里解析出来")
    env = {
        "ok": True, "answer": "结论", "evidence": [{"source": "a.pdf"}],
        "degraded": None, "tool_used": "agentic_rag_query", "elapsed_ms": 9000,
        "answer_level": "cautious", "confidence": "0.62",
    }
    r = coerce(env)
    check("answer_level 解析", r.answer_level == "cautious", r.answer_level)
    check("confidence 解析为 float", r.confidence == 0.62, str(r.confidence))

    bad = coerce({"ok": True, "answer": "x", "confidence": "不是数字"})
    check("confidence 非法值降级为 None（不抛异常）", bad.confidence is None)


async def main() -> int:
    print("=" * 60)
    print("Agentic RAG 接入层自测（离线，MockTransport）")
    print(f"  服务地址={agentic_rag._BASE_URL} 超时={agentic_rag._TIMEOUT}s "
          f"并发={agentic_rag._MAX_CONCURRENCY}")
    print("=" * 60)

    await case_normalize()
    await case_ctxvar_isolation()
    await case_unavailable()
    await case_no_retry_on_504()
    await case_concurrency_gate()
    await case_contract_fields()

    print("\n" + "=" * 60)
    print(f"通过 {len(_PASSED)} / {len(_PASSED) + len(_FAILED)}")
    for f in _FAILED:
        print(f"  [FAIL] {f}")
    print("=" * 60)
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
