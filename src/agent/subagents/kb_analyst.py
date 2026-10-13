# -*- coding: utf-8 -*-
"""kb-analyst：知识库子智能体（P6 第一个接入的子智能体）。

它为什么值得做成子智能体而不是给主智能体三个工具
------------------------------------------------
1. **上下文隔离**：子智能体只拿到一句任务描述（subagents.py:803），
   知识库检索的中间过程（改写了几轮、召回了几条）不会污染主智能体的上下文。
2. **能力完整**：kb_search 内部跑的是完整 run_agent（10 节点，含路由/证据评估/
   必要时改写重查），主智能体拿到的是**一份有出处的结论**，不是裸召回片段。
3. **可裁剪**：主智能体不再握着 kb_* 三个工具，派发权收到 task 一处。

工具为什么按前缀 "kb_" 选而不是按 server 名
-------------------------------------------
server 名（rag）是部署概念，前缀（kb_）是能力概念。以后把 kb 工具挪到别的
server、或者一个 server 里混装多种能力时，按前缀选不会失效。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .contracts import ENVELOPE_INSTRUCTION

from src.observe.logging_setup import get_logger

logger = get_logger(__name__)

NAME = "kb-analyst"
TOOL_PREFIX = "kb_"

DESCRIPTION = (
    "知识库检索与分析。用于回答**内部文档/产品手册/设备配置**类问题："
    "它会在知识库里检索并把证据整理成带出处的结论。"
    "只在问题确实属于知识库范围时派给它；闲聊、写代码、查业务订单不要派。"
)

SYSTEM_PROMPT = (
    "你是知识库检索专家，只做一件事：在知识库里把问题查清楚，给出带出处的结论。\n\n"
    "工具选择（按顺序）：\n"
    "1. agentic_rag_query：**首选**。走完整 Agentic RAG 图（分析→路由→多路召回→RRF→"
    "证据评估→必要时改写重查→生成），并发能力更好，返回 [n] 编号引用。\n"
    "2. kb_search：**只有 agentic_rag_query 不可用（返回 error/unavailable）时才用它**。"
    "它同样是完整流程，但走的是串行 MCP 通道（慢，20~40s）。\n"
    "3. kb_lookup：只要**原文证据**自己归纳（快，1~3s），不出结论。\n"
    "4. kb_stats：确认知识库接上没有、挂的是哪份索引（排障用）。\n\n"
    "硬性规则：\n"
    "1. 每个关键结论都要有 evidence，source 写清来自哪个文档；\n"
    "2. 知识库里没有就如实说没有，degraded 填 no_evidence，**绝对禁止用常识或推测补内容**；\n"
    "3. answer_level=refuse 表示证据不足已拒答，直接转成 no_evidence 上报，不要改写；\n"
    "4. 返回的 answer_level / confidence 必须**原样照抄**工具返回值，禁止自己判断或美化；\n"
    "5. 引用只能原样使用工具返回的 source，**禁止编造文档名、章节号、source ID**。\n"
    + ENVELOPE_INSTRUCTION
)


async def build() -> Optional[Dict[str, Any]]:
    """装配 kb-analyst 规格。两条通道都没有就返回 None（不注册这个子智能体）。

    为什么不注册一个"空工具"的子智能体：主智能体看到它就会派，派了必然失败，
    不如让它根本不存在 —— 这跟 executor.kb_wired() 的降级思路一致。

    P7：工具表是"HTTP 服务通道 + MCP stdio 通道"的并集。
    两条通道跑的是**同一份 Agentic RAG 源码与权重**，互为容灾：
    HTTP 挂了 kb_search 还能顶上，MCP 挂了 HTTP 还能顶上。
    """
    from src.mcp_client.client import langchain_tools

    tools: List[Any] = []
    try:
        tools = list(await langchain_tools(prefix=TOOL_PREFIX) or [])
    except Exception as e:
        logger.warning(f"kb MCP 工具加载失败（仅走 HTTP 通道）: {type(e).__name__}: {e}")

    # HTTP 通道优先：放列表最前，提示词里也写成首选
    try:
        from src.capabilities import agentic_rag
        if agentic_rag.available():
            tools.insert(0, agentic_rag.agentic_rag_query)
    except Exception as e:
        logger.warning(f"Agentic RAG 通道不可用: {type(e).__name__}: {e}")

    if not tools:
        return None

    return {
        "name": NAME,
        "description": DESCRIPTION,
        "system_prompt": SYSTEM_PROMPT,
        "tools": tools,
    }
