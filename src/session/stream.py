"""
流式发送（P5：打字机效果）

为什么单独开一个文件而不是把 send_message 改造成双模式：
    send_message 里"落库 + 记帐 + 缓存写"的顺序是调过的（先落用户消息、
    命中缓存也要落库、缓存只写零工具），把它改成"流式/非流式"两个分支
    会让那套顺序出现两份，改一处漏一处是迟早的事。
    这里只做**流式这一件事**：复用同一批 repo / cache / tracing 调用，
    差别只在"边跑边 yield"。

事件类型（前端据此渲染）：
    start  {tier, decision, model}           —— 先告诉前端档位，好显示状态文案
    token  {text}                            —— 模型增量文本，打字机的原料
    tool   {name, args}                      —— 正在调哪个工具（rag 档会看到 kb_search）
    done   {content, meta, tools_used, ...}  —— 收尾，含落库后的 message_id
    error  {error}

🔴 两个必须注意的点：
    1. token 是**增量**不是全量。前端直接 append 即可，不要每次用新文本替换 ——
       替换会让已经渲染出的内容闪一下，而且长答案下性能很差。
    2. 缓存命中时也要走流式，否则前端会卡在"等待首字"。这里把命中内容切成
       若干块推过去，视觉上一样是打字机。
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator, Dict, List, Optional

from src import context
from src.agent import core
from src.cache import cache_get, cache_key, cache_set
from src.cache.redis_client import history_digest, needs_context
from src.capabilities import search
from src.config import settings
from src.db import repo
from src.identity.namespace import make_thread_id
from src.observe import tracing
from src.routing import executor
from src.routing.tiers import AUTO, resolve_tier

from .service import CACHEABLE_TIERS, _model_label

# 缓存命中时把文本切成多大的块往外推
_CHUNK = 24


def _cacheable(tier: str) -> bool:
    return settings.cache_enabled and settings.redis_on and tier in CACHEABLE_TIERS


async def send_message_stream(user: Dict[str, Any], session_id: str, query: str,
                              requested_tier: Optional[str] = None
                              ) -> AsyncIterator[Dict[str, Any]]:
    user_id = user["id"]
    sess = repo.get_session(user_id, session_id)
    thread_id = make_thread_id(user_id, session_id)

    decision = resolve_tier(requested_tier, sess.get("preferred_tier", AUTO))
    tier = decision.tier

    repo.add_message(session_id, user_id, "user", query, tier=tier)
    search_before = search.usage(user_id)
    history = repo.recent_turns(user_id, session_id,
                                settings.context_turns, settings.context_msg_chars)

    yield {"type": "start", "tier": tier, "decision": decision.to_dict(),
           "model": _model_label(tier), "thread_id": thread_id}

    tr = tracing.start_trace(
        "mingjian.chat", user_id=user_id, session_id=session_id, input=query,
        metadata={"tier": tier, "decision_source": decision.source,
                  "model": _model_label(tier), "async": False, "stream": True},
    )

    ck = ""
    if _cacheable(tier):
        digest = "" if not needs_context(query) else history_digest(history)
        ck = cache_key(tier, _model_label(tier), query, digest, user_id=user_id)
        hit = cache_get(ck)
        if isinstance(hit, dict) and hit.get("content"):
            content = hit["content"]
            for i in range(0, len(content), _CHUNK):
                yield {"type": "token", "text": content[i:i + _CHUNK]}
            meta = dict(hit.get("meta", {}))
            meta.update({"cached": True, "elapsed_sec": 0.0,
                         "decision": decision.to_dict()})
            msg_id = repo.add_message(session_id, user_id, "assistant", content,
                                      tier=tier, meta=meta)
            repo.add_usage(user_id, "message", 1, session_id=session_id)
            repo.add_usage(user_id, "cache_hit", 1, session_id=session_id)
            repo.touch_session(session_id)
            tr.span("cache.hit", metadata={"key": ck}).end(output="命中")
            tracing.end_trace(tr, output=content,
                              metadata={"cached": True, "tier": tier, "elapsed_sec": 0.0})
            yield {"type": "done", "content": content, "cached": True,
                   "message_id": msg_id, "tier": tier,
                   "tools_used": hit.get("tools_used", []),
                   "artifacts": hit.get("artifacts", []), "meta": meta,
                   "elapsed_sec": 0.0}
            return

    t0 = time.time()
    ctx = context.bind(user_id=user_id, session_id=session_id, thread_id=thread_id)
    sp = tr.span("agent.execute", metadata={"tier": tier, "stream": True})

    content = ""
    usage: Dict[str, int] = {}
    tools_used: List[str] = []
    error = ""

    try:
        # 未接入档（如知识库没挂上）在这里就返回，不会去调模型编造
        if tier not in executor.AGENT_TIERS and not (
                tier == executor.RAG and await executor.kb_wired()):
            degradation = f"{tier}_not_wired"
            content = executor.NOT_WIRED.get(tier, f"档位 {tier} 尚未接入。")
            yield {"type": "token", "text": content}
            meta = {"ok": False, "degradation": degradation,
                    "decision": decision.to_dict(), "elapsed_sec": 0.0,
                    "tools_used": [], "artifacts": [], "search_used": 0}
            msg_id = repo.add_message(session_id, user_id, "assistant", content,
                                      tier=tier, meta=meta)
            repo.touch_session(session_id)
            yield {"type": "done", "content": content, "message_id": msg_id,
                   "tier": tier, "tools_used": [], "artifacts": [], "meta": meta,
                   "elapsed_sec": 0.0}
            return

        async for ev in core.run_agent(query, history, tier, thread_id):
            t = ev.get("type")
            if t == "token":
                content += ev.get("text", "")
                yield ev
            elif t == "tool":
                name = ev.get("name", "")
                if name:
                    tools_used.append(name)
                yield {"type": "tool", "name": name, "args": ev.get("args", {})}
            elif t == "error":
                error = ev.get("error", "")
                yield {"type": "error", "error": error}
            elif t == "done":
                # 执行核只保留最后一条 AI 消息，这里以它为准
                content = ev.get("content", "") or content
                usage = ev.get("usage") or {}
    finally:
        ctx.reset()

    elapsed = round(time.time() - t0, 3)
    search_used = search.usage(user_id) - search_before
    artifacts = executor._collect_artifacts()

    meta: Dict[str, Any] = {
        "decision": decision.to_dict(),
        "elapsed_sec": elapsed,
        "tools_used": tools_used,
        "artifacts": artifacts,
        "search_used": search_used,
        "ok": not error,
    }
    if error:
        meta["degradation"] = "agent_error"
        meta["error"] = error
    if usage:
        meta["usage"] = usage

    # 🔴 补三道质量闸门（与 executor 对齐）。
    # 之前流式通道缺失：rag 档走 SSE 时拿不到证据探针 / 引用校验信号，
    # 同一套平台两条通道能力不一致。现在复用 executor.apply_quality_gates。
    if tier == executor.RAG and "task" not in tools_used:
        meta["degradation"] = "rag_no_retrieval"
        meta["warn"] = "本轮未发生知识库派发，答案为模型自有知识，未经验证"
    try:
        from src.routing import executor as _exec
        _exec.apply_quality_gates(tier, tools_used, content, meta)
    except Exception as e:  # 闸门不可用不影响主流程
        meta["quality_gates_error"] = f"{type(e).__name__}: {e}"

    sp.end(metadata={"elapsed_sec": elapsed, "tools_used": tools_used,
                     "search_used": search_used, "ok": not error})

    if ck and not error and not (settings.cache_zero_tool_only and tools_used):
        cache_set(ck, {"content": content, "tools_used": tools_used,
                       "artifacts": artifacts,
                       "meta": {k: v for k, v in meta.items() if k != "elapsed_sec"}},
                  settings.cache_ttl)

    msg_id = repo.add_message(session_id, user_id, "assistant", content,
                              tier=tier, meta=meta)
    repo.add_usage(user_id, "message", 1, session_id=session_id)
    if meta.get("degradation"):
        repo.add_usage(user_id, f"degraded:{meta['degradation']}", 1, session_id=session_id)
    for t in set(tools_used):
        repo.add_usage(user_id, f"tool:{t}", 1, session_id=session_id)
    repo.touch_session(session_id)
    if sess.get("title") in ("新对话", "", None):
        title = (query or "").strip().replace("\n", " ")[:20] or "新对话"
        repo.update_session(user_id, session_id, title=title)

    tr.span("llm.generate", as_type="generation", model=_model_label(tier),
            input=query).end(output=content,
                             usage_details=tracing.usage_details_from(usage),
                             metadata={"usage_raw": usage or None, "elapsed_sec": elapsed})
    tracing.end_trace(tr, output=content,
                      metadata={"tier": tier, "cached": False, "elapsed_sec": elapsed,
                                "tools_used": tools_used, "stream": True},
                      usage_details=tracing.usage_details_from(usage))

    yield {"type": "done", "content": content, "message_id": msg_id, "tier": tier,
           "tools_used": tools_used, "artifacts": artifacts, "meta": meta,
           "elapsed_sec": elapsed, "thread_id": thread_id, "cached": False}
