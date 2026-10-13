"""
明辨平台 · API（P1：身份与会话层）

相对旧项目 api/server_v3.py 的关键改动：
1. **所有业务接口必须登录**：current_user 依赖，不传 Key 直接 401
2. **session_id 由服务端生成**：客户端再也指定不了会话，也猜不到别人的
3. **文件按 user 隔离**：下载走 file_id + 归属校验，不再接受客户端传绝对路径
4. **CORS 可配置**：不再写死 "*"，生产用 MJ_CORS_ORIGINS 指定来源
5. **四档路由**：/v1/tiers 列出档位，发消息时可手动指定 tier 覆盖自动判断
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from src import __version__
from src.api.ratelimit import rate_limit
from src.api.quota import check_quota
from src.cache import health as cache_health
from src.cache import rate_remaining
from src.config import settings
from src.db import engine as dbeng
from src.db import repo
from src.identity import auth, keys
from src.identity.namespace import make_thread_id, storage_dir
from src.routing.tiers import ALL_TIERS, AUTO, RESEARCH, TIER_META, resolve_tier
from src.session import checkpointer, service, stream
from src.tasks import dispatch as tasks

from src.observe.logging_setup import configure_logging, get_logger

logger = get_logger(__name__)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

app = FastAPI(title="明辨平台", version=__version__,
              description="P1 身份与会话层 + P2 执行核 + P3 异步任务/缓存/限流")

def _cors_policy():
    """
    CORS 决策（抽成纯函数是为了能单测 —— 安全策略不该只能靠"起服务看一眼"验证）。

    返回 (origins, allow_credentials, log_level, log_msg)。

    关键修正（原 🟡 风险项）：生产环境必须显式声明来源。
    MJ_ENV=production 且没配 MJ_CORS_ORIGINS 时，旧行为是**静默放行所有来源**
    （fail-open）—— 等于把接口裸奔在公网上，而且因为"能跑"，永远不会被发现。
    现在改成 fail-closed：拒绝一切跨源请求。
    """
    prod = (os.getenv("MJ_ENV") or "").strip().lower() in ("prod", "production")

    if settings.cors_origins != ["*"]:
        return settings.cors_origins, True, None, ""
    if prod:
        return [], False, "error", ("MJ_ENV=production 但未配置 MJ_CORS_ORIGINS："
                                    "已按 fail-closed 拒绝所有跨源请求"
                                    "（设置 MJ_CORS_ORIGINS 后重启生效）")
    # 开发/内网：放行，但必须留痕
    return ["*"], False, "warning", ("未配置 MJ_CORS_ORIGINS，当前放行所有来源"
                                     "（生产环境请显式配置）")


_origins, _allow_cred, _lvl, _msg = _cors_policy()
if _lvl:
    getattr(logger, _lvl)(_msg)
app.add_middleware(CORSMiddleware, allow_origins=_origins,
                   allow_credentials=_allow_cred,
                   allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------
def _ser(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = {}
    for k, v in row.items():
        if isinstance(v, dt.datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _ser_all(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_ser(r) for r in rows if r is not None]


def _public_user(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    对外返回的用户信息，**必须剔除 api_key_hash**。

    密钥哈希虽然不是明文，但把它返回给客户端毫无必要，
    只会给离线爆破提供一个可验证的目标（拿到哈希就能本地反复试）。
    最小暴露原则：客户端只需要 id / name / role / status / key 前缀。
    """
    out = _ser(row) or {}
    out.pop("api_key_hash", None)
    return out


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup() -> None:
    # 幂等。放这里而不是模块顶部：uvicorn --reload 会重复 import，
    # 而 MJ_LOG_LEVEL 只有在真正启动时读一次才符合直觉。
    configure_logging()
    dbeng.init_db()
    await checkpointer.get_checkpointer()
    logger.info(f"{settings.app_name} {__version__} "
                f"| db={settings.db_url.split('://')[0]} "
                f"| checkpointer={checkpointer.kind()}")

    # 🔴 匿名访问 fail-closed：生产环境绝不允许 MJ_ALLOW_ANONYMOUS=1。
    # 一开 = 所有不带 Key 的请求落到同一个 u_anonymous，全量数据互见。
    # 不能靠注释提醒（auth.py 里已经写过"生产绝不能开"，但还是有误开风险），
    # 必须在启动时硬拦截。想调试请用 MJ_ENV=dev。
    prod = (os.getenv("MJ_ENV") or "").strip().lower() in ("prod", "production")
    if prod and settings.allow_anonymous:
        raise RuntimeError(
            "MJ_ENV=production 不允许 MJ_ALLOW_ANONYMOUS=1："
            "匿名模式下所有请求共享 u_anonymous，数据互相可见，"
            "这是多租户隔离的硬缺口。请关闭匿名开关或改用 dev 环境。"
        )


