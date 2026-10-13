"""
P5 实测脚本：MCP 业务系统集成

跑法（容器内）：
    docker exec mj-api python scripts/measure_mcp.py

产出三组数字，全部来自真实调用，没有任何一个是"估的"：
    A. 会话开销：persistent（常驻线程复用子进程） vs per_call（每次现起子进程）
    B. 工具选择准确率：同一批问题，只把可用工具从 3 个换成 16 个
    C. 写操作二次确认：四种路径（无 token / 合法 token / 改参数 / 伪造 token）

B 为什么这样设计：
    "工具变多模型就变傻"这句话谁都会说，但必须能量化才有说服力。
    关键是**只变工具集大小**，评测集、模型、温度、提示词全部不动 ——
    否则你分不清掉点是工具变多导致的，还是题目变难导致的。
    所以 20 道题全部是**核心 3 个工具就能答对**的题，
    16 工具那一组只是多挂了 13 个干扰项。
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.core import build_model          # noqa: E402
from src.config import settings                 # noqa: E402
from src.mcp_client import client as mcp        # noqa: E402

OUT = Path(".data/mcp_metrics.json")

# ---------------------------------------------------------------------------
# 评测集：20 条，全部可由核心 3 个工具答对（+ 4 条不该调工具）
# ---------------------------------------------------------------------------
EVAL: List[Dict[str, Any]] = [
    {"q": "订单 SO-900123 现在是什么状态？金额多少？", "expect": "query_order"},
    {"q": "帮我查一下 SO-900045 这个订单的明细", "expect": "query_order"},
    {"q": "SO-900188 发货了没有，走的哪个仓？", "expect": "query_order"},
    {"q": "客户下错了单，SO-900007 这单还能取消吗，先告诉我它当前状态", "expect": "query_order"},
    {"q": "SO-900066 什么时候下的单？", "expect": "query_order"},
    {"q": "SO-900150 这单买了哪些东西？", "expect": "query_order"},
    {"q": "SO-900231 的收货仓是哪个？", "expect": "query_order"},
    {"q": "查一下 SO-900099 这个订单", "expect": "query_order"},
    {"q": "SKU-10000 现在还有多少库存？", "expect": "query_stock"},
    {"q": "SKU-10077 够不够发 200 件？", "expect": "query_stock"},
    {"q": "SKU-10245 哪个仓有货？", "expect": "query_stock"},
    {"q": "查一下 SKU-10105 的库存数量", "expect": "query_stock"},
    {"q": "SKU-10315 是不是低于安全库存了？", "expect": "query_stock"},
    {"q": "SKU-10021 全仓合计多少件？", "expect": "query_stock"},
    {"q": "苏州仓发货一直延迟，帮我提个工单跟进一下", "expect": "create_ticket"},
    {"q": "我要投诉，给客服建个 P1 工单，内容是订单迟迟不发货", "expect": "create_ticket"},
    {"q": "你好", "expect": None},
    {"q": "用 Python 写个快速排序", "expect": None},
    {"q": "解释一下什么是 RAG", "expect": None},
    {"q": "TCP 三次握手是怎么回事", "expect": None},
]

CORE = ["query_order", "query_stock", "create_ticket"]


def pct(vals: List[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))
    return s[k]


# ---------------------------------------------------------------------------
# A. 会话开销
# ---------------------------------------------------------------------------
async def measure_session(n: int = 20) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for mode in ("persistent", "per_call"):
        settings.mcp_session = mode
        mcp.close_all()
        times: List[float] = []
        for i in range(n):
            sku = f"SKU-{10000 + (i % 60) * 7}"
            _, ms = await mcp.raw_call("query_stock", {"sku": sku})
            times.append(ms)
        out[mode] = {"n": n, "avg_ms": round(statistics.mean(times), 1),
                     "p50_ms": round(pct(times, 50), 1),
                     "p95_ms": round(pct(times, 95), 1),
                     "min_ms": round(min(times), 1), "max_ms": round(max(times), 1)}
    settings.mcp_session = "persistent"
    mcp.close_all()
    return out


# ---------------------------------------------------------------------------
# B. 工具选择准确率
# ---------------------------------------------------------------------------
async def select_tool(model, tools: List[Any], query: str) -> Optional[str]:
    """返回模型实际选择的工具名；没调工具返回 None。"""
    from langchain_core.messages import HumanMessage
    try:
        resp = await model.bind_tools(tools).ainvoke([HumanMessage(content=query)])
    except Exception:
        return "__error__"
    calls = getattr(resp, "tool_calls", None) or []
    return calls[0].get("name") if calls else None


async def measure_selection() -> Dict[str, Any]:
    model = build_model("auto")
    all_tools = await mcp.langchain_tools()
    if not all_tools:
        return {"error": "MCP 工具为空，无法评测"}
    core_tools = [t for t in all_tools if t.name in CORE]

    out: Dict[str, Any] = {}
    for label, tools in (("tools_3", core_tools), ("tools_16", all_tools)):
        hit = 0
        wrong = 0
        missed = 0
        over = 0
        detail: List[Dict[str, Any]] = []
        for item in EVAL:
            got = await select_tool(model, tools, item["q"])
            exp = item["expect"]
            if exp is None:
                if got is None:
                    hit += 1
                    verdict = "ok"
                else:
                    over += 1
                    verdict = "over_call"
            else:
                if got == exp:
                    hit += 1
                    verdict = "ok"
                elif got is None:
                    missed += 1
                    verdict = "no_call"
                else:
                    wrong += 1
                    verdict = "wrong_tool"
            detail.append({"q": item["q"], "expect": exp, "got": got, "verdict": verdict})
        n = len(EVAL)
        out[label] = {
            "tool_count": len(tools), "n": n,
            "accuracy": round(hit / n, 4),
            "wrong_tool": wrong, "no_call": missed, "over_call": over,
            "detail": detail,
        }
    return out


# ---------------------------------------------------------------------------
# C. 写操作二次确认
# ---------------------------------------------------------------------------
async def measure_confirm() -> Dict[str, Any]:
    mcp.close_all()
    r1, ms1 = await mcp.raw_call("create_ticket",
                                 {"subject": "实测工单", "body": "measure_mcp"})
    tok = (r1 or {}).get("confirm_token") or ""
    r2, ms2 = await mcp.raw_call("create_ticket",
                                 {"subject": "实测工单", "body": "measure_mcp",
                                  "confirm_token": tok})
    r3, _ = await mcp.raw_call("create_ticket",
                               {"subject": "被改过的工单", "body": "measure_mcp",
                                "confirm_token": tok})
    r4, _ = await mcp.raw_call("create_ticket",
                               {"subject": "实测工单", "body": "measure_mcp",
                                "confirm_token": "cf_forged_1"})
    return {
        "step1_pending": bool((r1 or {}).get("need_confirm")) and not (r1 or {}).get("created"),
        "step2_created": bool((r2 or {}).get("created")),
        "ticket_id": (r2 or {}).get("ticket_id"),
        "tamper_rejected": (r3 or {}).get("created") is not True,
        "forged_rejected": (r4 or {}).get("created") is not True,
        "ms": [round(ms1, 1), round(ms2, 1)],
    }


async def main() -> int:
    t0 = time.time()
    print("=== 明辨平台 P5 实测 | MCP 业务系统集成 ===\n")

    print("-- A. 会话开销（20 次 query_stock）--")
    sess = await measure_session()
    for k, v in sess.items():
        print(f"  {k:11s} avg={v['avg_ms']:7.1f}ms  p50={v['p50_ms']:6.1f}ms  "
              f"p95={v['p95_ms']:6.1f}ms  max={v['max_ms']:7.1f}ms")
    if sess.get("per_call", {}).get("avg_ms") and sess.get("persistent", {}).get("avg_ms"):
        ratio = sess["per_call"]["avg_ms"] / max(sess["persistent"]["avg_ms"], 0.001)
        print(f"  → per_call / persistent = {ratio:.1f}×")

    print("\n-- B. 工具选择准确率（同一批 20 题，只变工具集大小）--")
    sel = await measure_selection()
    if "error" in sel:
        print(f"  ❌ {sel['error']}")
    else:
        for k in ("tools_3", "tools_16"):
            v = sel[k]
            print(f"  {k:9s} 工具数={v['tool_count']:2d}  准确率={v['accuracy']:.1%}  "
                  f"选错={v['wrong_tool']}  该调没调={v['no_call']}  不该调却调={v['over_call']}")
        d = round(sel["tools_3"]["accuracy"] - sel["tools_16"]["accuracy"], 4)
        print(f"  → 准确率下降 {d:.1%}（{sel['tools_3']['accuracy']:.1%} → "
              f"{sel['tools_16']['accuracy']:.1%}）")
        for k in ("tools_16",):
            bad = [x for x in sel[k]["detail"] if x["verdict"] != "ok"]
            if bad:
                print(f"  → {k} 的错例：")
                for x in bad[:8]:
                    print(f"      {x['verdict']:10s} 期望={x['expect']} 实际={x['got']} | {x['q'][:28]}")

    print("\n-- C. 写操作二次确认 --")
    conf = await measure_confirm()
    for k in ("step1_pending", "step2_created", "tamper_rejected", "forged_rejected"):
        print(f"  {k:16s} {conf[k]}")
    print(f"  ticket_id         {conf.get('ticket_id')}  (ms={conf.get('ms')})")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"session": sess, "selection": sel, "confirm": conf,
                               "elapsed_sec": round(time.time() - t0, 1)},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {OUT}（耗时 {time.time() - t0:.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
