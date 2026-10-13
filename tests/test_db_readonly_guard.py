# -*- coding: utf-8 -*-
"""db-analyst 只读护栏离线自测（P-2）。

不依赖真实数据库，只测安全护栏的边界：
  1. 只读白名单：SELECT/SHOW/DESCRIBE/EXPLAIN/WITH 放行，DML/DDL 拦截
  2. 标识符清洗：拦截可注入字符
  3. CSV 渲染：列头 + 数据行格式

跑法：python tests/test_db_readonly_guard.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.capabilities.database import _check_readonly, _clean_identifier, _render_rows

_passed = 0
_failed = 0


def check(name: str, cond: bool):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [ok] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}")


print("[1] 只读白名单：放行只读语句")
check("SELECT 放行", _check_readonly("SELECT * FROM orders") is None)
check("SHOW 放行", _check_readonly("SHOW TABLES") is None)
check("DESCRIBE 放行", _check_readonly("DESCRIBE users") is None)
check("EXPLAIN 放行", _check_readonly("EXPLAIN SELECT 1") is None)
check("WITH 放行", _check_readonly("WITH x AS (SELECT 1) SELECT * FROM x") is None)
check("带括号的 SELECT 放行", _check_readonly("(SELECT * FROM t)") is None)

print("[2] 只读白名单：拦截 DML/DDL")
check("DELETE 拦截", _check_readonly("DELETE FROM users") is not None)
check("DROP 拦截", _check_readonly("DROP TABLE users") is not None)
check("UPDATE 拦截", _check_readonly("UPDATE users SET x=1") is not None)
check("INSERT 拦截", _check_readonly("INSERT INTO t VALUES (1)") is not None)
check("ALTER 拦截", _check_readonly("ALTER TABLE t ADD c INT") is not None)
check("TRUNCATE 拦截", _check_readonly("TRUNCATE TABLE t") is not None)
check("小写 update 也拦截", _check_readonly("update users set x=1") is not None)
check("空 SQL 拦截", _check_readonly("") is not None)
check("纯空白拦截", _check_readonly("   ") is not None)

print("[3] 标识符清洗：拦截注入字符")
check("合法表名放行", _clean_identifier("users") == "users")
check("带点表名放行", _clean_identifier("schema.users") == "schema.users")
check("分号注入拦截", _clean_identifier("x; DROP TABLE users") is None)
check("空格注入拦截", _clean_identifier("x y") is None)
check("反引号注入拦截", _clean_identifier("`users`") is None)
check("注释注入拦截", _clean_identifier("x--") is None)
check("引号注入拦截", _clean_identifier("x' OR '1'='1") is None)

print("[4] CSV 渲染")
out = _render_rows(["id", "name"], [(1, "张三"), (2, "李四")])
check("含列头", out.startswith("id,name"))
check("含数据行", "1,张三" in out and "2,李四" in out)
check("无列返回占位", _render_rows([], []) == "（无结果）")

print()
print(f"通过 {_passed} / {_passed + _failed}")
sys.exit(1 if _failed else 0)