# ---------------------------------------------------------------------------
# 元信息
# ---------------------------------------------------------------------------
@app.on_event("shutdown")
async def on_shutdown() -> None:
    await checkpointer.close()
    # 不关的话 stdio 子进程会变成孤儿进程，重启几次机器上就堆一堆 python
    try:
        from src.mcp_client.client import close_all
        close_all()
    except Exception:
        pass


@app.get("/v1/mcp/tools")
async def list_mcp_tools(user: Dict = Depends(auth.current_user)):
    """列出当前挂载的 MCP 工具（排障用：确认到底挂上了几个、哪些是写操作）。"""
    try:
        from src.mcp_client.client import available_tools
        specs = await available_tools()
    except Exception as e:
        return {"tools": [], "error": f"{type(e).__name__}: {e}"}
    return {"count": len(specs), "tools": [
        {"name": s.name, "server": s.server, "read_only": s.read_only,
         "description": (s.description or "")[:200]} for s in specs]}


@app.get("/health")
async def health():
    ch = cache_health()
    # Langfuse 的客户端是惰性的：这里主动探一次，是为了让 /health 能报出
    # "配了 Key 但连不上" 这种情况 —— 不探的话永远显示 on，排障时反而误导。
    from src.observe import tracing
    lf = tracing.health()
    # MCP 如实上报：server 起不来就是 tools=0，不假装挂上了
    try:
        from src.mcp_client.client import status as mcp_status
        mcp = await mcp_status()
    except Exception as e:
        mcp = {"enabled": False, "tools": 0, "error": f"{type(e).__name__}: {e}"}

    # P7：知识库 HTTP 通道探活（不配就是 off，连不上就是 down）
    try:
        from src.capabilities import agentic_rag
        rag_health = (await agentic_rag.health()) if agentic_rag.available() \
            else {"ok": False, "channel": "off", "reason": "未配置 MJ_AGENTIC_RAG_URL"}
    except Exception as e:
        rag_health = {"ok": False, "channel": "http", "error": f"{type(e).__name__}: {e}"}
    # P9：模型熔断状态。开闸时必须能在 /health 看见 ——
    # 否则"为什么全都快速失败"只能靠猜。
    try:
        from src.llm import resilience
        cb = resilience.health()
    except Exception as e:                  # noqa: BLE001
        cb = {"llm_circuit": "error", "error": f"{type(e).__name__}: {e}"}
    return {
        "status": "ok",
        "service": "mingjian-platform",
        "version": __version__,
        "phase": "中台化 P1-P6",
        "db": settings.db_url.split("://")[0],
        "checkpointer": checkpointer.kind(),
        "llm_ready": bool(settings.llm_api_key and settings.llm_base_url),
        "tiers": ALL_TIERS,
        "anonymous_allowed": settings.allow_anonymous,
        # P3 三项如实上报：没配 Redis 就是 off，连不上就是 down，绝不粉饰
        "redis": ch["redis"],
        "cache": ch["cache"],
        "rate_limit": ch["rate_limit"],
        "async_mode": "eager" if settings.eager else "celery",
        "celery_broker": settings.broker_url.split("://")[0],
        "mcp": mcp.get("tools", 0),
        "mcp_detail": mcp,
        # P7：知识库两条通道如实上报。
        # 只报"工具数量"是不够的 —— 工具挂上了但服务 502 属于"看起来通了其实没通"，
        # 正是最难排的那类故障。所以这里主动探一次 HTTP 服务。
        "agentic_rag": rag_health,
        "llm_circuit": cb,
        # P4：子智能体动态注册的当前生效名单（含环境变量覆盖 + 禁用名单后的最终结果）。
        # 运维/面试从这里一眼看到"实际装配了哪些能力"，而不是去猜代码里的默认值。
        "subagents": _subagents_health(),
        **lf,
    }


def _subagents_health() -> Dict[str, Any]:
    """子智能体装配的健康视图：总开关、生效名单、按档位派发清单。"""
    from src.agent.subagents import registry
    return {
        "enabled": registry.enabled(),
        "active": registry.enabled_subagents(),
        "by_tier": {t: registry.planned_for(t) for t in ALL_TIERS},
    }


@app.get("/v1/tiers")
async def list_tiers(user: Dict = Depends(auth.current_user)):
    """四档清单（供前端渲染切换器）。"""
    return {"tiers": [{"id": t, **TIER_META[t]} for t in ALL_TIERS], "auto": AUTO}


