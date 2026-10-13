"""
任务派发与查询（P3）

对外只暴露四个动作：create / get / list / cancel。
状态机与幂等都收在这里，API 层不碰 Celery 任何对象 ——
这样即使将来把 Celery 换成别的队列，改的也只有这一个文件。
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import hashlib
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar

from src.cache.redis_client import normalize_query
from src.config import settings
from src.db import repo

TERMINAL = repo.TERMINAL_STATUS

_T = TypeVar("_T")


async def _run_off_loop(coro_factory: Callable[[], Awaitable[_T]]) -> _T:
    """在**独立线程的新事件循环**里跑一个协程（eager 模式专用）。

    🔴 为什么不能直接 `await coro_factory()`：
    eager 模式（没配 Redis broker）下任务是内联执行的，而 research 档实测 **125 秒**。
    在当前事件循环里 await 它 = 把 FastAPI 的 event loop 整整占死 125 秒，
    期间这台机器上**所有请求**（包括 /health 探活）全部排队等待。
    Celery 路径不存在这个问题，因为任务在 worker 进程里跑。

    为什么是新循环 + 显式 copy_context：
      - `_arun` 内部有 `asyncio.run()` 之类的调用，在已运行的循环里会抛
        "cannot be called from a running event loop"，所以必须换线程开新循环；
      - 工具层靠 ContextVar 拿 user_id / session_id（见 src/context.py），
        而 contextvars 不会跨线程自动继承 —— 必须显式 copy_context().run()。
        漏了这一步，任务里的文件会写到"上一个用户"的目录里。
    """
    ctx = contextvars.copy_context()

    def _runner() -> _T:
        return ctx.run(lambda: asyncio.run(coro_factory()))

    return await asyncio.to_thread(_runner)


def dedupe_key_for(user_id: str, session_id: str, tier: str, query: str) -> str:
    """
    幂等键。

    注意用的是**归一化后**的提问：多打两个空格、大小写不同，
    在用户看来是同一个请求，在字符串比较里却是两个。
    不归一化的话，"用户狂点两下"就会真的跑两个 125 秒的调研任务。
    """
    raw = f"{user_id}|{session_id}|{tier}|{normalize_query(query)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _age_sec(row: Dict[str, Any]) -> float:
    ts = row.get("updated_at") or row.get("created_at")
    if ts is None:
        return 0.0
    if isinstance(ts, dt.datetime):
        if ts.tzinfo is None:                 # SQLite 可能返回 naive datetime
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - ts).total_seconds())
    return 0.0


def _apply_stall(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Celery 没有 stalled 这个概念：worker 被 kill 时它只知道"任务还在跑"。

    我们的判断依据很朴素 —— running 状态超过 task_stall_sec 还没更新过，
    那这个 worker 大概已经不在了（容器 OOM / 被 kill / 机器重启）。
    显式标出来，前端才不会一直转圈等一个永远不会回来的结果。
    """
    if row["status"] == "running" and _age_sec(row) > settings.task_stall_sec:
        row = repo.update_task(row["id"], status="stalled",
                               error=f"超过 {settings.task_stall_sec}s 未更新，worker 可能已终止")
    return row


async def create(user: Dict[str, Any], session_id: str, query: str,
                 tier: str) -> Tuple[Dict[str, Any], bool]:
    """
    建任务 + 派发。返回 (任务行, 是否复用了已有任务)。

    幂等规则：同一个用户在同一会话、同档位、归一化后相同的提问，
    只要上一个任务还没终止，就直接返回那一个，不派第二个。

    两条执行路径**共用同一个 _arun**，保证 eager 与真实队列行为一致：
      - eager（没配 broker）：在当前事件循环里 await。
        **不能直接调 Celery 的 delay()** —— 那时正处在运行中的事件循环里，
        任务函数内的 asyncio.run() 会抛 "cannot be called from a running event loop"。
      - 正常：丢给 Celery，worker 进程里 asyncio.run(_arun(...))。
    """
    user_id = user["id"]
    dkey = dedupe_key_for(user_id, session_id, tier, query)

    existing = repo.find_reusable_task(user_id, dkey, settings.task_dedupe_window)
    if existing is not None:
        return _apply_stall(existing), True

    row = repo.create_task(user_id, session_id, tier, query, dkey)

    if settings.eager:
        from src.tasks.jobs import _arun
        repo.update_task(row["id"], status="running", celery_id="eager")
        try:
            # 见 _run_off_loop 的注释：125s 的长任务不能占死事件循环
            result = await _run_off_loop(
                lambda: _arun(user_id, session_id, query, tier))
        except Exception as e:                  # noqa: BLE001
            repo.update_task(row["id"], status="failure",
                             error=f"{type(e).__name__}: {e}")
            return _apply_stall(repo.get_task(row["id"])), False
        repo.update_task(row["id"], status="success", result=result)
        return _apply_stall(repo.get_task(row["id"])), False

    from src.tasks.jobs import run_message
    async_result = run_message.delay(row["id"], user_id, session_id, query, tier)
    repo.update_task(row["id"], celery_id=getattr(async_result, "id", "") or "")
    return _apply_stall(repo.get_task(row["id"])), False


