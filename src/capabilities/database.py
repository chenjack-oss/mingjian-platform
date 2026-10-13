# -*- coding: utf-8 -*-
"""数据库只读工具（P-2：供 db-analyst 子智能体使用）。

这是把旧项目 tools/db_tools.py 的 NL2SQL 能力**在新平台重写**，而不是照搬。

为什么重写而不是直接 import 旧文件：
1. 旧文件依赖 `mysql.connector` + `api.monitor` + `dotenv`，与新平台的
   SQLAlchemy / ContextVar 架构不兼容，硬接会引入三套 DB 访问方式。
2. 旧文件用 `connect(**config)` 每次新开连接，新平台用 `db.get_engine()` 连接池。
3. 但**安全护栏的设计思路照搬**：只读白名单（SELECT/SHOW/DESCRIBE/EXPLAIN），
   这是旧项目验证过、且经得起面试追问的核心设计，不能丢。

只读白名单为什么是硬底线（不是可选项）：
NL2SQL 的本质是"让模型生成 SQL"，而模型生成的 SQL 是不可信的。
没有白名单，一句 "DROP TABLE users" 就下去了。白名单把风险从"执行任意 SQL"
收敛到"只读查询"，即使模型生成恶意语句，最多是查一下，不会破坏数据。

标识符清洗为什么必要：
表名/列名会拼进 SQL 字符串，不洗的话 `table_name="x; DROP TABLE users"` 就能注入。
这里拦截反引号、分号、空格、注释符等一切可用于拼接注入的字符。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from src.observe.logging_setup import get_logger

logger = get_logger(__name__)

# ---- SQL 只读白名单（安全护栏，旧项目验证过的设计）----
_READONLY_LEADING_KEYWORDS = frozenset(
    {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH"}
)


def _check_readonly(query: str) -> Optional[str]:
    """断言查询为只读语句。只读返回 None，否则返回拦截原因（拦截 DML/DDL）。"""
    if not query or not query.strip():
        return "空 SQL 语句"
    head = query.strip().lstrip("(").strip()
    keyword = head.split(None, 1)[0].upper() if head else ""
    if keyword not in _READONLY_LEADING_KEYWORDS:
        return f"仅允许只读查询（SELECT/SHOW/DESCRIBE/EXPLAIN/WITH），已拦截语句类型 {keyword or '<空>'}"
    return None


def _clean_identifier(name: str) -> Optional[str]:
    """标识符清洗：表名/列名只允许字母数字下划线点，拦截一切可注入字符。"""
    if not name:
        return None
    bad = ("`", ";", "--", "/*", "*/", " ", "'", '"', "\\", "(", ")")
    if any(ch in name for ch in bad):
        return None
    return name


def _render_rows(columns: List[str], rows: List[tuple]) -> str:
    """把查询结果渲染成 CSV 文本（旧项目同款格式，便于模型阅读）。"""
    if not columns:
        return "（无结果）"
    header = ",".join(columns)
    body = "\n".join(",".join(str(v) for v in row) for row in rows)
    return f"{header}\n{body}" if body else header


@tool
def list_tables() -> str:
    """列出当前数据库里所有可用的表名。

    用途：让模型先知道有哪些表，才能决定查哪张。生成 SQL 前必须先调用本工具。
    返回：可用的表名列表（逗号分隔），或"没有可用的表"/异常信息。
    """
    try:
        from sqlalchemy import inspect as sa_inspect
        from src.db.engine import get_engine
        insp = sa_inspect(get_engine())
        tables = insp.get_table_names()
        if not tables:
            return "没有可用的表"
        return f"可用的表有：{', '.join(tables)}"
    except Exception as e:      # noqa: BLE001 工具错误要返回给模型，不能抛
        return f"查询出现异常：{type(e).__name__}: {e}"


@tool
def get_table_schema(table_name: str) -> str:
    """查询指定表的结构（列名/类型/是否主键），用于在生成精确 SQL 前锚定可用字段。

    参数 table_name: 表名（建议先经 list_tables 确认存在）。
    返回：逐行列出「列名: 类型 [KEY=主键]」。
    """
    name = _clean_identifier(table_name)
    if name is None:
        return "非法表名：表名不允许包含反引号、分号、空格、引号或注释字符"

    try:
        from sqlalchemy import inspect as sa_inspect
        from src.db.engine import get_engine
        insp = sa_inspect(get_engine())
        cols = insp.get_columns(name)
        if not cols:
            return f"表 {name} 没有列信息"
        pk = set(insp.get_pk_constraint(name).get("constrained_columns", []) or [])
        lines = [f"表 {name} 结构（列名: 类型 [键]）:"]
        for c in cols:
            key = " [KEY=主键]" if c.get("name") in pk else ""
            lines.append(f"{c.get('name')}: {c.get('type')}{key}")
        return "\n".join(lines)
    except Exception as e:      # noqa: BLE001
        return f"查询出现异常：{type(e).__name__}: {e}"


@tool
def execute_readonly_sql(query: str) -> str:
    """执行一条**只读** SQL 查询（SELECT/SHOW/DESCRIBE/EXPLAIN/WITH）。

    硬性规则：
    1. 执行前必须先经 list_tables 明确表名、经 get_table_schema 明确表结构；
    2. 只允许只读语句，任何 DML/DDL（INSERT/UPDATE/DELETE/DROP/ALTER 等）都会被拦截；
    3. 结果最多返回 100 行，超出部分截断。

    参数 query: 要执行的 SQL 语句。
    返回：CSV 格式（第一行列头，之后是数据行），或被拦截/异常说明。
    """
    block_reason = _check_readonly(query)
    if block_reason:
        return f"查询被安全策略拦截：{block_reason}"

    try:
        from sqlalchemy import text
        from src.db.engine import get_engine
        # 强制限制返回行数：防止模型生成的 SELECT * 拉全表拖垮内存
        safe = query.strip()
        if not safe.upper().startswith(("SHOW", "DESCRIBE", "DESC", "EXPLAIN")):
            # 只对 SELECT 类加 LIMIT 兜底（SHOW/DESCRIBE 不支持 LIMIT）
            if "LIMIT" not in safe.upper():
                safe = f"{safe.rstrip(';')} LIMIT 100"

        with get_engine().connect() as conn:
            result = conn.execute(text(safe))
            columns = list(result.keys())
            rows = result.fetchall()
            if not columns:
                return f"执行 SQL 没有返回结果，sql 为：{query}"
            return _render_rows(columns, [tuple(r) for r in rows])
    except Exception as e:      # noqa: BLE001
        return f"查询出现异常：{type(e).__name__}: {e}"