def _scan_skills() -> List[Dict[str, str]]:
    """
    扫描技能目录，解析每个 SKILL.md 的 YAML frontmatter 头几行。

    为什么不 import src.agent.core 来拿：那会把 deepagents 一并拉进 server 启动链路，
    为了渲染一个列表不值得。这里只读文件头 600 字符，name/version/description 都在前几行。
    """
    # WEB_DIR = <root>/src/web → 项目根是其上两级；skills 目录在项目根下
    skills_dir = Path(os.getenv("MJ_SKILLS_DIR",
                                str(WEB_DIR.parent.parent / "skills")))
    out: List[Dict[str, str]] = []
    if not skills_dir.is_dir():
        return out
    for d in sorted(skills_dir.iterdir()):
        f = d / "SKILL.md"
        if not f.is_file():
            continue
        item = {"id": d.name, "name": d.name, "version": "", "description": ""}
        try:
            head = f.read_text(encoding="utf-8", errors="ignore")[:800]
        except OSError:
            continue
        for line in head.splitlines():
            # 简单解析：只认 "field: value" 形式，frontmatter 的缩进行忽略
            stripped = line.strip()
            for field in ("name", "version", "description"):
                if stripped.startswith(f"{field}:"):
                    item[field] = stripped.split(":", 1)[1].strip()
        out.append(item)
    return out


@app.get("/v1/meta")
async def meta(user: Dict = Depends(auth.current_user)):
    """
    前端渲染所需的平台元信息：模型、技能、能力开关。

    全部**如实上报**：rag 未接入就是 False，检索没配 Key 就是 False，
    前端据此显示"未接入"而不是假装功能存在。
    """
    from src.capabilities.search import available as search_available

    redis_up = cache_health()["redis"] == "up"
    from src.observe import tracing
    return {
        "version": __version__,
        "phase": "P3",
        "model": {"main": settings.llm_model,
                  "fast": settings.llm_model_fast or settings.llm_model},
        "skills": _scan_skills(),
        "features": {
            "web_search": search_available(),
            "files": True,
            "rag": False,        # P4 接入 Agentic RAG 后改 True
            "async_tasks": True,
            "cache": bool(redis_up and settings.cache_enabled),
            "rate_limit": bool(redis_up and settings.rate_enabled),
            "observability": tracing.enabled(),
        },
    }


@app.get("/v1/me")
async def me(user: Dict = Depends(auth.current_user)):
    return {"user": _public_user(user), "usage": repo.usage_summary(user["id"]),
            "rate_remaining": round(rate_remaining(user["id"]), 2)}


# ---------------------------------------------------------------------------
# 用户管理（需管理员令牌）
# ---------------------------------------------------------------------------
class CreateUserIn(BaseModel):
    name: str = ""
    role: str = "user"


@app.post("/v1/users", dependencies=[Depends(auth.require_admin)])
async def create_user(body: CreateUserIn):
    """
    创建用户。返回的 api_key **只出现这一次**，之后系统里只有哈希。
    """
    res = repo.create_user(body.name, body.role)
    repo.add_audit(res["user"]["id"], "user_create", target=res["user"]["id"],
                   detail=f"name={body.name}, role={body.role}")
    return {"user": _public_user(res["user"]), "api_key": res["api_key"],
            "hint": "请立即保存此 Key，系统不再保存明文"}


@app.get("/v1/users", dependencies=[Depends(auth.require_admin)])
async def list_users():
    return {"users": [_public_user(u) for u in repo.list_users()]}


class RotateIn(BaseModel):
    user_id: str


@app.post("/v1/users/rotate-key", dependencies=[Depends(auth.require_admin)])
async def rotate_key(body: RotateIn):
    if repo.get_user_by_id(body.user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    return {"user_id": body.user_id, "api_key": repo.rotate_api_key(body.user_id)}


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
class CreateSessionIn(BaseModel):
    title: str = "新对话"
    preferred_tier: str = AUTO


class UpdateSessionIn(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    preferred_tier: Optional[str] = None


class SendMessageIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=20000)
    tier: Optional[str] = None      # 手动覆盖；不传则走自动
    # 显式异步。字段名用 run_async（async 是 Python 关键字），
    # 但对外接受 "async" —— 前端/文档里写 async 更符合直觉。
    run_async: bool = Field(False, alias="async")

    model_config = {"populate_by_name": True}


@app.post("/v1/sessions")
async def create_session(body: CreateSessionIn, user: Dict = Depends(auth.current_user)):
    if body.preferred_tier not in [AUTO, *ALL_TIERS]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"preferred_tier 必须是 auto 或 {ALL_TIERS}")
    sess = repo.create_session(user["id"], body.title, body.preferred_tier)
    return {"session": _ser(sess), "thread_id": make_thread_id(user["id"], sess["id"])}


@app.get("/v1/sessions")
async def list_sessions(status_filter: Optional[str] = None,
                        user: Dict = Depends(auth.current_user)):
    return {"sessions": _ser_all(repo.list_sessions(user["id"], status_filter))}


@app.get("/v1/sessions/{session_id}")
async def get_session(session_id: str, user: Dict = Depends(auth.current_user)):
    try:
        sess = repo.get_session(user["id"], session_id)
        msgs = repo.list_messages(user["id"], session_id)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该会话")
    return {"session": _ser(sess), "messages": _ser_all(msgs),
            "thread_id": make_thread_id(user["id"], session_id)}


