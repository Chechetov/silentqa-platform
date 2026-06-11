"""Auth dependencies for the access matrix (spec 5.6).

Three planes:
- cookie session (tenant users)      → get_current_user / require_viewer / require_admin
- X-API-Key (recorder ingestion)     → require_ingestion_auth (cookie OR key)
- broker JWT                         → auth_jwt.get_current_broker (отдельный модуль)
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from tenancy.context import get_tenant_slug

from .auth_sessions import SESSION_COOKIE, load_session


@dataclass
class UserCtx:
    user_id: str
    email: str
    role: str


async def get_current_user(request: Request) -> UserCtx | None:
    """Cookie session → UserCtx; None на платформенном контуре/без сессии."""
    if get_tenant_slug() is None:
        return None
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    data = await load_session(sid)
    if not data:
        return None
    return UserCtx(user_id=data["user_id"], email=data["email"], role=data["role"])


async def require_viewer(
    user: UserCtx | None = Depends(get_current_user),
) -> UserCtx:
    if user is None:
        # detail-маркер: SPA редиректит на логин ТОЛЬКО по нему (спека 5.2)
        raise HTTPException(status_code=401, detail="auth_required")
    return user


async def require_admin(user: UserCtx = Depends(require_viewer)) -> UserCtx:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin_required")
    return user


def _api_key_matches(provided: str, expected_hash: str | None) -> bool:
    if not expected_hash:
        return False
    digest = hashlib.sha256(provided.encode()).hexdigest()
    return secrets.compare_digest(digest, expected_hash)


async def require_ingestion_auth(
    request: Request,
    user: UserCtx | None = Depends(get_current_user),
) -> None:
    """Двойная авторизация ingestion-эндпоинтов (спека 5.3/5.6).

    Порядок: валидная cookie-сессия → ок; предъявленный X-API-Key обязан
    совпасть с ключом ИМЕННО этого тенанта (двойное совпадение), иначе 403;
    без креденшелов — пускаем только пока api_key_required=false (rollout
    6.4), иначе 401.
    """
    if user is not None:
        return
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    provided = request.headers.get("X-API-Key")
    if provided:
        if _api_key_matches(provided, tenant.get("api_key_hash")):
            return
        raise HTTPException(status_code=403, detail="api_key_mismatch")
    if not tenant.get("api_key_required", True):
        return
    raise HTTPException(status_code=401, detail="api_key_required")


async def require_amocrm_tenant(
    user: UserCtx = Depends(require_admin),
) -> UserCtx:
    """/api/amocrm/*: admin + только тенант с AmoCRM-интеграцией (спека 5.6)."""
    from tenancy.registry import AMOCRM_TENANT_SLUGS

    if get_tenant_slug() not in AMOCRM_TENANT_SLUGS:
        raise HTTPException(status_code=403, detail="amocrm_not_enabled")
    return user
