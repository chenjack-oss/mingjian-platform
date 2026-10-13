# -*- coding: utf-8 -*-
"""评估共享 harness：用真实模型 + 真实 MCP 工具跑一道题，记录全部工具调用。

为什么单独抽出来：
    L1（工具+参数）、L2（端到端答案质量）需要的是**同一条执行轨迹**——
    L1 看轨迹里的调用序列，L2 看轨迹末端的最终回答。抽成公共 harness 保证
    两层评估面对的是**同一次运行**，不会出现"L1 说工具选对了、L2 说答案错了"
    却无法对齐到同一条轨迹的尴尬。

设计约束：
- 真实执行工具（本地 SQLite 只读查询无副作用；create_ticket 第一步不落库）；
- max_steps 上限防模型死循环，超限记 truncated=True；
- 多轮题（turns）在同一 messages 序列上续跑，验证指代消解依赖的是真实上下文；
- 每次调用记录 (name, args, result)，args 原文保留供参数精确率比对。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage, ToolMessage  # noqa: E402


async def run_question(model, tools_lc: List[Any], question: str = "",
                       turns: Optional[List[str]] = None,
                       max_steps: int = 4) -> Dict[str, Any]:
    """跑一道题（单轮或多轮），返回执行轨迹。

    :return: {calls: [{turn, step, name, args, result_preview}],
              final_text, steps, truncated, elapsed_s, per_turn_text: [...]}
    """
    tool_map = {t.name: t for t in tools_lc}
    msgs: List[Any] = []
    calls: List[Dict[str, Any]] = []
    steps = 0
    truncated = False
    per_turn_text: List[str] = []
    t0 = time.time()

    user_inputs = turns if turns else [question]
    for ti, user_q in enumerate(user_inputs):
        msgs.append(HumanMessage(content=user_q))
        final_text = ""
        while steps < max_steps * len(user_inputs):   # 每轮最多 max_steps 次工具循环
            resp = await model.bind_tools(tools_lc).ainvoke(msgs)
            msgs.append(resp)
            tcs = getattr(resp, "tool_calls", None) or []
            if not tcs:
                final_text = str(resp.content or "")
                break
            steps += 1
            for tc in tcs:
                name, args = tc.get("name"), tc.get("args") or {}
                tool = tool_map.get(name)
                if tool is None:
                    result = {"error": f"unknown tool {name}"}
                else:
                    try:
                        result = await tool.ainvoke(args)
                    except Exception as exc:  # noqa: BLE001  工具异常也是轨迹的一部分
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False, default=str)
                calls.append({"turn": ti, "step": steps, "name": name,
                              "args": args, "result_preview": result[:2000]})
                msgs.append(ToolMessage(content=result, tool_call_id=tc.get("id", "")))
        else:
            truncated = True
        per_turn_text.append(final_text)

    return {
        "calls": calls,
        "final_text": per_turn_text[-1] if per_turn_text else "",
        "per_turn_text": per_turn_text,
        "steps": steps,
        "truncated": truncated,
        "elapsed_s": round(time.time() - t0, 2),
    }


def called_tool_names(trace: Dict[str, Any], turn: Optional[int] = None) -> List[str]:
    """取轨迹里实际调用过的工具名（保序去重）。"""
    names: List[str] = []
    for c in trace["calls"]:
        if turn is not None and c["turn"] != turn:
            continue
        if c["name"] not in names:
            names.append(c["name"])
    return names


def param_hit(trace: Dict[str, Any], expect_args: Dict[str, Any]) -> Dict[str, bool]:
    """关键参数逐项比对：期望值是否出现在某次实际调用的 args 里（字符串归一）。

    归一：去空格、统一大小写——SO-900188 与 "so-900188" 算同一个参数值。
    """
    actual_values = set()
    for c in trace["calls"]:
        for v in (c.get("args") or {}).values():
            actual_values.add(str(v).strip().lower())
    return {k: str(v).strip().lower() in actual_values for k, v in expect_args.items()}
