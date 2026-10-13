"""
Redis 客户端：语义缓存 + 令牌桶限流（P3）

**设计前提：Redis 是可选组件，不是硬依赖。**
`MJ_REDIS_URL` 留空时，本模块所有方法返回"未启用/放行"，绝不抛异常 ——
主链路（/v1/chat）在任何情况下都不允许因为缓存或限流挂掉。
这不是"假装实现了"，/health 会如实报 `redis: off`。

为什么一个 Redis 承担三件事（缓存 / 限流 / Celery broker）：
    单机 4 核 7.4G，每多一个中间件就多一份内存、一份运维、一份故障面。
    缓存本来就必须要 Redis，把限流与 broker 也放上去 = **零新增组件**。
    代价是三者互相影响（FLUSHDB 清缓存会把队列也清掉）——
    所以这里用**不同的库号**隔离：缓存 0 号库、broker 1 号库。
"""

from __future__ import annotations

import hashlib
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from src.config import settings

# ---------------------------------------------------------------------------
# 惰性连接 + 故障冷却
# ---------------------------------------------------------------------------
_client = None
_client_failed_at = 0.0
_last_error = ""


def _build():
    try:
        import redis  # 延迟导入：没装 redis 包时本模块其余部分照样可用
    except ImportError:
        return None
    return redis.Redis.from_url(
        settings.redis_url,
        socket_timeout=settings.redis_timeout,
        socket_connect_timeout=settings.redis_timeout,
        health_check_interval=30,
        decode_responses=True,
    )


def _get():
    """
    拿连接，带冷却。

    为什么要有冷却：Redis 挂掉之后如果每个请求都去 connect 一次，
    0.5s 的超时会直接叠到每个请求的延迟上 —— 保护机制反而成了放大器。
    失败后 N 秒内直接返回 None（快速失败），过了冷却期再试一次。
    """
    global _client, _client_failed_at, _last_error
    if not settings.redis_on:
        return None
    now = time.time()
    if _client_failed_at and (now - _client_failed_at) < settings.redis_cooldown:
        return None
    if _client is None:
        try:
            _client = _build()
        except Exception as e:          # noqa: BLE001 - 建连失败一律降级，不打断主链路
            _last_error = f"build: {e}"
            _client_failed_at = now
            return None
    return _client


def _mark_fail(e: Exception) -> None:
    global _client_failed_at, _last_error
    _last_error = f"{type(e).__name__}: {e}"
    _client_failed_at = time.time()


# ---------------------------------------------------------------------------
# 语义缓存
# ---------------------------------------------------------------------------
_WS = re.compile(r"\s+")


def normalize_query(q: str) -> str:
    """归一化：去空白差异 + 统一大小写。中英文都只做最保守的处理。"""
    return _WS.sub(" ", (q or "").strip()).lower()


def cache_key(tier: str, model: str, query: str, hist_digest: str = "",
              user_id: str = "") -> str:
    """
    缓存 key 结构：`mj:cache:{scope}:{tier}:{model}:{user}:{hist}:{query}`

    每一段的存在理由：
    - **scope**：手动改 MJ_CACHE_SCOPE 就能整体失效（P4 接入知识库后换成 kb_version，
      知识库一更新老缓存自然失效，不用写清缓存的代码）
    - **tier**：同一问题在不同档位下答案不同（direct 不检索、research 出报告），混存会串味
    - **model**：换模型后输出风格与事实边界都变了，旧答案不该继续喂给用户
    - **user**：🔴 多租户隔离的最后一关。缓存的是模型文本，但 direct/auto 档的答案
      可能带着用户 A 的企业语境，用户 B 问同样的话会命中 A 的答案（串味）。
      所以 key 必须带 user_id —— 宁可命中率降一点，也不能跨用户串味。
      （排查于 2026-10-13：原先缺这一维，是"多租户隔离"的真实缺口。）
    - **hist**：**最容易漏的一段**。多轮会话里同一个"它多少钱"在不同上文下
      答案完全不同，只看当前提问会把上一轮的答案错喂出来。
      所以把上文摘要也打进 key —— 宁可少命中，也不能答错。

    不做 query 原文入 key：中文长句会让 key 几百字节，且含换行等不可见字符。
    """
    qd = hashlib.sha256(normalize_query(query).encode("utf-8")).hexdigest()[:32]
    uid = user_id or "anon"
    return f"mj:cache:{settings.cache_scope}:{tier}:{model}:{uid}:{hist_digest or 'x'}:{qd}"


