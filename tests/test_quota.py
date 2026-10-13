# -*- coding: utf-8 -*-
"""每日配额硬限流（P-4 补齐）离线自测。

覆盖：
  1. usage_today 按 UTC 日界过滤（今日计数 vs 昨日计数）
  2. check_quota 未超配额放行、超配额抛 429（带 Retry-After + 剩余量）
  3. 配额配置为 0 = 不限制（关闭该维度）
  4. seconds_until_utc_midnight 返回正数且 ≤ 86400

跑法：python tests/test_quota.py
"""
from __future__ import annotations

import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="mj_quota_test_")
os.environ["MJ_DB_URL"] = f"sqlite:///{_tmp}/test.db"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime as dt                              # noqa: E402
from src.db import engine as db                    # noqa: E402
from src.db import repo                            # noqa: E402
from src.config import settings                    # noqa: E402

db.init_db()

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


user = repo.create_user("配额测试")["user"]["id"]

print("[1] usage_today 按 UTC 日界过滤")
repo.add_usage(user, "message", 3)
today = repo.usage_today(user, ("message", "tool:internet_search"))
check("今日 message=3", today["message"] == 3)
check("今日 search=0", today["tool:internet_search"] == 0)

# 模拟"昨日"的记账：直接改 usage 表的 created_at 到昨天
from src.db.engine import usage, execute
from sqlalchemy import update as sa_update
yesterday = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
execute(sa_update(usage).values(created_at=yesterday))
today2 = repo.usage_today(user, ("message", "tool:internet_search"))
check("昨日记账不计入今日", today2["message"] == 0)

print("[2] check_quota 超配额抛 429")
from src.api.quota import check_quota
from fastapi import HTTPException

# check_quota 是 FastAPI 依赖，接收 auth.current_user 返回的 user 字典（只用其 ["id"]）
_user_dict = {"id": user}

# 未超配额（当前今日 message=0，配额默认 200）
try:
    check_quota(_user_dict)
    check("未超配额放行", True)
except HTTPException:
    check("未超配额放行", False)

# 塞满配额：直接写 200 条 message 记账（今日）
for _ in range(settings.quota_messages_per_day):
    repo.add_usage(user, "message", 1)
try:
    check_quota(_user_dict)
    check("超配额抛 429", False)
except HTTPException as e:
    check("超配额抛 429", e.status_code == 429)
    check("带 Retry-After", "Retry-After" in (e.headers or {}))
    check("带剩余量", "X-Quota-Messages-Used" in (e.headers or {}))

print("[3] 配额为 0 = 不限制该维度")
orig = settings.quota_messages_per_day
settings.quota_messages_per_day = 0
try:
    check_quota(_user_dict)  # 配额 0 = 关闭，即使已用 200 也应放行
    check("配额 0 不限制", True)
except HTTPException:
    check("配额 0 不限制", False)
settings.quota_messages_per_day = orig

print("[4] seconds_until_utc_midnight 范围")
s = repo.seconds_until_utc_midnight()
check("返回正数", s > 0)
check("≤ 86400 秒", s <= 86400)

print("\n" + "=" * 62)
print(f"通过 {_passed} / {_passed + _failed}")
sys.exit(1 if _failed else 0)
