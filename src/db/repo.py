"""
数据访问层

两条硬规则（这是多用户隔离的最后一道防线）：
1. **所有查询必须带 user_id 条件**。本文件不提供任何"只按 session_id 查"的方法，
   从 API 上杜绝了"换个 id 就能看别人数据"的可能。
2. **越权必须抛异常，不能返回 None 了事**。返回 None 时调用方容易写成
   `if not row: return {"error": "not found"}`，于是"不存在"和"无权访问"混为一谈，
   反而泄漏了"该资源存在"这一信息。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

import sqlalchemy as sa

from src.db import engine as db
from src.db.engine import (audit, files, memories, memory_extracts, messages,
                           org_members, orgs, sessions, tasks, usage, users)
from src.identity import keys


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------
def create_user(name: str, role: str = "user") -> Dict[str, Any]:
    """
    创建用户并返回 (user, 明文 API Key)。

    明文 Key 只在此处返回一次，之后只剩哈希 —— 调用方（CLI / 管理接口）
    必须立刻展示给用户，不要写进日志。
    """
    uid = keys.new_id("user")
    plain = keys.new_api_key()
    db.execute(users.insert().values(
        id=uid,
        name=name or uid,
        api_key_hash=keys.hash_key(plain),
        api_key_prefix=keys.key_prefix(plain),
        role=role,
        status="active",
    ))
    return {"user": get_user_by_id(uid), "api_key": plain}


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    return db.fetch_one(users.select().where(users.c.id == user_id))


def get_user_by_key(plain: str) -> Optional[Dict[str, Any]]:
    """按明文 API Key 找用户。只对 active 用户生效。"""
    if not plain:
        return None
    return db.fetch_one(
        users.select().where(
            (users.c.api_key_hash == keys.hash_key(plain)) & (users.c.status == "active")
        )
    )


def list_users() -> List[Dict[str, Any]]:
    return db.fetch_all(users.select().order_by(users.c.created_at.desc()))


def set_user_status(user_id: str, status: str) -> None:
    db.execute(users.update().where(users.c.id == user_id).values(status=status))


def rotate_api_key(user_id: str) -> str:
    plain = keys.new_api_key()
    db.execute(users.update().where(users.c.id == user_id).values(
        api_key_hash=keys.hash_key(plain),
        api_key_prefix=keys.key_prefix(plain),
    ))
    return plain


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
def create_session(user_id: str, title: str = "新对话",
                   preferred_tier: str = "auto") -> Dict[str, Any]:
    sid = keys.new_id("session")
    db.execute(sessions.insert().values(
        id=sid, user_id=user_id, title=title or "新对话",
        preferred_tier=preferred_tier, status="active",
    ))
    return get_session(user_id, sid)


def get_session(user_id: str, session_id: str) -> Dict[str, Any]:
    row = db.fetch_one(sessions.select().where(sessions.c.id == session_id))
    if row is None:
        raise NotFound(f"会话不存在: {session_id}")
    if row["user_id"] != user_id:
        raise Forbidden(f"无权访问会话: {session_id}")
    return row


def list_sessions(user_id: str, status: Optional[str] = "active") -> List[Dict[str, Any]]:
    stmt = sessions.select().where(sessions.c.user_id == user_id)
    if status:
        stmt = stmt.where(sessions.c.status == status)
    return db.fetch_all(stmt.order_by(sessions.c.updated_at.desc()))


def update_session(user_id: str, session_id: str, **fields) -> Dict[str, Any]:
    allowed = {"title", "status", "preferred_tier"}
    patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not patch:
        return get_session(user_id, session_id)
    get_session(user_id, session_id)  # 先做归属校验，防止改到别人的会话
    patch["updated_at"] = sa.func.now()
    db.execute(sessions.update().where(sessions.c.id == session_id).values(**patch))
    return get_session(user_id, session_id)


def delete_session(user_id: str, session_id: str) -> None:
    get_session(user_id, session_id)
    # 显式删子表：SQLite 默认不开启外键级联，不能指望数据库帮我们清
    db.execute(messages.delete().where(messages.c.session_id == session_id))
    db.execute(files.delete().where(files.c.session_id == session_id))
    db.execute(sessions.delete().where(sessions.c.id == session_id))


def touch_session(session_id: str) -> None:
    db.execute(sessions.update().where(sessions.c.id == session_id)
               .values(updated_at=sa.func.now()))


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------
def add_message(session_id: str, user_id: str, role: str, content: str,
                tier: str = "", meta: Optional[Dict[str, Any]] = None) -> int:
    res = db.execute(messages.insert().values(
        session_id=session_id, user_id=user_id, role=role,
        content=content or "", tier=tier, meta=meta or {},
    ))
    # SQLAlchemy Core 的 insert 拿不到 returning 的主键（方言差异），用 max(id) 兜底
    row = db.fetch_one(sa.select(sa.func.max(messages.c.id)).where(messages.c.session_id == session_id))
    return int(row[list(row.keys())[0]] or 0) if row else 0


def list_messages(user_id: str, session_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    get_session(user_id, session_id)
    rows = db.fetch_all(
        messages.select().where(messages.c.session_id == session_id)
        .order_by(messages.c.id.asc())
    )
    return rows[-limit:] if limit else rows


def recent_turns(user_id: str, session_id: str, turns: int,
                 msg_chars: int) -> List[Dict[str, str]]:
    """
    取最近 N 轮对话作为模型上下文。

    两个保护：
    - 只取最近 N 轮，避免长会话把 prompt 撑爆（这是 P1 的粗粒度做法，
      P2 会换成 SummarizationMiddleware 做真正的上下文压缩）
    - 单条超过 msg_chars 的做截断：旧报告动辄几千字，全量塞进去既费钱又稀释注意力
    """
    rows = list_messages(user_id, session_id, limit=turns * 2)
    out = []
    for r in rows:
        content = (r.get("content") or "").strip()
        if not content:
            continue
        if len(content) > msg_chars:
            content = content[:msg_chars] + " …（已截断）"
        out.append({"role": r["role"], "content": content})
    return out[-(turns * 2):]


# ---------------------------------------------------------------------------
# 文件
# ---------------------------------------------------------------------------
def add_file(user_id: str, session_id: str, filename: str,
             storage_path: str, size: int) -> Dict[str, Any]:
    fid = keys.new_id("file")
    db.execute(files.insert().values(
        id=fid, user_id=user_id, session_id=session_id,
        filename=filename, storage_path=storage_path, size=size,
    ))
    return db.fetch_one(files.select().where(files.c.id == fid))


def get_file(user_id: str, file_id: str) -> Dict[str, Any]:
    row = db.fetch_one(files.select().where(files.c.id == file_id))
    if row is None:
        raise NotFound(f"文件不存在: {file_id}")
    if row["user_id"] != user_id:
        raise Forbidden(f"无权访问文件: {file_id}")
    return row


def list_files(user_id: str, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    stmt = files.select().where(files.c.user_id == user_id)
    if session_id:
        stmt = stmt.where(files.c.session_id == session_id)
    return db.fetch_all(stmt.order_by(files.c.created_at.desc()))


def delete_file(user_id: str, file_id: str) -> Dict[str, Any]:
    row = get_file(user_id, file_id)
    db.execute(files.delete().where(files.c.id == file_id))
    return row


# ---------------------------------------------------------------------------
# 用量
# ---------------------------------------------------------------------------
def add_usage(user_id: str, kind: str, amount: int = 1,
              session_id: Optional[str] = None, model: Optional[str] = None) -> None:
    db.execute(usage.insert().values(
        user_id=user_id, session_id=session_id, kind=kind, amount=amount,
        model=model,
    ))


def usage_summary(user_id: str) -> Dict[str, int]:
    rows = db.fetch_all(
        sa.select(usage.c.kind, sa.func.sum(usage.c.amount).label("total"))
        .where(usage.c.user_id == user_id).group_by(usage.c.kind)
    )
    return {r["kind"]: int(r["total"] or 0) for r in rows}


def usage_today(user_id: str, kinds: tuple) -> Dict[str, int]:
    """今日（UTC 日界）某几类用量的合计，用于配额硬限流。

    为什么按 UTC 日界而不是本地时区：服务器可能跨时区部署，
    本地"今天"的边界在不同机器上不一致会导致配额判定飘忽。
    UTC 日界是唯一全局一致、可复现的切分点。
    返回 {kind: count}（只含 kinds 里出现的）。
    """
    day_start = dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    rows = db.fetch_all(
        sa.select(usage.c.kind, sa.func.sum(usage.c.amount).label("total"))
        .where(usage.c.user_id == user_id)
        .where(usage.c.kind.in_(kinds))
        .where(usage.c.created_at >= day_start)
        .group_by(usage.c.kind)
    )
    out = {k: 0 for k in kinds}
    for r in rows:
        out[r["kind"]] = int(r["total"] or 0)
    return out


def seconds_until_utc_midnight() -> int:
    """距离下一个 UTC 0 点的秒数（配额重置倒计时，用于 Retry-After）。"""
    now = dt.datetime.now(dt.timezone.utc)
    midnight = (now + dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - now).total_seconds())


def token_usage(user_id: str, since_days: int = 30) -> Dict[str, int]:
    """P-4：按模型聚合 token 用量（输入/输出分开）。

    用于成本核算：token 数是客观事实，成本换算在 API 层按配置单价做。
    返回 {model: {input_tokens, output_tokens}}
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)
    rows = db.fetch_all(
        sa.select(usage.c.model, usage.c.kind, sa.func.sum(usage.c.amount).label("total"))
        .where(usage.c.user_id == user_id)
        .where(usage.c.kind.in_(["token_input", "token_output"]))
        .where(usage.c.created_at >= cutoff)
        .group_by(usage.c.model, usage.c.kind)
    )
    out: Dict[str, Dict[str, int]] = {}
    for r in rows:
        model = r["model"] or "unknown"
        bucket = out.setdefault(model, {"input_tokens": 0, "output_tokens": 0})
        if r["kind"] == "token_input":
            bucket["input_tokens"] = int(r["total"] or 0)
        elif r["kind"] == "token_output":
            bucket["output_tokens"] = int(r["total"] or 0)
    return out


