"""domain-check: гейт для Caddy on_demand_tls ask (спека Plan 3a §3)."""
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.routes import tenancy_check


class StubRegistry:
    def __init__(self):
        self.rows = [
            {"slug": "fulldent", "schema_name": "t_fulldent", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True},
            {"slug": "legacy", "schema_name": "t_legacy", "status": "active",
             "custom_domains": ["old.example.com"], "api_key_hash": None,
             "api_key_required": True},
            {"slug": "frozen", "schema_name": "t_frozen", "status": "suspended",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True},
        ]

    async def all_tenants(self):
        return self.rows


def make_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(tenancy_check, "_registry", StubRegistry())
    app = FastAPI()
    app.include_router(tenancy_check.router)
    return TestClient(app)


def test_apex_and_admin_allowed(monkeypatch):
    c = make_client(monkeypatch)
    assert c.get("/api/tenancy/domain-check?domain=silentqa.com").status_code == 200
    assert c.get("/api/tenancy/domain-check?domain=admin.silentqa.com").status_code == 200


def test_active_tenant_subdomain_allowed(monkeypatch):
    c = make_client(monkeypatch)
    assert c.get("/api/tenancy/domain-check?domain=fulldent.silentqa.com").status_code == 200
    # регистр и трейлинг-точка нормализуются
    assert c.get("/api/tenancy/domain-check?domain=FullDent.SilentQA.com.").status_code == 200


def test_custom_domain_allowed(monkeypatch):
    c = make_client(monkeypatch)
    assert c.get("/api/tenancy/domain-check?domain=old.example.com").status_code == 200


def test_rejections(monkeypatch):
    c = make_client(monkeypatch)
    for bad in ("ghost.silentqa.com",
                "frozen.silentqa.com",
                "a.b.silentqa.com",
                "silentqa.com.evil.com",
                ""):
        r = c.get("/api/tenancy/domain-check", params={"domain": bad})
        assert r.status_code == 404, bad
