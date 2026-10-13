"""
档位执行器（P2：接执行核）

职责：把档位翻译成"跑哪个图 + 收集事件 + 诚实降级"。

诚实原则没变：没接后端就明说没接后端，绝不用"假装检索到了"的话术糊弄。
rag 档在知识库接入（P4）之前会直接返回降级说明，不会去调模型编检索结果。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from src.agent import core
from src.routing.tiers import AGENT_TIERS, RAG, RESEARCH, TIER_META

NOT_WIRED = {
    RAG: (
        "【检索答】档尚未接入知识库。\n\n"
        "原因是 kb_search / kb_lookup 这两个 MCP 工具没挂上："
        "需要 MJ_MCP_SERVERS 含 rag，并用 MJ_MCP_RAG_CMD / MJ_MCP_RAG_CWD "
        "指向 Agentic RAG 项目自己的虚拟环境（两边依赖装不进同一个环境）。\n"
        "我不编造检索结果 —— 可以切到【自动】档：模型会自己判断要不要联网检索，"
        "或【深度调研】档走完整方法论。"
    ),
}


async def kb_wired() -> bool:
    """知识库真的挂上了吗（有 kb_ 开头的工具才算）。

    为什么看工具名而不是看配置开关：
    开关是"我打算挂"，工具是"真挂上了"。排障时这两个必须分得开。
    """
    try:
        tools = await core.mcp_tools()
    except Exception:
        return False
    return any(getattr(t, "name", "").startswith("kb_") for t in tools or [])


async def execute(query: str, history: List[Dict[str, str]], tier: str,
                  thread_id: str) -> Dict[str, Any]:
    """
    返回：
      {
        "content": 最终文本,
        "events":  事件列表（token/tool/done，供前端逐步渲染）,
        "tools_used": 调用过的工具名列表,
        "artifacts": 产出的文档（由 write_document 返回）,
        "meta":     {"ok":bool, "degradation":str}
      }
    """
    # P5：rag 档能不能用，取决于知识库 MCP Server 是否真的挂上了。
    # 这里**运行时探测**而不是写死常量 —— 否则"挂上了但配置错"和"压根没配"
    # 会表现成一回事，排障时无从下手。
    if tier == RAG and not await kb_wired():
        content = NOT_WIRED.get(tier, f"档位 {tier} 尚未接入。")
        return {
            "content": content,
            "events": [],
            "tools_used": [],
            "artifacts": [],
            "meta": {"ok": False, "degradation": f"{tier}_not_wired",
                     "label": TIER_META.get(tier, {}).get("label", tier)},
        }

    if tier not in AGENT_TIERS and tier != RAG:
        content = NOT_WIRED.get(tier, f"档位 {tier} 尚未接入。")
        return {
            "content": content,
            "events": [],
            "tools_used": [],
            "artifacts": [],
            "meta": {"ok": False, "degradation": f"{tier}_not_wired",
                     "label": TIER_META.get(tier, {}).get("label", tier)},
        }

    events: List[Dict[str, Any]] = []
    tools_used: List[str] = []
    artifacts: List[Dict[str, Any]] = []
    error = ""

    # P-2 下半场：编排前置层。只在 research 档触发 —— 深度调研是唯一需要
    # "显式规划拆解"的档位，direct/auto/rag/tool 都不该被强制规划
    # （吸收旧项目"永远出报告"的教训：简单任务绝不进规划）。
    # 编排只做"复杂度判断 + 规划拆解"，把拆解清单拼进 query 交给主 Agent；
    # 并发派发由主图内置的 gather 完成，不在这里重造。
    effective_query = query
    orchestration: Dict[str, Any] = {}
    if tier == RESEARCH:
        try:
            from src.agent import orchestrator
            orchestration = await orchestrator.orchestrate(query)
            plan_text = orchestration.get("plan_text", "")
            if plan_text:
                effective_query = f"{plan_text}\n\n{query}"
        except Exception as e:      # noqa: BLE001 编排失败降级为原问题直跑
            logger.warning(f"编排前置失败（降级直跑）: {type(e).__name__}: {e}")
            orchestration = {}

    async for ev in core.run_agent(effective_query, history, tier, thread_id):
        events.append(ev)
        t = ev.get("type")
        if t == "tool":
            name = ev.get("name", "")
            if name:
                tools_used.append(name)
        elif t == "error":
            error = ev.get("error", "")
        elif t == "done":
            pass

    # write_document 的产物从工具返回结果里拿不到（astream messages 模式不含工具结果），
    # 改为直接从产物目录里识别本轮新增的 .md 文件（P3 会改成显式回传工具结果）
    artifacts = _collect_artifacts()

    if error:
        # 模型侧不可用（熔断 / 超时 / 5xx / 429）不该把裸异常抛给用户：
        # 用户看不懂 "Connection reset by peer"，还会以为是自己问错了。
        # 这里换成明确的兜底话术，但 **降级必须可见** ——
        # meta.degradation 标明降级原因，不假装这是一次正常回答。
        try:
            from src.llm import resilience
            if resilience.is_llm_down(error):
                return {
                    "content": resilience.FALLBACK_NO_MODEL,
                    "events": events, "tools_used": tools_used,
                    "artifacts": artifacts,
                    "meta": {"ok": False, "degradation": "llm_unavailable",
                             "error": error, "retryable": True},
                }
        except Exception:               # noqa: BLE001 判定失败就按普通错误处理
            pass
        return {
            "content": f"执行核出错：{error}",
            "events": events, "tools_used": tools_used, "artifacts": artifacts,
            "meta": {"ok": False, "degradation": "agent_error", "error": error},
        }

    content = ""
    usage: Dict[str, int] = {}
    for ev in reversed(events):
        if ev.get("type") == "done":
            content = ev.get("content", "")
            usage = ev.get("usage") or {}
            break

    # 御史台式的兜底：rag 档本该把活派给 kb-analyst，一次 task 都没发
    # 就说明模型绕过检索直接凭记忆作答了 —— 这时答案是不可信的。
    # 不静默放行，也不擅自改写模型的话，而是把事实写进 meta 让上层可观测。
    # （2026-09-29 实测到过一次：模型编出《NR 系列 Web 手册 V1.02》和不存在的
    #   source ID kb-h3c-nr-web-v102-202403，根因是提示词还在命令它调已被下放的 kb_*。）
    meta: Dict[str, Any] = {"ok": True, "degradation": ""}
    if tier == RAG and "task" not in tools_used:
        meta = {"ok": True, "degradation": "rag_no_retrieval",
                "warn": "本轮未发生知识库派发，答案为模型自有知识，未经验证"}

    # P-1 + P-2：任务复杂度信号 + 编排结果写进 meta（可观测）。
    # research 档走编排前置层（有 plan），其他档只打复杂度标签不改路径。
    try:
        if orchestration.get("complexity"):
            # 编排已判过，直接复用（避免重复判断，也保证 meta 与编排一致）
            meta["complexity"] = orchestration["complexity"]
        else:
            from src.routing.complexity import judge
            meta["complexity"] = judge(query).to_dict()
    except Exception:   # noqa: BLE001 复杂度判定失败不影响主流程
        meta["complexity"] = {"level": "unknown", "reason": "judge_error"}
    if orchestration:
        meta["orchestration"] = {
            "needs_planning": orchestration.get("needs_planning", False),
            "plan_count": len(orchestration.get("plan", []) or []),
        }

    # 三道质量闸门抽成公共函数，供 executor 与 stream 两个通道共用
    # （避免复制粘贴导致两条通道行为漂移 —— 曾排查发现流式通道缺失这些闸门）。
    apply_quality_gates(tier, tools_used, content, meta)

    return {
        "content": content,
        "events": events,
        "tools_used": tools_used,
        "artifacts": artifacts,
        "usage": usage,
        "meta": meta,
    }


def apply_quality_gates(tier: str, tools_used: List[str], content: str,
                        meta: Dict[str, Any]) -> None:
    """把证据质量探针 + 引用溯源校验写进 meta（就地修改，不返回）。

    executor 与 stream 两个通道都调用它，保证 rag 档的可观测性一致。
    rag_no_retrieval 在调用方单独判断（因为 stream 的 tools_used 收集时机不同），
    这里只做 ①② 两件事。

    ① P7：证据质量探针（answer_level / confidence / citations）
    ② P7：引用溯源校验（citation_check）
    """
    # ① 证据质量探针。
    # 之前只有一道"有没有派发"的闸门，kb-analyst 即使召回了弱证据、
    # 甚至明确拒答（answer_level=refuse），响应里也看不出来 —— 信号死在模型上下文里。
    # 工具是被模型在 ReAct 循环里调用的，平台拿不到它的返回值，
    # 所以走 ContextVar 探针（协程隔离，多用户并发不串台）把结果带回请求边界。
    try:
        from src.capabilities import agentic_rag
        probe = agentic_rag.last_probe()
        if probe:
            agentic_rag.reset_probe()
            meta["evidence"] = {
                "channel": probe.get("channel", ""),
                "answer_level": probe.get("answer_level", ""),
                "confidence": probe.get("confidence"),
                "citations": probe.get("citations", 0),
                "iterations": probe.get("iterations", 0),
                "route": probe.get("route", ""),
                "elapsed_ms": probe.get("elapsed_ms", 0),
            }
            # 拒答或零证据：这轮检索实质失败，如实写进 degradation
            # （不改写模型的话，只标注事实 —— 跟 rag_no_retrieval 同一条纪律）
            if probe.get("answer_level") == "refuse" or not probe.get("citations"):
                meta["degradation"] = "rag_no_evidence"
                meta["warn"] = "知识库证据不足或已拒答，答案未获证据支撑"
    except Exception as e:  # 探针不可用不影响主流程
        meta["evidence_error"] = f"{type(e).__name__}: {e}"

    # ② 引用溯源校验。
    # 原来 citation_guard 只在 write_document 落盘前跑一次（files.py:88），
    # 而用户实际看到答案的地方是聊天 —— 模型在这儿编的 URL 没有任何一道校验。
    # 只在"本次真的联网检索过"时才校验：注册表为空时每个 URL 都会判为未溯源，
    # 那是在制造噪声（用户自己贴的链接、代码块里的链接都会被误伤）。
    # 校验结果只**写进 meta**，不改写正文：聊天里追加一大段"校验章节"等于污染答案。
    try:
        from src import context
        from src.capabilities import citation_guard
        key = context.citation_key() or ""
        if citation_guard.get_registered(key):
            res = citation_guard.verify_citations(content, key)
            if res.get("total"):
                meta["citation_check"] = {
                    "total": res["total"],
                    "verified": res["verified"],
                    "coverage": res["coverage"],
                    "unverified": res["unverified"][:5],
                }
                if res["unverified"]:
                    meta["degradation"] = meta.get("degradation") or "citation_unverified"
                    meta.setdefault("warn", "正文存在无法在本次检索结果中溯源的链接")
    except Exception as e:  # 校验失败不影响主流程
        meta["citation_error"] = f"{type(e).__name__}: {e}"


def _collect_artifacts() -> List[Dict[str, Any]]:
    """列出当前用户/会话目录下最近产出的 .md 文档。"""
    try:
        import time
        from pathlib import Path

        from src import context
        from src.config import settings
        from src.identity.namespace import storage_dir

        uid = context.get_user_id() or "anonymous"
        sid = context.get_session_id() or "default"
        d = Path(storage_dir(settings.storage_root, uid, sid))
        if not d.exists():
            return []
        now = time.time()
        out = []
        for p in d.iterdir():
            if p.is_file() and p.suffix == ".md" and (now - p.stat().st_mtime) < 600:
                out.append({"name": p.name, "size": p.stat().st_size, "path": str(p)})
        return out
    except Exception:
        return []
