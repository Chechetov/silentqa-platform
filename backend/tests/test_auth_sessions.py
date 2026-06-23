"""Redis session store: tenant-namespaced keys, TTL, rate limit."""
import pytest

from tenancy.context import reset_tenant_schema, set_tenant_schema

from app import auth_sessions
from app.config import settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def tenant_ctx():
    token = set_tenant_schema("t_realestate")
    yield
    reset_tenant_schema(token)


@pytest.mark.anyio
async def test_create_load_destroy_roundtrip(fake_redis, tenant_ctx):
    sid = await auth_sessions.create_session("u1", "a@b.c", "admin")
    assert sid and len(sid) >= 32
    key = f"t:realestate:sess:{sid}"
    assert key in fake_redis.store
    assert fake_redis.ttls[key] == settings.SESSION_TTL_SECONDS
    data = await auth_sessions.load_session(sid)
    assert data == {"user_id": "u1", "email": "a@b.c", "role": "admin",
                    "employee_name": None, "impersonated_by": None}
    await auth_sessions.destroy_session(sid)
    assert await auth_sessions.load_session(sid) is None


@pytest.mark.anyio
async def test_sessions_do_not_cross_tenants(fake_redis):
    token = set_tenant_schema("t_realestate")
    sid = await auth_sessions.create_session("u1", "a@b.c", "viewer")
    reset_tenant_schema(token)

    token = set_tenant_schema("t_acme")
    assert await auth_sessions.load_session(sid) is None
    reset_tenant_schema(token)


@pytest.mark.anyio
async def test_login_rate_limit_per_email(fake_redis, tenant_ctx):
    # за relay/Caddy client.host вырождается в один IP (см. комментарий в
    # реализации) — основной лимит по email
    for _ in range(settings.LOGIN_RATE_MAX_ATTEMPTS):
        assert await auth_sessions.register_login_attempt("1.2.3.4", "a@b.c") is True
    assert await auth_sessions.register_login_attempt("1.2.3.4", "a@b.c") is False
    # другой email с того же IP — независимый счётчик (IP-лимит выше)
    assert await auth_sessions.register_login_attempt("1.2.3.4", "x@y.z") is True


@pytest.mark.anyio
async def test_login_rate_limit_ip_backstop(fake_redis, tenant_ctx):
    limit = settings.LOGIN_RATE_MAX_ATTEMPTS * settings.LOGIN_RATE_IP_MULTIPLIER
    for i in range(limit):
        assert await auth_sessions.register_login_attempt("9.9.9.9", f"u{i}@t.io") is True
    assert await auth_sessions.register_login_attempt("9.9.9.9", "uX@t.io") is False
