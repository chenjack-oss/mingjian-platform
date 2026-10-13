# -*- coding: utf-8 -*-
"""任务级 interrupt/resume 离线自测（P-3）。

不依赖真实 LLM，用 monkeypatch 控制 plan 节点，专注测中断机制：
  1. 挂 checkpointer 后，复杂任务在 confirm 节点中断（__interrupt__ 出现）
  2. checkpoint_id 可读，resume 后中断清除、plan 更新为确认结果
  3. orchestrate_with_interrupt / resume_plan 全链路

跑法：python tests/test_interrupt.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.checkpoint.memory import InMemorySaver

from src.agent import orchestrator

_passed = 0
_failed = 0


def check(name: str, cond: bool):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [ok] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}")


async def _test_interrupt_mechanism():
    saver = InMemorySaver()
    graph = orchestrator._build_graph(checkpointer=saver)
    config = {"configurable": {"thread_id": "it-1"}}

    state = orchestrator.OrchestrateState(
        query="对比分析A和B", complexity={}, plan=[],
        needs_planning=True, require_confirmation=True, confirmed=True)

    # 1. 第一次跑：plan 会真调 LLM 拆解（与生产一致），然后 confirm 中断
    #    注意：不能 monkeypatch _plan —— 图在 _build_graph() 时已绑定节点函数，
    #    编译期绑定，改模块属性不影响已编译图（这是 LangGraph 的固定行为）。
    out = await graph.ainvoke(state, config=config)

    interrupts = out.get("__interrupt__", []) or []
    check("复杂任务在 confirm 中断", len(interrupts) == 1)
    if interrupts:
        it = interrupts[0]
        val = it.value if hasattr(it, "value") else it
        check("中断类型为 plan_confirmation", isinstance(val, dict) and val.get("type") == "plan_confirmation")
        check("中断载荷含 plan（非空）", isinstance(val, dict) and len(val.get("plan", [])) >= 2)

    # 2. checkpoint 可读
    snapshot = await saver.aget_tuple(config)
    cid = snapshot.config["configurable"].get("checkpoint_id", "")
    check("checkpoint_id 非空", bool(cid))

    # 3. resume 续跑
    from langgraph.types import Command
    out2 = await graph.ainvoke(Command(resume={"confirmed": True, "plan": ["改后的任务"]}), config=config)
    check("resume 后中断清除", len(out2.get("__interrupt__", []) or []) == 0)
    check("resume 后 confirmed=True", out2.get("confirmed") is True)
    check("resume 后 plan 为用户确认值", out2.get("plan") == ["改后的任务"])


async def _test_resume_plan_api():
    # 测 orchestrator.resume_plan 的编排层封装（用 InMemorySaver 隔离，但
    # resume_plan 内部用 get_checkpointer，需 monkeypatch 返回我们的 saver）
    orig_get = None
    try:
        from src.session import checkpointer as cp
        orig_get = cp.get_checkpointer
        saver = InMemorySaver()

        async def _fake_get():
            return saver, "memory(test)"

        cp.get_checkpointer = _fake_get

        # 先跑一次产生中断
        r = await orchestrator.orchestrate_with_interrupt("对比分析A和B方案", "it-2")
        if not r.get("interrupted"):
            check("复杂任务应中断", False)
            return
        check("orchestrate_with_interrupt 返回中断", r["interrupted"] is True)
        check("返回 checkpoint_id", bool(r.get("checkpoint_id")))

        # resume
        r2 = await orchestrator.resume_plan("it-2", r["checkpoint_id"],
                                            {"confirmed": True, "plan": ["A", "B"]})
        check("resume_plan 返回确认后 plan", r2["plan"] == ["A", "B"])
    finally:
        if orig_get is not None:
            from src.session import checkpointer as cp
            cp.get_checkpointer = orig_get


async def main():
    print("[1] interrupt 中断机制")
    await _test_interrupt_mechanism()
    print("[2] orchestrate_with_interrupt / resume_plan")
    await _test_resume_plan_api()


asyncio.run(main())

print()
print(f"通过 {_passed} / {_passed + _failed}")
sys.exit(1 if _failed else 0)
