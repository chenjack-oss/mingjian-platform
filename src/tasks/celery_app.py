"""
Celery 应用（P3：异步长任务）

**为什么明辨真的需要它**：research 档实测 125 秒，远超 Nginx 与浏览器的默认超时。
同步接口在这个时长下必死（网关 499 / 前端超时重发），所以长任务必须异步化。
这不是"为了加 Celery 而加"。

**关键配置每一条都有理由**：
- `task_acks_late=True`：任务**执行完**才 ack。默认是收到就 ack，
  worker 中途 OOM 或被 kill 时任务就永久消失了。
- `task_reject_on_worker_lost=True`：worker 进程被硬杀（SIGKILL / 容器 OOM）
  时把任务退回队列，配合 acks_late 才能真正做到"不丢任务"。
- `worker_prefetch_multiplier=1`：一次只预取 1 个任务。默认预取 4×并发数，
  一个 125 秒的调研任务会把整个 worker 队列堵死，其他用户的短任务排不上。
- `visibility_timeout`：Redis broker 下，任务被取出后超过这个时间没 ack 就**重新投递**。
  必须 > 任务最长耗时（实测 125s，这里给 900s），否则任务会被重复执行。
- `result_backend=None` + `task_ignore_result=True`：**结果不进 Redis**，
  我们自己写 Postgres 的 tasks 表 —— 报告要能翻历史、要可查询，Redis 满足不了。
"""

from __future__ import annotations

from celery import Celery

from src.config import settings

app = Celery("mingjian", broker=settings.broker_url, backend=None)

app.conf.update(
    # ---- 可靠性 ----
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # ---- 调度 ----
    worker_prefetch_multiplier=1,
    task_default_queue="mingjian",
    # ---- 时限 ----
    task_soft_time_limit=settings.task_soft_time_limit,
    task_time_limit=settings.task_time_limit,
    # ---- 结果：自己落 Postgres，不走 Celery backend ----
    result_backend=None,
    task_ignore_result=True,
    task_store_errors_even_if_ignored=False,
    # ---- broker 细节 ----
    broker_transport_options={"visibility_timeout": settings.task_visibility_timeout},
    # ---- 本地/eager ----
    task_always_eager=settings.eager,
    task_eager_propagates=False,
    # ---- 杂项 ----
    timezone="Asia/Shanghai",
    enable_utc=False,
    worker_hijack_root_logger=False,
    # 任务模块必须显式列出：worker 是独立进程，不 import 就不知道有哪些任务。
    # 用 conf.imports 而不是在模块底部 import，是为了避免 celery_app ↔ jobs 循环导入。
    imports=("src.tasks.jobs",),
)
