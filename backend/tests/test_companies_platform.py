"""/api/companies: глобальные конфиги — только платформенный контур (5.6).

До Plan 3 (platform-admin сессии) платформенный контур interim-гейтится
HTTP Basic с существующими AUTH_USERNAME/AUTH_PASSWORD.
"""
import base64

import pytest
from starlette.testclient import TestClient

from app.config import settings

ROWS = [{
    "slug": "realestate", "schema_name": "t_realestate", "status": "active",
    "custom_domains": [], "api_key_hash": None, "api_key_required": False,
}]


@pytest.fixture
def clients(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app

    tenant = TestClient(app, base_url="https://realestate.silentqa.com")
    platform = TestClient(app, base_url="https://silentqa.com")
    return tenant, platform


def _basic(user, pwd) -> dict:
    cred = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return {"Authorization": f"Basic {cred}"}


def test_companies_404_on_tenant_contour(clients):
    tenant, _ = clients
    # даже с валидным Basic — на тенант-контуре раздел не существует
    r = tenant.get("/api/companies",
                   headers=_basic(settings.AUTH_USERNAME, settings.AUTH_PASSWORD))
    assert r.status_code == 404


def test_companies_401_on_platform_without_basic(clients):
    _, platform = clients
    r = platform.get("/api/companies")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Basic"


def test_companies_ok_on_platform_with_basic(clients):
    _, platform = clients
    r = platform.get("/api/companies",
                     headers=_basic(settings.AUTH_USERNAME, settings.AUTH_PASSWORD))
    # 200 — список (читает companies/*.json с диска, БД не нужна)
    assert r.status_code == 200
