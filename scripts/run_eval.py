# -*- coding: utf-8 -*-
"""统一评测编排器：一键串起明辨（Agent）× Agentic RAG（RAG）两项目 L0~L3。

为什么需要它（而不是各层脚本自己跑）
--------------------------------------
两个项目现在各有一堆散装脚本（明辨 measure_tools_v2 / e2e_eval / robustness_eval，
RAG retrieval_eval / ragas_eval / regression），但没有统一入口。每次评测要：
  1. 人工记住脚本在哪、传什么参数、顺序是什么；
  2. 人工翻 reports/ 找最新产出的 JSON；
  3. 人工跑 regression 门禁、人工把数字汇总成报告。

漏一步就出「数字说不清是哪次跑出来的」的事故（2026-10-05 图通道报告 params 块
完全一致、只剩目录名可区分就是教训）。本编排器把这些固化成一条命令。

设计约束（沿用《评估体系技术方案 v1.0》的铁律）
----------------------------------------------
- 只编排、不改各层脚本的评测逻辑 —— 各层脚本仍是单一事实来源；
- 每层串行跑（LLM 层烧钱且共享同一模型端点，并发会互相拖慢 + 污染延迟指标）；
- 每层跑完自动定位最新 JSON 报告，收集进一次统一 run 记录；
- 自动对 RAG 侧跑 regression 门禁（baseline_locked diff）；
- 汇总报告写 md + docx 双格式，带配置快照。

用法
----
    # 只看能跑哪些层
    python scripts/run_eval.py --list

    # 跑全部（L0~L3，最烧钱，重大改动时跑）
    python scripts/run_eval.py --all

    # 只跑免 LLM 的层（L0 纯向量 + L3 故障注入，秒级~分钟级，每次 commit 可跑）
    python scripts/run_eval.py --l0 --l3

    # 只跑明辨（Agent）侧
    python scripts/run_eval.py --agent

    # 只跑 RAG 侧
    python scripts/run_eval.py --rag
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------- 路径定位（两项目的位置）----------
_MINGJIAN_SCRIPTS = Path(__file__).resolve().parent                      # 明辨 scripts/
_MINGJIAN_ROOT = _MINGJIAN_SCRIPTS.parent                                 # 明辨项目根
_RAG_ROOT = Path("D:/Agentic RAG")                                       # Agentic RAG 项目根
_RAG_EVAL = _RAG_ROOT / "evaluation"

# 🔴 两项目 venv 不同，必须分开：
#   - RAG 侧依赖 FlagEmbedding（BGE-M3）等，只有 RAG 自己的 venv 有；
#   - 明辨侧用当前解释器（run_eval.py 所在项目的 venv）。
_RAG_PYTHON = _RAG_ROOT / ".venv" / "Scripts" / "python.exe"
_AGENT_PYTHON = sys.executable

if not _RAG_PYTHON.exists():
    # 容错：RAG venv 不存在时回退到当前解释器（会在跑 RAG 层时报缺依赖，比静默用错 venv 好）
    _RAG_PYTHON = sys.executable

# ---------- 各层定义 ----------
# 每层：(脚本路径, 参数列表, 层名, 项目, 是否烧 LLM)
# 参数里的评测集路径用绝对路径，避免 cwd 依赖。
_LAYERS: List[Dict[str, Any]] = [
    # ---- RAG 侧 ----
    {
        "key": "rag_l0", "project": "rag", "name": "Agentic RAG · L0 检索层（纯向量，免LLM）",
        "script": _RAG_EVAL / "retrieval_eval.py",
        "args": ["--eval-set", str(_RAG_EVAL / "eval_set_v3_golden_final.jsonl")],
        "llm": False,
    },
    {
        "key": "rag_l1", "project": "rag", "name": "Agentic RAG · L1 四指标（LLM judge）",
        "script": _RAG_EVAL / "ragas_eval.py",
        "args": [],  # ragas_eval.py 是模块型，无 __main__，需特殊调用（见 _run_rag_l1）
        "llm": True,
    },
    # ---- 明辨侧 ----
    {
        "key": "agent_l1", "project": "agent", "name": "明辨 · L1 工具+参数（真实模型+MCP）",
        "script": _MINGJIAN_SCRIPTS / "measure_tools_v2.py",
        "args": ["--tiers", "single_tool,multi_tool,no_tool,multi_turn"],
        "llm": True,
    },
    {
        "key": "agent_l2", "project": "agent", "name": "明辨 · L2 端到端任务成功率（LLM judge）",
        "script": _MINGJIAN_SCRIPTS / "e2e_eval.py",
        "args": ["--tiers", "single_tool,multi_tool,multi_turn,no_tool,write_confirm"],
        "llm": True,
    },
    {
        "key": "agent_l3", "project": "agent", "name": "明辨 · L3 鲁棒性（故障注入断言）",
        "script": _MINGJIAN_SCRIPTS / "robustness_eval.py",
        "args": [],
        "llm": False,
    },
]

_LAYER_BY_KEY = {l["key"]: l for l in _LAYERS}


def _run_rag_l1() -> Tuple[int, str]:
    """ragas_eval.py 没有 __main__ 入口，是模块。这里用 -c 驱动它。

    ragas_eval.py 的公开入口是 `run_eval(...)`（返回 dict）和 `_save(...)`。
    用一段最小驱动代码调用它跑 v3 主集，产出 JSON 到 reports/。
    """
    code = (
        "import sys, json, asyncio\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, r'" + str(_RAG_EVAL) + "')\n"
        "from ragas_eval import run_eval\n"
        "async def go():\n"
        "    r = await run_eval()\n"
        "    print('L1 DONE', json.dumps({k: v for k, v in r.items() if not isinstance(v, dict)}, ensure_ascii=False))\n"
        "asyncio.run(go())\n"
    )
    return subprocess.run(
        [_RAG_PYTHON, "-c", code], cwd=str(_RAG_ROOT),
        text=True, capture_output=True
    ).returncode, "ragas_eval"


def _run_layer(layer: Dict[str, Any]) -> Tuple[int, str, float]:
    """跑一层，返回 (退出码, 最新报告路径, 耗时秒)。"""
    key = layer["key"]
    t0 = time.time()
    # RAG 层用 RAG venv，明辨层用明辨 venv（两项目依赖不同）
    python = _RAG_PYTHON if layer["project"] == "rag" else _AGENT_PYTHON

    if key == "rag_l1":
        code, _ = _run_rag_l1()
    else:
        cmd = [python, str(layer["script"])] + layer["args"]
        r = subprocess.run(cmd, cwd=str(layer["script"].parent), text=True)
        code = r.returncode

    elapsed = time.time() - t0
    report = _latest_report(key)
    return code, report, elapsed


def _latest_report(key: str) -> str:
    """找该层最新产出的 JSON 报告（按修改时间倒序取最新）。"""
    pats: Dict[str, Path] = {
        "rag_l0": _RAG_EVAL / "reports" / "retrieval_l0_*.json",
        "rag_l1": _RAG_EVAL / "reports" / "eval_agentic_*.json",
        "agent_l1": _MINGJIAN_ROOT / "reports" / "eval_tools_*.json",
        "agent_l2": _MINGJIAN_ROOT / "reports" / "eval_l2_*.json",
        "agent_l3": _MINGJIAN_ROOT / "reports" / "eval_l3_*.json",
    }
    pat = pats.get(key)
    if not pat:
        return ""
    cands = sorted([p for p in pat.parent.glob(pat.name) if p.is_file()],
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return str(cands[0]) if cands else ""


def _run_regression_gate() -> int:
    """RAG 侧门禁：最新 L0 报告 vs baseline_locked 基线。"""
    base = sorted((_RAG_EVAL / "reports" / "baseline_locked").glob("retrieval_l0_*.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not base:
        print("🟡 无 baseline_locked 基线，跳过户门禁")
        return 0
    cur = _latest_report("rag_l0")
    if not cur:
        print("🟡 无最新 L0 报告，跳过门禁")
        return 0
    cmd = [_RAG_PYTHON, str(_RAG_EVAL / "regression.py"),
           "--baseline", str(base[0]), "--current", cur]
    r = subprocess.run(cmd, cwd=str(_RAG_ROOT), text=True)
    return r.returncode


def _collect_config_snapshot() -> Dict[str, Any]:
    """配置快照：记录本次 run 的环境与关键开关（铁律 3）。"""
    snap: Dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        # 记录两项目 venv 指向，防"用错解释器跑错依赖"的隐性事故
        "rag_python": str(_RAG_PYTHON),
        "agent_python": str(_AGENT_PYTHON),
    }
    # 尽量读明辨 settings 的关键开关（不 import 整个 app，避免副作用）
    try:
        sys.path.insert(0, str(_MINGJIAN_ROOT))
        from src.config import settings
        snap["agent"] = {
            "llm_base_url": settings.llm_base_url,
            "llm_model": getattr(settings, "llm_model", ""),
            "mcp_serialize": getattr(settings, "mcp_serialize", ""),
            "quota_messages_per_day": getattr(settings, "quota_messages_per_day", ""),
            "quota_searches_per_day": getattr(settings, "quota_searches_per_day", ""),
        }
    except Exception as e:
        snap["agent"] = {"error": f"{type(e).__name__}: {e}"}
    return snap


def _write_summary(results: List[Dict[str, Any]], out_dir: Path) -> Path:
    """写汇总 md（各层状态 + 报告路径 + 配置快照）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    md_path = out_dir / f"eval_summary_{ts}.md"
    snap = _collect_config_snapshot()

    lines = ["# 评测汇总（统一编排）", "", f"> 生成：{snap['ts']}｜编排器 run_eval.py", ""]
    lines.append("## 各层结果")
    lines.append("")
    lines.append("| 层 | 状态 | 耗时 | 报告 |")
    lines.append("|---|---|---|---|")
    for r in results:
        status = "✅ 通过" if r["code"] == 0 else "🔴 失败/告警"
        lines.append(f"| {r['name']} | {status} | {r['elapsed']:.1f}s | {r['report'] or '—'} |")
    lines.append("")
    lines.append("## 配置快照")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(snap, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## 门禁")
    lines.append("")
    lines.append("RAG 侧 regression 门禁结果见上方 `rag_l0` 状态（红=指标跌落超阈值，需人工介入）。")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    # 同步生成 docx（复用 scripts/md_to_docx.py，够用就好）
    try:
        docx_tool = _MINGJIAN_SCRIPTS / "md_to_docx.py"
        docx_path = md_path.with_suffix(".docx")
        subprocess.run([_AGENT_PYTHON, str(docx_tool), str(md_path), str(docx_path)],
                       text=True, capture_output=True)
    except Exception:
        docx_path = None  # docx 生成失败不影响主流程

    return md_path


def main() -> int:
    ap = argparse.ArgumentParser(description="明辨 × Agentic RAG 统一评测编排器")
    ap.add_argument("--list", action="store_true", help="列出所有可跑的层")
    ap.add_argument("--all", action="store_true", help="跑全部层（L0~L3，最烧钱）")
    ap.add_argument("--agent", action="store_true", help="只跑明辨侧（L1/L2/L3）")
    ap.add_argument("--rag", action="store_true", help="只跑 RAG 侧（L0/L1）")
    ap.add_argument("--l0", action="store_true")
    ap.add_argument("--l1", action="store_true")
    ap.add_argument("--l2", action="store_true")
    ap.add_argument("--l3", action="store_true")
    ap.add_argument("--gate", action="store_true", help="只跑门禁（不重跑评测）")
    args = ap.parse_args()

    if args.list:
        for l in _LAYERS:
            llm = "烧LLM" if l["llm"] else "免LLM"
            print(f"  [{l['key']:<9}] {l['name']}（{llm}）")
        return 0

    # 选层
    selected: List[Dict[str, Any]] = []
    if args.all:
        selected = list(_LAYERS)
    elif args.agent:
        selected = [l for l in _LAYERS if l["project"] == "agent"]
    elif args.rag:
        selected = [l for l in _LAYERS if l["project"] == "rag"]
    else:
        for flag, key in [("l0", "rag_l0"), ("l1", "rag_l1"),
                          ("l2", "agent_l2"), ("l3", "agent_l3")]:
            if getattr(args, flag):
                selected.append(_LAYER_BY_KEY[key])
        # agent_l1 单独：--l1 同时跑 RAG L1 和明辨 L1
        if args.l1:
            selected.append(_LAYER_BY_KEY["agent_l1"])

    # 去重保序
    seen = set()
    selected = [l for l in selected if not (l["key"] in seen or seen.add(l["key"]))]

    if args.gate:
        return _run_regression_gate()

    if not selected:
        print("没选任何层。用 --all / --agent / --rag / --l0..--l3，或 --list 查看。")
        return 1

    # 确认烧钱的层
    llm_layers = [l for l in selected if l["llm"]]
    if llm_layers:
        names = "、".join(l["key"] for l in llm_layers)
        print(f"⚠️ 以下层会调用真实 LLM（烧钱）：{names}")
        print("   免 LLM 层：--l0 --l3；全部：--all。开始跑……")

    results: List[Dict[str, Any]] = []
    for layer in selected:
        print(f"\n{'=' * 60}\n▶ {layer['name']}  [{layer['key']}]")
        code, report, elapsed = _run_layer(layer)
        print(f"  退出码 {code}｜耗时 {elapsed:.1f}s｜报告 {report or '—'}")
        results.append({"key": layer["key"], "name": layer["name"],
                        "code": code, "elapsed": elapsed, "report": report})

    # 门禁（跑 RAG L0 后自动 diff）
    if any(r["key"] == "rag_l0" for r in results):
        print(f"\n{'=' * 60}\n▶ RAG regression 门禁")
        gate_code = _run_regression_gate()
        print(f"  门禁退出码 {gate_code}")

    # 汇总
    out_dir = _MINGJIAN_ROOT / "reports"
    summary = _write_summary(results, out_dir)
    print(f"\n✅ 汇总报告：{summary}")

    # 总体退出码：任一层失败（非0）→ 1
    return 1 if any(r["code"] != 0 for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
