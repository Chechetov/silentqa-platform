import asyncio


def test_platform_session_roundtrip(fake_redis):
    from app import platform_sessions as ps

    async def flow():
        sid = await ps.create_platform_session("a1", "boss@x.io")
        data = await ps.load_platform_session(sid)
        assert data == {"admin_id": "a1", "email": "boss@x.io"}
        assert fake_redis.ttls[f"platform:sess:{sid}"] > 0
        await ps.destroy_platform_session(sid)
        assert await ps.load_platform_session(sid) is None

    asyncio.run(flow())


def test_platform_rate_limit_by_email(fake_redis, monkeypatch):
    from app import platform_sessions as ps
    from app.config import settings
    monkeypatch.setattr(settings, "LOGIN_RATE_MAX_ATTEMPTS", 3)

    async def flow():
        for _ in range(3):
            assert await ps.register_platform_login_attempt("1.1.1.1", "boss@x.io")
        assert not await ps.register_platform_login_attempt("2.2.2.2", "boss@x.io")

    asyncio.run(flow())


def test_tenant_rate_limit_still_works(fake_redis):
    """Рефактор _register_attempt не сломал тенантский путь."""
    import asyncio
    from tenancy.context import set_tenant_schema, reset_tenant_schema
    from app.auth_sessions import register_login_attempt

    async def flow():
        token = set_tenant_schema("t_acme")
        try:
            assert await register_login_attempt("1.1.1.1", "u@x.io")
        finally:
            reset_tenant_schema(token)

    asyncio.run(flow())
