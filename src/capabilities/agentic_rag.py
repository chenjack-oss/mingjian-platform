# -*- coding: utf-8 -*-
"""Agentic RAG 服务化接入（P7）：把知识库检索从 MCP stdio 升级为 HTTP 直连。

为什么要做这一层（而不是继续用 kb_search 就够了）
------------------------------------------------
同一个 Agentic RAG 有两套接法，差别不在能力，而在**吞吐与可运维性**：

  ① MCP stdio 子进程（P5 起在用）：api 容器内 spawn `mcp_rag_server`，
     工具 kb_search / kb_lookup / kb_stats。
     实测硬伤：单 stdio 会话并发调用时 p95 从 1.3s 劣化到 23.9s，
     所以被迫加了 `MJ_MCP_SERIALIZE_SERVERS=rag`（`mcp_client.py:220-223` 全局锁）
     —— **并发上限 = 1**，知识库检索成了整条链路的串行瓶颈。
  ② HTTP 独立服务（docker-compose 的 `rag` 服务，127.0.0.1:8082）：
     同一份 Agentic RAG 源码，`uvicorn main:app` 跑 POST /agent/query，
     服务端有 anyio.CapacityLimiter（默认 4）+ asyncio.wait_for(120s) + 启动预热
    （`main.py:70-83`）。并发 4、可独立扩缩容、不影响 api 容器。

本模块让平台主链路消费 ②，① 降级为容灾备份。价值点：
  - 并发从 1 → 4（对齐服务端闸门，不把服务端压垮）
  - 检索不再与 api 抢 CPU（BGE-M3 编码挪到独立容器）
  - 置信度 answer_level / relevance 首次接出到 API 响应（executor.py 读探针）

四条工程纪律（每条都有实测或源码依据）
--------------------------------------
1. **客户端闸门 ≤ 服务端闸门**：信号量取 4 = 服务端 CapacityLimiter(4)。
   客户端不限流的话，第 5 个请求会在服务端排队却仍占着本地协程和连接，
   表现为"服务没崩但全部变慢"，且排障时看不出是排队还是真慢。
2. **客户端超时 110s < 服务端 120s**：必须比服务端短。
   否则永远等不到服务端那个语义明确的 504，只看到自己的 TimeoutError，
   排障时无法区分"真超时"和"网络断了"。差 10s 是留给服务端写响应的余量。
3. **只重试连接类错误，且只重试一次**：504/5xx 说明服务端已经在满负荷，
   重试等于在峰值上再叠一倍压力（这就是典型的重试风暴）。连接失败通常是
   容器重启/冷启动，重试一次有意义。
4. **连接复用**：模块级 httpx.AsyncClient（keep-alive 连接池）。
   每次新建 client 会把 TLS 握手/连接建立的开销压进每次检索。

降级粒度
--------
HTTP 不可达 → 抛 AgenticRagUnavailable → 工具层返回 {"error": True, "kind": "unavailable"}
→ 契约解析成 degraded=not_wired → kb-analyst 回落到 kb_search（stdio）。
两路都挂 → kb-analyst 根本不注册（registry.specs_for_tier 已处理），rag 档如实降级。
"""

from __future__ import annotations

import asyncio
import logging
from src.observe.logging_setup import get_logger
import os
import time
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import tool

from src import context
from src.config import settings

logger = get_logger(__name__)

# ---------- 配置（统一走 settings，环境变量 MJ_AGENTIC_RAG_* 覆盖）----------
_BASE_URL = (settings.agentic_rag_url or "").rstrip("/")
# 见纪律 2：比服务端 REQUEST_TIMEOUT_SECONDS（默认 120）短
_TIMEOUT = float(settings.agentic_rag_timeout or 110)
_CONNECT_TIMEOUT = float(settings.agentic_rag_connect_timeout or 5)
# 见纪律 1：对齐服务端 anyio.CapacityLimiter 默认 4
_MAX_CONCURRENCY = int(settings.agentic_rag_concurrency or 4)
_RETRY_ON_CONNECT = int(os.getenv("MJ_AGENTIC_RAG_RETRY", "1"))

# ---------- 进程级单例 ----------
# asyncio.Semaphore / httpx.AsyncClient 都绑定事件循环，
# 换 loop（Celery worker、测试里的 asyncio.run）时必须重建，否则报
# "is bound to a different event loop"。所以缓存时连 loop 一起记。
_sem: Optional[asyncio.Semaphore] = None
_sem_loop: Optional[asyncio.AbstractEventLoop] = None
_client: Optional[httpx.AsyncClient] = None
_client_loop: Optional[asyncio.AbstractEventLoop] = None