# ---------------------------------------------------------------------------
# 异步任务（P3）
# ---------------------------------------------------------------------------
# 终态：不会再变的状态。非终态才允许被 dedupe 命中。
TERMINAL_STATUS = ("success", "failure", "revoked")
# P-3：interrupted 是**非终态**（等人工确认后可 resume），但要与 pending/running 区分。
# 不放进 TERMINAL_STATUS —— 它还能继续（resume），不是"不会再变"。
INTERRUPTED = "interrupted"


def create_task(user_id: str, session_id: str, tier: str, query: str,
                dedupe_key: str = "") -> Dict[str, Any]:
    tid = keys.new_id("task")
    db.execute(tasks.insert().values(
        id=tid, user_id=user_id, session_id=session_id, celery_id="",
        status="pending", tier=tier, query=query, dedupe_key=dedupe_key,
    ))
    return get_task(tid)


def get_task(task_id: str) -> Dict[str, Any]:
    row = db.fetch_one(tasks.select().where(tasks.c.id == task_id))
    if row is None:
        raise NotFound(f"任务不存在: {task_id}")
    return row


def get_task_for(user_id: str, task_id: str) -> Dict[str, Any]:
    """带归属校验。别人的任务一律当作不存在，不泄露"它确实存在"。"""
    row = get_task(task_id)
    if row["user_id"] != user_id:
        raise Forbidden(f"无权访问任务: {task_id}")
    return row


