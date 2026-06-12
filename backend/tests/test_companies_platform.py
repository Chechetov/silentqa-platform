"""/api/companies: глобальные конфиги — только платформенный контур (спека §2).

Гейт — платформенная сессия (require_platform_admin); Basic выпилен (Plan 3b).
"""
import asyncio

import pytest
from starlette.testclient import TestClient

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


def _admin_cookie(fake_redis) -> dict:
    from app import platform_sessions as ps
    sid = asyncio.run(ps.create_platform_session("a1", "boss@x.io"))
    return {ps.PLATFORM_COOKIE: sid}


def test_companies_404_on_tenant_contour(clients, fake_redis):
    tenant, _ = clients
    # на тенант-контуре раздел не существует (контур-гейт Task 3)
    r = tenant.get("/api/companies", cookies=_admin_cookie(fake_redis))
    assert r.status_code == 404


def test_companies_401_on_platform_without_session(clients):
    _, platform = clients
    r = platform.get("/api/companies")
    assert r.status_code == 401
    assert r.json()["detail"] == "platform_auth_required"


def test_companies_ok_on_platform_with_session(clients, fake_redis):
    _, platform = clients
    r = platform.get("/api/companies", cookies=_admin_cookie(fake_redis))
    assert r.status_code == 200  # список (читает companies/*.json; пусто если каталога нет)