def history_digest(history) -> str:
    """上文摘要：只取最后 4 条（再早的对话对当前答案影响已经很小）。"""
    if not history:
        return ""
    tail = history[-4:]
    joined = "\n".join(f"{m.get('role','')}:{m.get('content','')}" for m in tail)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


# 指代词 / 回指词：出现这些词的提问**必须**带上文，否则会把上一轮的答案错喂出来
ANAPHORA = ("它", "他", "她", "这个", "那个", "上面", "上述", "前面", "刚才",
            "再", "还有", "同样", "为什么这样", "这两", "前者", "后者")


def needs_context(query: str) -> bool:
    """
    判断这个提问是否依赖上文。

    **为什么需要这个判断**（这是实测踩出来的）：
    如果把上文摘要无脑打进 key，那么在同一个会话里重复问同一个问题**永远命中不了** ——
    因为第一轮的答案已经进入历史，第二轮的 history_digest 就变了。
    实测证据：同一会话连问两次同一个自包含问题，第二次 `cached=False`、`cache_hit` 计数为 0。

    所以反过来判断：提问**自包含**（够长 + 没有指代词）时，key 里不带上文，
    这样重复提问能命中；含指代词或过短的提问照旧带上文 ——
    **宁可少命中，也不能把上一轮的答案当成这一轮的答案**。
    """
    q = normalize_query(query)
    if not q:
        return True
    if len(q) < 15:                     # 太短多为追问（"为什么？" "继续"）
        return True
    return any(w in q for w in ANAPHORA)


def cache_get(key: str) -> Optional[Any]:
    if not (settings.redis_on and settings.cache_enabled):
        return None
    cli = _get()
    if cli is None:
        return None
    try:
        import json
        raw = cli.get(key)
        return json.loads(raw) if raw else None
    except Exception as e:              # noqa: BLE001
        _mark_fail(e)
        return None


def cache_set(key: str, value: Any, ttl: Optional[int] = None) -> bool:
    """
    写入缓存。TTL 带 ±N% 抖动 —— **这一行是防雪崩的关键**。

    不抖动的后果：同一时刻创建的一批 key 会在同一秒集体过期，
    那一瞬间所有请求同时 miss、同时打到 LLM，后端直接被自己打爆。
    """
    if not (settings.redis_on and settings.cache_enabled):
        return False
    cli = _get()
    if cli is None:
        return False
    try:
        import json
        base = int(ttl or settings.cache_ttl)
        jitter = int(base * settings.cache_jitter_pct / 100)
        real_ttl = max(1, base + random.randint(-jitter, jitter))
        cli.setex(key, real_ttl, json.dumps(value, ensure_ascii=False))
        return True
    except Exception as e:              # noqa: BLE001
        _mark_fail(e)
        return False


def cache_delete(key: str) -> bool:
    if not settings.redis_on:
        return False
    cli = _get()
    if cli is None:
        return False
    try:
        cli.delete(key)
        return True
    except Exception as e:              # noqa: BLE001
        _mark_fail(e)
        return False


