"""Матрица доступа 5.6: negative-кейсы против реального app (без БД).

Auth-зависимости отрабатывают до первого SQL (ленивое соединение) — поэтому
401/403 проверяемы без Postgres. Positive-пути покрыты в test_auth_deps.py.
"""
import asyncio

import pytest
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE


ROWS = [{
    "slug": "realestate", "schema_name": "t_realestate", "status": "active",
    "custom_domains": [], "api_key_hash": None, "api_key_required": True,
}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app  # noqa: WPS433 — import после патча безопасен

    return TestClient(app, base_url="https://realestate.silentqa.com")


@pytest.fixture
def platform_client(monkeypatch, fake_redis):
    """TestClient на платформенном хосте admin.silentqa.com."""
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app  # noqa: WPS433 — import после патча безопасен

    return TestClient(app, base_url="https://admin.silentqa.com")


def _viewer_cookie(fake_redis) -> dict:
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_realestate")
        try:
            return await auth_sessions.create_session("u1", "v@t.io", "viewer")
        finally:
            reset_tenant_schema(token)

    sid = asyncio.run(seed())  # инвариант 13
    return {SESSION_COOKIE: sid}


def _manager_cookie(fake_redis) -> dict:
    """Создаёт manager-сессию под t_realestate."""
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_realestate")
        try:
            return await auth_sessions.create_session("u2", "m@t.io", "manager")
        finally:
            reset_tenant_schema(token)

    sid = asyncio.run(seed())  # инвариант 13
    return {SESSION_COOKIE: sid}


PLATFORM_MUTATIONS = [
    ("get", "/api/platform/tenants"),
    ("post", "/api/platform/tenants"),
    ("patch", "/api/platform/tenants/acme"),
    ("post", "/api/platform/tenants/acme/rotate-key"),
    ("get", "/api/platform/tenants/acme/users"),
    ("post", "/api/platform/tenants/acme/impersonate"),
    ("get", "/api/companies"),
]

TEAM_ADMIN_ONLY = [
    ("get", "/api/users"),
    ("post", "/api/users"),
    ("patch", "/api/users/00000000-0000-0000-0000-000000000001"),
    ("delete", "/api/users/00000000-0000-0000-0000-000000000001"),
    ("post", "/api/users/00000000-0000-0000-0000-000000000001/reset-password"),
]

VIEWER_GETS = [
    "/api/sessions",
    "/api/sessions/00000000-0000-0000-0000-000000000001/extraction",
    "/api/sessions/00000000-0000-0000-0000-000000000001/transcript",
    "/api/sessions/00000000-0000-0000-0000-000000000001/analysis",
    "/api/sessions/00000000-0000-0000-0000-000000000001/audio",
    "/api/sessions/00000000-0000-0000-0000-000000000001/sentiment",
    "/api/sessions/00000000-0000-0000-0000-000000000001/quality",
    "/api/sessions/00000000-0000-0000-0000-000000000001/full",
    "/api/managers",
    "/api/templates",
    "/api/complexes",
]

INGESTION = [
    ("post", "/api/sessions"),
    ("get", "/api/sessions/00000000-0000-0000-0000-000000000001"),
    ("post", "/api/sessions/00000000-0000-0000-0000-000000000001/finish"),
    ("post", "/api/sessions/00000000-0000-0000-0000-000000000001/link-lead"),
    ("post", "/api/sessions/00000000-0000-0000-0000-000000000001/chunks"),
    ("get", "/api/sessions/00000000-0000-0000-0000-000000000001/missing-chunks"),
    ("post", "/api/sessions/00000000-0000-0000-0000-000000000001/upload-audio"),
]

ADMIN_MUTATIONS = [
    ("delete", "/api/sessions/00000000-0000-0000-0000-000000000001"),
    ("post", "/api/sessions/00000000-0000-0000-0000-000000000001/reprocess"),
    ("patch", "/api/sessions/00000000-0000-0000-0000-000000000001/speaker-map"),
    ("patch", "/api/sessions/00000000-0000-0000-0000-000000000001/reassign-speaker"),
    ("post", "/api/templates"),
    ("patch", "/api/templates/00000000-0000-0000-0000-000000000001"),
    ("delete", "/api/templates/00000000-0000-0000-0000-000000000001"),
    ("patch", "/api/complexes/00000000-0000-0000-0000-000000000001"),
    ("delete", "/api/complexes/00000000-0000-0000-0000-000000000001"),
    ("post", "/api/complexes/00000000-0000-0000-0000-000000000001/merge"),
    ("post", "/api/extractions/00000000-0000-0000-0000-000000000001/relink"),
    # /api/amocrm/* безопасны здесь только с гейтом require_amocrm_tenant
    # (Task 12): зависимость отвечает 403 ДО handler'а — живой AmoCRM не дёргается.
    ("post", "/api/amocrm/reprocess"),
    ("get", "/api/amocrm/search-leads?q=ab"),
]


@pytest.mark.parametrize("path", VIEWER_GETS)
def test_viewer_endpoints_401_without_session(client, path):
    r = client.get(path)
    assert r.status_code == 401, path
    assert r.json()["detail"] == "auth_required"


@pytest.mark.parametrize("method,path", INGESTION)
def test_ingestion_endpoints_401_without_creds(client, method, path):
    r = getattr(client, method)(path)
    assert r.status_code == 401, (method, path)
    assert r.json()["detail"] == "api_key_required"


@pytest.mark.parametrize("method,path", ADMIN_MUTATIONS)
def test_admin_mutations_403_for_viewer(client, fake_redis, method, path):
    cookies = _viewer_cookie(fake_redis)
    r = getattr(client, method)(path, cookies=cookies)
    assert r.status_code == 403, (method, path)


def test_webhooks_surface_removed(client):
    assert client.get("/api/webhooks").status_code == 404
    # POST падает в StaticFiles-маунт "/" → 405 Method Not Allowed (не 404!)
    assert client.post("/api/webhooks", json={}).status_code in (404, 405)


def test_broker_auth_contour_stays_open(client):
    # /api/auth/* не за cookie-гейтом (спека 5.6): claim/start валидирует
    # body (422 без него), а не требует сессию (не 401 auth_required)
    r = client.post("/api/auth/claim/start", json={})
    assert r.status_code == 422


def test_health_open(client):
    assert client.get("/health").status_code == 200


def test_ui_routes_public_no_basic(client):
    # SPA-статика публична by design (спека 5.2): / отдаёт index.html без Basic
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_recorder_route_removed(client):
    # роут указывал на отсутствующий static/recorder.html — удалён
    r = client.get("/recorder")
    assert r.status_code in (404, 200)  # 404 или index.html от SPA-fallback
    # главное: это больше не FileResponse несуществующего файла (раньше — 500)


@pytest.mark.parametrize("method,path", PLATFORM_MUTATIONS)
def test_platform_mutations_401_without_platform_session(platform_client, method, path):
    """Платформенные мутации на admin-хосте без cookie → 401 platform_auth_required."""
    r = getattr(platform_client, method)(path)
    assert r.status_code == 401, (method, path)
    assert r.json()["detail"] == "platform_auth_required"


@pytest.mark.parametrize("method,path", PLATFORM_MUTATIONS)
def test_platform_paths_404_on_tenant_host(client, method, path):
    """Платформенные пути на тенант-хосте → 404 (контур-гейт, спека §1)."""
    r = getattr(client, method)(path)
    assert r.status_code == 404, (method, path)
    assert r.json()["detail"] == "Not found"


@pytest.mark.parametrize("method,path", TEAM_ADMIN_ONLY)
def test_team_admin_only_403_for_viewer(client, fake_redis, method, path):
    """Команда /api/users: viewer → 403 admin_required."""
    cookies = _viewer_cookie(fake_redis)
    r = getattr(client, method)(path, cookies=cookies)
    assert r.status_code == 403, (method, path)
    assert r.json()["detail"] == "admin_required", (method, path)


@pytest.mark.parametrize("method,path", TEAM_ADMIN_ONLY)
def test_team_admin_only_403_for_manager(client, fake_redis, method, path):
    """Команда /api/users: manager → 403 admin_required."""
    cookies = _manager_cookie(fake_redis)
    r = getattr(client, method)(path, cookies=cookies)
    assert r.status_code == 403, (method, path)
    assert r.json()["detail"] == "admin_required", (method, path)


def test_change_password_401_for_anonymous(client):
    """/api/user-auth/change-password без cookie (аноним) → 401."""
    r = client.post("/api/user-auth/change-password", json={
        "old_password": "x", "new_password": "y"
    })
    assert r.status_code == 401, r.status_code
    assert r.json()["detail"] == "auth_required"


def test_amocrm_admin_of_non_amocrm_tenant_403(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    rows = [{
        "slug": "acme", "schema_name": "t_acme", "status": "active",
        "custom_domains": [], "api_key_hash": None, "api_key_required": True,
    }]

    async def fake_all(self):
        return rows

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u1", "a@t.io", "admin")
        finally:
            reset_tenant_schema(token)

    sid = asyncio.run(seed())  # инвариант 13
    c = TestClient(app, base_url="https://acme.silentqa.com")
    r = c.post("/api/amocrm/reprocess", cookies={SESSION_COOKIE: sid},
               json={"lead_id": 1})
    assert r.status_code == 403
    assert r.json()["detail"] == "amocrm_not_enabled"
