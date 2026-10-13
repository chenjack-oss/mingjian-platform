"""
命名空间：把 thread_id 从"客户端自报"改成"服务端派生"

这是旧项目最严重的一个安全问题：
    server_v3.py 里 `thread_id = request.thread_id or uuid4()`
    意味着客户端说什么就是什么 —— 猜到别人的 thread_id 就能接管对方的会话、
    取消对方的任务、读写对方 output/session_{id} 目录。

本模块把 thread_id 变成服务端派生的三段式：
    u:{user_id}:s:{session_id}
任何外部输入都不再能直接指定它。所有以此为 key 的存储
（LangGraph checkpoint、TaskManager、WebSocket 连接表）自动获得用户级隔离。
"""

from __future__ import annotations

from typing import Optional, Tuple

SEP = ":"


def make_thread_id(user_id: str, session_id: str) -> str:
    return f"u{SEP}{user_id}{SEP}s{SEP}{session_id}"


def parse_thread_id(thread_id: str) -> Tuple[Optional[str], Optional[str]]:
    """
    解析三段式 thread_id。解析失败返回 (None, None) —— 调用方必须当作非法输入处理，
    绝不能"解析不出来就用原值"，那等于把隔离又打开了口子。
    """
    if not thread_id:
        return None, None
    parts = thread_id.split(SEP)
    if len(parts) != 4 or parts[0] != "u" or parts[2] != "s":
        return None, None
    return parts[1], parts[3]


def assert_owned(thread_id: str, user_id: str) -> str:
    """
    校验 thread_id 归属。不匹配直接抛 PermissionError。

    为什么用抛异常而不是返回 bool：
    这类校验一旦被调用方忘记判断返回值，就会静默变成越权。抛异常无法被忽略。
    """
    uid, _sid = parse_thread_id(thread_id)
    if uid != user_id:
        raise PermissionError(f"会话 {thread_id} 不属于用户 {user_id}")
    return thread_id


def storage_dir(root: str, user_id: str, session_id: str) -> str:
    """
    文件存储目录：root/{user_id}/{session_id}

    旧项目是 updated/session_{thread_id}，同样是客户端可控；
    这里改成服务端派生的两级目录，从文件系统层面就隔离了。
    """
    return f"{root.rstrip('/')}/{user_id}/{session_id}"
