"""
联网搜索工具（豆包 Global Search，从旧项目平移并改造）

改造点：
1. **预算键从"会话"改成"用户"** —— 旧项目按 thread_id 计数，多开几个会话就能把配额翻几倍
2. **去掉对 api.monitor 的硬依赖** —— 事件推送改为可选回调，工具本身可以在纯 Python 里单测
3. 保留旧项目验证过的两个关键机制：
   - **工具层硬熔断**（prompt 里的"最多搜 5 次"是软约束，模型会无视；实测同一 query 被调 14 次）
   - **原子 check-and-consume**（先判断再自增会竞态，实测出现过"预算耗尽 11/10"）
   - **重复 query 缓存**（不再重复消耗额度）
4. 真实返回后把 URL 登记进引用池，供落盘前的溯源校验使用
"""

from __future__ import annotations

import logging
from src.observe.logging_setup import get_logger
import asyncio
import os
import threading
import time
from typing import Any, Dict, List, Literal, Optional

import requests
from langchain_core.tools import StructuredTool

from src import context
from src.config import settings

logger = get_logger(__name__)

_SEARCH_BUDGET = int(os.getenv("MJ_SEARCH_BUDGET", "12"))
# 全局预算的统计窗口（秒）。过期自动重置，等价于"每 24h 刷新一次配额"。
_BUDGET_TTL = int(os.getenv("MJ_SEARCH_BUDGET_TTL", "86400"))
_MAX_TRACKED = 500
_lock = threading.RLock()
_used: Dict[str, int] = {}
_query_cache: Dict[str, Dict[str, Any]] = {}

_ENDPOINT = os.getenv("DOUBAO_SEARCH_ENDPOINT",
                      "https://open.feedcoopapi.com/search_api/global_search")
_API_KEY = os.getenv("DOUBAO_SEARCH_API_KEY", "")
_TIMEOUT = (5, 30)
_MAX_RETRY = 2

_TOPIC_HINT = {"news": "最新", "finance": "财经 财报 行情", "general": ""}

_UNAVAILABLE = "联网搜索未配置（缺少 DOUBAO_SEARCH_API_KEY），当前不可用。请改用已有知识回答，并如实说明未做联网检索。"

_ERR_MSG = {
    10400: "搜索请求参数不合法（请缩短或简化查询词后重试）",
    10403: "搜索接口鉴权失败（API Key 无效或已过期）",
    10408: "搜索请求超时（请稍后重试或缩短查询词）",
    10410: "搜索额度已用尽（请联系管理员充值）",
    10412: "搜索结果为空（请换用更宽泛的关键词）",
    10500: "搜索服务内部错误（请稍后重试）",
    700429: "搜索请求触发限流（请降低并发后重试）",
    700901: "搜索服务暂时不可用（请稍后重试）",
}


def _try_consume(key: str):
    """
    原子「检查 + 占用」，两步分开会竞态，必须放同一临界区。

    P8：优先走 **Redis 全局预算**。原因 —— 进程内 dict 在多 worker 部署下
    每个 worker 各数各的，用户实际能搜 `_SEARCH_BUDGET × worker数` 次，
    而花掉的是真金白银的外部 API 额度。Redis 不可用（单机 / 未配置 / 连不上）
    时回退本地计数，行为与改造前一致。
    """
    try:
        from src.cache import redis_client
        got = redis_client.budget_consume(
            f"search:{key}", _SEARCH_BUDGET, 1, ttl=_BUDGET_TTL)
        if got is not None:
            return got
    except Exception as e:              # noqa: BLE001
        logger.debug("[search] 全局预算不可用，回退本地计数: %s", e)

    with _lock:
        if len(_used) > _MAX_TRACKED:
            _used.clear()
            _query_cache.clear()
        cur = _used.get(key, 0)
        if cur >= _SEARCH_BUDGET:
            return False, cur
        _used[key] = cur + 1
        return True, cur


def _cache_get(key: str, query: str):
    with _lock:
        return _query_cache.get(f"{key}::{query}")


def _cache_put(key: str, query: str, value: Dict[str, Any]):
    with _lock:
        _query_cache[f"{key}::{query}"] = value


def _extract_snippet(doc: Dict[str, Any]) -> str:
    parts: List[str] = []
    for item in doc.get("Snippet") or []:
        if item.get("Type") == "text" and item.get("Text"):
            parts.append(item["Text"].strip())
    return "\n".join(parts).strip()


