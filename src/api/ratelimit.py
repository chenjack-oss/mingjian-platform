"""
按 user_id 的令牌桶限流（P3）

**为什么放在应用层而不是 Nginx**：
限流维度是 `user_id`，这是**业务语义**，网关层拿不到 ——
Nginx 只能按 IP 限，而一个出口 IP 后面可能坐着十个人，
按 IP 限的结果是：正常用户互相挤掉，真正在刷的人换个代理就绕过去了。

**失败策略是 fail-open**：Redis 挂掉时一律放行。
理由很直接 —— 因为限流组件故障而拒绝正常请求，影响远大于短暂放开限流。
（如果业务上是"宁可错杀"的场景，这里要反过来，但对话服务不是。）
"""

from __future__ import annotations

from typing import Dict

from fastapi import Depends, HTTPException, status

from src.cache import rate_allow
from src.config import settings
from src.identity import auth


def rate_limit(user: Dict = Depends(auth.current_user)) -> Dict:
    allowed, remaining = rate_allow(user["id"])
    if not allowed:
        # 429 必须带 Retry-After，否则客户端只能盲目重试，反而把压力再放大一轮
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(f"请求过于频繁（桶容量 {settings.rate_capacity}，"
                    f"补充速率 {settings.rate_refill_per_sec}/s），请稍后再试"),
            headers={"Retry-After": "1"},
        )
    return user