@app.get("/v1/sessions/{session_id}/resume")
async def resume_session(session_id: str, user: Dict = Depends(auth.current_user)):
    """会话恢复入口（P-1）：显式暴露"接续点"，让客户端能接着上次聊。

    为什么单独做一个 resume 而不是复用 get_session：
    get_session 只回产品数据（messages 表），而"能不能真的接续"取决于
    LangGraph 的图状态（checkpoint）是否还在 —— 这是另一个层。
    本接口把两层合成一个"恢复视图"，前端拿一次就知道：
      1. 历史消息（产品层，messages 表）
      2. thread_id（图层的接续键）
      3. checkpointer 类型 + 图状态是否可恢复（生产=postgres 可恢复；内存=重启即丢）
    现在（会话恢复阶段）不碰任务级 interrupt；P-3 会在这里返回 checkpoint_id
    与 interrupt_payload，实现从断点续跑。
    """
    try:
        sess = repo.get_session(user["id"], session_id)
        msgs = repo.list_messages(user["id"], session_id)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该会话")

    thread_id = make_thread_id(user["id"], session_id)
    resumable = False
    cp_kind = "unknown"
    cp_state = None
    try:
        from src.session import checkpointer
        cp_kind = checkpointer.kind()
        # 只有 postgres 才有跨重启恢复能力；内存型重启即丢，如实标注
        saver, _ = await checkpointer.get_checkpointer()
        if cp_kind.startswith("postgres"):
            resumable = True
            # 探测该 thread 是否有 checkpoint（图状态）可读
            try:
                state = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
                if state is not None:
                    cp_state = {
                        "checkpoint_id": getattr(state.config, "configurable", {}).get("checkpoint_id", ""),
                        "has_checkpoint": True,
                    }
            except Exception:   # noqa: BLE001 探测失败只影响 cp_state，不影响恢复视图
                cp_state = {"has_checkpoint": False, "probe_error": True}
    except Exception:           # noqa: BLE001 checkpointer 不可用不致命
        pass

    return {
        "session": _ser(sess),
        "messages": _ser_all(msgs),
        "thread_id": thread_id,
        "resumable": resumable,
        "checkpointer": cp_kind,
        "checkpoint_state": cp_state,
        "note": "resume 通过向 /messages 继续发消息实现；resumable=false 表示图状态会随进程重启丢失"
                "（当前 checkpointer 非 postgres）",
    }


@app.patch("/v1/sessions/{session_id}")
async def patch_session(session_id: str, body: UpdateSessionIn,
                        user: Dict = Depends(auth.current_user)):
    try:
        sess = repo.update_session(user["id"], session_id, **body.model_dump())
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该会话")
    return {"session": _ser(sess)}


@app.delete("/v1/sessions/{session_id}")
async def remove_session(session_id: str, user: Dict = Depends(auth.current_user)):
    try:
        repo.delete_session(user["id"], session_id)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该会话")
    # 清理该会话的引用校验注册表：不清理会只增不减（内存慢泄漏）。
    # key 用的是 make_thread_id（citation_key），与检索时 register_urls 的键一致。
    try:
        from src.capabilities import citation_guard
        citation_guard.clear(make_thread_id(user["id"], session_id))
    except Exception:               # noqa: BLE001 清理失败不影响删除主流程
        pass
    return {"status": "deleted", "session_id": session_id}


@app.post("/v1/sessions/{session_id}/messages")
async def send_message(session_id: str, body: SendMessageIn,
                       user: Dict = Depends(rate_limit),
                       _quota: Dict = Depends(check_quota)):
    try:
        sess = repo.get_session(user["id"], session_id)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该会话")

    try:
        tier = resolve_tier(body.tier, sess.get("preferred_tier", AUTO)).tier
    except ValueError as e:
        # 档位名非法要在这一层就拦成 400：以前是丢给 service 去抛，
        # 但异步分支下 service 根本不会被调用，非法档位会变成 500。
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    # 何时自动转异步：research 档实测 125 秒，远超网关与浏览器超时，同步必死。
    # 手动 async=true 则任何档都能转。
    want_async = body.run_async or (tier == RESEARCH and settings.async_auto_research)
    if want_async:
        # 必须 await：少了 await 会直接把协程对象交给 FastAPI 序列化，
        # 报 ValueError: 'coroutine' object is not iterable，接口 500。
        # （这个 bug 只在"发消息走异步"这条路径上出现 —— /v1/tasks 那条路径当时写了 await 所以没暴露，
        #   说明**每条分支都要有自己的测试**，靠一条路径通过不能推断另一条也对。）
        return await _dispatch_async(user, session_id, body.content, tier)

    try:
        res = await service.send_message(user, session_id, body.content, body.tier)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    res["thread_id"] = make_thread_id(user["id"], session_id)
    return res


