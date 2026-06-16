import asyncio

from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE


def _client(monkeypatch, fake_redis, modules):
    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True, "modules": modules}]

    async def fake_all(self):
        return rows

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def _viewer():
    from app import auth_sessions
    from tenancy.context import reset_tenant_schema, set_tenant_schema

    async def seed():
        t = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u", "v@t.io", "viewer")
        finally:
            reset_tenant_schema(t)

    return {SESSION_COOKIE: asyncio.run(seed())}


def test_extraction_403_when_complexes_off(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis, {"complexes": False})
    r = c.get("/api/sessions/00000000-0000-0000-0000-000000000001/extraction", cookies=_viewer())
    assert r.status_code == 403 and r.json()["detail"] == "module_disabled"


def test_tags_endpoint_requires_session(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis, {"knowledge_base": True})
    assert c.get("/api/sessions/00000000-0000-0000-0000-000000000001/tags").status_code == 401


def test_kb_tag_invalid_uuid_422(monkeypatch, fake_redis):
    # kb_tag is typed uuid.UUID → a non-UUID value is rejected at the route
    # boundary (422), never reaching the DB as a `uuid = text` comparison (500).
    c = _client(monkeypatch, fake_redis, {"knowledge_base": True})
    r = c.get("/api/sessions?kb_tag=notauuid", cookies=_viewer())
    assert r.status_code == 422
