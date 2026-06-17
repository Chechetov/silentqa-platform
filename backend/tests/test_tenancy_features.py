import pytest
from starlette.testclient import TestClient

ROWS = [{
    "slug": "acme", "schema_name": "t_acme", "status": "active",
    "custom_domains": [], "api_key_hash": None, "api_key_required": True,
    "modules": {"complexes": False},
}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def test_features_resolves_defaults(client):
    r = client.get("/api/tenancy/features")
    assert r.status_code == 200
    body = r.json()
    assert body["knowledge_base"] is True   # default ON
    assert body["complexes"] is False        # explicit OFF
    assert body["amocrm"] is False           # default OFF


def test_features_includes_display_name(monkeypatch, fake_redis):
    # реестр-строка с display_name → features отдаёт его наружу
    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "fulldent", "schema_name": "t_fulldent", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": {}, "display_name": "Клиника Фулдент"}]

    async def fake_all(self):
        return rows

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    from starlette.testclient import TestClient
    c = TestClient(app, base_url="https://fulldent.silentqa.com")
    r = c.get("/api/tenancy/features")
    assert r.status_code == 200
    assert r.json().get("display_name") == "Клиника Фулдент"