def list_tasks(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    return db.fetch_all(
        tasks.select().where(tasks.c.user_id == user_id)
        .order_by(tasks.c.created_at.desc()).limit(limit)
    )


def find_reusable_task(user_id: str, dedupe_key: str,
                       window_sec: int = 120) -> Optional[Dict[str, Any]]:
    """
    幂等查询：同一用户 + 同会话 + 同档位 + 归一化后相同的提问。

    命中规则：
    - **未终止**（pending/running）的任务：无条件复用 —— 这正是要拦的"狂点按钮"
    - **已终止**的任务：只在 `window_sec` 秒内复用（断线重连场景）。
      超过窗口就真的再跑一次，否则用户永远拿不到新答案。

    时间比较放在 Python 里做而不是写进 SQL：SQLite 的 datetime 存成字符串，
    跨方言写时间比较容易踩到格式不一致的坑，这里量级很小（取最近 20 条）。
    """
    if not dedupe_key:
        return None
    rows = db.fetch_all(
        tasks.select().where((tasks.c.user_id == user_id)
                             & (tasks.c.dedupe_key == dedupe_key))
        .order_by(tasks.c.created_at.desc()).limit(20)
    )
    for r in rows:
        if r["status"] not in TERMINAL_STATUS:
            return r
    for r in rows:
        ts = r.get("updated_at") or r.get("created_at")
        if isinstance(ts, dt.datetime):
            now = dt.datetime.now(dt.timezone.utc)
            ref = ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)
            if (now - ref).total_seconds() <= window_sec:
                return r
    return None


