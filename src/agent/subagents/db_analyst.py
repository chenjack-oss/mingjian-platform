# -*- coding: utf-8 -*-
"""db-analyst：数据库查询子智能体（P-2 接回的第二个子智能体）。

它对应旧项目 database_query_agent 的 NL2SQL 能力，但在新平台重写。

为什么 NL2SQL 必须是子智能体而不是给主智能体三个工具
----------------------------------------------------
NL2SQL 有**独立的中间过程**，不该暴露给主智能体：
  理解意图 → 列可用表 → 锚定表结构 → 生成 SQL → 只读校验 → 执行 → 解读结果
这一串里"列了哪些表、试错了几次 SQL"都是过程噪声，主智能体只需要
"查到了什么、结论是什么"。这正符合子智能体的判据（有独立过程 + 不污染主上下文）。

而且 NL2SQL 的失败模式很特别：SQL 语法错、字段不存在、查到空结果——
这些都需要子智能体**自己消化成信封里的 degraded 字段**，而不是把裸报错甩给主智能体。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .contracts import ENVELOPE_INSTRUCTION

from src.observe.logging_setup import get_logger

logger = get_logger(__name__)

NAME = "db-analyst"
TOOL_PREFIX = ""   # db 工具没有统一前缀，工具名即能力名，主智能体按名称剥离

DESCRIPTION = (
    "结构化数据库查询（NL2SQL）。用于回答**业务数据**类问题，如"
    "「最近 7 天的订单量」「库存低于预警值的商品」「某个客户的消费记录」。"
    "它会理解你的问题→找到相关表→生成只读 SQL→执行→把结果解读成结论。"
    "只在问题确实要查数据库时派给它；闲聊、写代码、查知识库不要派。"
)

SYSTEM_PROMPT = (
    "你是数据库查询专家，只做一件事：把自然语言问题转成**只读** SQL，查出结果并解读。\n\n"
    "工具选择（严格按这个顺序）：\n"
    "1. list_tables：**第一步必调**，先看有哪些表可用；\n"
    "2. get_table_schema：确定目标表后调，锚定列名/类型/主键，避免生成不存在的字段；\n"
    "3. execute_readonly_sql：最后才调，执行只读查询。\n\n"
    "硬性规则：\n"
    "1. 只能生成只读 SQL（SELECT/SHOW/DESCRIBE/EXPLAIN），**绝对禁止** INSERT/UPDATE/DELETE/DROP/ALTER；\n"
    "2. 生成 SQL 前必须确认表名和字段名真实存在，**禁止凭空猜字段名**；\n"
    "3. 查不到结果就如实说「没有查到」，degraded 填 no_evidence，**禁止编造数据**；\n"
    "4. 结果里的数字、日期要原样引用，**禁止四舍五入或模糊化**；\n"
    "5. 一次查不到就换角度再查一次（比如换表、换筛选条件），最多试 3 次。\n"
    + ENVELOPE_INSTRUCTION
)


async def build() -> Optional[Dict[str, Any]]:
    """装配 db-analyst。工具加载失败返回 None（不注册，主智能体就不会派）。"""
    try:
        from src.capabilities import database
        tools: List[Any] = [database.list_tables,
                            database.get_table_schema,
                            database.execute_readonly_sql]
    except Exception as e:
        logger.warning(f"db 工具加载失败: {type(e).__name__}: {e}")
        return None

    return {
        "name": NAME,
        "description": DESCRIPTION,
        "system_prompt": SYSTEM_PROMPT,
        "tools": tools,
    }
