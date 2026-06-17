def test_registry_row_exposes_modules(monkeypatch):
    # async registry maps the modules column into the cached row dict
    import asyncio
    from app.tenancy_http import TenantRegistry

    class FakeRow:
        slug = "acme"; schema_name = "t_acme"; status = "active"
        custom_domains = []; api_key_hash = None; api_key_required = True
        modules = {"knowledge_base": True, "complexes": False}
        display_name = "Acme"
        company_config_id = None

    class FakeRes:
        def __iter__(self): return iter([FakeRow()])

    class FakeDB:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *a, **k): return FakeRes()

    monkeypatch.setattr("app.database.async_session", lambda: FakeDB())
    reg = TenantRegistry(ttl_seconds=0)
    rows = asyncio.run(reg.all_tenants())
    assert rows[0]["modules"] == {"knowledge_base": True, "complexes": False}


from app.modules import MODULE_DEFAULTS, module_enabled


def test_default_semantics():
    # knowledge_base absent → ON; complexes/amocrm absent → OFF
    assert module_enabled({}, "knowledge_base") is True
    assert module_enabled({}, "complexes") is False
    assert module_enabled({}, "amocrm") is False


def test_explicit_overrides_default():
    assert module_enabled({"knowledge_base": False}, "knowledge_base") is False
    assert module_enabled({"complexes": True}, "complexes") is True


def test_unknown_module_defaults_off():
    assert module_enabled({}, "nonexistent") is False
    assert "knowledge_base" in MODULE_DEFAULTS


import asyncio

from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE


async def _coro(v):
    return v


def _admin_cookie(schema):
    from app import auth_sessions
    from tenancy.context import reset_tenant_schema, set_tenant_schema

    async def seed():
        token = set_tenant_schema(schema)
        try:
            return await auth_sessions.create_session("u1", "a@t.io", "admin")
        finally:
            reset_tenant_schema(token)

    return {SESSION_COOKIE: asyncio.run(seed())}


def test_amocrm_blocked_when_module_off(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    rows = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": {"amocrm": False}}]
    monkeypatch.setattr(TenantRegistry, "all_tenants", lambda self: _coro(rows))
    from app.main import app

    c = TestClient(app, base_url="https://acme.silentqa.com")
    r = c.post("/api/amocrm/reprocess", cookies=_admin_cookie("t_acme"), json={"lead_id": 1})
    assert r.status_code == 403
    assert r.json()["detail"] == "module_disabled"
