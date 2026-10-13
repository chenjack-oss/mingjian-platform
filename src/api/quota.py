# -*- coding: utf-8 -*-
"""每日配额硬限流（P-4 补齐）。

与 ratelimit.py 的区别（两个不同维度的限制，别混）：
  - ratelimit = **速率**限流：令牌桶，防单用户短时间狂刷（秒级维度）。
  - quota     = **配额**限流：每日用量上限（天级维度），防单用户一天用爆预算。

现状（P-4 只记帐没限流）：`quota_*_per_day` 配了、`/v1/usage` 也报出了配置值，
但**从没做过检查** —— 这是"说了没做"的欠账，本次补齐。

配额维度（复用 usage 表的客观记账，不另建计数）：
  - 消息数 = usage 里 kind="message" 的当日计数
  - 检索数 = usage 里 kind="tool:internet_search" 的当日计数

为什么按 UTC 日界：服务器可能跨时区，本地"今天"边界不一致会让配额判定飘忽，
UTC 日界是唯一全局一致、可复现的切分点。重置倒计时见 repo.seconds_until_utc_midnight。
"""
from __future__ import annotations

from typing import Dict

from fastapi import Depends, HTTPException, status

from src.config import settings
from src.db import repo
from src.identity import auth


def check_quota(user: Dict = Depends(auth.current_user)) -> Dict:
    """发消息前的每日配额守卫。超配额抛 429，带剩余量与重置倒计时。

    检索配额的判定时机：这里的"检索数"来自**历史**记账（本次调用之前的当日累计），
    所以是"今日已检索 N 次，再检索就超"的预判。本次请求本身会不会触发检索，
    要到模型跑完才知道 —— 配额是**事后记账 + 事前拦截累计**的折中：
    拦住的是"已经用爆了还要继续用"，而不是"这次会用到爆"。
    这是诚实且可解释的语义，不假装能做到调用前精确预判。
    """
    today = repo.usage_today(user["id"], ("message", "tool:internet_search"))

    msg_count = today["message"]
    search_count = today["tool:internet_search"]

    exceeded = []
    if settings.quota_messages_per_day > 0 and msg_count >= settings.quota_messages_per_day:
        exceeded.append(f"今日消息数已达上限 {settings.quota_messages_per_day}")
    if settings.quota_searches_per_day > 0 and search_count >= settings.quota_searches_per_day:
        exceeded.append(f"今日检索数已达上限 {settings.quota_searches_per_day}")

    if exceeded:
        reset_in = repo.seconds_until_utc_midnight()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="；".join(exceeded) + f"（UTC 0 点重置，约 {reset_in // 3600}h"
                    f"{(reset_in % 3600) // 60}m 后恢复）",
            headers={
                "Retry-After": str(reset_in),
                "X-Quota-Messages-Used": str(msg_count),
                "X-Quota-Searches-Used": str(search_count),
            },
        )
    return user