def touch_task(task_id: str) -> None:
    """
    心跳：只刷 updated_at，不改状态。

    为什么必须有它：stalled 判定依赖"running 状态多久没更新"。
    没有心跳的话，一个正常的 125 秒任务在跑到第 120 秒时也会被误判成 stalled。
    """
    db.execute(tasks.update().where(tasks.c.id == task_id)
               .values(updated_at=sa.func.now()))


def update_task(task_id: str, **fields) -> Dict[str, Any]:
    allowed = {"celery_id", "status", "result", "error"}
    patch = {k: v for k, v in fields.items() if k in allowed}
    if not patch:
        return get_task(task_id)
    patch["updated_at"] = sa.func.now()
    db.execute(tasks.update().where(tasks.c.id == task_id).values(**patch))
    return get_task(task_id)


# ---------------------------------------------------------------------------
# 长期记忆（P-1）
# ---------------------------------------------------------------------------
def upsert_memory(owner_id: str, scope: str, kind: str, content: str,
                  source_session: str = "", importance: int = 1,
                  memory_id: str = "") -> str:
    """写入一条记忆。同 owner+scope+kind+content 存在时更新 importance，否则新建。

    返回 memory_id。用"内容相等"做去重（而非 exact 匹配）需要额外字段，
    P-1 先做最简：同 content 就更新 importance 取较大值。
    """
    existing = db.fetch_all(
        memories.select()
        .where(memories.c.owner_id == owner_id)
        .where(memories.c.scope == scope)
        .where(memories.c.kind == kind)
        .where(memories.c.content == content)
        .limit(1)
    )
    if existing:
        old = existing[0]
        new_imp = max(old["importance"], importance)
        db.execute(memories.update().where(memories.c.id == old["id"])
                   .values(importance=new_imp, updated_at=sa.func.now()))
        return old["id"]

    mid = memory_id or keys.new_id("mem")
    db.execute(memories.insert().values(
        id=mid, scope=scope, owner_id=owner_id, kind=kind, content=content,
        source_session=source_session or None, importance=importance,
    ))
    return mid


def list_memories(owner_id: str, scope: str = "user", kinds: tuple = (),
                  limit: int = 50) -> List[Dict[str, Any]]:
    """列出一个 owner 的记忆。kinds 为空 = 全部。按 importance 降序、时间倒序。"""
    stmt = (memories.select()
            .where(memories.c.owner_id == owner_id)
            .where(memories.c.scope == scope)
            .order_by(memories.c.importance.desc(), memories.c.updated_at.desc())
            .limit(limit))
    if kinds:
        stmt = stmt.where(memories.c.kind.in_(kinds))
    return db.fetch_all(stmt)


def delete_memory(owner_id: str, memory_id: str) -> bool:
    """删除一条记忆（仅 owner 可删）。返回是否删到。"""
    res = db.execute(memories.delete()
                     .where(memories.c.id == memory_id)
                     .where(memories.c.owner_id == owner_id))
    return bool(res.rowcount)