@app.post("/v1/sessions/{session_id}/messages/stream")
async def stream_message(session_id: str, body: SendMessageIn,
                         user: Dict = Depends(rate_limit),
                         _quota: Dict = Depends(check_quota)):
    """
    SSE 流式发消息（打字机效果的后端一半）。

    为什么用 SSE 而不是 WebSocket：
        这是一问一答，流是单向的（服务端 → 客户端）。WebSocket 要处理心跳、
        重连、双向状态机，成本远高于收益。SSE 直接跑在 HTTP 上，
        nginx 反向代理也不用额外配协议升级。

    为什么 POST + fetch 读流，而不是 EventSource：
        EventSource 只支持 GET，而提问内容可能很长（20000 字），
        塞进 query string 既难看又不安全。用 fetch + ReadableStream 手工解 SSE 帧即可。

    两个必须设的响应头：
        X-Accel-Buffering: no  —— 不关的话 nginx 会把流缓冲起来攒着一起发，
                                 打字机直接退化成"等半天然后整段出现"
        Cache-Control: no-cache
    """
    try:
        sess = repo.get_session(user["id"], session_id)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该会话")

    try:
        tier = resolve_tier(body.tier, sess.get("preferred_tier", AUTO)).tier
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    # 🔴 流式接口的 research 异步分流（与非流式 send_message 对齐）。
    # research 档实测 125s，且它的"慢"是方法论的慢（拆解→多步检索→交叉验证），
    # 不是逐 token 能缓解的 —— 流式通道若同步跑，SSE 连接会挂 125 秒且全程黑屏。
    # 所以 research 档直接转异步任务，通过 SSE 推一个 task 事件，前端转轮询。
    want_async = body.run_async or (tier == RESEARCH and settings.async_auto_research)
    if want_async:
        import json as _json
        resp = await _dispatch_async(user, session_id, body.content, tier)
        payload = _json.loads(resp.body.decode("utf-8"))
        async def _task_stream():
            yield ("event: task\n"
                   f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n")
        return StreamingResponse(
            _task_stream(),
            media_type="text/event-stream; charset=utf-8",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache",
                     "Connection": "keep-alive"},
        )

    async def _gen():
        import json
        try:
            async for ev in stream.send_message_stream(user, session_id,
                                                       body.content, body.tier):
                ev["thread_id"] = make_thread_id(user["id"], session_id)
                yield f"event: {ev.get('type', 'message')}\n" \
                      f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:
            # 流已经开始了就不能再改 HTTP 状态码，只能把错误作为事件推给前端
            yield f"event: error\ndata: {json.dumps({'error': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream; charset=utf-8",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache",
                 "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# 异步任务（P3）
# ---------------------------------------------------------------------------
class CreateTaskIn(BaseModel):
    session_id: str
    content: str = Field(..., min_length=1, max_length=20000)
    tier: Optional[str] = None


async def _dispatch_async(user: Dict, session_id: str, content: str, tier: str):
    """
    建任务并派发。

    返回码的语义要分清：
      - 202：任务已受理但还没跑完（正常异步）
      - 200：eager 模式下任务已内联跑完，直接带结果返回（行为等价，只是没跨进程）
    两种都返回同一个 task 结构，客户端用 status 字段判断即可，不用区分模式。
    """
    row, reused = await tasks.create(user, session_id, content, tier)
    payload = {
        "async": True,
        "reused": reused,          # True = 命中幂等键，复用了已有任务
        "task": tasks.public(row, with_result=False),
        "thread_id": make_thread_id(user["id"], session_id),
    }
    if row["status"] == "success" and row.get("result"):
        payload["result"] = row["result"]
        # 必须显式给 200：路由装饰器上写的是 202，直接 return dict 会沿用 202，
        # 客户端就分不清"还在跑"和"已经跑完了"。
        return JSONResponse(status_code=status.HTTP_200_OK, content=payload)
    if row["status"] == "failure":
        payload["error"] = row.get("error", "")
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=payload)


@app.post("/v1/tasks", status_code=status.HTTP_202_ACCEPTED)
async def create_task(body: CreateTaskIn, user: Dict = Depends(rate_limit),
                      _quota: Dict = Depends(check_quota)):
    try:
        sess = repo.get_session(user["id"], body.session_id)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该会话")
    try:
        tier = resolve_tier(body.tier, sess.get("preferred_tier", AUTO)).tier
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return await _dispatch_async(user, body.session_id, body.content, tier)


@app.get("/v1/tasks")
async def list_tasks(limit: int = 50, user: Dict = Depends(auth.current_user)):
    # 列表不返回 result：一份调研报告几千字，列 50 条能把响应体撑到几 MB
    return {"tasks": [tasks.public(r, with_result=False)
                      for r in tasks.list_for(user["id"], limit)]}


@app.get("/v1/tasks/{task_id}")
async def get_task(task_id: str, user: Dict = Depends(auth.current_user)):
    try:
        row = tasks.get(user["id"], task_id)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return {"task": tasks.public(row, with_result=True)}


