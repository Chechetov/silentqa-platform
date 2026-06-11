"""Дашборд-авторизация тенант-юзеров: /api/user-auth/* (спека 5.2).

Отдельный неймспейс: /api/auth/* занят брокерским (recorder) контуром.
Пароли — argon2 (как сеет provision_tenant.py). Сессии — Redis.
"""
from __future__ import annotations

import logging

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tenancy.context import get_tenant_slug

from .. import auth_sessions
from ..auth_user import UserCtx, get_current_user
from ..config import settings
from ..database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/user-auth", tags=["user-auth"])

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


@router.get("/me")
async def me(user: UserCtx | None = Depends(get_current_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="auth_required")
    return {"email": user.email, "role": user.role}