# ---------------------------------------------------------------------------
# 令牌桶限流（Lua 脚本保证原子性）
# ---------------------------------------------------------------------------
# 为什么必须用 Lua：读 tokens → 计算 → 写回 这三步如果放到 Python 里做，
# 两个请求会同时读到同一个旧值，各自算完各自写回 —— 限流形同虚设。
# 用 Lua 让这三步在 Redis 里一次执行完，中途不会被其他命令插入。
_TOKEN_BUCKET = """
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill   = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local cost     = tonumber(ARGV[4])
local ttl      = tonumber(ARGV[5])

local data   = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts     = tonumber(data[2])
if tokens == nil then
    tokens = capacity
    ts     = now
end

local delta = now - ts
if delta < 0 then delta = 0 end        -- 时钟回拨时不能把令牌扣成负的
tokens = math.min(capacity, tokens + delta * refill)

if tokens < cost then
    redis.call('HSET', key, 'tokens', tokens, 'ts', now)
    redis.call('EXPIRE', key, ttl)
    return {0, tokens}
end

tokens = tokens - cost
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return {1, tokens}
"""

_script = None


# ---------- 本地令牌桶兜底 ----------
# 为什么要有它：P7 之前 Redis 不可用（没配 / 连不上 / 熔断中）时 rate_allow 直接放行，
# 等于**限流整体失效**。fail-open 的取舍没错（拒绝正常请求 > 短暂放开），
# 但"完全不限"和"用进程内有界桶限住"之间还有一档，之前没做。
# 局限要如实说：只保护单进程，多 worker 下实际放行量 ≈ 进程数 × 容量；
# 没有共享存储就做不到分布式，这是物理限制，兜底的意义是"从无限放行降级为有界放行"。
_local_buckets: Dict[str, List[float]] = {}      # user_id -> [tokens, last_ts]
_local_lock = threading.RLock()
_LOCAL_MAX_USERS = 5000
local_fallback_hits = 0                          # 供 /health 如实上报