@app.post("/v1/tasks/{task_id}/retry")
async def retry_task(task_id: str, user: Dict = Depends(rate_limit)):
    """
    重跑一个 stalled / failure 的任务。

    存在理由（实测）：worker 整个容器没了的时候，Celery + Redis 不会把消息从
    `unacked` 里放回来（见 src/tasks/dispatch.py:retry 的说明）。
    所以"任务不丢"必须由应用层兜底 —— 这个接口就是那个兜底。
    """
    try:
        ok, row = await tasks.retry(user["id"], task_id)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if not ok:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"当前状态 {row['status']} 不支持重跑（仅 stalled/failure 可重跑）")
    payload = {"task": tasks.public(row, with_result=False)}
    if row["status"] == "success" and row.get("result"):
        payload["result"] = row["result"]
        return JSONResponse(status_code=status.HTTP_200_OK, content=payload)
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=payload)


@app.delete("/v1/tasks/{task_id}")
async def cancel_task(task_id: str, user: Dict = Depends(auth.current_user)):
    try:
        res = tasks.cancel(user["id"], task_id)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if not res["ok"] and res["reason"] in ("already_finished", "eager_mode"):
        # 409 而不是 200：客户端需要知道"我没撤回来"，否则会以为已经停了
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"任务无法取消（{res['reason']}），当前状态 {res['status']}")
    repo.add_audit(user["id"], "task_cancel", target=task_id)
    return {"status": res["status"], "task_id": task_id}


# ---------------------------------------------------------------------------
# 任务中断续跑（P-3）
# ---------------------------------------------------------------------------
class ResumeTaskIn(BaseModel):
    confirmed: bool = True
    plan: Optional[List[str]] = None          # 用户修改后的子任务清单（可选）


@app.post("/v1/tasks/{task_id}/resume")
async def resume_task(task_id: str, body: ResumeTaskIn,
                      user: Dict = Depends(rate_limit)):
    """从 interrupt 断点续跑一个 interrupted 任务。

    中断语义（P-3）：research 档规划出子任务后，若配置了人工确认，
    任务会停在 interrupted 状态等用户确认。用户提交 confirmed + 可选修改后的 plan，
    任务从断点继续执行（带着确认后的规划重新派发主图）。
    """
    confirmation = {"confirmed": body.confirmed, "plan": body.plan or []}
    try:
        ok, row = await tasks.resume(user["id"], task_id, confirmation)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if not ok:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"当前状态 {row['status']} 不支持续跑（仅 interrupted 可续跑）")
    repo.add_audit(user["id"], "task_resume", target=task_id,
                   detail=f"confirmed={body.confirmed}, plan_count={len(body.plan or [])}")
    payload = {"task": tasks.public(row, with_result=False)}
    if row["status"] == "success" and row.get("result"):
        payload["result"] = row["result"]
        return JSONResponse(status_code=status.HTTP_200_OK, content=payload)
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=payload)


# ---------------------------------------------------------------------------
# 文件（按用户隔离）
# ---------------------------------------------------------------------------
@app.post("/v1/sessions/{session_id}/files")
async def upload_file(session_id: str, file: UploadFile = File(...),
                      user: Dict = Depends(rate_limit)):
    try:
        repo.get_session(user["id"], session_id)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该会话")

    data = await file.read()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"文件超过 {settings.max_upload_mb}MB 上限")

    directory = Path(storage_dir(settings.storage_root, user["id"], session_id))
    directory.mkdir(parents=True, exist_ok=True)
    fid = keys.new_id("file")
    # 文件名只取 basename，防止 ../../ 这类路径穿越写进存储路径
    safe_name = os.path.basename(file.filename or "unnamed")
    path = directory / f"{fid}_{safe_name}"
    path.write_bytes(data)

    rec = repo.add_file(user["id"], session_id, safe_name, str(path), len(data))
    repo.add_usage(user["id"], "upload", 1, session_id=session_id)
    return {"file": _ser(rec)}


@app.get("/v1/files")
async def list_files(session_id: Optional[str] = None,
                     user: Dict = Depends(auth.current_user)):
    return {"files": _ser_all(repo.list_files(user["id"], session_id))}


@app.get("/v1/files/{file_id}/download")
async def download_file(file_id: str, user: Dict = Depends(auth.current_user)):
    try:
        rec = repo.get_file(user["id"], file_id)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    except repo.Forbidden:
        # 别人的文件一律当不存在，不泄露"它确实存在"
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    p = Path(rec["storage_path"])
    if not p.exists():
        raise HTTPException(status.HTTP_410_GONE, "文件记录存在但磁盘上已丢失")
    return FileResponse(p, filename=rec["filename"])


@app.delete("/v1/files/{file_id}")
async def remove_file(file_id: str, user: Dict = Depends(auth.current_user)):
    try:
        rec = repo.get_file(user["id"], file_id)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    try:
        Path(rec["storage_path"]).unlink(missing_ok=True)
    except Exception:
        pass
    repo.delete_file(user["id"], file_id)
    return {"status": "deleted", "file_id": file_id}


