"""P3：缓存与限流（Redis 一个组件承担三种用途）"""

from src.cache.redis_client import (
    cache_delete,
    cache_get,
    cache_key,
    cache_set,
    health,
    rate_allow,
    rate_remaining,
)

__all__ = [
    "cache_get", "cache_set", "cache_delete", "cache_key",
    "rate_allow", "rate_remaining", "health",
]
