"""
异步任务（P3）

任务状态机（存在 Postgres 的 tasks 表，不是 Celery 的 result backend）：

    pending ──> running ──> success
                   │
                   ├──> failure      （异常）
                   ├──> revoked      （用户主动取消）
                   └──> stalled      （running 超时未更新：worker 大概被 kill 了）

**为什么状态自己管而不交给 Celery**：
1. 结果要能翻历史、能被查询（"我上周跑的调研报告呢"）—— Redis 满足不了
2. Celery 的 backend 不配就查不到状态，配了又多一个组件
3. stalled 这个状态 Celery 根本没有：worker 被 kill 时 Celery 只知道"任务还在跑"

**幂等**：dispatch 层用 dedupe_key 拦截重复提交（用户狂点按钮 / 前端断线重连）。
这里再兜一层：任务开始时就写 running，重复的任务进来会看到已有结果。
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any, Dict

from celery.exceptions import SoftTimeLimitExceeded

from src.config import settings
from src.db import engine as dbeng
from src.db import repo
from src.tasks.celery_app import app


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# worker 进程内**复用同一个事件循环**。
#
# 为什么不能用 asyncio.run()：它会为每一个任务新建一个循环，跑完就关。
# 而 LangGraph 的 AsyncPostgresSaver 与它的连接池是模块级单例 ——
# 第一个任务在循环 A 里建的连接，第二个任务拿到的是循环 B，
# 用旧连接会直接报 "Event loop is closed" / "attached to a different loop"。
# 一个 worker 进程固定一个循环，连接池里的连接就始终有效。
_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


async def _run_with_heartbeat(task_id: str, coro,
                              interval: int = settings.task_heartbeat_sec):
    """
    一边执行任务，一边每 interval 秒写一次心跳。

    心跳的意义：让"这个任务还活着"成为一个**可观测的事实**，而不是靠猜。
    worker 被 kill 时没有任何回调会通知我们，唯一能判断的依据就是心跳断了。
    没有心跳的话，一个正常的 125 秒长任务会在第 120 秒被误判成 stalled。
    """
    async def beat():
        while True:
            await asyncio.sleep(interval)
            try:
                repo.touch_task(task_id)
            except Exception:                # noqa: BLE001  心跳失败不该让主任务挂掉
                pass

    beater = asyncio.ensure_future(beat())
    try:
        return await coro
    finally:
        beater.cancel()


async def _arun(user_id: str, session_id: str, query: str, tier: str) -> Dict[str, Any]:
    """
    worker 是独立进程，没有 FastAPI 的 startup 钩子，所以这里自己补两件事：
    建表（幂等）与初始化 checkpointer —— 漏了前者第一次跑会 no such table，
    漏了后者 LangGraph 会拿不到检查点。
    """
    dbeng.init_db()
    from src.session import checkpointer
    await checkpointer.get_checkpointer()

    user = repo.get_user_by_id(user_id)
    if user is None:
        raise ValueError(f"用户不存在: {user_id}")

    from src.session import service
    return await service.send_message(user, session_id, query, tier)


@app.task(bind=True, name="mingjian.message", acks_late=True,
          soft_time_limit=settings.task_soft_time_limit,
          time_limit=settings.task_time_limit)
def run_message(self, task_id: str, user_id: str, session_id: str,
                query: str, tier: str) -> Dict[str, Any]:
    """
    执行一轮对话。参数全是 id 与字符串：**不传对象** ——
    Celery 的消息要走 JSON 序列化，传 dict/对象会出现"本地能跑、队列里跑不了"的怪问题。
    """
    repo.update_task(task_id, status="running", celery_id=self.request.id or "")

    try:
        result = _get_loop().run_until_complete(
            _run_with_heartbeat(task_id, _arun(user_id, session_id, query, tier)))
    except SoftTimeLimitExceeded:
        repo.update_task(task_id, status="failure",
                         error=f"任务超过软时限 {settings.task_soft_time_limit}s 被终止")
        raise
    except Exception as e:                      # noqa: BLE001
        # 记录后**继续抛出**：让 Celery 知道任务失败（配合 acks_late 决定是否重投递）
        repo.update_task(task_id, status="failure", error=f"{type(e).__name__}: {e}")
        raise

    repo.update_task(task_id, status="success", result=result)
    return {"ok": True, "task_id": task_id}
