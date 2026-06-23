import asyncio

from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE


def _rows(modules):
    return [{"slug": "acme", "schema_name": "t_acme", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": modules}]


def _client(monkeypatch, modules):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return _rows(modules)

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def _cookie(role):
    from app import auth_sessions
    from tenancy.context import reset_tenant_schema, set_tenant_schema

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u", "x@t.io", role)
        finally:
            reset_tenant_schema(token)

    return {SESSION_COOKIE: asyncio.run(seed())}


def test_knowledge_gated_off_403(monkeypatch, fake_redis):
    c = _client(monkeypatch, {"knowledge_base": False})
    r = c.get("/api/knowledge/categories", cookies=_cookie("viewer"))
    assert r.status_code == 403 and r.json()["detail"] == "module_disabled"


def test_categories_require_session(monkeypatch, fake_redis):
    c = _client(monkeypatch, {"knowledge_base": True})
    assert c.get("/api/knowledge/categories").status_code == 401


def test_create_category_admin_only(monkeypatch, fake_redis):
    c = _client(monkeypatch, {"knowledge_base": True})
    r = c.post("/api/knowledge/categories", cookies=_cookie("viewer"),
               json={"name": "C", "slug": "c"})
    assert r.status_code == 403 and r.json()["detail"] == "admin_required"
