"""
MCP 客户端（自研适配层，不用 langchain-mcp-adapters）

为什么自己写这层：
    1. 需要**会话生命周期可控**，且要能被 FastAPI 的多任务环境安全复用（见下方最大的坑）。
    2. 需要**超时**。业务系统卡住时，不加 wait_for 会把整个 Agent 链路一起拖死。
    3. 需要**降级**：MCP Server 起不来时返回空工具集，主链路照常跑（/health 如实报 off）。

🔴 本文件最值钱的一个坑（实测踩到，不是看书看的）：
    MCP 的 stdio 会话（stdio_client + ClientSession）内部用了 anyio 的
    TaskGroup / CancelScope，**它绑定创建它的那个 task**。
    一开始我做成"进程内一个长连接，谁用谁 await"，结果：
        RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
    并且 FastAPI 那边连带出现 "This portal is not running"，整个 lifespan 被打死。
    根因：请求 A 的 task 打开了会话，请求 B 的 task 结束时 anyio 试图在 B 里收尾 A 的作用域。

    修法：**给每个 MCP Server 开一个常驻线程，线程里跑自己的 event loop 和会话**，
    请求侧通过 asyncio.run_coroutine_threadsafe 跨线程提交调用，再用 wrap_future 等结果。
    这样会话只属于那一个 loop、那一个 task，永不跨任务；调用方又是纯 async 不阻塞。

    为什么不用"每次调用现起子进程"来回避：
    stdio 模式下新建连接 = spawn 一个 Python 解释器，实测开销见 scripts/measure_mcp.py，
    长任务里工具会被调十几次，每次多几十到几百毫秒是实打实的延迟。

第二个坑：mcp 2.x 把所有字段改成了 snake_case。
    1.x 的 isError / structuredContent / inputSchema 在 2.2.0 上全是 AttributeError。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.config import settings

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.observe.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]
    server: str
    read_only: bool = True


# ---------------------------------------------------------------------------
# 连接：只在 runner 线程的 loop 里使用，禁止跨 loop
# ---------------------------------------------------------------------------
@dataclass
class _Connection:
    name: str
    args: List[str]
    cwd: str = ""          # 外部 server 必须在它自己的目录下启动（否则 import 不到自己的模块）
    _session: Optional[ClientSession] = None
    _cm: Any = None
    _tools: List[ToolSpec] = field(default_factory=list)

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def _spawn(self) -> None:
        params = StdioServerParameters(
            command=self.args[0], args=self.args[1:],
            env=dict(os.environ), cwd=self.cwd or os.getcwd(),
        )
        # stdio_client / ClientSession 都是 async context manager：
        # 想做长连接就必须手动 __aenter__ 并持有引用，写成 async with 退出时就把子进程关了
        self._cm = stdio_client(params)
        r, w = await self._cm.__aenter__()
        sess = ClientSession(r, w)
        await sess.__aenter__()
        await sess.initialize()
        self._session = sess

    async def close(self) -> None:
        for obj in (self._session, self._cm):
            if obj is None:
                continue
            try:
                await obj.__aexit__(None, None, None)
            except Exception:
                pass
        self._session = None
        self._cm = None
        self._tools = []

    async def ensure(self) -> None:
        if not self.connected:
            await self._spawn()

    async def list_tools(self) -> List[ToolSpec]:
        await self.ensure()
        res = await self._session.list_tools()
        out: List[ToolSpec] = []
        for t in res.tools:
            ann = getattr(t, "annotations", None)
            ro = True if ann is None else bool(getattr(ann, "read_only_hint", True))
            out.append(ToolSpec(name=t.name, description=t.description or "",
                                input_schema=t.input_schema or {},
                                server=self.name, read_only=ro))
        self._tools = out
        return out

    async def call(self, tool: str, args: Dict[str, Any]) -> Any:
        if settings.mcp_session == "per_call":
            # 仅用于对照测量：每次调用现起子进程
            await self.close()
        await self.ensure()
        try:
            res = await asyncio.wait_for(self._session.call_tool(tool, args),
                                         timeout=settings.mcp_timeout)
        except asyncio.TimeoutError:
            return {"error": True, "kind": "timeout",
                    "message": f"业务系统 {self.name} 调用超时（>{settings.mcp_timeout}s）"}
        except Exception:
            # 断线重连一次：子进程被 OOM killer 干掉是真实会发生的事
            try:
                await self.close()
                await self.ensure()
                res = await asyncio.wait_for(self._session.call_tool(tool, args),
                                             timeout=settings.mcp_timeout)
            except Exception as e2:
                return {"error": True, "kind": "transport",
                        "message": f"MCP 调用失败（重连后仍失败）: {type(e2).__name__}: {e2}"}

        if getattr(res, "is_error", False):
            txt = "".join(getattr(c, "text", "") for c in (res.content or []))
            return {"error": True, "kind": "tool", "message": txt[:800]}
        return _normalize(res)


def _normalize(res: Any) -> Any:
    """把 MCP 返回压平。

    MCPServer 会把 dict 返回值再包一层 {"result": ...}（2.2.0 实测），
    不拆开的话模型看到的顶层 key 永远是 result，容易误读结构。
    """
    sc = getattr(res, "structured_content", None)
    if isinstance(sc, dict):
        return sc["result"] if set(sc.keys()) == {"result"} else sc
    parts = [getattr(c, "text", "") for c in (getattr(res, "content", None) or [])]
    raw = "\n".join(p for p in parts if p).strip()
    try:
        parsed = json.loads(raw)
        return parsed["result"] if isinstance(parsed, dict) and set(parsed) == {"result"} else parsed
    except Exception:
        return raw


# ---------------------------------------------------------------------------
# Runner：常驻线程 + 独立 loop（会话的唯一归属）
# ---------------------------------------------------------------------------
class _Runner:
    def __init__(self, name: str, args: List[str], cwd: str = "",
                 exclusive: bool = False):
        self.name = name
        self._args = args
        self._cwd = cwd
        # 只在 exclusive 时才建锁：asyncio.Lock 会在**首次 await 的那个 loop**上
        # 绑定，而调用恰好都发生在 runner 自己的 loop 里，所以这里是安全的。
        self._call_lock = asyncio.Lock() if exclusive else None
        if exclusive:
            logger.info(f"[mcp] server {name} 已开启调用串行化（大帧 + CPU 密集）")
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        # cwd 必须透传给 _Connection：外部 server（如 Agentic RAG）
        # 只有在自己的目录下才能 import 到自己的模块
        self._conn = _Connection(name=name, args=args, cwd=cwd)
        self._error: Optional[str] = None

    # -- 线程主体 ---------------------------------------------------------
    def _bootstrap(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._conn.ensure())
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
        self._ready.set()
        if self._error is None:
            self._loop.run_forever()      # 会话与它的 TaskGroup 永久留在这个 loop 里
        self._loop.close()

    def _spawn_thread(self) -> None:
        """只负责起线程，**不等待**。

        等待必须放到调用方那边去做（见 _ensure_ready），否则一次首连就把整个
        FastAPI 事件循环冻住 —— 冷启动时第一个请求会连带所有并发请求一起卡。
        """
        if self._thread is not None:
            return
        t = threading.Thread(target=self._bootstrap, name=f"mcp-{self.name}",
                             daemon=True)
        self._thread = t
        t.start()

    async def _ensure_ready(self, timeout: Optional[float] = None) -> None:
        """等待 runner 首连就绪（async 版本，不占事件循环）。

        超时默认取 settings.mcp_start_timeout（可配），因为**外部 server 冷启动
        可能远慢于项目内 server**：实测 Agentic RAG 的 mcp_rag_server 要先加载
        BGE-M3 权重，首连就绪 69.3s —— 原先硬编码的 30s 会让它永远挂不上，
        而且报错是"启动超时"，看起来像配置错而不是超时不够。

        坑点：threading.Event.wait() 是**阻塞调用**。_submit 是 async 函数，
        直接在里面 `self._ready.wait(30)` 会把当前线程（= FastAPI 事件循环所在
        线程）卡死整整 30 秒 —— 期间所有请求都排不上队，表现是"服务假死"。
        正确做法是把阻塞等待丢给线程池（asyncio.to_thread），
        让事件循环在等待期间继续处理别的请求。
        """
        self._spawn_thread()
        if self._thread is None:
            return
        if timeout is None:
            timeout = float(settings.mcp_start_timeout)
        ok = await asyncio.to_thread(self._ready.wait, timeout)
        if not ok and self._error is None:
            self._error = f"MCP Server 启动超时（{timeout:.0f}s）"

    # -- 跨线程提交（调用方是任意 loop）------------------------------------
    async def _submit(self, coro_factory) -> Any:
        await self._ensure_ready()
        if self._error:
            return {"error": True, "kind": "unavailable", "message": self._error}
        if self._loop is None:
            return {"error": True, "kind": "unavailable", "message": "runner 未就绪"}

        async def _wrapped():
            # exclusive=True 的 server（如 rag）在 runner 自己的 loop 里串行化。
            # 理由见 P6.0 实测：business（小帧）200 次 / 并发 50 零错配，
            # 但**大帧**（kb_lookup 返回原文片段）超过 PIPE_BUF(4096) 后 POSIX
            # 不再保证单次写原子，理论上存在串帧可能；而且 rag 是 CPU 密集，
            # 并发本来就只抢 CPU（p95 从 1.3s 掉到 23.9s）。串行化零损失、零风险。
            if self._call_lock is not None:
                async with self._call_lock:
                    return await coro_factory()
            return await coro_factory()

        fut: concurrent.futures.Future = asyncio.run_coroutine_threadsafe(
            _wrapped(), self._loop)
        try:
            # wrap_future 把 concurrent Future 变成当前 loop 可 await 的对象，
            # 关键点：等待期间**不占调用方线程**，不会把 FastAPI 的 loop 卡住
            return await asyncio.wait_for(asyncio.wrap_future(fut),
                                          timeout=settings.mcp_timeout + 5)
        except asyncio.TimeoutError:
            fut.cancel()
            return {"error": True, "kind": "timeout",
                    "message": f"MCP 调用超时（>{settings.mcp_timeout + 5}s）"}

    async def call(self, tool: str, args: Dict[str, Any]) -> Any:
        return await self._submit(lambda: self._conn.call(tool, args))

    async def list_tools(self) -> List[ToolSpec]:
        r = await self._submit(self._conn.list_tools)
        if isinstance(r, dict) and r.get("error"):
            raise RuntimeError(r.get("message", "MCP 不可用"))
        return r or []

    @property
    def connected(self) -> bool:
        return self._thread is not None and self._conn.connected

    def status(self) -> Dict[str, Any]:
        return {"connected": self.connected,
                "tools": len(self._conn._tools),
                **({"error": self._error} if self._error else {})}

    def close(self) -> None:
        if self._loop is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._conn.close(), self._loop)
            fut.result(timeout=5)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass


_runners: Dict[str, _Runner] = {}
_runners_lock = threading.Lock()


def _runner_for(name: str) -> _Runner:
    with _runners_lock:
        if name not in _runners:
            args, cwd = settings.mcp_command(name)
            _runners[name] = _Runner(name, args, cwd,
                                     exclusive=settings.serialize_server(name))
        return _runners[name]


def server_names() -> List[str]:
    if not settings.mcp_enabled:
        return []
    return [s.strip() for s in settings.mcp_servers.split(",") if s.strip()]


def allowlist() -> List[str]:
    return [t.strip() for t in settings.mcp_tools.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
async def available_tools() -> List[ToolSpec]:
    """列出所有已配置 server 的工具（按白名单裁剪）。失败返回空列表，不抛出。"""
    specs: List[ToolSpec] = []
    allow = allowlist()
    for name in server_names():
        try:
            ts = await _runner_for(name).list_tools()
        except Exception as e:
            logger.warning(f"[mcp] server {name} 不可用（降级为空工具集）: {type(e).__name__}: {e}")
            continue
        for t in ts:
            if allow and t.name not in allow:
                continue
            specs.append(t)
    return specs


def _json_type(spec: Dict[str, Any]) -> Any:
    t = (spec or {}).get("type")
    if isinstance(t, list):          # ["string","null"] 这类联合类型
        t = next((x for x in t if x != "null"), "string")
    return {"string": str, "integer": int, "number": float,
            "boolean": bool, "array": list, "object": dict}.get(t, str)


def _schema_to_model(tool_name: str, schema: Dict[str, Any]):
    from pydantic import create_model
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    fields: Dict[str, Tuple[Any, Any]] = {}
    for k, v in props.items():
        typ = _json_type(v)
        if k in required:
            fields[k] = (typ, ...)
        else:
            d = v.get("default") if isinstance(v, dict) else None
            fields[k] = (Optional[typ], d if d is not None else None)
    return create_model(f"{tool_name}Args", **fields) if fields else None


async def langchain_tools(server: str = "", prefix: str = "") -> List[Any]:
    """把 MCP 工具转成 LangChain StructuredTool。

    server / prefix：P6 给子智能体按职责裁剪工具用（deepagents 的 graph.py:762 支持
    per-subagent 工具覆盖："Inherit parent tools unless the subagent declares its own"）。
    两个参数都留空 = 全部工具，与旧行为完全一致。

    为什么要能裁剪：P5 实测 3 个工具选择准确率 100%、16 个降到 90%，
    工具越少模型越准。工具边界既是权限边界，也是准确率边界。
    """
    from langchain_core.tools import StructuredTool

    specs = await available_tools()
    if not specs:
        return []

    out: List[Any] = []
    for spec in specs:
        if server and spec.server != server:
            continue
        if prefix and not spec.name.startswith(prefix):
            continue
        runner = _runner_for(spec.server)
        model = _schema_to_model(spec.name, spec.input_schema)

        async def _ainvoke(_name: str = spec.name, _r: _Runner = runner, **kwargs):
            return await _r.call(_name, {k: v for k, v in kwargs.items()
                                         if v is not None})

        desc = spec.description.strip()
        if not spec.read_only:
            desc += "\n[写操作：必须先取得用户确认，按工具说明的两步协议执行]"
        try:
            out.append(StructuredTool.from_function(
                coroutine=_ainvoke, name=spec.name, description=desc,
                args_schema=model, infer_schema=False,
            ))
        except Exception as e:
            logger.warning(f"[mcp] 工具 {spec.name} 转换失败（跳过）: {type(e).__name__}: {e}")
    return out


async def status() -> Dict[str, Any]:
    """给 /health 用：如实报告，连不上就报 down，不粉饰。"""
    if not settings.mcp_enabled:
        return {"enabled": False, "servers": {}, "tools": 0}
    info: Dict[str, Any] = {"servers": {}, "tools": 0}
    for name in server_names():
        r = _runner_for(name)
        try:
            ts = await r.list_tools()
            entry = {"connected": True, "tools": len(ts),
                     "read_only": sum(1 for t in ts if t.read_only)}
            info["tools"] += len(ts)
        except Exception as e:
            entry = {"connected": False, "tools": 0,
                     "error": f"{type(e).__name__}: {e}"}
        info["servers"][name] = entry
    info["enabled"] = True
    return info


def close_all() -> None:
    for r in list(_runners.values()):
        r.close()
    _runners.clear()


# ---------------------------------------------------------------------------
# 供测量脚本用：裸调用（不带 LangChain 包装），返回 (结果, 毫秒)
# ---------------------------------------------------------------------------
async def raw_call(tool: str, args: Dict[str, Any],
                   server: str = "business") -> Tuple[Any, float]:
    r = _runner_for(server)
    t0 = time.perf_counter()
    res = await r.call(tool, args)
    return res, (time.perf_counter() - t0) * 1000