# ---------------------------------------------------------------------------
# 用量
# ---------------------------------------------------------------------------
@app.get("/v1/usage")
async def usage(user: Dict = Depends(auth.current_user)):
    summary = repo.usage_summary(user["id"])
    return {
        "user_id": user["id"],
        "summary": summary,
        "quota": {"searches_per_day": settings.quota_searches_per_day,
                  "messages_per_day": settings.quota_messages_per_day},
        "note": "P1 只记帐不做硬限流；硬限流在 P2 与控制层一起落地",
    }


# ---------------------------------------------------------------------------
# 成本核算（P-4）：token 是事实，单价是配置 —— 两者分离
# ---------------------------------------------------------------------------
@app.get("/v1/cost")
async def cost(user: Dict = Depends(auth.current_user, ), since_days: int = 30):
    """按模型聚合 token 用量并换算成本。

    token 数来自 usage 表的客观记账；金额按配置单价（每百万 token 元）换算。
    未配置单价（=0）时只报 token 数，cost 为 null —— 绝不编造金额。
    """
    since_days = max(1, min(since_days, 365))
    per_model = repo.token_usage(user["id"], since_days=since_days)

    ip = settings.llm_price_input_per_million
    op = settings.llm_price_output_per_million
    has_price = ip > 0 or op > 0

    total_input = total_output = 0
    cost_by_model = []
    for model, bucket in per_model.items():
        it = bucket["input_tokens"]
        ot = bucket["output_tokens"]
        total_input += it
        total_output += ot
        entry = {"model": model, "input_tokens": it, "output_tokens": ot}
        if has_price:
            entry["cost"] = round((it * ip + ot * op) / 1_000_000, 6)
        cost_by_model.append(entry)

    total_cost = None
    if has_price:
        total_cost = round((total_input * ip + total_output * op) / 1_000_000, 6)

    return {
        "user_id": user["id"],
        "since_days": since_days,
        "prices": {
            "input_per_million": ip,
            "output_per_million": op,
            "currency": "CNY",
        },
        "totals": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cost": total_cost,
        },
        "by_model": cost_by_model,
        "note": "未配置单价时 cost 为 null，仅报客观 token 数",
    }


# ---------------------------------------------------------------------------
# 长期记忆（P-1）：用户可见、可删 —— 隐私合规底线
# ---------------------------------------------------------------------------
@app.get("/v1/memories")
async def memories_list(user: Dict = Depends(auth.current_user),
                        scope: str = "user", limit: int = 50):
    """列出当前用户的长期记忆（scope=user）。组织级记忆 scope=org（P-4 多级租户后开放）。"""
    mems = repo.list_memories(user["id"], scope=scope, limit=max(1, min(limit, 200)))
    return {
        "user_id": user["id"],
        "scope": scope,
        "count": len(mems),
        "memories": mems,
    }


@app.delete("/v1/memories/{memory_id}")
async def memories_delete(memory_id: str, user: Dict = Depends(auth.current_user)):
    ok = repo.delete_memory(user["id"], memory_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在或无权删除")
    repo.add_audit(user["id"], "memory_delete", target=memory_id)
    return {"status": "deleted", "memory_id": memory_id}


# ---------------------------------------------------------------------------
# 审计日志（P-4）
# ---------------------------------------------------------------------------
@app.get("/v1/audit")
async def audit_log(limit: int = 200, action: Optional[str] = None,
                    user: Dict = Depends(auth.current_user)):
    """查询当前用户的审计日志。管理员可传 all=1 查全部（P-4 多级租户后开放 org 级）。"""
    rows = repo.list_audit(user_id=user["id"], limit=limit, action=action)
    return {"count": len(rows), "audit": rows}


# ---------------------------------------------------------------------------
# 多租户（P-6）：组织 + 成员。叠加式，个人数据私有 + 组织能力显式共享。
#
# 隔离：查组织详情/成员/用量都必须先确认"当前用户是成员"，否则 403。
# 越权一律 403（不返回 404），避免"组织是否存在"的信息泄漏。
# ---------------------------------------------------------------------------
class OrgCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)


class MemberAddIn(BaseModel):
    user_id: str = Field(..., min_length=1)
    role: str = Field("member", pattern="^(admin|member)$")


class MemberUpdateIn(BaseModel):
    role: str = Field(..., pattern="^(admin|member)$")


@app.get("/v1/orgs")
async def orgs_list(user: Dict = Depends(auth.current_user)):
    """列出当前用户所属的组织（含角色）。"""
    orgs = repo.list_orgs(user["id"])
    return {"count": len(orgs), "orgs": orgs}


@app.post("/v1/orgs")
async def orgs_create(body: OrgCreateIn, user: Dict = Depends(auth.current_user)):
    """创建组织，创建者自动成为 owner。"""
    org = repo.create_org(user["id"], body.name)
    repo.add_audit(user["id"], "org_create", target=org["id"], detail=f"name={body.name}")
    return {"org": org}


