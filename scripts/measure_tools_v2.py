# -*- coding: utf-8 -*-
"""L1 工具+参数评估 v2：100 题评测集（eval_set_agent.jsonl），替代 measure_mcp 的 24 题手编版。

与 v1 的区别：
- 题库：100 题程序化生成（gen_eval_set.py），16 工具全覆盖 + 多工具串联 + 拒答 + 多轮；
- 执行：经 eval_harness 真实跑工具循环（不是单发 bind_tools 猜第一个工具），
  多工具题必须**全部串到**才算对——单发只能验证"第一步选对"，验证不了规划完整度；
- 指标：工具选择准确率 / 误调率 / 漏调率 / 多工具集齐率 / 关键参数精确率，分档报告；
- write_confirm 档不在本脚本（属行为评估，见 e2e_eval.py / robustness_eval.py）。

口径红线（与 measure_mcp 相同）：只变一个变量——模型、温度、提示词全冻结，
题库固定，任何两轮报告 diff 才可归因。

用法：
    python scripts/measure_tools_v2.py [--tiers single_tool,multi_tool,no_tool,multi_turn]
产出：
    .data/eval_l1_<时间戳>.json（含 config 指纹与逐题明细）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.eval_harness import called_tool_names, param_hit, run_question  # noqa: E402
from src.agent.core import build_model  # noqa: E402
from src.config import settings  # noqa: E402
from src.mcp_client import client as mcp  # noqa: E402

EVAL_SET = Path(__file__).resolve().parent / "eval_set_agent.jsonl"
OUT_DIR = Path(".data")


def _verdict(expect: List[str], got: List[str], accept: List[List[str]] = None) -> str:
    """单题判定：exact / over(多调) / missed(漏调) / wrong(选错) / no_call。

    accept（信息等价工具组）：got 与任一组集合相等即 exact——
    query_order 内联 shipment/invoice、supplier_detail 内联 contracts，
    模型走富返回捷径不算漏调（2026-10-13 首轮 10 题误判的归因修正）。
    """
    es, gs = set(expect), set(got)
    groups = [set(g) for g in accept] if accept else [es]
    if any(gs == g for g in groups):
        return "exact"
    if not gs:
        return "no_call"
    universe = set().union(*groups) if groups else set()
    if not universe:
        return "over_call"          # no_tool 题调了工具
    if gs - universe:
        return "wrong_tool"         # 调了所有等价组之外的工具
    if any(gs > g for g in groups):
        return "over_call"          # 某等价组的真超集
    return "missed"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="single_tool,multi_tool,no_tool,multi_turn")
    args = ap.parse_args()
    tiers = set(args.tiers.split(","))

    rows = [json.loads(l) for l in open(EVAL_SET, encoding="utf-8") if l.strip()]
    rows = [r for r in rows if r["tier"] in tiers]
    print(f"L1 评估 {len(rows)} 题（tiers={sorted(tiers)}）\n")

    model = build_model("auto")
    tools = await mcp.langchain_tools()
    if not tools:
        print("❌ MCP 工具为空"); return 1
    print(f"工具数 {len(tools)}｜模型 {settings.llm_model}\n")

    per_q: List[Dict[str, Any]] = []
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        # multi_turn 的 expect_tools 是按轮嵌套的 [[t1],[t2]]，拍平做集合比对
        raw_expect = r["expect_tools"]
        expect = [t for grp in raw_expect if isinstance(grp, list) for t in grp] \
            if raw_expect and isinstance(raw_expect[0], list) else list(raw_expect)

        trace = await run_question(model, tools, question=r.get("question", ""),
                                   turns=r.get("turns"))
        got = called_tool_names(trace)
        verdict = _verdict(expect, got, accept=r.get("accept"))
        phit = param_hit(trace, r.get("expect_args") or {})
        per_q.append({
            "id": r["id"], "tier": r["tier"], "expect": expect, "got": got,
            "verdict": verdict, "param_hit": phit,
            "steps": trace["steps"], "truncated": trace["truncated"],
            "elapsed_s": trace["elapsed_s"],
        })
        flag = {"exact": "✅", "over_call": "🟡", "missed": "🟡"}.get(verdict, "🔴")
        print(f"[{i:3d}/{len(rows)}] {flag} {r['id']} {r['tier']:<12} {verdict:<10} "
              f"expect={expect} got={got} ({trace['elapsed_s']}s)")

    # ---- 汇总 ----
    by_tier: Dict[str, Counter] = defaultdict(Counter)
    for q in per_q:
        by_tier[q["tier"]][q["verdict"]] += 1

    n = len(per_q)
    exact = sum(1 for q in per_q if q["verdict"] == "exact")
    param_tot = sum(len(q["param_hit"]) for q in per_q)
    param_ok = sum(sum(q["param_hit"].values()) for q in per_q)
    summary = {
        "n": n,
        "tool_accuracy": round(exact / n, 4),
        "over_call_rate": round(sum(q["verdict"] == "over_call" for q in per_q) / n, 4),
        "missed_rate": round(sum(q["verdict"] in ("missed", "no_call") for q in per_q) / n, 4),
        "wrong_tool_rate": round(sum(q["verdict"] == "wrong_tool" for q in per_q) / n, 4),
        "param_precision": round(param_ok / param_tot, 4) if param_tot else None,
        "truncated": sum(1 for q in per_q if q["truncated"]),
        "by_tier": {t: dict(c) for t, c in by_tier.items()},
    }

    print("\n=== L1 汇总 ===")
    print(f"工具选择准确率(exact) {summary['tool_accuracy']:.1%}｜"
          f"误调率 {summary['over_call_rate']:.1%}｜漏调率 {summary['missed_rate']:.1%}｜"
          f"选错率 {summary['wrong_tool_rate']:.1%}")
    print(f"关键参数精确率 {summary['param_precision']}（{param_ok}/{param_tot}）")
    for t, c in sorted(by_tier.items()):
        tot = sum(c.values())
        print(f"  {t:<12} exact {c.get('exact',0)}/{tot}  明细 {dict(c)}")

    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "summary": summary,
        "per_q": per_q,
        "params": {"model": settings.llm_model, "tool_count": len(tools),
                   "mcp_session": settings.mcp_session, "eval_set": EVAL_SET.name},
    }
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"eval_l1_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