def _normalize(query: str, resp: Dict[str, Any]) -> Dict[str, Any]:
    documents = (((resp or {}).get("Result") or {}).get("Documents")) or []
    results: List[Dict[str, Any]] = []
    for idx, doc in enumerate(documents):
        publish = ((doc.get("DocumentInfo") or {}).get("PublishTime")) or ""
        host = ((doc.get("HostInfo") or {}).get("Hostname")) or ""
        results.append({
            "title": doc.get("Title") or "",
            "url": doc.get("Url") or "",
            "content": _extract_snippet(doc),
            "score": round(1.0 - idx / max(len(documents), 1), 4),
            "published_date": publish,
            "source": host,
        })
    return {"query": query, "results": results}


def _build_payload(query: str, doc_count: int, snippet_len: int) -> Dict[str, Any]:
    return {
        "SearchType": "web",
        "Query": query,
        "DocCount": max(1, min(int(doc_count), 20)),
        "MaxSnippetLength": max(100, min(int(snippet_len), 5000)),
        "MaxImageCountPerDoc": 0,
    }


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_API_KEY}", "Content-Type": "application/json"}


def _check_body(body: Dict[str, Any]) -> Dict[str, Any]:
    meta = body.get("ResponseMetadata") or {}
    if meta.get("Error"):
        code = (meta.get("Error") or {}).get("Code")
        raise RuntimeError(_ERR_MSG.get(code, f"搜索失败（错误码 {code}）"))
    return body


def _do_search_sync(query: str, doc_count: int, snippet_len: int) -> Dict[str, Any]:
    """同步实现。**只在没有事件循环可让出时**才走这条路（同步子图、脚本直调）。"""
    if not _API_KEY:
        raise RuntimeError("DOUBAO_SEARCH_API_KEY 未配置")
    payload = _build_payload(query, doc_count, snippet_len)
    headers = _headers()
    last_err: Optional[Exception] = None
    for attempt in range(_MAX_RETRY + 1):
        try:
            resp = requests.post(_ENDPOINT, json=payload, headers=headers,
                                 timeout=_TIMEOUT)
            return _check_body(resp.json())
        except RuntimeError:
            raise
        except Exception as e:
            last_err = e
            if attempt < _MAX_RETRY:
                time.sleep(0.6 * (2 ** attempt))
                continue
    raise RuntimeError(f"搜索请求失败（已重试 {_MAX_RETRY} 次）: {last_err}")


