"""Дашборд-авторизация тенант-юзеров: /api/user-auth/* (спека 5.2).

Отдельный неймспейс: /api/auth/* занят брокерским (recorder) контуром.
Пароли — argon2 (как сеет provision_tenant.py). Сессии — Redis.
"""
from __future__ import annotations

import json
import logging

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tenancy.context import get_tenant_slug

from .. import auth_sessions
from ..auth_user import UserCtx, get_current_user, require_viewer
from ..config import settings
from ..database import get_db
from ..redis_client import get_redis

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/user-auth", tags=["user-auth"])

IMPERSONATION_SESSION_TTL = 7200  # 2 часа (спека §3.4)

_ph = PasswordHasher()
# Статический argon2-хеш заведомо неверного секрета: verify против него на
# ветке "email не найден" выравнивает тайминг (не раскрываем существование
# аккаунта). Тот же приём, что в брокерском /api/auth/login.
_DUMMY_HASH = _ph.hash("timing-equalizer-not-a-real-password")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


def _verify(stored_hash: str, password: str) -> bool:
    try:
        _ph.verify(stored_hash, password)
        return True
    except Exception:
        return False


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    # Инвариант 9: на платформенном контуре таблицы users нет — 404 до SQL.
    if get_tenant_slug() is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")

    ip = request.client.host if request.client else "unknown"
    if not await auth_sessions.register_login_attempt(ip, body.email):
        raise HTTPException(status_code=429, detail="too_many_attempts")

    row = (
        await db.execute(
            text(
                "SELECT id, email, password_hash, role FROM users "
                "WHERE LOWER(email) = LOWER(:email)"
            ),
            {"email": body.email},
        )
    ).first()

    stored = row.password_hash if row is not None else _DUMMY_HASH
    if not _verify(stored, body.password) or row is None:
        raise HTTPException(status_code=401, detail="invalid_credentials")

    await db.execute(
        text("UPDATE users SET last_login = now() WHERE id = :id"),
        {"id": row.id},
    )
    await db.commit()

    sid = await auth_sessions.create_session(str(row.id), row.email, row.role)
    response.set_cookie(
        auth_sessions.SESSION_COOKIE,
        sid,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite="lax",
        max_age=settings.SESSION_TTL_SECONDS,
        path="/",
        # Domain НЕ задаём — host-only cookie скоупится на домен тенанта.
    )
    return {"email": row.email, "role": row.role}


@router.post("/logout")
async def logout(request: Request, response: Response):
    if get_tenant_slug() is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    sid = request.cookies.get(auth_sessions.SESSION_COOKIE)
    if sid:
        await auth_sessions.destroy_session(sid)
    response.delete_cookie(auth_sessions.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/impersonate")
async def impersonate_exchange(token: str):
    """Тенант-контур: сжигаем одноразовый токен (атомарный GETDEL),
    проверяем slug, выдаём короткую сессию роль admin + impersonated_by."""
    slug = get_tenant_slug()
    if slug is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    raw = await get_redis().getdel(f"platform:imp:{token}")
    if not raw:
        raise HTTPException(status_code=401, detail="invalid_token")
    data = json.loads(raw)
    if data["slug"] != slug:
        raise HTTPException(status_code=401, detail="invalid_token")
    sid = await auth_sessions.create_session(
        "platform-admin", data["admin_email"], "admin",
        impersonated_by=data["admin_email"], ttl=IMPERSONATION_SESSION_TTL,
    )
    logger.info("impersonation login: %s -> %s", data["admin_email"], slug)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        auth_sessions.SESSION_COOKIE, sid,
        httponly=True, secure=settings.SESSION_COOKIE_SECURE, samesite="lax",
        max_age=IMPERSONATION_SESSION_TTL, path="/",
    )
    return resp


@router.get("/me")
async def me(user: UserCtx | None = Depends(get_current_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="auth_required")
    return {"email": user.email, "role": user.role,
            "employee_name": user.employee_name,
            "impersonated_by": user.impersonated_by}


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


async def _get_password_hash(db: AsyncSession, user_id: str) -> str | None:
    row = (await db.execute(text(
        "SELECT password_hash FROM users WHERE id = :id"), {"id": user_id})).first()
    return row.password_hash if row else None


async def _set_password_hash(db: AsyncSession, user_id: str, new_hash: str) -> None:
    await db.execute(text(
        "UPDATE users SET password_hash = :pw WHERE id = :id"),
        {"pw": new_hash, "id": user_id})
    await db.commit()


@router.post("/change-password")
async def change_password(body: ChangePasswordRequest, request: Request,
                          db: AsyncSession = Depends(get_db),
                          user: UserCtx = Depends(require_viewer)):
    if len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="password_too_short")
    stored = await _get_password_hash(db, user.user_id)
    if stored is None or not _verify(stored, body.old_password):
        raise HTTPException(status_code=403, detail="wrong_password")
    await _set_password_hash(db, user.user_id, _ph.hash(body.new_password))
    keep = request.cookies.get(auth_sessions.SESSION_COOKIE)
    await auth_sessions.destroy_user_sessions(user.email, keep_sid=keep)
    return {"ok": True}
