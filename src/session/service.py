"""
会话服务：把「上下文绑定 → 档位决策 → 执行 → 落库 → 记帐」串成一次请求

P2 相对 P1 的变化：
1. **执行前绑定请求上下文**（user/session/thread），让工具层的预算与文件目录
   天然带用户隔离 —— 这是把隔离从 API 层贯彻到工具层的那一环
2. **档位不再由规则猜测**，只表达"允许用什么 / 希望怎么做"，判断权交回模型
3. 记录本轮实际用过的工具与产出的文档，供前端展示，也为后续配额统计留数据
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from src import context
from src.cache import cache_get, cache_key, cache_set
from src.cache.redis_client import history_digest, needs_context
from src.capabilities import search
from src.config import settings
from src.db import repo
from src.observe import tracing
from src.identity.namespace import make_thread_id
from src.routing import executor
from src.routing.tiers import AUTO, DIRECT, resolve_tier

# 只有这两档允许缓存：
#   direct —— 零工具、纯模型输出，重复提问答案稳定
#   auto   —— 命中缓存的前提是"本轮没调用任何工具"（见下方 cacheable 判断）
# tool / research 档不缓存：前者读的是用户刚上传的文件（随时在变），
# 后者联网检索，结果带时效性，缓存住就是在给用户喂过期信息。
CACHEABLE_TIERS = (DIRECT, AUTO)


def session_context(user_id: str, session_id: str) -> Dict[str, Any]:
    return {"thread_id": make_thread_id(user_id, session_id),
            "user_id": user_id, "session_id": session_id}


def _model_label(tier: str) -> str:
    """缓存 key 里要带模型名：换模型后旧答案不该继续喂给用户。"""
    if tier == DIRECT and settings.llm_model_fast:
        return settings.llm_model_fast
    return settings.llm_model


def _cacheable(tier: str) -> bool:
    return settings.cache_enabled and settings.redis_on and tier in CACHEABLE_TIERS


async def send_message(user: Dict[str, Any], session_id: str, query: str,
                       requested_tier: Optional[str] = None) -> Dict[str, Any]:
    user_id = user["id"]
    sess = repo.get_session(user_id, session_id)     # 内含归属校验
    thread_id = make_thread_id(user_id, session_id)

    decision = resolve_tier(requested_tier, sess.get("preferred_tier", AUTO))
    tier = decision.tier

    # 先落用户消息：即使后面模型挂了，用户输入也不会丢
    repo.add_message(session_id, user_id, "user", query, tier=tier)

    # 检索用量的"起读数"：本轮结束时用 终值-起读数 得出真实消耗。
    # 不能等 ctx.reset() 之后再读 —— 那时 context.budget_key() 已是 None，
    # usage() 会落到 "global" 键上永远返回 0（实测踩到：工具明明调用了，
    # 记帐里 search_used 却是 0，用量统计形同虚设）。
    search_before = search.usage(user_id)

    history = repo.recent_turns(user_id, session_id,
                                settings.context_turns, settings.context_msg_chars)

    # ---- 开一条 trace（没配 Langfuse 时返回 no-op 句柄，下面照常调用）----
    tr = tracing.start_trace(
        "mingjian.chat", user_id=user_id, session_id=session_id, input=query,
        metadata={"tier": tier, "decision_source": decision.source,
                  "model": _model_label(tier), "async": False},
    )

    # ---- 缓存读 ----
    ck = ""
    if _cacheable(tier):
        # 自包含的提问才敢不带上文（见 redis_client.needs_context 里的实测说明）；
        # 否则必须带上文摘要 —— 宁可不命中，也不能把上一轮的答案错喂出来。
        digest = "" if not needs_context(query) else history_digest(history)
        ck = cache_key(tier, _model_label(tier), query, digest, user_id=user_id)
        hit = cache_get(ck)
        if isinstance(hit, dict) and hit.get("content"):
            out = _finish_cached(user_id, session_id, query, tier, decision, hit)
            tr.span("cache.hit", metadata={"key": ck}).end(output="命中")
            tracing.end_trace(tr, output=out["content"],
                              metadata={"cached": True, "tier": tier, "elapsed_sec": 0.0})
            return out

    t0 = time.time()
    # 绑定上下文：工具层的预算键/文件目录/引用池全部由此派生
    ctx = context.bind(user_id=user_id, session_id=session_id, thread_id=thread_id)
    sp = tr.span("agent.execute", metadata={"tier": tier, "tools_allowed": tier})
    try:
        result = await executor.execute(query, history, tier, thread_id)
    finally:
        ctx.reset()

    elapsed = round(time.time() - t0, 3)
    content = result.get("content", "")
    meta = dict(result.get("meta", {}))
    search_used = search.usage(user_id) - search_before
    usage = result.get("usage") or {}
    meta.update({
        "decision": decision.to_dict(),
        "elapsed_sec": elapsed,
        "tools_used": result.get("tools_used", []),
        "artifacts": result.get("artifacts", []),
        "search_used": search_used,
    })
    if usage:
        meta["usage"] = usage
    sp.end(metadata={"elapsed_sec": elapsed, "tools_used": result.get("tools_used", []),
                     "search_used": search_used, "ok": meta.get("ok", True)})

    # ---- 缓存写：只写"零工具"的回答 ----
    # 本轮只要调用过任何工具（联网检索 / 读文件 / 写文档），结果就带时效性或副作用，
    # 缓存住等于把一次性的结果当成永久答案。宁可少缓存，也不能答错。
    if ck and meta.get("ok") and not (settings.cache_zero_tool_only
                                      and result.get("tools_used")):
        # 调用了工具的回答带时效性（检索结果、文件内容会变），
        # 用**更短的 TTL**而不是一刀切 1 小时：既吃到缓存的收益，
        # 又不至于把一次性的检索结果当成长期有效的答案喂给用户。
        ttl = (settings.cache_ttl_with_tools if result.get("tools_used")
               else settings.cache_ttl)
        cache_set(ck, {"content": content, "tools_used": result.get("tools_used", []),
                       "artifacts": result.get("artifacts", []),
                       "meta": {k: v for k, v in meta.items() if k != "elapsed_sec"}},
                  ttl)

    msg_id = repo.add_message(session_id, user_id, "assistant", content,
                              tier=tier, meta=meta)

    repo.add_usage(user_id, "message", 1, session_id=session_id)
    if meta.get("degradation"):
        repo.add_usage(user_id, f"degraded:{meta['degradation']}", 1, session_id=session_id)
    for t in set(result.get("tools_used", []) or []):
        repo.add_usage(user_id, f"tool:{t}", 1, session_id=session_id)

    # P-4：token 记账（成本核算的基础）。usage 来自 LangChain 的 usage_metadata，
    # 字段是 OpenAI 兼容的 input_tokens/output_tokens（也兼容 total_tokens 兜底）。
    # 只记客观 token 数，成本换算在 API 层按配置单价做 —— token 不会错，单价才可能配错。
    _model = _model_label(tier)
    if usage:
        it = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        ot = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        if it:
            repo.add_usage(user_id, "token_input", int(it),
                           session_id=session_id, model=_model)
        if ot:
            repo.add_usage(user_id, "token_output", int(ot),
                           session_id=session_id, model=_model)

    repo.touch_session(session_id)

    if sess.get("title") in ("新对话", "", None):
        title = (query or "").strip().replace("\n", " ")[:20] or "新对话"
        repo.update_session(user_id, session_id, title=title)

    # generation span：模型这一跳的耗时与 token。
    # usage 拿不到就留空 —— 假的成本数字比没有更危险。
    tr.span("llm.generate", as_type="generation",
            model=_model_label(tier), input=query).end(
        output=content,
        usage_details=tracing.usage_details_from(usage),
        metadata={"usage_raw": usage or None, "elapsed_sec": elapsed},
    )
    tracing.end_trace(
        tr, output=content,
        metadata={"tier": tier, "cached": False, "elapsed_sec": elapsed,
                  "tools_used": result.get("tools_used", []),
                  "search_used": search_used,
                  "degradation": meta.get("degradation", "")},
        usage_details=tracing.usage_details_from(usage),
    )

    # P-1：长期记忆异步提炼。消息已落库（上面 add_message），此时后台提炼能读到
    # 完整的本轮对话。用 fire-and-forget：记忆是"增强"不是"必需"，
    # 绝不能因为提炼阻塞把 send_message 的延迟拖长。
    # 提炼函数本身有幂等保护（memory_extracts 记录 last_message_id），
    # 重复触发不会重复提炼。异常在 extract_memories 内部已兜住，不会冒到这里。
    try:
        from src.agent import memory as _mem
        asyncio.create_task(_mem.extract_memories(user_id, session_id))
    except Exception:   # noqa: BLE001 提炼触发失败不致命
        pass

    return {
        "session_id": session_id,
        "message_id": msg_id,
        "tier": tier,
        "decision": decision.to_dict(),
        "content": content,
        "tools_used": result.get("tools_used", []),
        "artifacts": result.get("artifacts", []),
        "meta": meta,
        "elapsed_sec": elapsed,
        "thread_id": thread_id,
    }


def _finish_cached(user_id: str, session_id: str, query: str, tier: str,
                   decision, hit: Dict[str, Any]) -> Dict[str, Any]:
    """
    缓存命中路径。

    两条不能省的收尾：
    1. **命中也要落库**。缓存只在 Redis 里，不写 messages 表的话
       刷新页面这条回答就没了，而且下一轮拿不到上文 —— 缓存就变成了数据丢失。
    2. **记一笔 cache_hit 用量**。没有这个计数，"缓存到底有没有用"永远只能靠感觉，
       命中率是调 TTL 与桶大小的唯一依据。
    """
    content = hit.get("content", "")
    meta = dict(hit.get("meta", {}))
    meta.update({"cached": True, "elapsed_sec": 0.0,
                 "decision": decision.to_dict()})
    msg_id = repo.add_message(session_id, user_id, "assistant", content,
                              tier=tier, meta=meta)
    repo.add_usage(user_id, "message", 1, session_id=session_id)
    repo.add_usage(user_id, "cache_hit", 1, session_id=session_id)
    repo.touch_session(session_id)
    return {
        "session_id": session_id,
        "message_id": msg_id,
        "tier": tier,
        "decision": decision.to_dict(),
        "content": content,
        "tools_used": hit.get("tools_used", []),
        "artifacts": hit.get("artifacts", []),
        "meta": meta,
        "elapsed_sec": 0.0,
        "thread_id": make_thread_id(user_id, session_id),
        "cached": True,
    }


def history(user_id: str, session_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    repo.get_session(user_id, session_id)
    return repo.list_messages(user_id, session_id, limit=limit)
