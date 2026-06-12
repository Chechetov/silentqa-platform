import asyncio

import pytest
from fastapi import HTTPException


def _req(cookies=None, tenant=False):
    """Мини-стаб Request: cookies + тенант-контекст через contextvar."""
    class R:
        pass
    r = R()
    r.cookies = cookies or {}
    return r


def test_require_platform_admin_needs_session(fake_redis):
    from app.auth_platform import get_current_platform_admin, require_platform_admin

    async def flow():
        assert await get_current_platform_admin(_req()) is None
        with pytest.raises(HTTPException) as e:
            await require_platform_admin(None)
        assert e.value.status_code == 401
        assert e.value.detail == "platform_auth_required"

    asyncio.run(flow())


def test_platform_admin_none_on_tenant_contour(fake_redis):
    from tenancy.context import set_tenant_schema, reset_tenant_schema
    from app import platform_sessions as ps
    from app.auth_platform import get_current_platform_admin
    from app.platform_sessions import PLATFORM_COOKIE

    async def flow():
        sid = await ps.create_platform_session("a1", "boss@x.io")
        token = set_tenant_schema("t_acme")
        try:
            assert await get_current_platform_admin(_req({PLATFORM_COOKIE: sid})) is None
        finally:
            reset_tenant_schema(token)
        ctx = await get_current_platform_admin(_req({PLATFORM_COOKIE: sid}))
        assert ctx.email == "boss@x.io"

    asyncio.run(flow())
