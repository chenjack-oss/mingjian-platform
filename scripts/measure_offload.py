"""
P6 补充实测：「同质工具下放」对工具选择准确率的回升

跑法（项目根，本地 .venv）：
    .venv/Scripts/python.exe scripts/measure_offload.py

为什么补这个脚本（与 measure_delegation.py 是一对）：
    measure_delegation.py 测的是「kb_* 异质工具下放」——结果是 84.6%→84.6%（≈0），
    因为知识库和业务是异质工具，模型本就不混，下放它是权限/架构决策，不是准确率优化。

    那「下放」到底什么时候能提准确率？答案是**同质工具**——measure_mcp.py 早就证明
    「核心 3 工具 100% → 16 个同质 query_* 工具 90%」，掉点全部来自**相似业务工具之间的混淆**。
    所以把一部分**同质**业务工具下放给子智能体、让主智能体选型空间收缩，准确率理应回升。
    本脚本直接验证这条曲线：before_16（16 个同质业务全挂） vs after_11（留 10 核心 + task 派发 6 个售后类）。

设计哲学不变：只变「工具集结构」，评测集 / 模型 / 温度锁死。
    before_16  主智能体挂全部 16 个业务工具（售后类也在手里）
    after_11   主智能体只留 10 个核心业务工具 + 1 个 task 派发工具；
               售后/退款/评价/历史价/合同/员工 6 个下放给 after-sale-analyst
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langchain_core.tools import StructuredTool                  # noqa: E402

from src.agent.core import build_model          # noqa: E402
from src.mcp_client import client as mcp        # noqa: E402

OUT = Path(".data/mcp_offload.json")

# 下放给 after-sale-analyst 的 6 个「同质且易混」的售后/边缘业务工具
OFFLOAD = {"query_return", "query_refund", "query_review",
           "query_price_history", "query_contract", "query_employee"}

TASK_DESC = ("把子任务派发给专门子智能体。售后退货、退款、用户评价、历史价格、合同、员工类查询，"
             "必须派发给 subagent_type='after-sale-analyst'。")

SYS_BEFORE = (
    "你是企业业务智能助手，根据用户问题选择最合适的一个工具：\n"
    "- 业务数据（订单/库存/客户/发票/物流/优惠券/仓库/供应商/低库存/工单/退货/退款/评价/历史价/合同/员工）：调用对应业务工具。\n"
    "- 与业务无关的闲聊或通用问题：不要调用任何工具，直接回答。"
)
SYS_AFTER = (
    "你是企业业务智能助手，根据用户问题选择最合适的一个工具：\n"
    "- 核心业务（订单/库存/客户/发票/物流/优惠券/仓库/供应商/低库存/工单）：调用对应业务工具。\n"
    "- 售后退货、退款、用户评价、历史价格、合同、员工类查询：这些工具不在你手里，"
    "必须用 task 工具派发给 after-sale-analyst 子智能体。\n"
    "- 与业务无关的闲聊或通用问题：不要调用任何工具，直接回答。"
)

# 评测集 20 题：keep=保留的核心工具 / offload=被下放工具 / none=不该调工具
EVAL: List[Dict[str, Any]] = [
    {"type": "keep", "expect": "query_order",   "q": "订单 SO-900123 现在是什么状态？"},
    {"type": "keep", "expect": "query_stock",   "q": "SKU-10077 现在还有多少库存？"},
    {"type": "keep", "expect": "query_customer","q": "客户 C-3001 的等级和联系方式是多少？"},
    {"type": "keep", "expect": "query_invoice", "q": "SO-900123 这张单开过发票了吗？"},
    {"type": "keep", "expect": "query_shipment","q": "SO-900066 的物流到哪了？运单号多少？"},
    {"type": "keep", "expect": "query_coupon",  "q": "客户 C-3001 现在有哪些可用的优惠券？"},
    {"type": "keep", "expect": "query_warehouse","q": "苏州仓当前的库存总量是多少？"},
    {"type": "keep", "expect": "query_supplier","q": "供应商 S-501 的结算方式和账期？"},
    {"type": "keep", "expect": "low_stock",     "q": "现在有哪些 SKU 低于安全库存了？"},
    {"type": "keep", "expect": "create_ticket", "q": "帮我提个工单，让客服跟进发货延迟的问题"},
    # 被下放的 6 个：before 期望调对应工具，after 期望调 task
    {"type": "offload", "expect": "query_return",  "q": "SO-900150 的退货单现在处理到哪一步了？"},
    {"type": "offload", "expect": "query_refund",  "q": "SO-900007 的退款现在退到哪里了？"},
    {"type": "offload", "expect": "query_review",  "q": "SKU-10000 最近的用户评价怎么样？"},
    {"type": "offload", "expect": "query_price_history", "q": "SKU-10000 这半年的采购价格走势如何？"},
    {"type": "offload", "expect": "query_contract","q": "我们跟客户 C-3001 签的合同什么时候到期？"},
    {"type": "offload", "expect": "query_employee","q": "负责苏州仓的员工是谁？有联系方式吗？"},
    {"type": "none", "expect": None, "q": "你好"},
    {"type": "none", "expect": None, "q": "用 Python 写个快速排序"},
    {"type": "none", "expect": None, "q": "解释一下什么是 Transformer"},
    {"type": "none", "expect": None, "q": "今天天气怎么样"},
]


def _mock_task_tool() -> Any:
    async def task(subagent_type: str, description: str) -> dict:
        return {}

    return StructuredTool.from_function(coroutine=task, name="task", description=TASK_DESC)


async def select_tool(model, tools: List[Any], system: str, query: str) -> Optional[str]:
    try:
        resp = await model.bind_tools(tools).ainvoke(
            [SystemMessage(content=system), HumanMessage(content=query)])
    except Exception:
        return "__error__"
    calls = getattr(resp, "tool_calls", None) or []
    return calls[0].get("name") if calls else None


def judge(item: Dict[str, Any], got: Optional[str], mode: str) -> str:
    t = item["type"]
    if t == "none":
        return "ok" if got is None else "over_call"
    if t == "keep":
        if got == item["expect"]:
            return "ok"
        return "no_call" if got is None else "wrong_tool"
    # offload 题：before 期望调原工具，after 期望调 task
    want = item["expect"] if mode == "before" else "task"
    if got == want:
        return "ok"
    return "no_call" if got is None else "wrong_tool"


async def measure() -> Dict[str, Any]:
    model = build_model("auto")
    all_business = await mcp.langchain_tools(server="business")
    if not all_business:
        return {"error": "business MCP 工具为空，无法评测"}

    keep_tools = [t for t in all_business if t.name not in OFFLOAD]
    task_tool = _mock_task_tool()

    configs = [
        ("before_16", "before", all_business,          SYS_BEFORE),
        ("after_11",  "after",  keep_tools + [task_tool], SYS_AFTER),
    ]

    out: Dict[str, Any] = {"n": len(EVAL), "offloaded": sorted(OFFLOAD)}
    for label, mode, tools, sysp in configs:
        hit = wrong = missed = over = 0
        detail: List[Dict[str, Any]] = []
        for item in EVAL:
            got = await select_tool(model, tools, sysp, item["q"])
            verdict = judge(item, got, mode)
            if verdict == "ok":
                hit += 1
            elif verdict == "wrong_tool":
                wrong += 1
            elif verdict == "no_call":
                missed += 1
            else:
                over += 1
            detail.append({"q": item["q"], "type": item["type"], "expect": item.get("expect"),
                           "got": got, "verdict": verdict})
        n = len(EVAL)
        out[label] = {"tool_count": len(tools), "n": n, "accuracy": round(hit / n, 4),
                      "wrong_tool": wrong, "no_call": missed, "over_call": over, "detail": detail}
    return out


async def main() -> int:
    t0 = time.time()
    print("=== 明辨平台 P6 补充实测 | 同质工具下放的准确率回升 ===\n")
    print("只变「工具集结构」，评测集/模型/温度锁死；下放的是 6 个同质易混的售后/边缘业务工具\n")

    res = await measure()
    if "error" in res:
        print(f"  ❌ {res['error']}")
        return 1

    a, b = res["before_16"], res["after_11"]
    print(f"  {'配置':<16}{'工具数':>5}{'准确率':>9}{'选错':>6}{'该调没调':>9}{'不该调却调':>10}")
    for label, v in (("before_16(下放前)", a), ("after_11(下放后)", b)):
        print(f"  {label:<16}{v['tool_count']:>5}{v['accuracy']:>9.1%}"
              f"{v['wrong_tool']:>6}{v['no_call']:>9}{v['over_call']:>10}")

    delta = round(b["accuracy"] - a["accuracy"], 4)
    print(f"\n  → 下放后准确率 {a['accuracy']:.1%} → {b['accuracy']:.1%}（回升 {delta:+.1%}）")

    for label, v in (("before_16", a), ("after_11", b)):
        bad = [x for x in v["detail"] if x["verdict"] != "ok"]
        if bad:
            print(f"\n  {label} 的错例（{len(bad)} 题）：")
            for x in bad[:10]:
                print(f"    [{x['type']:>7}] {x['verdict']:>10} 期望={x['expect']} 实际={x['got']} | {x['q'][:22]}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"selection": res, "elapsed_sec": round(time.time() - t0, 1)},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {OUT}（耗时 {time.time() - t0:.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