def get(user_id: str, task_id: str) -> Dict[str, Any]:
    return _apply_stall(repo.get_task_for(user_id, task_id))


def mark_interrupted(user_id: str, task_id: str, checkpoint_id: str,
                     interrupt_payload: Dict[str, Any]) -> Dict[str, Any]:
    """P-3：把任务标记为 interrupted（等人工确认），记录断点。

    checkpoint_id + interrupt_payload 是 P-1 就在 tasks 表预留的字段，
    这里才真正写入 —— resume 时靠它们从断点续跑。
    状态转 interrupted（非终态），前端轮询到后展示确认 UI。
    """
    repo.update_task(task_id, status=repo.INTERRUPTED,
                     checkpoint_id=checkpoint_id,
                     interrupt_payload=interrupt_payload or None)
    return repo.get_task_for(user_id, task_id)


def list_for(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    return [_apply_stall(r) for r in repo.list_tasks(user_id, limit)]


def cancel(user_id: str, task_id: str) -> Dict[str, Any]:
    """
    取消任务。

    现实约束（要如实告诉调用方，不能假装一定撤得回来）：
    - 已经跑完的任务取消没有意义 → 409
    - eager 模式（任务在当前进程内联执行，没有 broker）撤不回来 → 409
    - 正在跑的任务：revoke 只是"不再等它"，worker 里已经在跑的那一次
      仍会跑完（terminate=True 会发 SIGTERM，但不保证立刻停）
    """
    row = repo.get_task_for(user_id, task_id)
    if row["status"] in TERMINAL:
        return {"ok": False, "reason": "already_finished", "status": row["status"]}

    if settings.eager:
        repo.update_task(task_id, status="revoked", error="eager 模式下任务已内联执行，无法取消")
        return {"ok": False, "reason": "eager_mode", "status": "revoked"}

    try:
        from src.tasks.celery_app import app
        app.control.revoke(row.get("celery_id") or task_id, terminate=True, signal="SIGTERM")
    except Exception as e:                      # noqa: BLE001
        repo.update_task(task_id, status="failure", error=f"取消失败: {e}")
        return {"ok": False, "reason": "revoke_failed", "status": "failure"}

    repo.update_task(task_id, status="revoked", error="用户取消")
    return {"ok": True, "reason": "revoked", "status": "revoked"}


async def retry(user_id: str, task_id: str) -> Tuple[bool, Dict[str, Any]]:
    """
    重跑一个 stalled / failure 的任务。返回 (是否受理, 任务行)。

    **为什么要有这个接口**（这是实测逼出来的，不是设计出来的）：
    我把 worker 用 SIGKILL 干掉（等价于容器 OOM），然后发现任务**卡在 Redis 的
    `unacked` 集合里再也没回来** —— 队列长度一直是 0，等了 2 分钟也没有重新投递。
    原因：`task_reject_on_worker_lost` 只覆盖"worker 父进程还在、子进程死了"这一种
    （比如任务自己 OOM / 段错误），**整个容器没了就没人去 reject 那条消息**。

    所以"任务不丢"不能只靠 broker —— 应用层必须给一个兜底出口：
    用户看到 stalled 就点重试，幂等键保证不会跑出两份。
    """
    row = repo.get_task_for(user_id, task_id)
    if row["status"] not in ("stalled", "failure"):
        return False, row

    repo.update_task(task_id, status="pending", error=None)
    user = repo.get_user_by_id(user_id) or {"id": user_id}
    if settings.eager:
        from src.tasks.jobs import _arun
        try:
            result = await _run_off_loop(
                lambda: _arun(user_id, row["session_id"], row["query"], row["tier"]))
        except Exception as e:                  # noqa: BLE001
            repo.update_task(task_id, status="failure",
                             error=f"{type(e).__name__}: {e}")
            return True, repo.get_task(task_id)
        repo.update_task(task_id, status="success", result=result)
        return True, repo.get_task(task_id)

    from src.tasks.jobs import run_message
    ar = run_message.delay(task_id, user_id, row["session_id"], row["query"], row["tier"])
    repo.update_task(task_id, celery_id=getattr(ar, "id", "") or "")
    return True, repo.get_task(task_id)


async def resume(user_id: str, task_id: str,
                 confirmation: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """P-3：从 interrupt 断点续跑一个任务。返回 (是否受理, 任务行)。

    完整流程：
      1. 校验任务处于 interrupted 状态，且有 checkpoint_id；
      2. 用 Command(resume=confirmation) 续跑编排图，拿回确认后的规划；
      3. 把确认后的 plan 拼进 query，重新派发主图（run_agent）。

    为什么 resume 后要重跑主图而不是"继续上一次的图"：
    中断点放在**编排层的 plan 确认**，不在主图里。主图（deepagents 的 ReAct）
    还没开始跑，所以 resume 是"带着确认后的规划重新派发主图"，
    不是"在主图中间继续"——这是编排前置层设计带来的简洁性。
    """
    row = repo.get_task_for(user_id, task_id)
    if row["status"] != repo.INTERRUPTED:
        return False, row
    if not row.get("checkpoint_id"):
        repo.update_task(task_id, status="failure",
                         error="任务缺少 checkpoint_id，无法续跑")
        return False, repo.get_task(task_id)

    thread_id = row.get("session_id") or task_id  # 编排图的 thread 用 task_id 隔离更清晰
    try:
        from src.agent import orchestrator
        plan_result = await orchestrator.resume_plan(
            thread_id, row["checkpoint_id"], confirmation)
        plan_text = plan_result.get("plan_text", "")
    except Exception as e:      # noqa: BLE001 resume 编排失败 → 任务失败
        repo.update_task(task_id, status="failure",
                         error=f"续跑编排失败: {type(e).__name__}: {e}")
        return False, repo.get_task(task_id)

    # 确认后的 query：规划拼在前面
    new_query = row["query"]
    if plan_text:
        new_query = f"{plan_text}\n\n{row['query']}"

    # 重新派发主图（复用 retry 的派发逻辑，但 query 带上了确认后的规划）
    repo.update_task(task_id, status="pending", checkpoint_id=None,
                     interrupt_payload=None, query=new_query, error=None)
    user = repo.get_user_by_id(user_id) or {"id": user_id}
    if settings.eager:
        from src.tasks.jobs import _arun
        try:
            result = await _run_off_loop(
                lambda: _arun(user_id, row["session_id"], new_query, row["tier"]))
        except Exception as e:                  # noqa: BLE001
            repo.update_task(task_id, status="failure",
                             error=f"{type(e).__name__}: {e}")
            return True, repo.get_task(task_id)
        repo.update_task(task_id, status="success", result=result)
        return True, repo.get_task(task_id)

    from src.tasks.jobs import run_message
    ar = run_message.delay(task_id, user_id, row["session_id"], new_query, row["tier"])
    repo.update_task(task_id, celery_id=getattr(ar, "id", "") or "")
    return True, repo.get_task(task_id)


def public(row: Dict[str, Any], with_result: bool = True) -> Dict[str, Any]:
    """对外序列化：datetime → ISO，结果可选（列表接口不返回大报告体）。"""
    out: Dict[str, Any] = {
        "id": row["id"],
        "session_id": row["session_id"],
        "tier": row["tier"],
        "query": row["query"],
        "status": row["status"],
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }
    if row.get("error"):
        out["error"] = row["error"]
    # P-3：interrupted 状态时把"待确认的规划"带出来，前端据此渲染确认 UI。
    # interrupt_payload 存的是 {type, plan, hint}（见 orchestrator.confirm 节点），
    # 只序列化公开字段，checkpoint_id 不外泄（resume 由服务端按 task_id 找断点）。
    if row.get("status") == repo.INTERRUPTED and row.get("interrupt_payload"):
        out["interrupt"] = row["interrupt_payload"]
    if with_result and row.get("result"):
        out["result"] = row["result"]
    return out


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if isinstance(v, dt.datetime) else None
