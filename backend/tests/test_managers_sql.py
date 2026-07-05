import asyncio
import pytest
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def _cookie(fake_redis, role="viewer", employee=None):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u1", "v@x.io", role,
                                                      employee_name=employee)
        finally:
            reset_tenant_schema(token)
    return {SESSION_COOKIE: asyncio.run(seed())}


def test_list_managers_shape_from_sql(client, fake_redis, monkeypatch):
    from app.routes import managers as m

    async def agg(db, scope):
        return [{"name": "Иванов", "total_calls": 7, "avg_score": 6.43,
                 "last_call_date": "2026-07-01T00:00:00+00:00"}]
    monkeypatch.setattr(m, "_manager_agg_rows", agg)
    r = client.get("/api/managers", cookies=_cookie(fake_redis))
    assert r.status_code == 200
    assert r.json() == [{"name": "Иванов", "total_calls": 7, "avg_score": 6.43,
                         "last_call_date": "2026-07-01T00:00:00+00:00"}]


def test_manager_scope_filters(client, fake_redis, monkeypatch):
    from app.routes import managers as m
    seen = {}

    async def agg(db, scope):
        seen["scope"] = scope
        return []
    monkeypatch.setattr(m, "_manager_agg_rows", agg)
    client.get("/api/managers", cookies=_cookie(fake_redis, role="manager",
                                                employee="Иванов"))
    assert seen["scope"] == "Иванов"


def test_manager_sessions_404_when_empty(client, fake_redis, monkeypatch):
    from app.routes import managers as m

    async def none_rows(db, name):
        return []
    monkeypatch.setattr(m, "_sessions_for", none_rows)
    r = client.get("/api/managers/Иванов/sessions", cookies=_cookie(fake_redis))
    assert r.status_code == 404
