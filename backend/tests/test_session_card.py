import asyncio
from starlette.testclient import TestClient
from app.auth_sessions import SESSION_COOKIE


def _client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "fulldent", "schema_name": "t_fulldent", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True, "modules": {}}]

    async def fake_all(self):
        return rows
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://fulldent.silentqa.com")


def _viewer():
    from app import auth_sessions
    from tenancy.context import reset_tenant_schema, set_tenant_schema

    async def seed():
        t = set_tenant_schema("t_fulldent")
        try:
            return await auth_sessions.create_session("u", "v@t.io", "viewer")
        finally:
            reset_tenant_schema(t)
    return {SESSION_COOKIE: asyncio.run(seed())}


def test_card_requires_session(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    r = c.get("/api/sessions/00000000-0000-0000-0000-000000000001/card")
    assert r.status_code == 401


def test_card_404_when_no_file(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    r = c.get("/api/sessions/00000000-0000-0000-0000-000000000001/card", cookies=_viewer())
    assert r.status_code == 404