async def _do_search_async(query: str, doc_count: int, snippet_len: int) -> Dict[str, Any]:
    """异步实现（主路径）。

    为什么必须单独有一份 async：
        工具是**在 FastAPI 的事件循环线程里**被执行的 —— 图走 astream，
        LangGraph 对同步工具就是直接调用，不会替你扔线程池。
        于是 `requests.post` 这一次 3~10 秒的阻塞会把整个服务冻住，
        期间所有请求都排不上队：不是"变慢"，是"假死"。
        重试退避用的 `time.sleep` 更狠，一次网络抖动额外冻 0.6 + 1.2 秒。

        httpx.AsyncClient 在等待响应时让出控制权，asyncio.sleep 只挂起当前协程，
        两者都不占事件循环。

    这里**刻意不做客户端复用**：httpx.AsyncClient 会绑定到创建它的事件循环，
    模块级缓存后在另一个 loop（uvicorn 多 worker / 测试用例的新 loop）里复用
    会直接报 "Event loop is closed" 一类错误 —— 这个坑在 Agentic RAG 接入层踩过。
    建连开销相对一次外网搜索可以忽略。
    """
    import httpx

    if not _API_KEY:
        raise RuntimeError("DOUBAO_SEARCH_API_KEY 未配置")
    payload = _build_payload(query, doc_count, snippet_len)
    headers = _headers()
    timeout = httpx.Timeout(_TIMEOUT[0], read=_TIMEOUT[1])
    last_err: Optional[Exception] = None
    for attempt in range(_MAX_RETRY + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as cli:
                resp = await cli.post(_ENDPOINT, json=payload, headers=headers)
                resp.raise_for_status()
                return _check_body(resp.json())
        except RuntimeError:
            raise
        except Exception as e:
            last_err = e
            if attempt < _MAX_RETRY:
                await asyncio.sleep(0.6 * (2 ** attempt))
                continue
    raise RuntimeError(f"搜索请求失败（已重试 {_MAX_RETRY} 次）: {last_err}")


def _prepare(query: str, topic: str, max_results: int,
             include_raw_content: bool):
    """预算 / 缓存 / 参数预处理（sync 与 async 共用）。

    返回 (key, short_circuit, effective_query, snippet_len)；
    short_circuit 非 None 表示应当直接把它返回给模型（未配置 / 预算耗尽 / 缓存命中）。
    """
    key = context.budget_key() or "global"

    if not _API_KEY:
        return key, _UNAVAILABLE, "", 0

    allowed, used = _try_consume(key)
    if not allowed:
        logger.warning("[search] 用户 %s 搜索预算耗尽（%s/%s）", key, used, _SEARCH_BUDGET)
        return key, {
            "query": query, "results": [], "search_limited": True,
            "message": (f"已达本次搜索预算上限（{_SEARCH_BUDGET} 次检索）。"
                        "请立即停止检索，基于已获取的信息直接输出结论；"
                        "有缺口就在结论里如实标注『未检索到』。"),
        }, "", 0

    cached = _cache_get(key, query)
    if cached is not None:
        return key, {**cached, "cache_hit": True,
                     "message": "该关键词本次已检索过（缓存结果）。"
                                "请换角度或直接总结，不要重复检索。"}, "", 0

    hint = _TOPIC_HINT.get(topic, "")
    effective = f"{hint} {query}".strip() if hint else query
    snippet_len = 3000 if include_raw_content else 800
    return key, None, effective, snippet_len


def _finalize(query: str, key: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    result = _normalize(query, raw)
    _cache_put(key, query, result)
    # 登记真实 URL，供写文档前的溯源校验比对（防模型编造来源）
    try:
        from src.capabilities.citation_guard import register_urls
        register_urls(context.citation_key(),
                      [r.get("url", "") for r in result.get("results", [])])
    except Exception as e:  # 登记失败不影响主流程
        logger.debug("[search] 引用登记失败: %s", e)
    return result


def _on_error(query: str, e: Exception) -> Dict[str, Any]:
    logger.warning("[search] 搜索失败 query=%r: %s", query, e)
    # 失败不抛异常：一次网络抖动不该打断整条链路，让模型自己换关键词或换路径
    return {"query": query, "results": [], "error": str(e)}


def _internet_search_sync(query: str,
                          topic: Literal["news", "finance", "general"] = "general",
                          max_results: int = 5,
                          include_raw_content: bool = False) -> Dict[str, Any]:
    key, short, effective, snippet_len = _prepare(
        query, topic, max_results, include_raw_content)
    if short is not None:
        return short
    try:
        return _finalize(query, key,
                         _do_search_sync(effective, max_results, snippet_len))
    except Exception as e:
        return _on_error(query, e)


async def _internet_search_async(query: str,
                                 topic: Literal["news", "finance", "general"] = "general",
                                 max_results: int = 5,
                                 include_raw_content: bool = False) -> Dict[str, Any]:
    key, short, effective, snippet_len = _prepare(
        query, topic, max_results, include_raw_content)
    if short is not None:
        return short
    try:
        return _finalize(query, key,
                         await _do_search_async(effective, max_results, snippet_len))
    except Exception as e:
        return _on_error(query, e)


# 双实现：async 走主路径（FastAPI 事件循环里 await，不阻塞服务），
# sync 兜住"没有事件循环可让"的调用方（同步子图 / 脚本直调 / 单测）。
# 用 StructuredTool 而不是 @tool 装饰器，就是因为要同时挂 func 与 coroutine ——
# 只给一个的话，另一条路径要么报"不能在 running loop 里 invoke"，
# 要么退化成阻塞调用，白改。
internet_search = StructuredTool.from_function(
    func=_internet_search_sync,
    coroutine=_internet_search_async,
    name="internet_search",
    description=(
        "联网检索公开信息。只在确实需要外部事实、行业数据、新闻动态时才调用；"
        "凭已有知识能回答的问题不要调用它。"
        "返回结构：{query, results:[{title,url,content,published_date,source}]}"
    ),
)

def available() -> bool:
    return bool(_API_KEY)


def usage(key: Optional[str] = None) -> int:
    with _lock:
        return _used.get(key or context.budget_key() or "global", 0)