def get_memory_extract(session_id: str) -> int:
    """该会话已提炼到的消息 id（0 = 还没提炼过）。"""
    rows = db.fetch_all(memory_extracts.select()
                        .where(memory_extracts.c.session_id == session_id).limit(1))
    return int(rows[0]["last_message_id"]) if rows else 0


def set_memory_extract(session_id: str, last_message_id: int) -> None:
    """更新提炼进度（幂等 upsert）。"""
    db.execute(memory_extracts.delete()
               .where(memory_extracts.c.session_id == session_id))
    db.execute(memory_extracts.insert().values(
        session_id=session_id, last_message_id=last_message_id,
    ))


# ---------------------------------------------------------------------------
# 审计日志（P-4）
# ---------------------------------------------------------------------------
def add_audit(user_id: str, action: str, target: Optional[str] = None,
              detail: Optional[str] = None) -> None:
    """记录一条审计日志。失败不抛 —— 审计是旁路，不能因为记日志失败拖垮主操作。"""
    try:
        db.execute(audit.insert().values(
            user_id=user_id, action=action, target=target, detail=detail,
        ))
    except Exception:           # noqa: BLE001 审计失败不影响主流程
        pass


def list_audit(user_id: Optional[str] = None, limit: int = 200,
               action: Optional[str] = None) -> List[Dict[str, Any]]:
    """查询审计日志。user_id 为空 = 全部（仅管理员用）。按时间倒序。"""
    stmt = audit.select().order_by(audit.c.id.desc()).limit(max(1, min(limit, 1000)))
    if user_id:
        stmt = stmt.where(audit.c.user_id == user_id)
    if action:
        stmt = stmt.where(audit.c.action == action)
    return db.fetch_all(stmt)


# ---------------------------------------------------------------------------
# 多租户（P-6）：组织 + 成员。叠加式，不碰现有业务表的 user_id 隔离。
#
# 隔离规则（与现有"个人数据"同一条红线）：
#   1. 查组织详情/成员/用量，必须先确认"当前用户是该组织成员"，否则 Forbidden。
#   2. 越权抛 Forbidden（不是返回 None），避免"不存在"和"无权"混为一谈。
#   3. owner 唯一、且不可被移除（移除 owner 等于解散组织，走 delete_org）。
# ---------------------------------------------------------------------------
ORG_ROLES = ("owner", "admin", "member")


def create_org(user_id: str, name: str) -> Dict[str, Any]:
    """创建组织，创建者自动成为 owner。"""
    oid = keys.new_id("org")
    db.execute(orgs.insert().values(id=oid, name=name or "未命名组织", owner_id=user_id))
    db.execute(org_members.insert().values(org_id=oid, user_id=user_id, role="owner"))
    return get_org(user_id, oid)


def get_org(user_id: str, org_id: str) -> Dict[str, Any]:
    """查组织详情。非成员一律 Forbidden（不泄露组织是否存在）。"""
    _require_member(org_id, user_id)
    row = db.fetch_one(orgs.select().where(orgs.c.id == org_id))
    if row is None:
        raise NotFound(f"组织不存在: {org_id}")
    return row


def list_orgs(user_id: str) -> List[Dict[str, Any]]:
    """列出用户所属的组织（含角色）。"""
    rows = db.fetch_all(
        sa.select(orgs, org_members.c.role)
        .join(org_members, orgs.c.id == org_members.c.org_id)
        .where(org_members.c.user_id == user_id)
        .order_by(orgs.c.created_at.desc())
    )
    out = []
    for r in rows:
        item = dict(r)
        item["role"] = item.pop("role", "member")
        out.append(item)
    return out


def delete_org(user_id: str, org_id: str) -> None:
    """删除组织。仅 owner 可删。成员关系与组织级记忆一并清理。"""
    role = _role_in(org_id, user_id)
    if role != "owner":
        raise Forbidden("仅组织 owner 可删除组织")
    # 清理组织级记忆（scope=org, owner_id=org_id）
    db.execute(memories.delete()
               .where(memories.c.scope == "org")
               .where(memories.c.owner_id == org_id))
    db.execute(org_members.delete().where(org_members.c.org_id == org_id))
    db.execute(orgs.delete().where(orgs.c.id == org_id))


