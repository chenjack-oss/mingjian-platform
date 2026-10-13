"""
LangGraph 检查点装配：PostgresSaver（生产）/ InMemorySaver（兜底）

为什么必须用 PostgresSaver：
    旧项目用 InMemorySaver，两个后果 ——
    1. 进程一重启，所有人所有会话全部清空；
    2. 多实例部署时，用户第二次请求打到另一个实例就读不到状态。
    PostgresSaver 把图状态落到数据库，重启与多实例都不再丢。

为什么还保留内存兜底：
    本地没有 Postgres 时，硬失败会让整个服务起不来。
    这里的选择是**显式降级 + 显式告警**，而不是静默降级 ——
    /health 接口会如实返回 checkpointer=…，任何时候都能查到当前用的是什么。
"""

from __future__ import annotations

import inspect
import re
import threading
from typing import Any, Optional, Tuple

from src.config import settings

from src.observe.logging_setup import get_logger

logger = get_logger(__name__)

_saver: Any = None
_kind: str = "uninitialized"
_initialized: bool = False
_lock = threading.Lock()


def _to_libpq_dsn(url: str) -> str:
    """
    SQLAlchemy URL → libpq 连接串。

    psycopg / psycopg_pool 只认 `postgresql://user:pass@host:port/db` 这种 libpq 格式，
    直接把 `postgresql+psycopg://...` 传进去会报
        missing "=" after "postgresql+psycopg://..." in connection info string
    而业务库（SQLAlchemy）必须带驱动后缀，所以这里做一次转换。
    （实测踩到：容器启动后 checkpointer 报 PoolTimeout，日志里才是这条真正的根因。）
    """
    m = re.match(r"^postgres(?:ql)?\+[A-Za-z0-9_]+://(.*)$", url)
    if m:
        return "postgresql://" + m.group(1)
    return url


async def _build_postgres():
    """
    必须用 **Async**PostgresSaver。

    踩过的坑（P2 才暴露，P1 没真跑模型所以看不出来）：
        同步的 PostgresSaver 只实现了 put/get，异步的 aput/aget 直接
        raise NotImplementedError()。而本平台 P2 起全程用 graph.astream() 流式跑，
        走到异步路径就整个崩，表现是 agent 返回一条空的 NotImplementedError。
        所以这里一律走 langgraph.checkpoint.postgres.aio + psycopg_pool.AsyncConnectionPool。
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    dsn = _to_libpq_dsn(settings.db_url)
    # 用连接池而不是单连接：单连接在并发请求下会互相阻塞甚至报错
    pool = AsyncConnectionPool(
        dsn,
        open=False,
        min_size=1,
        max_size=8,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    await pool.open(wait=True, timeout=30)
    saver = AsyncPostgresSaver(pool)
    await saver.setup()          # 建 checkpoint 相关表（幂等）
    return saver


def _build_memory():
    from langgraph.checkpoint.memory import InMemorySaver
    return InMemorySaver()


async def get_checkpointer() -> Tuple[Any, str]:
    """
    返回 (saver, kind)。kind 用于 /health 如实上报，不粉饰。

    为什么是 async：Postgres 路径要 await pool.open() 与 await saver.setup()，
    而 setup() 建表必须在事件循环里做。改成 async 后由 startup 与 get_graph 分别 await。
    """
    global _saver, _kind, _initialized
    with _lock:
        if _saver is not None:
            return _saver, _kind

        mode = (settings.checkpointer or "auto").lower()
        _initialized = True

        if mode == "memory":
            _saver, _kind = _build_memory(), "memory(显式指定)"
            return _saver, _kind

        if mode in ("auto", "postgres"):
            if settings.is_postgres or mode == "postgres":
                try:
                    _saver = await _build_postgres()
                    _kind = "postgres"
                    logger.info("[checkpointer] 已启用 AsyncPostgresSaver")
                    return _saver, _kind
                except Exception as e:
                    logger.warning(f"[checkpointer] ⚠️ AsyncPostgresSaver 初始化失败，回退内存: "
                                   f"{type(e).__name__} {e}")
                    _saver, _kind = _build_memory(), f"memory(postgres失败:{type(e).__name__})"
                    return _saver, _kind

        _saver, _kind = _build_memory(), "memory(非 postgres 库)"
        logger.info(f"[checkpointer] 使用 InMemorySaver（当前库: {settings.db_url.split('://')[0]}）")
        return _saver, _kind


def kind() -> str:
    """供 /health 读取。未初始化/已关闭都如实说明，不假装是 postgres。"""
    if not _initialized:
        # 关闭后 _kind 是 "closed"，要如实报而不是伪装成"尚未装配"
        return _kind if _kind == "closed" else "uninitialized(尚未装配)"
    return _kind


async def close() -> None:
    """
    关掉连接池（async 版，因为 AsyncConnectionPool.close() 是协程）。

    不关的话进程退出时会看到
        couldn't stop thread 'pool-1-worker-0' within 5.0 seconds
    虽然只是退出期的噪音，但热重载 / 反复重启会泄漏线程，所以显式关掉。
    """
    global _saver, _kind, _initialized
    with _lock:
        saver, _saver, _kind, _initialized = _saver, None, "closed", False
    if saver is None:
        return
    try:
        conn = getattr(saver, "conn", None)
        if conn is not None and hasattr(conn, "close"):
            res = conn.close()
            if inspect.isawaitable(res):
                await res
    except Exception as e:
        logger.warning(f"[checkpointer] 关闭连接池失败（忽略）: {type(e).__name__} {e}")
