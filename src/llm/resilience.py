"""
大模型调用的熔断与降级（P9）

**现状（改造前的真实缺口，不是凭感觉）**：
    `src/llm/chat.py:57` 失败就是 `raise LLMUnavailable`，
    `core.py:139` 的 `ChatOpenAI(max_retries=1)` 是**盲重试** ——
    不区分错误类型（参数错误也重试），不统计失败率，没有熔断。
    结果：模型端点抖动时，每个请求都要等满 60s 超时 ×2，
    用户看到的是"全员卡死"，而不是"快速失败 + 明确提示"。

**三条设计原则**：
1. **快速失败优先于重试**。端点已经挂了，让用户等 120s 拿到一个错误，
   不如 200ms 内告诉它"现在不可用，稍后重试"。
2. **区分错误类型**。4xx（除 429）是请求本身的问题，重试一万次也是错；
   429/5xx/连接错误才值得重试 —— 但重试必须带退避与抖动，否则就是重试风暴。
3. **降级必须可见**。任何降级都要在响应里留痕（`degraded` 字段），
   静默降级 = 用户以为拿到了正常答案，实际上是兜底话术。

**三态机**（closed → open → half_open）：
    closed    正常放行，累计失败
    open      达到阈值，直接拒绝调用（**不打后端**，快速失败）
    half_open 冷却期结束，只放行**一个**探测请求；
               成功则关闸，失败则回 open 并延长冷却
    └─ 为什么 half_open 只放一个：一恢复就把全部流量放过去，
       端点还没缓过来会被立刻再次打垮（"恢复即雪崩"）。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from src.config import settings
from src.observe.logging_setup import get_logger

logger = get_logger(__name__)

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

# ---------- 错误分类 ----------
# 为什么要分类：不分类的重试会把"请求写错了"也重试一遍，
# 白白消耗额度、拖长用户等待，还把真实错误淹没在重试日志里。
TRANSIENT = "transient"      # 值得重试：连接失败、超时、5xx、429
PERMANENT = "permanent"      # 不该重试：401/403/404/400 参数错误等
UNKNOWN = "unknown"


def classify_error(e: BaseException) -> str:
    """把异常分成「值得重试 / 不该重试 / 认不出来」。

    认不出来时按 PERMANENT 处理（保守）：
    宁可少重试一次，也不要对着一个必然失败的请求连打三次。
    """
    name = type(e).__name__.lower()
    msg = str(e).lower()

    # 连接类 / 超时类
    for k in ("timeout", "timedout", "connect", "connection", "reset",
              "temporarily", "unavailable", "readerror", "network"):
        if k in name or k in msg:
            return TRANSIENT

    # HTTP 状态码：藏在异常消息里（httpx / openai SDK 都这么干）
    for code in ("429", "500", "502", "503", "504", "529"):
        if code in msg:
            return TRANSIENT
    for code in ("400", "401", "403", "404", "405", "422"):
        if code in msg:
            return PERMANENT

    # 关键字兜底
    for k in ("rate limit", "rate_limit", "overloaded", "server_error",
              "internal error", "bad gateway", "service unavailable"):
        if k in msg:
            return TRANSIENT
    for k in ("invalid", "unauthorized", "forbidden", "not found",
              "authentication", "permission", "malformed"):
        if k in msg:
            return PERMANENT

    return UNKNOWN


def retryable(e: BaseException) -> bool:
    return classify_error(e) == TRANSIENT


# 模型侧"不可用"的判定：用于把裸异常换成兜底话术。
# 只认这几类 —— 别的错误（工具报错、参数错误）不该被伪装成"模型挂了"，
# 那会把真实 bug 藏起来。
_LLM_DOWN_KEYS = (
    "circuitopen", "熔断", "llmunavailable", "模型调用失败", "模型后端不可用",
    "timeout", "timed out", "timedout", "connection", "connecterror",
    "502", "503", "504", "500", "429", "rate limit", "rate_limit",
    "service unavailable", "bad gateway", "overloaded",
)


def is_llm_down(error: Any) -> bool:
    """判断一条错误是不是"模型侧不可用"（而不是用户问错了 / 工具出 bug）。"""
    s = ("" if error is None else str(error)).lower()
    if not s:
        return False
    return any(k in s for k in _LLM_DOWN_KEYS)


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 8.0) -> float:
    """指数退避 + **抖动**。

    为什么必须有抖动：不加抖动的话，一批同时失败的请求会在同一毫秒重试，
    形成整齐的重试尖峰 —— 这正是把半死的端点彻底打死的方式。
    """
    d = min(cap, base * (2 ** attempt))
    return d * (0.5 + random.random() / 2)      # 50%~100% 之间抖动


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------
class CircuitOpen(RuntimeError):
    """熔断器打开时抛出。调用方应当走降级，而不是继续等。"""

    def __init__(self, name: str, until: float = 0.0):
        self.name = name
        self.until = until
        super().__init__(f"熔断器已打开（{name}），快速失败不再打后端")


class _LocalState:
    """进程内状态（Redis 不可用时的回退，与限流兜底同一思路）。"""

    def __init__(self) -> None:
        self.fail_at: List[float] = []      # 失败时间戳（滑动窗口）
        self.open_until: float = 0.0
        self.probe_until: float = 0.0


_local: Dict[str, _LocalState] = {}


def _state(name: str) -> _LocalState:
    return _local.setdefault(name, _LocalState())


class CircuitBreaker:
    """
    一个命名的熔断器。**每个后端一个实例**：主模型挂了不能顺手把备用模型也熔断掉，
    否则降级链就形同虚设。
    """

    def __init__(self, name: str,
                 threshold: Optional[int] = None,
                 window_sec: Optional[float] = None,
                 cooldown_sec: Optional[float] = None):
        self.name = name
        self.threshold = threshold or settings.llm_circuit_threshold
        self.window = window_sec or settings.llm_circuit_window_sec
        self.cooldown = cooldown_sec or settings.llm_circuit_cooldown_sec

    # -- Redis key --------------------------------------------------------
    def _k(self, suffix: str) -> str:
        return f"mj:cb:{self.name}:{suffix}"

    def _redis(self):
        try:
            from src.cache import redis_client
            if not settings.redis_on:
                return None
            return redis_client._get()
        except Exception:               # noqa: BLE001
            return None

    # -- 状态查询 ---------------------------------------------------------
    def state(self) -> str:
        cli = self._redis()
        if cli is None:
            st = _state(self.name)
            now = time.time()
            if st.open_until and now < st.open_until:
                return OPEN
            if st.open_until and now >= st.open_until:
                return HALF_OPEN
            return CLOSED
        try:
            if cli.get(self._k("open")):
                return OPEN
            return CLOSED
        except Exception as e:          # noqa: BLE001
            logger.debug("熔断状态读取失败，按 closed 处理: %s", e)
            return CLOSED

    def _fails(self) -> int:
        cli = self._redis()
        if cli is None:
            st = _state(self.name)
            now = time.time()
            st.fail_at = [t for t in st.fail_at if now - t <= self.window]
            return len(st.fail_at)
        try:
            raw = cli.get(self._k("fail"))
            return int(raw) if raw else 0
        except Exception:               # noqa: BLE001
            return 0

    # -- 记账 -------------------------------------------------------------
    def record_success(self) -> None:
        """成功：清空失败计数并关闸（half_open 探测成功后回到 closed）。"""
        cli = self._redis()
        if cli is None:
            st = _state(self.name)
            st.fail_at.clear()
            st.open_until = 0.0
            st.probe_until = 0.0
            return
        try:
            pipe = cli.pipeline()
            pipe.delete(self._k("fail"))
            pipe.delete(self._k("open"))
            pipe.execute()
        except Exception as e:          # noqa: BLE001
            logger.debug("熔断状态写入失败（不影响主链路）: %s", e)

    def record_failure(self) -> str:
        """失败：计数 +1，达到阈值则开闸。返回开闸后的状态。"""
        cli = self._redis()
        if cli is None:
            st = _state(self.name)
            now = time.time()
            st.fail_at = [t for t in st.fail_at if now - t <= self.window]
            st.fail_at.append(now)
            if len(st.fail_at) >= self.threshold:
                st.open_until = now + self.cooldown
                logger.warning("熔断打开（本地）: %s，%d 次失败 / %.0fs 窗口，"
                               "冷却 %.1fs", self.name, len(st.fail_at),
                               self.window, self.cooldown)
                return OPEN
            return CLOSED
        try:
            pipe = cli.pipeline()
            pipe.incr(self._k("fail"))
            pipe.expire(self._k("fail"), int(self.window))
            pipe.execute()
            n = int(cli.get(self._k("fail")) or 0)
            if n >= self.threshold:
                # open 键的 TTL = 冷却时长：到期自动回到 half_open，
                # 不需要后台任务去巡检（少一个组件少一类故障）
                cli.set(self._k("open"), "1", ex=int(self.cooldown))
                logger.warning("熔断打开: %s，%d 次失败 / %.0fs 窗口，冷却 %.1fs",
                               self.name, n, self.window, self.cooldown)
                return OPEN
        except Exception as e:          # noqa: BLE001
            logger.debug("熔断记账失败（不影响主链路）: %s", e)
        return CLOSED

    def _take_probe(self) -> bool:
        """half_open 阶段抢探测名额：只放行一个请求。"""
        cli = self._redis()
        if cli is None:
            st = _state(self.name)
            now = time.time()
            if st.probe_until and now < st.probe_until:
                return False
            st.probe_until = now + settings.llm_timeout
            return True
        try:
            # SET NX EX：抢到即是探测者，TTL 到期自动释放
            return bool(cli.set(self._k("probe"), "1",
                                nx=True, ex=int(max(5, settings.llm_timeout))))
        except Exception:               # noqa: BLE001
            return True

    # -- 主入口 -----------------------------------------------------------
    async def call(self, fn: Callable[[], Awaitable[Any]],
                   max_retries: Optional[int] = None) -> Any:
        """带熔断的重试调用。

        顺序：**先看闸 → 再重试 → 最后记账**。
        先重试后看闸的话，一次熔断窗口内的所有请求都会各自重试满 N 次才失败，
        等于把压力放大 N 倍 —— 熔断的意义就没了。
        """
        st = self.state()
        if st == OPEN:
            raise CircuitOpen(self.name)

        half_open = False
        if st == HALF_OPEN:
            if not self._take_probe():
                raise CircuitOpen(self.name)
            half_open = True
            logger.info("熔断 %s 进入半开，放行一个探测请求", self.name)

        retries = settings.llm_max_retries if max_retries is None else max_retries
        last: Optional[BaseException] = None

        for attempt in range(retries + 1):
            try:
                out = await fn()
                self.record_success()
                if half_open:
                    logger.info("熔断 %s 探测成功，已关闸", self.name)
                return out
            except CircuitOpen:
                raise
            except BaseException as e:      # noqa: BLE001 - 分类后决定是否重试
                last = e
                # 用户主动取消 / 系统退出不参与重试
                if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                    raise
                if attempt >= retries or not retryable(e):
                    break
                delay = backoff_delay(attempt)
                logger.warning("模型调用失败（%s），%.2fs 后第 %d 次重试: %s",
                               classify_error(e), delay, attempt + 1, e)
                await asyncio.sleep(delay)

        self.record_failure()
        raise last if last is not None else RuntimeError("调用失败（无异常详情）")


# ---------------------------------------------------------------------------
# 降级链
# ---------------------------------------------------------------------------
def breaker_for(model_name: str) -> CircuitBreaker:
    """每个模型一个熔断器。共享一个的话主模型会把备用模型一起熔断掉。"""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in model_name)
    return CircuitBreaker(f"llm:{safe}")


FALLBACK_NOTE = (
    "\n\n---\n"
    "⚠️ **本次回答由降级策略生成**：主模型当前不可用，系统已自动切换到备用路径。"
    "内容可能不如常规回答完整，建议稍后重试以获取完整答案。"
)

FALLBACK_NO_MODEL = (
    "抱歉，模型服务当前不可用（已连续多次调用失败，系统已触发熔断保护）。\n\n"
    "这不是你的问题，也不是问题本身无法回答 —— 是推理服务暂时不可用。"
    "请稍后重试；若持续失败，可联系管理员查看服务状态。\n\n"
    "（为保障响应速度，系统在检测到服务异常时会快速失败而不是让请求长时间挂起。）"
)

FALLBACK_WITH_EVIDENCE = (
    "模型服务当前不可用，无法对检索结果做归纳。以下是**未经加工的知识库原文片段**，"
    "供你自行判断：\n\n"
)


# ---------------------------------------------------------------------------
# 接入点：塞进 LangChain 的 httpx 层
# ---------------------------------------------------------------------------
# 为什么在这里做而不是"包住整个 agent 调用"：
#   包在外层的话粒度是"一整轮对话"，一次对话里其实有 5~10 次模型调用，
#   熔断窗口内每个请求都要各自等满超时才失败 —— 保护不住后端。
#   放在 HTTP 层，粒度是**每一次模型调用**，且能拿到真实的 HTTP 状态码做分类。
#
# 为什么用子类而不是 event_hooks：
#   httpx 的 event_hooks 只能在请求前/响应后做副作用，**无法短路请求**。
#   熔断的核心恰恰是"这次根本不要发出去"。
#
# 🔴 2026-10-13 修复（L3 故障注入发现的盲区）：
#   httpx.send() 对 5xx/429 **返回响应而不抛异常**，熔断器会把上游持续报错
#   误记为成功（甚至清零失败计数）；而 500 是 openai SDK 层才转异常的，
#   完全绕过 HTTP 层熔断记账 → 端点持续 5xx 时熔断永不打开。
#   修复：send 拿到 5xx/429 响应时显式转成 httpx.HTTPStatusError（先读完 body
#   防连接池泄漏），异常消息带状态码 → classify_error 归 TRANSIENT → 正常进
#   重试与熔断记账链路。4xx（除 429）保持返回响应，由 SDK 抛 PERMANENT，不重试。
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})


def _status_error(resp, request) -> httpx.HTTPStatusError:
    """5xx/429 响应 → 异常。message 必须带状态码（classify_error 靠它分类）。"""
    return httpx.HTTPStatusError(
        f"{resp.status_code} upstream error", request=request, response=resp)


class ResilientAsyncClient(httpx.AsyncClient):
    """带熔断的 httpx 客户端：每次 send 都过一次熔断器。"""

    def __init__(self, *args, breaker: Optional[CircuitBreaker] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._breaker = breaker or CircuitBreaker("llm:default")

    async def send(self, request, **kwargs):        # noqa: D102
        parent = super()

        async def _fn():
            resp = await parent.send(request, **kwargs)
            if resp.status_code in _RETRYABLE_STATUS:
                await resp.aread()                    # 读完再抛，连接可回收
                raise _status_error(resp, request)
            return resp

        return await self._breaker.call(_fn)


class ResilientClient(httpx.Client):
    """同步版本（deepagents 的同步路径 / 单测会走这里）。"""

    def __init__(self, *args, breaker: Optional[CircuitBreaker] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._breaker = breaker or CircuitBreaker("llm:default")

    def send(self, request, **kwargs):              # noqa: D102
        parent = super()
        # 同步路径没有事件循环可 await，重试退避只能阻塞当前线程 ——
        # 这条路只在"本来就没有 loop 可让"的场景出现，可以接受。
        for attempt in range(settings.llm_max_retries + 1):
            if self._breaker.state() == OPEN:
                raise CircuitOpen(self._breaker.name)
            try:
                resp = parent.send(request, **kwargs)
                if resp.status_code in _RETRYABLE_STATUS:
                    resp.read()                      # 读完再抛，连接可回收
                    raise _status_error(resp, request)
                self._breaker.record_success()
                return resp
            except CircuitOpen:
                raise
            except BaseException as e:              # noqa: BLE001
                if attempt >= settings.llm_max_retries or not retryable(e):
                    self._breaker.record_failure()
                    raise
                time.sleep(backoff_delay(attempt))


def build_http_clients(model_name: str, timeout: float):
    """给 ChatOpenAI 造一对带熔断的 httpx 客户端。

    ⚠️ 必须同时把 LangChain 的 `max_retries` 设为 0：
    否则 SDK 内部会再重试一遍，两层重试相乘（2×2=4 次），
    而且 SDK 那层不区分错误类型 —— 熔断等于被绕过一半。
    """
    if not settings.llm_circuit_enabled:
        return None, None
    br = breaker_for(model_name)
    return (ResilientClient(breaker=br, timeout=timeout),
            ResilientAsyncClient(breaker=br, timeout=timeout))


def health() -> Dict[str, Any]:
    """给 /health 用：如实上报各后端的熔断状态。"""
    if not settings.llm_circuit_enabled:
        return {"llm_circuit": "off"}
    names = {settings.llm_model}
    if settings.llm_model_fast:
        names.add(settings.llm_model_fast)
    if settings.llm_fallback_model:
        names.add(settings.llm_fallback_model)
    out: Dict[str, Any] = {"llm_circuit": "on"}
    for n in sorted(names):
        br = breaker_for(n)
        out[n] = {"state": br.state(), "fails": br._fails()}
    return out
