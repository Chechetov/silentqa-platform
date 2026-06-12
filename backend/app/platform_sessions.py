"""Сессии платформенного админа: Redis platform:sess:*, cookie sqa_admin.

Отдельный неймспейс от тенантских t:{slug}:sess:* — украденный sid
платформы бесполезен на тенант-хосте и наоборот (разные cookie + разные
ключи). Контур уже отрезан middleware-гейтом, это второй пояс.
"""
from __future__ import annotations

import json
import secrets

from .auth_sessions import _register_attempt
from .config import settings
from .redis_client import get_redis

PLATFORM_COOKIE = "sqa_admin"


def _sess_key(sid: str) -> str:
    return f"platform:sess:{sid}"


async def create_platform_session(admin_id: str, email: str) -> str:
    sid = secrets.token_urlsafe(32)
    payload = json.dumps({"admin_id": admin_id, "email": email})
    await get_redis().set(_sess_key(sid), payload, ex=settings.SESSION_TTL_SECONDS)
    return sid


async def load_platform_session(sid: str) -> dict | None:
    raw = await get_redis().get(_sess_key(sid))
    return json.loads(raw) if raw else None


async def destroy_platform_session(sid: str) -> None:
    await get_redis().delete(_sess_key(sid))


async def register_platform_login_attempt(ip: str, email: str) -> bool:
    return await _register_attempt(
        f"platform:rl:login:email:{email.lower()}",
        f"platform:rl:login:ip:{ip}",
    )
