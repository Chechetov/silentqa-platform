"""Server-side dashboard sessions in Redis (spec 5.2) + login rate limit.

Key layout (spec 4.4): sessions  t:{slug}:sess:{sid}
                       ratelimit t:{slug}:rl:login:{ip}
Both are namespaced by the *current* tenant slug, so a session id stolen
from one tenant is useless on another (load_session reads through the
tenant context of the resolving request, not of the cookie).
"""
from __future__ import annotations

import json
import secrets

from tenancy.context import require_tenant_slug

from .config import settings
from .redis_client import get_redis

SESSION_COOKIE = "sqa_session"


def _sess_key(slug: str, sid: str) -> str:
    return f"t:{slug}:sess:{sid}"


async def create_session(user_id: str, email: str, role: str) -> str:
    sid = secrets.token_urlsafe(32)
    slug = require_tenant_slug()
    payload = json.dumps({"user_id": user_id, "email": email, "role": role})
    await get_redis().set(
        _sess_key(slug, sid), payload, ex=settings.SESSION_TTL_SECONDS
    )
    return sid


async def load_session(sid: str) -> dict | None:
    slug = require_tenant_slug()
    raw = await get_redis().get(_sess_key(slug, sid))
    return json.loads(raw) if raw else None


async def destroy_session(sid: str) -> None:
    slug = require_tenant_slug()
    await get_redis().delete(_sess_key(slug, sid))


async def register_login_attempt(ip: str, email: str) -> bool:
    """True — попытка разрешена; False — лимит исчерпан (отвечать 429).

    Два счётчика: основной — по email (за KZ-relay/Caddy client.host
    вырождается в IP релея для всего трафика, лимит чисто по IP лочил бы
    весь тенант одним атакующим); по IP — только backstop с множителем
    против тупого перебора множества email.
    """
    slug = require_tenant_slug()
    r = get_redis()
    email_key = f"t:{slug}:rl:login:email:{email.lower()}"
    ip_key = f"t:{slug}:rl:login:ip:{ip}"
    n_email = await r.incr(email_key)
    if n_email == 1:
        await r.expire(email_key, settings.LOGIN_RATE_WINDOW_SECONDS)
    n_ip = await r.incr(ip_key)
    if n_ip == 1:
        await r.expire(ip_key, settings.LOGIN_RATE_WINDOW_SECONDS)
    return (
        n_email <= settings.LOGIN_RATE_MAX_ATTEMPTS
        and n_ip <= settings.LOGIN_RATE_MAX_ATTEMPTS * settings.LOGIN_RATE_IP_MULTIPLIER
    )
