# -*- coding: utf-8 -*-
"""web-analyst：网络搜索子智能体（P-2 接回的第三个子智能体）。

对应旧项目 network_search_agent（tavily_tool）的联网检索能力。

为什么用新平台的 internet_search 而不是接旧 tavily
--------------------------------------------------
旧项目 tavily_tool 依赖 TAVILY_API_KEY + api.monitor，而且只是"单次搜索"。
新平台 src/capabilities/search.py 的 internet_search 已经更强：
  - 带**每日预算**（防超量扣费）
  - 带**结果缓存**（同样的检索不重复扣费）
  - 带**引用登记**（检索结果自动进 citation_guard，供落盘校验）
所以这里不接旧 tavily，而是把新平台的 internet_search 封装成"深度搜索"子智能体。

深度搜索 vs 单次搜索（这是子智能体存在的理由）
----------------------------------------------
单次 internet_search 是"一次调用出结果"，适合主智能体随手查一个事实。
但"深度调研"是**多步独立过程**：拆检索词 → 多轮搜索 → 交叉验证 → 去重 → 归纳。
这个过程（拆了几个词、搜了几轮、哪些结果互相矛盾）是过程噪声，主智能体不需要，
所以封装成 web-analyst 子智能体，主智能体只拿"验证过的结论 + 出处"。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .contracts import ENVELOPE_INSTRUCTION

from src.observe.logging_setup import get_logger

logger = get_logger(__name__)

NAME = "web-analyst"
TOOL_PREFIX = ""   # internet_search 没有前缀，按工具名剥离

DESCRIPTION = (
    "网络深度搜索。用于回答需要**外部实时信息**的问题，如行业动态、新闻事件、"
    "公开数据、竞品信息。它会拆解检索词、多轮搜索、交叉验证、去重，最后给出"
    "带出处的结论。只在需要联网事实时派给它；凭已有知识能答的问题不要派。"
)

SYSTEM_PROMPT = (
    "你是网络信息检索专家，只做一件事：把问题拆成多个检索角度，多轮搜索并交叉验证，"
    "给出带出处的结论。\n\n"
    "工作方法：\n"
    "1. 把问题拆成 2~3 个互补的检索词（不同角度，避免只用原句搜）；\n"
    "2. 用 internet_search 逐个检索，每个检索词搜一次；\n"
    "3. 交叉验证：关键事实至少要有 2 个不同来源互相印证才写进结论；\n"
    "4. 去重与归纳：合并相同信息，保留可溯源的结论。\n\n"
    "硬性规则：\n"
    "1. 每个结论都要有出处（URL 或来源名），**禁止编造链接**；\n"
    "2. 来源互相矛盾时，如实说明分歧，**不要自己判定谁对**；\n"
    "3. 搜不到就如实说搜不到，degraded 填 no_evidence，**禁止用常识补内容**；\n"
    "4. 时效敏感的信息（价格/政策/版本）要标注检索时间。\n"
    + ENVELOPE_INSTRUCTION
)


async def build() -> Optional[Dict[str, Any]]:
    """装配 web-analyst。联网搜索不可用（没配 API key）时返回 None，不注册。"""
    try:
        from src.capabilities import search
        if not search.available():
            logger.info("[subagent] 联网搜索未配置 API key，跳过 web-analyst")
            return None
        tools: List[Any] = [search.internet_search]
    except Exception as e:
        logger.warning(f"联网搜索工具加载失败: {type(e).__name__}: {e}")
        return None

    return {
        "name": NAME,
        "description": DESCRIPTION,
        "system_prompt": SYSTEM_PROMPT,
        "tools": tools,
    }
