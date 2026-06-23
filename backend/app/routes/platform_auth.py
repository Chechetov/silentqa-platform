"""Логин платформенного админа: /api/platform/auth/* (спека §2)."""
from __future__ import annotations

import logging

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import platform_sessions
from ..auth_platform import PlatformAdminCtx, require_platform_admin
from ..config import settings
from ..database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/platform/auth", tags=["platform-auth"])

_ph = PasswordHasher()
# Статический argon2-хеш для выравнивания тайминга: если email не найден,
# верифицируем пароль против этого хеша, чтобы не раскрывать существование
# аккаунта через разницу во времени ответа (timing-safe ветка row is None).
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


async def _fetch_admin(db: AsyncSession, email: str):
    row = (
        await db.execute(
            text("SELECT id, email, password_hash FROM shared.platform_admins "
                 "WHERE LOWER(email) = LOWER(:email)"),
            {"email": email},
        )
    ).first()
    if row is None:
        return None
    return {"id": str(row.id), "email": row.email, "password_hash": row.password_hash}


async def _touch_last_login(db: AsyncSession, admin_id: str) -> None:
    await db.execute(
        text("UPDATE shared.platform_admins SET last_login = now() WHERE id = :id"),
        {"id": admin_id},
    )
    await db.commit()


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response,
                db: AsyncSession = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    if not await platform_sessions.register_platform_login_attempt(ip, body.email):
        raise HTTPException(status_code=429, detail="too_many_attempts")

    row = await _fetch_admin(db, body.email)
    stored = row["password_hash"] if row else _DUMMY_HASH
    if not _verify(stored, body.password) or row is None:
        raise HTTPException(status_code=401, detail="invalid_credentials")

    await _touch_last_login(db, row["id"])
    sid = await platform_sessions.create_platform_session(row["id"], row["email"])
    response.set_cookie(
        platform_sessions.PLATFORM_COOKIE, sid,
        httponly=True, secure=settings.SESSION_COOKIE_SECURE, samesite="lax",
        max_age=settings.SESSION_TTL_SECONDS, path="/",
    )
    logger.info("platform admin login: %s", row["email"])
    return {"email": row["email"]}


@router.post("/logout")
async def logout(request: Request, response: Response):
    sid = request.cookies.get(platform_sessions.PLATFORM_COOKIE)
    if sid:
        await platform_sessions.destroy_platform_session(sid)
    response.delete_cookie(platform_sessions.PLATFORM_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(admin: PlatformAdminCtx = Depends(require_platform_admin)):
    return {"email": admin.email}
