# -*- coding: utf-8 -*-
"""编排前置层离线自测（P-2 下半场）。

不依赖真实 LLM，只测确定性部分：
  1. 图结构：judge/plan 节点存在，编译成功
  2. judge 路由：simple→done，complex→plan
  3. render_plan：格式化正确，空清单返回空串
  4. orchestrate 降级：plan 抛异常时不崩溃（monkeypatch _plan）

跑法：python tests/test_orchestrator.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


print("[1] 图结构")
nodes = set(orchestrator._build_graph().get_graph().nodes.keys())
check("含 judge 节点", "judge" in nodes)
check("含 plan 节点", "plan" in nodes)
check("含 confirm 节点", "confirm" in nodes)
check("含 START", "__start__" in nodes)
check("含 END", "__end__" in nodes)

print("[2] judge 路由（不调 LLM）")
simple = orchestrator.OrchestrateState(query="你好", complexity={}, plan=[], needs_planning=False)
simple = {**simple, **orchestrator._judge(simple)}
check("简单任务 needs_planning=False", simple["needs_planning"] is False)
check("简单任务路由到 done", orchestrator._route_after_judge(simple) == "done")

complex_q = orchestrator.OrchestrateState(query="对比分析两个方案并出报告", complexity={}, plan=[], needs_planning=False)
complex_q = {**complex_q, **orchestrator._judge(complex_q)}
check("复杂任务 needs_planning=True", complex_q["needs_planning"] is True)
check("复杂任务路由到 plan", orchestrator._route_after_judge(complex_q) == "plan")

print("[3] render_plan")
check("空清单返回空串", orchestrator.render_plan([]) == "")
out = orchestrator.render_plan(["子任务A", "子任务B"])
check("含清单头", "任务规划" in out)
check("含子任务", "子任务A" in out and "子任务B" in out)
check("含并发引导", "并发派发" in out)

print("[4] orchestrate 降级（plan 抛异常不崩溃）")


async def _test_degrade():
    # 注意：不能 monkeypatch 模块级 _plan —— 图在 _build_graph() 调用时编译，
    # 节点函数引用是编译期绑定的。所以降级测试改拦截 _build_graph，
    # 让 orchestrate 拿到一个 ainvoke 必抛的图。
    orig_build = orchestrator._build_graph

    class _BoomGraph:
        async def ainvoke(self, state):
            raise RuntimeError("模拟图执行失败")

    orchestrator._build_graph = lambda checkpointer=None: _BoomGraph()
    try:
        r = await orchestrator.orchestrate("对比分析两个方案")
        # 图失败：orchestrate 降级为 needs_planning=False + plan=[]，不崩溃
        check("图失败仍返回", isinstance(r, dict))
        check("图失败降级为不规划", r["needs_planning"] is False)
        check("图失败 plan 为空", r["plan"] == [])
        check("图失败 plan_text 为空", r["plan_text"] == "")
    finally:
        orchestrator._build_graph = orig_build


asyncio.run(_test_degrade())

print()
print(f"通过 {_passed} / {_passed + _failed}")
sys.exit(1 if _failed else 0)