def _local_allow(user_id: str, cost: int = 1) -> Tuple[bool, float]:
    """进程内令牌桶：与 Redis 版同容量、同补充速率，只是作用域限于本进程。"""
    global local_fallback_hits
    cap = float(settings.rate_capacity)
    refill = float(settings.rate_refill_per_sec)
    now = time.time()
    with _local_lock:
        local_fallback_hits += 1
        st = _local_buckets.get(user_id)
        if st is None:
            # 新用户：给满桶（跟 Redis 版一致，避免刚启动就被打回）
            st = [cap, now]
            _local_buckets[user_id] = st
            if len(_local_buckets) > _LOCAL_MAX_USERS:
                # 按最久未使用淘汰一半：字典会随用户数无限增长
                for k, _ in sorted(_local_buckets.items(),
                                   key=lambda kv: kv[1][1])[:_LOCAL_MAX_USERS // 2]:
                    _local_buckets.pop(k, None)
        tokens = min(cap, st[0] + (now - st[1]) * refill)
        if tokens >= cost:
            st[0], st[1] = tokens - cost, now
            return True, st[0]
        st[0], st[1] = tokens, now
        return False, st[0]


def rate_allow(user_id: str, cost: int = 1) -> Tuple[bool, float]:
    """
    取一个令牌。返回 (是否放行, 剩余令牌数)。

    降级链：Redis 令牌桶 → **进程内令牌桶兜底**（P7 新增）。
    仍然不是 fail-closed：Redis 挂了不会拒绝正常请求，但也不再是"完全不限流"
    —— 从无限放行降级为有界放行。真正的 fail-closed 会把正常用户挡在门外，不划算。
    """
    if not (settings.redis_on and settings.rate_enabled):
        return _local_allow(user_id, cost)
    cli = _get()
    if cli is None:
        return _local_allow(user_id, cost)
    global _script
    try:
        if _script is None:
            _script = cli.register_script(_TOKEN_BUCKET)
        ttl = max(60, int(settings.rate_capacity / max(settings.rate_refill_per_sec, 0.01)) + 60)
        # 时间由 Python 传入：单机部署没有跨节点时钟偏移问题。
        # 若将来多节点部署，应改用 redis.call('TIME') 取服务端时间。
        allowed, remaining = _script(
            keys=[f"mj:rate:{user_id}"],
            args=[settings.rate_capacity, settings.rate_refill_per_sec,
                  time.time(), cost, ttl],
        )
        return bool(int(allowed)), float(remaining)
    except Exception as e:              # noqa: BLE001
        _mark_fail(e)
        return _local_allow(user_id, cost)


# ---------------------------------------------------------------------------
# 分布式预算（P8 新增）
# ---------------------------------------------------------------------------
# 为什么"搜索次数"这类配额不能再用进程内 dict：
#   uvicorn 开 N 个 worker 时每个 worker 各数各的，用户实际能搜 预算×N 次 ——
#   配额形同虚设，而花掉的是真金白银的外部 API 额度。
# 为什么必须用 Lua 而不是 GET + INCR：
#   check-and-consume 两步分开会竞态（实测进程内版本出现过"预算耗尽 11/10"）。
#   把判断和自增塞进同一段脚本，Redis 单线程执行 = 天然原子。
_BUDGET_LUA = """
local cur = redis.call('GET', KEYS[1])
if cur and tonumber(cur) >= tonumber(ARGV[1]) then
  return {0, tonumber(cur)}
end
local n = redis.call('INCRBY', KEYS[1], tonumber(ARGV[2]))
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return {1, n}
"""
_budget_script = None


def budget_consume(key: str, limit: int, cost: int = 1,
                   ttl: int = 86400) -> Optional[Tuple[bool, int]]:
    """
    原子扣减一份**全局**预算（如每用户每日搜索次数）。

    返回 (allowed, used)；Redis 不可用返回 None —— 由调用方决定是否回退本地计数，
    这里不替它做决定，因为"配额放宽"和"直接拒绝"在不同业务里取舍不同。
    """
    if not settings.redis_on:
        return None
    cli = _get()
    if cli is None:
        return None
    global _budget_script
    try:
        if _budget_script is None:
            _budget_script = cli.register_script(_BUDGET_LUA)
        allowed, used = _budget_script(keys=[f"mj:budget:{key}"],
                                       args=[limit, cost, ttl])
        return bool(int(allowed)), int(used)
    except Exception as e:              # noqa: BLE001
        _mark_fail(e)
        return None


def rate_remaining(user_id: str) -> float:
    """只查询不扣减，供 /v1/me 展示。失败时返回桶容量（保守值）。"""
    if not settings.redis_on:
        return float(settings.rate_capacity)
    cli = _get()
    if cli is None:
        return float(settings.rate_capacity)
    try:
        raw = cli.hget(f"mj:rate:{user_id}", "tokens")
        return float(raw) if raw is not None else float(settings.rate_capacity)
    except Exception as e:              # noqa: BLE001
        _mark_fail(e)
        return float(settings.rate_capacity)


# ---------------------------------------------------------------------------
# 健康上报
# ---------------------------------------------------------------------------
def health() -> dict:
    """
    如实上报：off（没配）/ down（配了但连不上）/ up。
    不允许为了"看起来健康"而把 down 报成 up —— 这个字段是用来排障的。
    """
    if not settings.redis_on:
        return {"redis": "off", "cache": False, "rate_limit": False,
                "rate_limit_local": True, "rate_local_fallback_hits": local_fallback_hits}
    cli = _get()
    if cli is None:
        return {"redis": "down", "cache": False, "rate_limit": False,
                "rate_limit_local": True, "rate_local_fallback_hits": local_fallback_hits,
                "last_error": _last_error}
    try:
        cli.ping()
        return {"redis": "up",
                "cache": settings.cache_enabled,
                "rate_limit": settings.rate_enabled,
                "rate_limit_local": False,
                "rate_local_fallback_hits": local_fallback_hits}
    except Exception as e:              # noqa: BLE001
        _mark_fail(e)
        return {"redis": "down", "cache": False, "rate_limit": False,
                "rate_limit_local": True, "rate_local_fallback_hits": local_fallback_hits,
                "last_error": _last_error}
