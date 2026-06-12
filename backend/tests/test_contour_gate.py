"""Изоляция контуров (спека §1): пути чужого контура → 404 ДО auth."""
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


PLATFORM_ONLY = ["/api/platform/tenants", "/api/platform/auth/me", "/api/companies"]
TENANT_ONLY = ["/api/sessions", "/api/managers", "/api/templates", "/api/user-auth/me"]


@pytest.mark.parametrize("path", PLATFORM_ONLY)
def test_platform_paths_404_on_tenant_host(client, path):
    # detail == "Not found" (маленькая f) — гейтовый маркер: доказывает, что
    # 404 пришёл ИМЕННО от гейта, а не от отсутствия роута ("Not Found").
    r = client.get(path, headers={"Host": "acme.silentqa.com"})
    assert r.status_code == 404
    assert r.json()["detail"] == "Not found"


@pytest.mark.parametrize("path", TENANT_ONLY)
def test_tenant_paths_404_on_platform_host(client, path):
    for host in ("silentqa.com", "admin.silentqa.com"):
        r = client.get(path, headers={"Host": host})
        assert r.status_code == 404
        assert r.json()["detail"] == "Not found"


def test_platform_path_passes_gate_on_platform_host(client):
    # Позитив: платформенный путь на платформенном хосте проходит гейт и
    # доходит до платформенной auth-зависимости (НЕ 404). Ловит инверсию условия.
    r = client.get("/api/companies", headers={"Host": "admin.silentqa.com"})
    assert r.status_code != 404
    assert r.status_code == 401  # platform_auth_required (нет сессии), гейт пропустил


def test_tenant_path_passes_gate_on_tenant_host(client):
    # Позитив: тенантский путь на тенант-хосте проходит гейт и доходит до
    # auth (НЕ 404).
    r = client.get("/api/sessions", headers={"Host": "acme.silentqa.com"})
    assert r.status_code != 404
    assert r.status_code == 401  # auth_required, гейт пропустил


def test_domain_check_alive_on_both(client):
    for host in ("admin.silentqa.com", "acme.silentqa.com"):
        r = client.get("/api/tenancy/domain-check?domain=acme.silentqa.com",
                       headers={"Host": host})
        assert r.status_code == 200
