"""
请求级上下文（ContextVar）

为什么必须是 ContextVar 而不是全局变量：
    FastAPI 用 asyncio，多个用户的请求在**同一个线程**里交错执行。
    全局变量会串台（A 的文件写到 B 的目录），threading.local 在协程里失效
    （所有协程同线程），只有 ContextVar 能做到协程级隔离。

这里比旧项目多了一个通道：**user_id**。
旧项目只有 session_dir 和 thread_id，工具层拿不到"当前是谁"，
预算、配额、文件目录都只能按会话算 —— 改成按用户算之后，
同一用户的多个会话共享一份搜索预算，才算真正的配额控制
（否则开几个会话就能把配额翻几倍，这是旧项目按会话计数的一个漏洞）。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import List, Optional, Tuple

_user_id_ctx: ContextVar[Optional[str]] = ContextVar("mj_user_id", default=None)
_session_id_ctx: ContextVar[Optional[str]] = ContextVar("mj_session_id", default=None)
_thread_id_ctx: ContextVar[Optional[str]] = ContextVar("mj_thread_id", default=None)

_Saved = Tuple[ContextVar, object]


class RequestContext:
    """
    一次请求内有效的上下文句柄。

    用法：
        ctx = bind(user_id=..., session_id=..., thread_id=...)
        try:
            ...
        finally:
            ctx.reset()

    必须 reset：ContextVar 的 set 不会自动撤销，
    不复位的话下一个请求（复用同一协程上下文时）会读到上一次的用户。
    """

    def __init__(self, user_id: str = "", session_id: str = "", thread_id: str = ""):
        self._saved: List[_Saved] = []
        if user_id:
            self._saved.append((_user_id_ctx, _user_id_ctx.set(user_id)))
        if session_id:
            self._saved.append((_session_id_ctx, _session_id_ctx.set(session_id)))
        if thread_id:
            self._saved.append((_thread_id_ctx, _thread_id_ctx.set(thread_id)))

    def reset(self) -> None:
        for var, token in reversed(self._saved):
            try:
                var.reset(token)
            except Exception:
                pass
        self._saved = []

    def __enter__(self) -> "RequestContext":
        return self

    def __exit__(self, *exc) -> None:
        self.reset()


def bind(user_id: str = "", session_id: str = "", thread_id: str = "") -> RequestContext:
    return RequestContext(user_id, session_id, thread_id)


def get_user_id() -> str:
    return _user_id_ctx.get() or ""


def get_session_id() -> str:
    return _session_id_ctx.get() or ""


def get_thread_id() -> str:
    return _thread_id_ctx.get() or ""


def budget_key() -> str:
    """预算计数键：**按用户**（见文件头说明）"""
    return get_user_id() or get_thread_id() or "global"


def citation_key() -> str:
    """引用池键：按会话隔离，不同会话检索到的 URL 不该互相打通"""
    return get_thread_id() or get_session_id() or "global"
