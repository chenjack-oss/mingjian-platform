# -*- coding: utf-8 -*-
"""doc-analyst：文档生成子智能体（P-2 建议新增的第四个子智能体）。

对应企业 AI 中台四大能力域里的「产出」这一环。

为什么"写文档"值得做成子智能体而不是主智能体的一个工具
----------------------------------------------------
写一份**带引用的结构化报告**有独立的中间过程：
  定大纲 → 分节撰写 → 逐段核对引用出处 → 成稿落盘
这个过程里"大纲怎么定的、每节写了几版、哪些引用没溯源"都是过程噪声。
主智能体只需要决策"要不要出文档"，具体怎么写、引用怎么校验，隔离在 doc-analyst。

而且 write_document 内部已经挂了 citation_guard（自动做引用溯源校验 + 追加审计章节），
这个校验逻辑对主智能体是黑盒，放进 doc-analyst 正好让"校验"和"撰写"同处一个上下文。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .contracts import ENVELOPE_INSTRUCTION

from src.observe.logging_setup import get_logger

logger = get_logger(__name__)

NAME = "doc-analyst"
TOOL_PREFIX = ""   # write_document 无前缀，按工具名剥离

DESCRIPTION = (
    "结构化文档生成。用于把调研结果写成一份**可交付的 Markdown 报告**，"
    "如调研报告、方案文档、总结纪要。它会先定大纲、分节撰写、核对引用出处、"
    "再落盘成文件。只在用户明确要「出文档/写报告」时派给它；"
    "普通问答不要派，直接回答即可。"
)

SYSTEM_PROMPT = (
    "你是文档撰写专家，只做一件事：把给定内容组织成一份结构清晰、引用可溯源的"
    "Markdown 文档并落盘。\n\n"
    "工作方法：\n"
    "1. 先定大纲：按「背景→分析→结论→建议」组织章节；\n"
    "2. 分节撰写：每节聚焦一个主题，标题层级清晰（# ## ###）；\n"
    "3. 引用核对：正文引用的每个来源都要在检索结果里能找到出处；\n"
    "4. 用 write_document 落盘（它会自动做引用溯源校验并追加审计章节）。\n\n"
    "硬性规则：\n"
    "1. 只写有依据的内容，**禁止编造数据、结论、来源**；\n"
    "2. 引用的链接必须是检索结果里真实出现的，**禁止编造 URL**；\n"
    "3. 文档标题用中文，文件名用「主题 + 报告.md」格式；\n"
    "4. 落盘后返回文件名与字数，方便主智能体告知用户。\n"
    + ENVELOPE_INSTRUCTION
)


async def build() -> Optional[Dict[str, Any]]:
    """装配 doc-analyst。写文档工具加载失败返回 None，不注册。"""
    try:
        from src.capabilities.files import write_document
        tools: List[Any] = [write_document]
    except Exception as e:
        logger.warning(f"写文档工具加载失败: {type(e).__name__}: {e}")
        return None

    return {
        "name": NAME,
        "description": DESCRIPTION,
        "system_prompt": SYSTEM_PROMPT,
        "tools": tools,
    }
