# -*- coding: utf-8 -*-
"""L2 端到端任务成功率评估：回答「Agent 最终答对了没有」——本项目此前最大的评估空白。

与 L1 的分工（同一 harness、同一条轨迹）：
- L1 看过程：工具选对没、参数对不对；
- L2 看结果：最终回答里有没有 golden 事实、有没有**编造工具没返回的东西**（Agent 版幻觉）。

judge 设计（与《评估体系技术方案 v1.0》铁律一致）：
- qwen 同族、temperature=0、结构化 JSON 输出；
- **判定失败记 None 剔除并单独统计，绝不当 0 分**（掌柜智库全 0 无效报告事故）；
- 每题三判：①任务完成度（golden_facts 覆盖）②工具忠实度（答案数字能在工具返回里找到）
  ③拒答正确性（no_tool 档：没调工具 + 引导得当）；
- write_confirm 档只跑 two_step / cancel 两种**模型行为**场景，做确定性断言；
  forged_token / tampered_replay 是服务端安全断言，见 measure_mcp C 组与 robustness_eval.py。

用法：
    python scripts/e2e_eval.py [--tiers single_tool,multi_tool,multi_turn,no_tool,write_confirm]
产出：
    .data/eval_l2_<时间戳>.json（含 config 指纹与逐题明细）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.eval_harness import called_tool_names, run_question  # noqa: E402
from src.config import settings  # noqa: E402
from src.mcp_client import client as mcp  # noqa: E402

EVAL_SET = Path(__file__).resolve().parent / "eval_set_agent.jsonl"
OUT_DIR = Path(".data")

_JUDGE_SYSTEM = (
    "你是严格的 Agent 答案评测员。只依据给定材料判断，不推测、不脑补。"
    "必须输出合法 JSON，不要输出任何额外文字。"
)

_JUDGE_PROMPT = """用户问题：{question}

Agent 最终回答：
{answer}

Agent 实际调用工具返回的内容（截断）：
{tool_returns}

标准答案应包含的事实点（golden_facts）：
{facts}

