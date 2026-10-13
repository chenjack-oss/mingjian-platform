"""
数据层：引擎 + 表定义 + 建表

为什么用 SQLAlchemy Core 而不是裸 SQL：
    我们要同时支持 Postgres（生产）和 SQLite（本地单测），
    两者在自增主键、JSON 类型、now() 上写法不同。
    用 Core 的 MetaData 让方言差异由 SQLAlchemy 吸收，业务代码零分支。

为什么"会话/消息/文件/用量"要单独建表，而不是全塞进 LangGraph 的 checkpoint：
    checkpoint 是**图的内部状态**（blob），不可查询、不可跨版本演进；
    而"列出我的会话""搜索历史消息""统计用量"是产品功能，必须是可查询的行。
    两者职责不同：checkpoint 管对话图状态，业务表管产品数据。
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, List

import sqlalchemy as sa
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import CursorResult

from src.config import settings

# ---------------------------------------------------------------------------
# 表定义
# ---------------------------------------------------------------------------
metadata = sa.MetaData()

users = sa.Table(
    "users", metadata,
    sa.Column("id", sa.String(64), primary_key=True),           # u_xxxx
    sa.Column("name", sa.String(128), nullable=False, default=""),
    sa.Column("api_key_hash", sa.String(128), nullable=False, unique=True),
    sa.Column("api_key_prefix", sa.String(32), nullable=False),  # 仅用于人眼识别，不可用于登录
    sa.Column("role", sa.String(32), nullable=False, default="user"),
    sa.Column("status", sa.String(32), nullable=False, default="active"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)

sessions = sa.Table(
    "sessions", metadata,
    sa.Column("id", sa.String(64), primary_key=True),            # s_xxxx
    sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False, index=True),
    sa.Column("title", sa.String(256), nullable=False, default="新对话"),
    sa.Column("preferred_tier", sa.String(32), nullable=False, default="auto"),
    sa.Column("status", sa.String(32), nullable=False, default="active"),  # active / archived
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)

messages = sa.Table(
    "messages", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id"), nullable=False, index=True),
    sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False, index=True),
    sa.Column("role", sa.String(16), nullable=False),            # user / assistant / system
    sa.Column("content", sa.Text, nullable=False, default=""),
    sa.Column("tier", sa.String(32), nullable=False, default=""),     # 产生这条回复的档位
    sa.Column("meta", sa.JSON, nullable=True),                        # badge / degradation / sources
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)

files = sa.Table(
    "files", metadata,
    sa.Column("id", sa.String(64), primary_key=True),            # f_xxxx
    sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False, index=True),
    sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id"), nullable=False, index=True),
    sa.Column("filename", sa.String(256), nullable=False),
    sa.Column("storage_path", sa.String(512), nullable=False),
    sa.Column("size", sa.Integer, nullable=False, default=0),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)

usage = sa.Table(
    "usage", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False, index=True),
    sa.Column("session_id", sa.String(64), nullable=True),
    sa.Column("kind", sa.String(32), nullable=False),            # message / search / upload / token_input / token_output
    sa.Column("amount", sa.Integer, nullable=False, default=1),
    # P-4：成本核算用。token 记账时记下模型名，成本换算才能按模型区分单价。
    # 非 token 记账（message/search 等）留空。
    sa.Column("model", sa.String(64), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)

# ---------------------------------------------------------------------------
# 异步任务（P3）
#
# 为什么单独建表而不是用 Celery 的 result backend：
#   backend 要么不配（查不到状态），要么配 Redis（查得到但不可查询、不持久）。
#   而"我上周跑的那份调研报告呢"是**产品需求**，必须是可查询的行。
#   状态机与结果都在这张表里，Celery 只负责"把活派出去"。
# ---------------------------------------------------------------------------
tasks = sa.Table(
    "tasks", metadata,
    sa.Column("id", sa.String(64), primary_key=True),            # t_xxxx
    sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False, index=True),
    sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id"), nullable=False, index=True),
    sa.Column("celery_id", sa.String(64), nullable=False, default=""),
    sa.Column("status", sa.String(16), nullable=False, default="pending", index=True),
    # pending / running / success / failure / revoked / stalled
    sa.Column("tier", sa.String(32), nullable=False, default=""),
    sa.Column("query", sa.Text, nullable=False, default=""),
    # 幂等键 = sha256(user|session|tier|归一化提问)。
    # 用户狂点按钮、前端断线重连都会重复提交，靠它把重复请求收敛到同一个任务。
    sa.Column("dedupe_key", sa.String(64), nullable=False, default="", index=True),
    sa.Column("result", sa.JSON, nullable=True),
    sa.Column("error", sa.Text, nullable=True),
    # P-1 预留：任务级 interrupt/resume 升级位。
    # 现在（会话恢复阶段）置空；P-3 落地 LangGraph interrupt() 时，
    # 把图当前 checkpoint_id 写进来，resume 时从它续跑 —— 不用改表、不用迁移。
    # 为什么放在 tasks 而不是 sessions：中断续跑是"任务"级概念，
    # 一个会话可能有多个任务（多次 research），各自的中断点独立。
    sa.Column("checkpoint_id", sa.String(64), nullable=True, default=None),
    # P-1 预留：中断时前端要展示"卡在哪、需要用户补什么"，把 interrupt payload
    # 存这里（JSON），resume 时原样回传。现在置空。
    sa.Column("interrupt_payload", sa.JSON, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)

# 长期记忆（P-1 记忆底座）：用户级 + 组织级共享
# scope：user（跨会话的用户事实/偏好/情节）/ org（组织内共享事实）
# 设计要点：提取是异步的（复用 Celery），不阻塞主链路；注入时"无关请忽略"。
memories = sa.Table(
    "memories", metadata,
    sa.Column("id", sa.String(64), primary_key=True),           # m_xxxx
    sa.Column("scope", sa.String(16), nullable=False, default="user", index=True),  # user / org
    sa.Column("owner_id", sa.String(64), nullable=False, index=True),  # user_id 或 org_id
    sa.Column("kind", sa.String(32), nullable=False),            # fact / preference / episode
    sa.Column("content", sa.Text, nullable=False),               # 提炼后的原子事实
    sa.Column("source_session", sa.String(64), nullable=True),   # 溯源
    sa.Column("importance", sa.Integer, nullable=False, default=1),  # 1~5
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)

# 记忆提取的幂等键：记录每个会话已提炼到哪条消息，避免重复/并发重复提炼
memory_extracts = sa.Table(
    "memory_extracts", metadata,
    sa.Column("session_id", sa.String(64), primary_key=True),
    sa.Column("last_message_id", sa.Integer, nullable=False, default=0),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)

# 审计日志（P-4）：关键操作留痕，中台"可运营"的合规底线。
# 记录谁在什么时候做了什么（动作 + 对象 + 结果），支持事后追查。
audit = sa.Table(
    "audit", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False, index=True),
    sa.Column("action", sa.String(64), nullable=False),           # user_create / task_cancel / memory_delete ...
    sa.Column("target", sa.String(128), nullable=True),           # 操作对象 id（session_id/task_id/memory_id）
    sa.Column("detail", sa.Text, nullable=True),                  # 补充上下文（JSON 字符串）
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)

# ---------------------------------------------------------------------------
# 多租户（P-6）：组织 + 成员。
#
# 设计决策（叠加式，而非全量 org_id 隔离）：
#   现有 7 张业务表（sessions/messages/files/usage/tasks/memories/audit）都按
#   user_id 做个人私有隔离，这是**正确且测试覆盖**的。多租户不推倒它们，
#   而是在其上叠加"组织 = 成员的集合"这一层关系：
#     - 个人数据保持私有（一个用户换个组织不改变他私有的会话）
#     - 组织能力靠"显式共享"叠加（组织级记忆 scope=org、组织级用量聚合）
#   为什么不做"所有表加 org_id 全量隔离"：那是"团队空间"产品形态，
#   会和"个人私有"混用，既破坏现有语义，又牵动 46 处 user_id 改动、回归风险极高。
#   叠加式是很多现代 SaaS 的真实做法（个人空间 + 团队空间并存）。
# ---------------------------------------------------------------------------
orgs = sa.Table(
    "orgs", metadata,
    sa.Column("id", sa.String(64), primary_key=True),             # o_xxxx
    sa.Column("name", sa.String(128), nullable=False),
    sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False, index=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)

org_members = sa.Table(
    "org_members", metadata,
    sa.Column("org_id", sa.String(64), sa.ForeignKey("orgs.id"), primary_key=True),
    sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), primary_key=True),
    # owner（创建者，唯一）/ admin（可管成员）/ member（普通成员）
    sa.Column("role", sa.String(16), nullable=False, default="member"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
_engine: Engine | None = None
# 必须是可重入锁：init_db() 持锁后还会调用 get_engine()，
# 而 get_engine() 内部也要拿同一把锁。用普通 Lock 会直接死锁
# （实测现象：init_db() 永久卡住，进程被杀，无任何报错）。
_init_lock = threading.RLock()
_initialized = False


def _ensure_parent_dir(url: str) -> None:
    """SQLite 路径的父目录要先建出来，否则 connect 会报 unable to open database file。"""
    if not url.startswith("sqlite"):
        return
    # sqlite:///relative/path.db 或 sqlite:////abs/path.db
    path = url.split("///", 1)[-1]
    if not path or path == ":memory:":
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _init_lock:
            if _engine is None:
                _ensure_parent_dir(settings.db_url)
                kwargs: Dict[str, Any] = {"echo": settings.db_echo, "future": True}
                if settings.is_postgres:
                    kwargs.update({"pool_pre_ping": True, "pool_size": 5, "max_overflow": 10})
                else:
                    # SQLite 在多线程下需要关闭同线程检查（FastAPI 线程池会跨线程复用连接）
                    kwargs.update({"connect_args": {"check_same_thread": False}})
                _engine = create_engine(settings.db_url, **kwargs)
    return _engine


def init_db() -> None:
    """建表（幂等）。生产建议后续换 Alembic 做版本化迁移。"""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        _ensure_parent_dir(settings.db_url)
        metadata.create_all(get_engine())
        _initialized = True


def execute(stmt) -> CursorResult:
    with get_engine().begin() as conn:
        return conn.execute(stmt)


def fetch_all(stmt) -> List[Dict[str, Any]]:
    with get_engine().begin() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


def fetch_one(stmt) -> Dict[str, Any] | None:
    with get_engine().begin() as conn:
        row = conn.execute(stmt).mappings().first()
    return dict(row) if row else None


def in_transaction(fn):
    """
    在一个事务里执行若干写操作。

    为什么需要它：创建会话 + 写入首条系统消息这类"要么都成要么都不成"的操作，
    不能分成两次自动提交，否则中间失败会留下孤儿会话。
    """
    with get_engine().begin() as conn:
        return fn(conn)
