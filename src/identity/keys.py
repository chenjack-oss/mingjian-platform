"""
ID 与 API Key 生成

为什么 API Key 只存哈希：
    数据库一旦泄露，明文 Key 等于把所有用户的账号交出去。
    存 sha256 后，登录时用同样的 sha256 比对即可，
    代价只是"忘记后无法找回，只能重签"——这是安全上正确的取舍。

为什么还要存 api_key_prefix：
    用户在 UI 上要能区分自己的多把 Key（"mj_a1b2… 这把是笔记本用的"），
    前缀用于展示，不足以用于登录（只有 8 位可见）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


ID_PREFIX = {"user": "u_", "session": "s_", "file": "f_", "task": "t_",
             "mem": "m_", "org": "o_"}
KEY_PREFIX = "mj_"


def new_id(kind: str) -> str:
    """生成带类型前缀的随机 ID。带前缀是为了日志里一眼能看出是什么对象。"""
    return ID_PREFIX[kind] + secrets.token_hex(8)


def new_api_key() -> str:
    """返回明文 Key，只在这一次返回，之后系统里只剩哈希。"""
    return KEY_PREFIX + secrets.token_hex(20)


def hash_key(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def verify_key(plain: str, stored_hash: str) -> bool:
    """用 hmac.compare_digest 做常量时间比较，避免计时侧信道。"""
    return hmac.compare_digest(hash_key(plain), stored_hash or "")


def key_prefix(plain: str) -> str:
    """展示用前缀：mj_ + 前 6 位"""
    return plain[: len(KEY_PREFIX) + 6]