class AgenticRagUnavailable(Exception):
    """服务不可达 / 超时 / 返回非法。上层据此回落到 MCP stdio 通道。"""


# ---------- 请求级探针（ContextVar）----------
# 为什么用 ContextVar 而不是返回值往上抛：
# 工具是被模型在 ReAct 循环里调用的，平台拿不到它的返回值
# （executor 只能看到事件流里的工具名）。而 answer_level 这类证据质量信号
# 必须落到 API 响应里才能被上层观测。ContextVar 是唯一能把"工具内部结果"
# 带回到"请求边界"的机制，且天然协程隔离（多用户并发不串台，见 context.py）。
_PROBE: ContextVar[Optional[Dict[str, Any]]] = ContextVar("mj_rag_probe", default=None)


def set_probe(data: Dict[str, Any]) -> None:
    _PROBE.set(data)


def last_probe() -> Optional[Dict[str, Any]]:
    """取本轮最后一次知识库调用的质量信号（供 executor 写入响应 meta）。"""
    return _PROBE.get()


def reset_probe() -> None:
    _PROBE.set(None)


def _ensure_sem() -> asyncio.Semaphore:
    global _sem, _sem_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _sem is None or (loop is not None and _sem_loop is not loop):
        _sem = asyncio.Semaphore(max(1, _MAX_CONCURRENCY))
        _sem_loop = loop
    return _sem


def _ensure_client() -> httpx.AsyncClient:
    global _client, _client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _client is None or _client.is_closed or (loop is not None and _client_loop is not loop):
        if _client is not None and not _client.is_closed:
            try:
                asyncio.get_event_loop().create_task(_client.aclose())
            except Exception:  # 换 loop 时关不掉就算了，交给 GC
                pass
        _client = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=httpx.Timeout(_TIMEOUT, connect=_CONNECT_TIMEOUT),
            limits=httpx.Limits(max_connections=_MAX_CONCURRENCY,
                                max_keepalive_connections=_MAX_CONCURRENCY),
        )
        _client_loop = loop
    return _client


def _normalize(payload: Dict[str, Any], elapsed_ms: int) -> Dict[str, Any]:
    """Agentic RAG 的 QueryResponse → 平台内部证据结构。

    两个量纲必须分开带出去（Agentic RAG 自己的口径）：
      score     = RRF 融合分按最高分归一化，第一名恒为 1.0，**只用于排序**
      relevance = dense 余弦原值，>0.7 才谈得上真相关，**才是绝对相关性**
    混用会得出"第一名永远满分"的假象。
    """
    cits: List[Dict[str, Any]] = []
    for c in (payload.get("citations") or [])[:8]:
        cits.append({
            "ref": c.get("ref"),
            "source": c.get("source", ""),
            "title": c.get("title", ""),
            "snippet": (c.get("snippet") or "")[:700],
            "score": round(float(c.get("score") or 0.0), 4),
            "relevance": round(float(c.get("relevance") or 0.0), 4),
        })

    answer = (payload.get("answer") or "").strip()
    level = (payload.get("answer_level") or "").strip()
    # 置信度取证据里的最大 relevance：它是**证据侧的绝对相关性**，
    # 不是"答案命中度"（answer_level 只看 RRF 归一分，高分也可能答非所问）。
    confidence = round(max([c["relevance"] for c in cits], default=0.0), 4)
    trace = payload.get("reasoning_trace") or []
    route = ""
    if isinstance(trace, list) and trace:
        route = str((trace[-1] or {}).get("node", ""))

    # 写探针：让平台边界能观测到这次检索的质量
    set_probe({
        "channel": "http",
        "answer_level": level,
        "confidence": confidence,
        "citations": len(cits),
        "iterations": payload.get("iterations", 0),
        "route": route,
        "elapsed_ms": elapsed_ms,
        "clarify_count": payload.get("clarify_count", 0),
    })

    return {
        "found": bool(answer),
        "answer": answer,
        "answer_level": level,
        "citations": cits,
        "iterations": payload.get("iterations", 0),
        "route": route,
        "elapsed_ms": elapsed_ms,
        # 只回最后 3 步：整个推理链塞进上下文既费 token 又容易带偏模型
        "trace_tail": [f"{t.get('node')}: {str(t.get('detail', ''))[:120]}"
                       for t in trace[-3:]],
        "hint": ("证据不足已拒答（answer_level=refuse），如实转述，禁止补内容。"
                 if level == "refuse" else ""),
    }