@app.get("/v1/orgs/{org_id}")
async def org_detail(org_id: str, user: Dict = Depends(auth.current_user)):
    """组织详情 + 成员列表。非成员 403。"""
    try:
        org = repo.get_org(user["id"], org_id)
        members = repo.list_members(org_id)
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "组织不存在")
    except repo.Forbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该组织")
    return {"org": org, "members": members}


@app.delete("/v1/orgs/{org_id}")
async def org_delete(org_id: str, user: Dict = Depends(auth.current_user)):
    """删除组织。仅 owner 可删。"""
    try:
        repo.delete_org(user["id"], org_id)
    except repo.Forbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅组织 owner 可删除组织")
    repo.add_audit(user["id"], "org_delete", target=org_id)
    return {"status": "deleted", "org_id": org_id}


@app.post("/v1/orgs/{org_id}/members")
async def org_add_member(org_id: str, body: MemberAddIn,
                         user: Dict = Depends(auth.current_user)):
    """邀请成员加入。仅 owner/admin 可操作。"""
    try:
        _require_org_admin(org_id, user["id"])
        repo.add_member(org_id, body.user_id, body.role)
    except repo.Forbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅 owner/admin 可管理成员")
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    repo.add_audit(user["id"], "org_add_member", target=org_id,
                   detail=f"member={body.user_id}, role={body.role}")
    return {"status": "added", "org_id": org_id, "user_id": body.user_id}


@app.patch("/v1/orgs/{org_id}/members/{member_id}")
async def org_update_member(org_id: str, member_id: str, body: MemberUpdateIn,
                            user: Dict = Depends(auth.current_user)):
    """改成员角色（admin/member）。仅 owner/admin 可操作；owner 不可被改。"""
    try:
        _require_org_admin(org_id, user["id"])
        repo.update_member_role(org_id, member_id, body.role)
    except repo.Forbidden as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    repo.add_audit(user["id"], "org_update_member", target=org_id,
                   detail=f"member={member_id}, role={body.role}")
    return {"status": "updated", "org_id": org_id, "user_id": member_id, "role": body.role}


@app.delete("/v1/orgs/{org_id}/members/{member_id}")
async def org_remove_member(org_id: str, member_id: str,
                            user: Dict = Depends(auth.current_user)):
    """移除成员。仅 owner/admin 可操作；owner 不可被移除。"""
    try:
        _require_org_admin(org_id, user["id"])
        repo.remove_member(org_id, member_id)
    except repo.Forbidden as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except repo.NotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")
    repo.add_audit(user["id"], "org_remove_member", target=org_id, detail=f"member={member_id}")
    return {"status": "removed", "org_id": org_id, "user_id": member_id}


@app.get("/v1/orgs/{org_id}/usage")
async def org_usage(org_id: str, since_days: int = 30,
                    user: Dict = Depends(auth.current_user)):
    """组织级 token 用量聚合（所有成员汇总）。非成员 403。"""
    try:
        repo.get_org(user["id"], org_id)
        per_model = repo.org_token_usage(org_id, since_days=since_days)
    except repo.Forbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该组织")
    ip = settings.llm_price_input_per_million
    op = settings.llm_price_output_per_million
    has_price = ip > 0 or op > 0
    total_input = total_output = 0
    by_model = []
    for model, bucket in per_model.items():
        it, ot = bucket["input_tokens"], bucket["output_tokens"]
        total_input += it
        total_output += ot
        entry = {"model": model, "input_tokens": it, "output_tokens": ot}
        if has_price:
            entry["cost"] = round((it * ip + ot * op) / 1_000_000, 6)
        by_model.append(entry)
    total_cost = round((total_input * ip + total_output * op) / 1_000_000, 6) if has_price else None
    return {
        "org_id": org_id,
        "since_days": since_days,
        "member_count": len(repo.org_member_ids(org_id)),
        "totals": {"input_tokens": total_input, "output_tokens": total_output,
                   "cost": total_cost},
        "by_model": by_model,
    }


def _require_org_admin(org_id: str, user_id: str) -> None:
    """确认 user 在 org 里是 owner 或 admin；否则 Forbidden。"""
    role = repo.get_membership(org_id, user_id)
    if role is None:
        raise repo.Forbidden(f"无权访问组织: {org_id}")
    if role["role"] not in ("owner", "admin"):
        raise repo.Forbidden("仅 owner/admin 可管理成员")


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def index():
    # 依次回落：index_v2.html（语析布局）→ index.html（P1 旧版）
    for name in ("index_v2.html", "index.html"):
        p = WEB_DIR / name
        if p.exists():
            return FileResponse(p)
    return JSONResponse({"message": "明辨平台运行中（前端文件缺失）"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.server:app", host=settings.host, port=settings.port, reload=False)