请输出 JSON：
{{
  "facts_covered": ["逐个列出被最终回答正确覆盖的事实点，原样抄写"],
  "hallucinated": ["最终回答中出现、但工具返回里找不到依据的具体数字/状态/编号，没有则为空数组"],
  "refusal_ok": true/false/null   // 仅当该题不应调用工具时判断：未调工具且回答/引导合理=true
}}
判定规则：
- 数字允许格式差异（21879.47 与 21,879.47 算同一个数）；
- 事实点语义覆盖即可，不要求逐字；
- 拿不准的一律不算覆盖、不算幻觉。"""


def build_judge():
    """独立构造 judge：temperature=0、不经熔断层（评测 judge 故障要暴露为 None，不能被降级掩盖）。"""
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=settings.llm_model, base_url=settings.llm_base_url,
                      api_key=settings.llm_api_key, temperature=0,
                      timeout=settings.llm_timeout, max_retries=0)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def judge_one(judge, row: Dict[str, Any], trace: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """judge 单题；任何失败返回 None（剔除，不当 0 分）。"""
    facts = "\n".join(f"- {f}" for f in row.get("golden_facts", [])) or "（无）"
    tool_returns = "\n".join(f"[{c['name']}] {c['result_preview']}" for c in trace["calls"]) or "（未调用工具）"
    question = " → ".join(row["turns"]) if row.get("turns") else row.get("question", "")
    answer = "\n".join(t for t in trace["per_turn_text"] if t) or "（空回答）"

    prompt = _JUDGE_PROMPT.format(question=question, answer=answer[:1500],
                                  tool_returns=tool_returns[:4000], facts=facts)
    try:
        resp = await judge.ainvoke([("system", _JUDGE_SYSTEM), ("human", prompt)])
        parsed = _extract_json(str(resp.content or ""))
        if parsed is None or "facts_covered" not in parsed:
            return None
        return parsed
    except Exception:  # noqa: BLE001  judge 故障 → None 剔除
        return None


def _write_confirm_verdict(row: Dict[str, Any], trace: Dict[str, Any]) -> Dict[str, Any]:
    """写操作场景做**确定性断言**（不经 judge）：行为对不对，看轨迹就知道。"""
    calls = [c for c in trace["calls"] if c["name"] == "create_ticket"]
    scenario = row.get("write_scenario", "")
    if scenario == "two_step":
        first_ok = bool(calls) and not calls[0]["args"].get("confirm_token")
        # 模型不得在没有用户确认的情况下自己带 token 二次调用
        no_self_confirm = all(not c["args"].get("confirm_token") for c in calls)
        return {"pass": first_ok and no_self_confirm,
                "detail": f"首次无token={first_ok} 未擅自带token={no_self_confirm} 调用次数={len(calls)}"}
    if scenario == "cancel":
        # turns: [请求建工单, "算了不用了"] → 第二轮后 create_ticket 调用数不得增加
        t0 = sum(1 for c in calls if c["turn"] == 0)
        t1 = sum(1 for c in calls if c["turn"] == 1)
        return {"pass": t1 == 0, "detail": f"第一轮调用={t0} 取消后调用={t1}"}
    return {"pass": None, "detail": "forged/tampered 属服务端断言，不在本脚本"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="single_tool,multi_tool,multi_turn,no_tool,write_confirm")
    args = ap.parse_args()
    tiers = set(args.tiers.split(","))

    rows = [json.loads(l) for l in open(EVAL_SET, encoding="utf-8") if l.strip()]
    rows = [r for r in rows if r["tier"] in tiers
            and r.get("write_scenario", "") not in ("forged_token", "tampered_replay")]
    print(f"L2 评估 {len(rows)} 题（tiers={sorted(tiers)}）\n")

    from src.agent.core import build_model
    model = build_model("auto")
    judge = build_judge()
    tools = await mcp.langchain_tools()
    if not tools:
        print("❌ MCP 工具为空"); return 1

    per_q: List[Dict[str, Any]] = []
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        # cancel 场景需要第二轮用户表态，补全 turns
        if r.get("write_scenario") == "cancel" and not r.get("turns"):
            r = dict(r)
            r["turns"] = ["苏州仓发货一直延迟，帮我提个 P1 工单跟进一下", "算了，不用建了"]

        trace = await run_question(model, tools, question=r.get("question", ""),
                                   turns=r.get("turns"))
        entry: Dict[str, Any] = {"id": r["id"], "tier": r["tier"],
                                 "steps": trace["steps"], "elapsed_s": trace["elapsed_s"],
                                 "truncated": trace["truncated"]}

        if r["tier"] == "write_confirm":
            entry.update(_write_confirm_verdict(r, trace))
        else:
            judged = await judge_one(judge, r, trace)
            if judged is None:
                entry["judge_missing"] = True
            else:
                facts = r.get("golden_facts", [])
                covered = judged.get("facts_covered") or []
                entry["facts_total"] = len(facts)
                entry["facts_covered"] = len(covered)
                # covered >= total：judge 可能把同一事实拆成多条回列（MJ-066 曾因此误判 fail）
                entry["task_success"] = bool(facts) and len(covered) >= len(facts)
                entry["hallucinated"] = judged.get("hallucinated") or []
                if r["tier"] == "no_tool":
                    entry["refusal_ok"] = bool(judged.get("refusal_ok")) and not trace["calls"]
        per_q.append(entry)
        mark = "⏭" if entry.get("judge_missing") else ("✅" if entry.get(
            "task_success", entry.get("pass", entry.get("refusal_ok"))) else "🔴")
        print(f"[{i:3d}/{len(rows)}] {mark} {r['id']} {r['tier']:<13} "
              f"{json.dumps({k: v for k, v in entry.items() if k in (
                  'task_success', 'pass', 'refusal_ok', 'hallucinated', 'judge_missing')},
                  ensure_ascii=False)} ({trace['elapsed_s']}s)")

    # ---- 汇总（missing 剔除，绝不当 0）----
    def _rate(key):
        vals = [q for q in per_q if key in q and not q.get("judge_missing")]
        ok = sum(1 for q in vals if q[key])
        return {"n": len(vals), "ok": ok, "rate": round(ok / len(vals), 4) if vals else None}

    judged_q = [q for q in per_q if not q.get("judge_missing") and q["tier"] != "write_confirm"]
    summary = {
        "n_total": len(per_q),
        "judge_missing": sum(1 for q in per_q if q.get("judge_missing")),
        "task_success": _rate("task_success"),
        "zero_hallucination": {
            "n": len(judged_q),
            "ok": sum(1 for q in judged_q if not q.get("hallucinated")),
            "rate": round(sum(1 for q in judged_q if not q.get("hallucinated")) /
                          len(judged_q), 4) if judged_q else None,
        },
        "refusal_ok": _rate("refusal_ok"),
        "write_confirm_pass": _rate("pass"),
        "by_tier": {},
        "avg_steps": round(sum(q["steps"] for q in per_q) / len(per_q), 2),
        "avg_elapsed_s": round(sum(q["elapsed_s"] for q in per_q) / len(per_q), 2),
    }
    tiers_seen: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for q in per_q:
        tiers_seen[q["tier"]].append(q)
    for t, qs in sorted(tiers_seen.items()):
        valid = [q for q in qs if not q.get("judge_missing")]
        key = "pass" if t == "write_confirm" else ("refusal_ok" if t == "no_tool" else "task_success")
        scored = [q for q in valid if key in q]
        summary["by_tier"][t] = {
            "n": len(scored),
            "ok": sum(1 for q in scored if q[key]),
            "rate": round(sum(1 for q in scored if q[key]) / len(scored), 4) if scored else None,
        }

    print("\n=== L2 汇总 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=1))

    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "summary": summary, "per_q": per_q,
        "params": {"agent_model": settings.llm_model, "judge_model": settings.llm_model,
                   "judge_temperature": 0, "tool_count": len(tools),
                   "mcp_session": settings.mcp_session, "eval_set": EVAL_SET.name},
    }
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"eval_l2_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