async def query(question: str, top_k: int = 5,
                session_id: str = "") -> Dict[str, Any]:
    """调一次完整 Agentic RAG 图（10 节点：分析→路由→多路召回→RRF→评估→改写→生成）。

    返回归一化结构；失败抛 AgenticRagUnavailable，由调用方决定回落。
    """
    q = (question or "").strip()
    if not q:
        raise AgenticRagUnavailable("空问题")

    body: Dict[str, Any] = {"question": q, "top_k": max(1, min(int(top_k or 5), 20))}
    # session_id 用于 Agentic RAG 侧的多轮澄清，平台侧按会话隔离
    try:
        sid = context.get_session_id() or ""
    except Exception:
        sid = ""
    body["session_id"] = session_id or sid or "mingjian"

    sem = _ensure_sem()
    client = _ensure_client()
    last_err: Optional[str] = None

    for attempt in range(_RETRY_ON_CONNECT + 1):
        t0 = time.perf_counter()
        try:
            async with sem:  # 纪律 1：客户端闸门
                resp = await client.post("/agent/query", json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.warning("[agentic-rag] 连接失败(%d/%d): %s", attempt + 1,
                           _RETRY_ON_CONNECT + 1, last_err)
            if attempt < _RETRY_ON_CONNECT:
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            raise AgenticRagUnavailable(f"服务不可达：{last_err}")
        except httpx.TimeoutException as e:
            # 纪律 3：超时不重试，服务端已经满负荷
            raise AgenticRagUnavailable(f"调用超时（>{_TIMEOUT}s）：{e}")
        except Exception as e:
            raise AgenticRagUnavailable(f"{type(e).__name__}: {e}")

        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if resp.status_code == 504:
            # 服务端明确超时：不重试，直接让上层回落快通道
            raise AgenticRagUnavailable("服务端超时（504）")
        if resp.status_code >= 500:
            raise AgenticRagUnavailable(f"服务端 {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise AgenticRagUnavailable(f"请求被拒 {resp.status_code}: {resp.text[:200]}")

        try:
            payload = resp.json()
        except Exception as e:
            raise AgenticRagUnavailable(f"响应不是 JSON: {e}")

        return _normalize(payload, elapsed_ms)

    raise AgenticRagUnavailable(last_err or "未知失败")


@tool
async def agentic_rag_query(question: str, top_k: int = 5):
    """企业知识库深度问答（Agentic RAG 完整图）：查询分析 → 自适应路由 → 多路召回
    （BGE-M3 稠密+稀疏）→ RRF 融合 → 证据评估 → 必要时改写重查 → 生成带 [n] 编号引用的答案。

    用在用户问的是**产品手册 / 设备配置 / 内部文档**里的内容、且需要一份可直接引用的结论时。
    返回：answer（答案正文，引用以 [n] 编号呈现）、answer_level
    （confident/cautious/refuse）、citations（ref 与正文 [n] 一一对应）、iterations、耗时。

    硬性：answer_level=refuse 表示证据不足已拒答，如实转述，禁止用常识补内容；
    引用只能原样使用 citations 里的 source，**禁止编造文档名或 source ID**。
    """
    try:
        return await query(question, top_k=top_k)
    except AgenticRagUnavailable as e:
        logger.warning("[agentic-rag] 不可用，回落到 MCP stdio 通道: %s", e)
        # kind 用契约能识别的取值：unavailable → degraded=not_wired（见 contracts.coerce）
        return {"error": True, "kind": "unavailable",
                "message": f"知识库服务不可用（{e}）。请改用 kb_search（MCP 通道）重试一次；"
                           f"若同样失败，如实上报 not_wired，禁止凭记忆回答。"}


async def health() -> Dict[str, Any]:
    """供 /health 上报用：知识库服务是否活着、走的哪个通道。"""
    try:
        client = _ensure_client()
        r = await client.get("/health", timeout=min(5.0, _CONNECT_TIMEOUT + 2))
        return {"ok": r.status_code == 200, "channel": "http",
                "status": r.status_code, "url": _BASE_URL}
    except Exception as e:
        return {"ok": False, "channel": "http", "url": _BASE_URL,
                "error": f"{type(e).__name__}: {e}"}


def available() -> bool:
    """是否配置了服务地址（不等于服务活着，活着与否看 health()）。"""
    return bool(_BASE_URL)


async def aclose() -> None:
    """关闭连接池（应用 shutdown 时调用）。"""
    global _client
    if _client is not None and not _client.is_closed:
        try:
            await _client.aclose()
        except Exception:
            pass
    _client = None