def add_member(org_id: str, user_id: str, role: str = "member") -> None:
    """加入组织。role 仅允许 admin/member（owner 只能由创建产生）。"""
    if role not in ("admin", "member"):
        raise ValueError("成员角色只能是 admin 或 member")
    db.execute(org_members.insert().values(org_id=org_id, user_id=user_id, role=role))


def list_members(org_id: str) -> List[Dict[str, Any]]:
    rows = db.fetch_all(
        sa.select(org_members.c.user_id, org_members.c.role, org_members.c.created_at,
                  users.c.name)
        .join(users, org_members.c.user_id == users.c.id)
        .where(org_members.c.org_id == org_id)
        .order_by(org_members.c.created_at.asc())
    )
    return [dict(r) for r in rows]


def get_membership(org_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    return db.fetch_one(org_members.select().where(
        (org_members.c.org_id == org_id) & (org_members.c.user_id == user_id)))


def update_member_role(org_id: str, user_id: str, role: str) -> None:
    """改成员角色。仅 owner/admin 可操作；不能改 owner；不能把别人升成 owner。"""
    if role not in ("admin", "member"):
        raise ValueError("角色只能是 admin 或 member（owner 不可授予）")
    cur = get_membership(org_id, user_id)
    if cur is None:
        raise NotFound(f"成员不存在: {user_id}")
    if cur["role"] == "owner":
        raise Forbidden("不能修改 owner 的角色")
    db.execute(org_members.update()
               .where((org_members.c.org_id == org_id) & (org_members.c.user_id == user_id))
               .values(role=role))


def remove_member(org_id: str, user_id: str) -> None:
    """移除成员。不能移除 owner。"""
    cur = get_membership(org_id, user_id)
    if cur is None:
        raise NotFound(f"成员不存在: {user_id}")
    if cur["role"] == "owner":
        raise Forbidden("不能移除组织 owner（如需解散请删除组织）")
    db.execute(org_members.delete().where(
        (org_members.c.org_id == org_id) & (org_members.c.user_id == user_id)))


def org_member_ids(org_id: str) -> List[str]:
    """组织的所有成员 user_id（用于组织级用量聚合）。"""
    rows = db.fetch_all(org_members.select().where(org_members.c.org_id == org_id))
    return [r["user_id"] for r in rows]


def org_token_usage(org_id: str, since_days: int = 30) -> Dict[str, int]:
    """组织级 token 用量聚合：把组织所有成员的 token 记账汇总。

    复用 usage 表的客观记账，只是聚合维度从"单用户"换成"组织成员集合"。
    """
    member_ids = org_member_ids(org_id)
    if not member_ids:
        return {}
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)
    rows = db.fetch_all(
        sa.select(usage.c.model, usage.c.kind, sa.func.sum(usage.c.amount).label("total"))
        .where(usage.c.user_id.in_(member_ids))
        .where(usage.c.kind.in_(["token_input", "token_output"]))
        .where(usage.c.created_at >= cutoff)
        .group_by(usage.c.model, usage.c.kind)
    )
    out: Dict[str, Dict[str, int]] = {}
    for r in rows:
        model = r["model"] or "unknown"
        bucket = out.setdefault(model, {"input_tokens": 0, "output_tokens": 0})
        if r["kind"] == "token_input":
            bucket["input_tokens"] = int(r["total"] or 0)
        elif r["kind"] == "token_output":
            bucket["output_tokens"] = int(r["total"] or 0)
    return out


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------
def _role_in(org_id: str, user_id: str) -> Optional[str]:
    m = get_membership(org_id, user_id)
    return m["role"] if m else None


def _require_member(org_id: str, user_id: str) -> str:
    """确认 user 是 org 成员，返回其角色；否则 Forbidden。"""
    role = _role_in(org_id, user_id)
    if role is None:
        raise Forbidden(f"无权访问组织: {org_id}")
    return role
