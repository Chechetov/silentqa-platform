import pytest
from starlette.testclient import TestClient

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app)


def test_platform_host_serves_admin_spa(client):
    r = client.get("/", headers={"Host": "admin.silentqa.com"})
    assert r.status_code == 200
    assert 'id="admin-app"' in r.text


def test_tenant_host_serves_client_spa(client):
    r = client.get("/", headers={"Host": "acme.silentqa.com"})
    assert r.status_code == 200
    assert 'id="admin-app"' not in r.text
