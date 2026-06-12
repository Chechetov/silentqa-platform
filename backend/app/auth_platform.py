"""Auth-зависимости платформенного контура (спека §2)."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from tenancy.context import get_tenant_slug

from .platform_sessions import PLATFORM_COOKIE, load_platform_session


@dataclass
class PlatformAdminCtx:
    admin_id: str
    email: str


async def get_current_platform_admin(request: Request) -> PlatformAdminCtx | None:
    """Cookie sqa_admin → ctx; None на тенант-контуре/без сессии."""
    if get_tenant_slug() is not None:
        return None
    sid = request.cookies.get(PLATFORM_COOKIE)
    if not sid:
        return None
    data = await load_platform_session(sid)
    if not data:
        return None
    return PlatformAdminCtx(admin_id=data["admin_id"], email=data["email"])


async def require_platform_admin(
    admin: PlatformAdminCtx | None = Depends(get_current_platform_admin),
) -> PlatformAdminCtx:
    if admin is None:
        # detail-маркер: admin-SPA редиректит на логин только по нему
        raise HTTPException(status_code=401, detail="platform_auth_required")
    return admin
