# -*- coding: utf-8 -*-
"""
编排前置层（P-2 下半场 + P-3 中断）：显式图定骨架，动态节点做探索。

这是中台方案 §2「混合架构」的落地：
- **骨架层（本文件，确定性）**：复杂度判断 → 规划拆解 →（可选）人工确认，用 StateGraph
  显式表达，拓扑可见、可测、可中断。
- **探索层（主图，不确定性）**：主 Agent 的 ReAct 循环 + 并发派发子 Agent，
  复用 deepagents 已编译的主图。

为什么编排层只做"判断 + 规划"，不做"并发派发"
----------------------------------------------
深入源码核实过一个关键事实：deepagents 的 task 工具 + LangGraph 的 ToolNode
**已经内置了并发**（主 Agent 一个轮次发多个 task tool_call，ToolNode 用
asyncio.gather 并发执行，见 langgraph/prebuilt ToolNode 源码 line 236）。
所以"并发派发"不需要在编排层重造。编排层只负责 deepagents **不负责**的部分。

P-3 为什么把 interrupt 点放在这里（而不是 deepagents 主图里）
------------------------------------------------------------
deepagents 主图没有"任务级断点续跑"的原生能力 —— 它的 `interrupt_on`
（HumanInTheLoopMiddleware）是"工具调用前审批"，不是"长任务暂停续跑"。
要在主图里 fork 中断节点会破坏 deepagents 的维护边界。

而编排层的 plan 节点是**天然的决策点**：规划出"要查哪几个子问题"后，
暂停让用户确认 —— 这正好避免了"跑完 125 秒才发现方向错了"的浪费。
这是生产环境最真实的中断场景（人类审批规划），且语义正确、代价可控。

State 定义用 TypedDict 而不是 dict 子类：
LangGraph 的节点函数必须返回"要更新的字段"（部分字典），
若用 dict 子类且节点原地改 state 再整体返回，ainvoke 会返回 None
（这是 LangGraph 的经典坑，本文件第一次实现时踩到，故显式注明）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command

from src.observe.logging_setup import get_logger

logger = get_logger(__name__)

# 规划拆解时用的模型档位：plan 是一次轻量 LLM 调用，用快模型即可
# （拆解子任务不需要最强模型，反而要快）。
_PLAN_MODEL_TIER = "direct"


class OrchestrateState(TypedDict, total=False):
    """编排状态。字段：
    - query: 原始提问
    - complexity: complexity.Complexity 的 to_dict()
    - plan: 拆解出的子任务清单（list[str]），simple 为空
    - needs_planning: bool
    - require_confirmation: bool，复杂任务规划后是否要人工确认（interrupt）
    - confirmed: bool，人工确认结果（resume 时注入）
    """
    query: str
    complexity: Dict[str, Any]
    plan: List[str]
    needs_planning: bool
    require_confirmation: bool
    confirmed: bool


def _judge(state: OrchestrateState) -> Dict[str, Any]:
    """复杂度判断（纯规则，同步，零成本）。复用 P-1 的 complexity.py。

    注意：返回**部分更新**（LangGraph 约定），不是整个 state。
    """
    from src.routing.complexity import judge
    q = state.get("query", "")
    c = judge(q)
    return {
        "complexity": c.to_dict(),
        "needs_planning": c.needs_planning,
    }


async def _plan(state: OrchestrateState) -> Dict[str, Any]:
    """规划拆解（LLM）：把复杂任务拆成 2~5 个可并行/可独立执行的子任务。

    只对 complex 任务调用。拆解失败（模型不可用/返回异常）时**降级为空清单**，
    交给主 Agent 按原问题自由发挥 —— 规划是"增强"不是"必需"。
    """
    query = state.get("query", "")
    try:
        from src.agent.core import build_model
        model = build_model(_PLAN_MODEL_TIER)
        prompt = (
            "把下面这个复杂任务拆解成 2~5 个**可独立执行**的子任务。\n"
            "规则：\n"
            "1. 每个子任务一句话说清「做什么」，不要展开细节；\n"
            "2. 子任务之间尽量相互独立（可并行），避免强依赖；\n"
            "3. 只输出子任务清单，每行一个，以「- 」开头，不要任何其他文字。\n\n"
            f"任务：{query}"
        )
        resp = await model.ainvoke([("human", prompt)])
        text = (getattr(resp, "content", "") or "").strip()
        items = [ln.lstrip("-• ").strip() for ln in text.splitlines()
                 if ln.strip().startswith(("-", "•"))]
        items = [it for it in items if it][:5]
        logger.info(f"[orchestrator] 规划拆解完成：{len(items)} 个子任务")
        return {"plan": items}
    except Exception as e:      # noqa: BLE001 规划失败降级为空清单
        logger.warning(f"[orchestrator] 规划失败（降级为不拆解）: {type(e).__name__}: {e}")
        return {"plan": []}


def _confirm(state: OrchestrateState) -> Dict[str, Any]:
    """人工确认中断点（P-3）。只在 require_confirmation=True 时进入。

    用 LangGraph 的 interrupt() 暂停，把规划清单抛给客户端等确认。
    确认结果通过 Command(resume=...) 回传（confirmed=True/False + 修改后的 plan）。
    这是"规划→人工审批→执行"的 HITL 语义，也是任务级断点续跑的落点。
    """
    plan = state.get("plan") or []
    decision = interrupt({
        "type": "plan_confirmation",
        "plan": plan,
        "query": state.get("query", ""),
        "hint": "请确认该执行计划，或提供修改意见。回复 confirmed 确认，或给出新的子任务清单。",
    })
    # decision 可能是 str（确认/拒绝）或 dict（带修改）
    if isinstance(decision, dict):
        confirmed = bool(decision.get("confirmed", decision.get("ok", False)))
        new_plan = decision.get("plan") or plan
        return {"confirmed": confirmed, "plan": new_plan if confirmed else plan}
    confirmed = str(decision).strip().lower() in ("confirmed", "ok", "yes", "确认", "同意", "是")
    return {"confirmed": confirmed}


def _route_after_judge(state: OrchestrateState) -> str:
    """judge 后的路由：simple 直答，complex 进规划。"""
    return "plan" if state.get("needs_planning") else "done"


def _route_after_plan(state: OrchestrateState) -> str:
    """plan 后的路由：要人工确认且确有规划 → confirm，否则 done。"""
    if state.get("require_confirmation") and (state.get("plan")):
        return "confirm"
    return "done"


def _build_graph(checkpointer: Any = None):
    """装配编排图（judge → 条件路由 → plan → 条件路由 → confirm → END）。

    checkpointer 可选：不传则无中断能力（orchestrate 的默认路径）；
    传了才支持 confirm 节点的 interrupt（P-3 resume 路径）。
    """
    g = StateGraph(OrchestrateState)
    g.add_node("judge", _judge)
    g.add_node("plan", _plan)
    g.add_node("confirm", _confirm)
    g.add_edge(START, "judge")
    g.add_conditional_edges(
        "judge",
        _route_after_judge,
        {"plan": "plan", "done": END},
    )
    g.add_conditional_edges(
        "plan",
        _route_after_plan,
        {"confirm": "confirm", "done": END},
    )
    g.add_edge("confirm", END)
    if checkpointer is not None:
        return g.compile(checkpointer=checkpointer)
    return g.compile()


def render_plan(plan: List[str]) -> str:
    """把子任务清单格式化成注入主 Agent 的规划上下文。空清单返回空串。"""
    if not plan:
        return ""
    lines = ["## 任务规划（已拆解，请按此清单并发派发给对应子智能体）"]
    for i, item in enumerate(plan, 1):
        lines.append(f"{i}. {item}")
    lines.append("上述子任务相互独立，可在一个轮次里用多个 task 调用并发派发。")
    return "\n".join(lines) + "\n"


async def orchestrate(query: str, require_confirmation: bool = False) -> Dict[str, Any]:
    """编排入口：判断复杂度 + （复杂时）规划拆解。

    参数 require_confirmation：复杂任务规划后是否要人工确认（interrupt）。
    默认 False —— 不中断，规划完直接返回（P-2 行为，向后兼容）。

    返回 {"needs_planning": bool, "complexity": dict, "plan": list[str], "plan_text": str}
    """
    state: OrchestrateState = {
        "query": query,
        "complexity": {},
        "plan": [],
        "needs_planning": False,
        "require_confirmation": require_confirmation,
        "confirmed": True,
    }
    graph = _build_graph(checkpointer=None)
    try:
        out = await graph.ainvoke(state)
        state.update(out or {})
    except Exception as e:      # noqa: BLE001 编排失败降级为不规划
        logger.warning(f"[orchestrator] 编排执行失败（降级直答）: {type(e).__name__}: {e}")
        state["needs_planning"] = False
        state["plan"] = []
    plan = state.get("plan") or []
    return {
        "needs_planning": bool(state.get("needs_planning")),
        "complexity": state.get("complexity", {}),
        "plan": plan,
        "plan_text": render_plan(plan),
    }


async def orchestrate_with_interrupt(query: str, thread_id: str) -> Dict[str, Any]:
    """P-3：带人工确认中断的编排。复杂任务规划后 interrupt，返回断点信息。

    返回：
      - 简单任务：{"interrupted": False, "plan": [], "plan_text": "", ...}
      - 复杂任务：{"interrupted": True, "checkpoint_id": str, "interrupt_payload": {...},
                   "plan": [...], "needs_planning": True}

    中断时，graph 的返回值里带 __interrupt__ 字段（新版 LangGraph 不抛异常，
    而是把 Interrupt 对象放进去）。checkpoint_id 从 checkpointer 读取，
    供 resume_plan 续跑。
    """
    from src.session import checkpointer as cp

    saver, _ = await cp.get_checkpointer()
    graph = _build_graph(checkpointer=saver)

    state: OrchestrateState = {
        "query": query,
        "complexity": {},
        "plan": [],
        "needs_planning": False,
        "require_confirmation": True,
        "confirmed": True,
    }
    config = {"configurable": {"thread_id": thread_id}}

    try:
        out = await graph.ainvoke(state, config=config)
    except Exception as e:      # noqa: BLE001 中断路径失败降级为普通编排
        logger.warning(f"[orchestrator] 中断编排失败（降级普通编排）: {type(e).__name__}: {e}")
        return await orchestrate(query)

    state.update(out or {})
    interrupts = out.get("__interrupt__", []) or []

    if not interrupts:
        # 没有中断（简单任务，或 plan 为空没进 confirm）
        plan = state.get("plan") or []
        return {
            "interrupted": False,
            "needs_planning": bool(state.get("needs_planning")),
            "complexity": state.get("complexity", {}),
            "plan": plan,
            "plan_text": render_plan(plan),
            "checkpoint_id": None,
            "interrupt_payload": None,
        }

    # 中断了：读 checkpoint_id + 中断载荷
    it = interrupts[0]
    val = it.value if hasattr(it, "value") else it
    payload = val if isinstance(val, dict) else {"type": str(val)}
    checkpoint_id = ""
    try:
        snapshot = await saver.aget_tuple(config)
        checkpoint_id = snapshot.config["configurable"].get("checkpoint_id", "")
    except Exception:           # noqa: BLE001 读不到 checkpoint 也要返回中断载荷
        pass

    plan = state.get("plan") or []
    return {
        "interrupted": True,
        "needs_planning": True,
        "complexity": state.get("complexity", {}),
        "plan": plan,
        "plan_text": render_plan(plan),
        "checkpoint_id": checkpoint_id,
        "interrupt_payload": payload,
    }


async def resume_plan(thread_id: str, checkpoint_id: str,
                      confirmation: Dict[str, Any]) -> Dict[str, Any]:
    """P-3：从中断断点续跑，拿回确认后的规划。

    参数 confirmation：{"confirmed": bool, "plan": [...]}（plan 可选，用户修改后的清单）。

    返回与 orchestrate 同构：{"needs_planning", "complexity", "plan", "plan_text"}
    """
    from langgraph.types import Command as _Cmd
    from src.session import checkpointer as cp

    saver, _ = await cp.get_checkpointer()
    graph = _build_graph(checkpointer=saver)

    config = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_id": checkpoint_id,
        },
    }
    resume_value = {
        "confirmed": bool(confirmation.get("confirmed", True)),
        "plan": confirmation.get("plan") or [],
    }
    out = await graph.ainvoke(_Cmd(resume=resume_value), config=config)

    plan = out.get("plan") or []
    return {
        "needs_planning": True,
        "complexity": out.get("complexity", {}),
        "plan": plan,
        "plan_text": render_plan(plan),
    }
