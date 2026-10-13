"""
FastAPI 认证依赖

改动点（相对旧项目）：
    旧项目所有接口零鉴权，任何人都能 POST /api/task、/api/cancel、/api/download。
    这里把"当前用户"变成所有业务接口的强制依赖 —— 不传 Key 就 401，
    传错就 403（区分不开"不存在"和"无权"时一律 401，避免泄露资源是否存在）。

Key 传递方式（二选一）：
    Authorization: Bearer mj_xxx
    X-API-Key: mj_xxx
"""

from __future__ import annotations

from typing import Dict, Optional

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config import settings
from src.db import repo

_bearer = HTTPBearer(auto_error=False, scheme_name="APIKey")

ANON_USER = {"id": "u_anonymous", "name": "anonymous", "role": "user", "status": "active"}


def _extract(cred: Optional[HTTPAuthorizationCredentials],
             x_api_key: Optional[str]) -> Optional[str]:
    if cred and cred.credentials:
        return cred.credentials.strip()
    if x_api_key:
        return x_api_key.strip()
    return None


def current_user(
    cred: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> Dict:
    """
    业务接口的统一依赖：解析出当前用户。

    匿名访问是**显式开关**（MJ_ALLOW_ANONYMOUS=1），默认关闭。
    本地调试可以打开，生产环境绝不能开 —— 一开就等于回到旧项目的裸奔状态。
    """
    plain = _extract(cred, x_api_key)

    if not plain:
        if settings.allow_anonymous:
            return dict(ANON_USER)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 API Key（Authorization: Bearer mj_xxx 或 X-API-Key）",
        )

    user = repo.get_user_by_key(plain)
    if user is None:
        # 不区分"Key 不存在"和"用户已停用"，统一 401，避免资源存在性探测
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="API Key 无效或用户已停用")
    return user


def require_admin(x_admin_token: Optional[str] = Header(default=None,
                                                        alias="X-Admin-Token")) -> None:
    """
    管理接口（创建用户 / 列用户）的守卫。

    未配置 MJ_ADMIN_TOKEN 时**一律拒绝**而不是放行 ——
    "忘了配就放行"是典型的不安全默认值。
    """
    if not settings.admin_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="未配置 MJ_ADMIN_TOKEN，管理接口已关闭")
    if not x_admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="管理员令牌不正确")
