"""
P6 实测脚本：工具簇下放后的「工具选择准确率回升」

跑法（项目根，本地 .venv）：
    .venv/Scripts/python.exe scripts/measure_delegation.py

为什么要有这个脚本：
    P5 的 measure_mcp.py 证明了「工具 3→16，选择准确率 100%→90%」——工具越多模型越傻。
    P6 的治理手段是把 kb_* 簇（kb_search/kb_lookup/kb_stats）从主智能体剥离、下放给
    kb-analyst 子智能体，主智能体只剩 task 派发权。
    但「下放之后回升到多少」一直没有数字 —— 本脚本补上这个洞。

设计哲学（与 measure_mcp.py 一致）：只变「工具集结构」，评测集 / 模型 / 温度全部锁死。
    flat_19       下放前（扁平）：16 business + 3 kb_*        —— 主智能体自己扛 19 个工具
    delegated_17  下放后（委派）：16 business + 1 task 派发工具 —— kb 剥离，知识库走派发

为什么 kb_* 用 mock 而不拉真实 rag server：
    本实验测的是「模型选哪个工具」这个**行为**，select_tool 只看 bind_tools().ainvoke()
    返回的 tool_calls，**从不真实执行工具**。所以 kb_* 只需要 schema 在场即可，
    按其真实定义（mcp_rag_server.py）1:1 mock，模型看到的描述与线上一致。
    这样既不依赖 rag server 拉起（它要加载 BGE-M3，慢且偶发 30s 超时），又保证可复现。

为什么两组的 system prompt 不同：
    扁平架构和委派架构本来就需要不同的引导文案（真实 core.py 就是这么写的：
    下放后明确提示「kb_* 不在你手里，必须用 task 派发给 kb-analyst」）。
    给两组各配「架构匹配的合理提示」，测的是「在该架构正常引导下的选型准确率」，
    这才是公平的 —— 现实中你不会给委派架构配一套扁平架构的提示。
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

OUT = Path(".data/mcp_delegation.json")

# ---------------------------------------------------------------------------
# kb_* 与 task 的 mock（schema 与 mcp_rag_server.py / deepagents 一致，只测选择不执行）
# ---------------------------------------------------------------------------
KB_SEARCH_DESC = ("知识库深度问答：走完整 Agentic RAG 流程（查询分析→多路召回→RRF融合→"
                  "证据评估→必要时改写重查→生成带引用的答案）。用在用户问的是知识库里的内容、"
                  "且需要一份有出处可直接引用的结论时。较慢。")
KB_LOOKUP_DESC = ("知识库原文检索（不生成答案）：BGE-M3 稠密+稀疏双路召回→RRF 融合，"
                  "返回 top-N 原文片段及来源与得分。用在只要证据/原文、或 kb_search 太慢时。")
KB_STATS_DESC = ("知识库索引元信息：向量后端（local/milvus）、文档条数、embedding 维度、"
                 "是否启用 reranker。排障确认知识库是否挂上时先调它。")
TASK_DESC = ("把子任务派发给专门子智能体执行。知识库问答（产品手册、设备配置、内部制度、"
             "政策流程类）必须派发给 subagent_type='kb-analyst'。")


def _mock_kb_tools() -> List[Any]:
    async def kb_search(query: str, top_k: int = 5) -> dict:
        return {}

    async def kb_lookup(query: str, top_k: int = 5) -> dict:
        return {}

    async def kb_stats() -> dict:
        return {}

    return [
        StructuredTool.from_function(coroutine=kb_search, name="kb_search", description=KB_SEARCH_DESC),
        StructuredTool.from_function(coroutine=kb_lookup, name="kb_lookup", description=KB_LOOKUP_DESC),
        StructuredTool.from_function(coroutine=kb_stats, name="kb_stats", description=KB_STATS_DESC),
    ]


def _mock_task_tool() -> Any:
    async def task(subagent_type: str, description: str) -> dict:
        return {}

    return StructuredTool.from_function(coroutine=task, name="task", description=TASK_DESC)


# ---------------------------------------------------------------------------
# 评测集：26 题。type: biz=业务题(答唯一) / kb=知识库题(架构相关) / none=不该调工具
# ---------------------------------------------------------------------------
EVAL: List[Dict[str, Any]] = [
    # --- 业务题 16：覆盖 12 个不同 business 工具，制造真实的选型压力 ---
    {"type": "biz", "expect": "query_order",   "q": "订单 SO-900123 现在是什么状态？金额多少？"},
    {"type": "biz", "expect": "query_order",   "q": "SO-900188 发货了没有，走的哪个仓？"},
    {"type": "biz", "expect": "query_stock",   "q": "SKU-10077 现在库存够不够发 200 件？"},
    {"type": "biz", "expect": "query_stock",   "q": "SKU-10245 哪个仓有货？"},
    {"type": "biz", "expect": "query_customer","q": "客户 C-3001 的联系方式和客户等级是多少？"},
    {"type": "biz", "expect": "query_customer","q": "C-3010 这个客户最近有没有下过单？"},
    {"type": "biz", "expect": "query_invoice", "q": "SO-900123 这张单开过发票了吗？发票金额多少？"},
    {"type": "biz", "expect": "query_invoice", "q": "帮我查一下 INV-2024-0088 这张发票的状态"},
    {"type": "biz", "expect": "query_shipment","q": "SO-900066 的物流到哪了？运单号多少？"},
    {"type": "biz", "expect": "query_refund",  "q": "SO-900007 的退款现在退到哪一步了？"},
    {"type": "biz", "expect": "query_return",  "q": "客户要退 SO-900150 里的货，退货单现在什么状态？"},
    {"type": "biz", "expect": "query_warehouse","q": "苏州仓当前的总库存是多少件？"},
    {"type": "biz", "expect": "query_supplier","q": "供应商 S-501 的联系方式和结算方式？"},
    {"type": "biz", "expect": "query_price_history", "q": "SKU-10000 这半年的采购价格走势怎么样？"},
    {"type": "biz", "expect": "low_stock",     "q": "现在有哪些 SKU 已经低于安全库存了？"},
    {"type": "biz", "expect": "create_ticket", "q": "苏州仓发货一直延迟，帮我提个工单让客服跟进"},
    # --- 知识库题 6：扁平组应选 kb_search/kb_lookup，委派组应选 task ---
    {"type": "kb", "q": "咱们设备标配的电源功率是多少瓦？产品手册里写的"},
    {"type": "kb", "q": "退货政策是几天内无理由？帮我查一下售后制度文件"},
    {"type": "kb", "q": "XX-200 型号的标准安装步骤是什么？看产品手册"},
    {"type": "kb", "q": "公司差旅报销的住宿标准是多少？查内部财务制度"},
    {"type": "kb", "q": "这款产品整机质保期是多久？说明书里的条款"},
    {"type": "kb", "q": "新员工入职要走哪些流程步骤？人事制度里写的"},
    # --- 无工具题 4：不该调任何工具 ---
    {"type": "none", "expect": None, "q": "你好"},
    {"type": "none", "expect": None, "q": "用 Python 写个快速排序"},
    {"type": "none", "expect": None, "q": "解释一下什么是 RAG"},
    {"type": "none", "expect": None, "q": "今天天气怎么样"},
]

# 知识库题的「正确动作集合」按架构区分
KB_OK_FLAT = {"kb_search", "kb_lookup"}      # 扁平组：自己拿 kb_*，选对检索类即算对
KB_OK_DELEGATED = {"task"}                    # 委派组：正确动作是把知识库问题派发给 kb-analyst

SYS_FLAT = (
    "你是企业业务智能助手，根据用户问题选择最合适的一个工具：\n"
    "- 业务数据（订单/库存/客户/发票/物流/退款/退货/仓库/供应商/价格/低库存/工单）：调用对应业务工具。\n"
    "- 企业知识库内容（产品手册、设备配置、内部制度、政策流程）：调用 kb_search 或 kb_lookup。\n"
    "- 与业务无关的闲聊或通用知识问题：不要调用任何工具，直接回答。"
)
SYS_DELEGATED = (
    "你是企业业务智能助手，根据用户问题选择最合适的一个工具：\n"
    "- 业务数据（订单/库存/客户/发票/物流/退款/退货/仓库/供应商/价格/低库存/工单）：调用对应业务工具。\n"
    "- 企业知识库内容（产品手册、设备配置、内部制度、政策流程）：这些工具不在你手里，"
    "必须用 task 工具派发给 kb-analyst 子智能体处理。\n"
    "- 与业务无关的闲聊或通用知识问题：不要调用任何工具，直接回答。"
)


async def select_tool(model, tools: List[Any], system: str, query: str) -> Optional[str]:
    """返回模型实际选择的第一个工具名；没调工具返回 None。"""
    try:
        resp = await model.bind_tools(tools).ainvoke(
            [SystemMessage(content=system), HumanMessage(content=query)])
    except Exception:
        return "__error__"
    calls = getattr(resp, "tool_calls", None) or []
    return calls[0].get("name") if calls else None


def judge(item: Dict[str, Any], got: Optional[str], mode: str) -> str:
    """返回 ok / wrong_tool / no_call / over_call。mode ∈ {flat, delegated}。"""
    t = item["type"]
    if t == "none":
        return "ok" if got is None else "over_call"
    if t == "biz":
        if got == item["expect"]:
            return "ok"
        return "no_call" if got is None else "wrong_tool"
    # kb 题
    ok_set = KB_OK_FLAT if mode == "flat" else KB_OK_DELEGATED
    if got in ok_set:
        return "ok"
    return "no_call" if got is None else "wrong_tool"


async def measure() -> Dict[str, Any]:
    model = build_model("auto")
    # 关键：server="business" 锁定 business server 的 16 个工具。
    # 不能用 langchain_tools()（全部）—— rag server 拉起与否不稳定（偶发 30s 超时），
    # 一旦拉起会把真实的 kb_* 也带进来，导致 delegated 组里混进 kb_*、没有真正「剥离」，
    # 对照就被污染了。这里显式只要 business 16 个，kb_* 统一用下面的 mock，干净可控。
    business = await mcp.langchain_tools(server="business")
    if not business:
        return {"error": "business MCP 工具为空，无法评测"}
    kb_tools = _mock_kb_tools()
    task_tool = _mock_task_tool()

    configs = [
        ("flat_19",      "flat",      business + kb_tools,  SYS_FLAT),
        ("delegated_17", "delegated", business + [task_tool], SYS_DELEGATED),
    ]

    out: Dict[str, Any] = {"n": len(EVAL)}
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
            detail.append({"q": item["q"], "type": item["type"],
                           "expect": item.get("expect") or ("kb" if item["type"] == "kb" else None),
                           "got": got, "verdict": verdict})
        n = len(EVAL)
        out[label] = {"tool_count": len(tools), "n": n,
                      "accuracy": round(hit / n, 4),
                      "wrong_tool": wrong, "no_call": missed, "over_call": over,
                      "detail": detail}
    return out


async def main() -> int:
    t0 = time.time()
    print("=== 明辨平台 P6 实测 | 工具簇下放后准确率回升 ===\n")
    print("只变「工具集结构」，评测集/模型/温度锁死；kb_* 按真实 schema mock（select 不执行工具）\n")

    res = await measure()
    if "error" in res:
        print(f"  ❌ {res['error']}")
        return 1

    a = res["flat_19"]
    b = res["delegated_17"]
    print(f"  {'配置':<13}{'工具数':>5}{'准确率':>9}{'选错':>6}{'该调没调':>9}{'不该调却调':>10}")
    for label, v in (("flat_19(下放前)", a), ("delegated_17(下放后)", b)):
        print(f"  {label:<13}{v['tool_count']:>5}{v['accuracy']:>9.1%}"
              f"{v['wrong_tool']:>6}{v['no_call']:>9}{v['over_call']:>10}")

    delta = round(b["accuracy"] - a["accuracy"], 4)
    print(f"\n  → 下放后准确率 {a['accuracy']:.1%} → {b['accuracy']:.1%}（回升 {delta:+.1%}）")

    for label, v in (("flat_19", a), ("delegated_17", b)):
        bad = [x for x in v["detail"] if x["verdict"] != "ok"]
        if bad:
            print(f"\n  {label} 的错例（{len(bad)} 题）：")
            for x in bad[:10]:
                print(f"    [{x['type']:>4}] {x['verdict']:>10} 期望={x['expect']} 实际={x['got']} | {x['q'][:24]}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"selection": res, "elapsed_sec": round(time.time() - t0, 1)},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {OUT}（耗时 {time.time() - t0:.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
