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
