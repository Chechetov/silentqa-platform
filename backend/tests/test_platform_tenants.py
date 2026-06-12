import asyncio
import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return []

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://admin.silentqa.com")


def _admin_cookie(fake_redis) -> dict:
    from app import platform_sessions as ps

    sid = asyncio.run(ps.create_platform_session("a1", "boss@x.io"))
    return {ps.PLATFORM_COOKIE: sid}


def test_tenants_list_requires_auth(client):
    assert client.get("/api/platform/tenants").status_code == 401


def test_tenants_list_with_stats(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_rows(db):
        return [{"slug": "acme", "display_name": "ACME", "status": "active",
                 "schema_name": "t_acme", "created_at": "2026-06-01T00:00:00Z"}]

    async def fake_stats(db, schema, dt_from, dt_to):
        return {"sessions_count": 5, "minutes": 42, "last_activity": "2026-06-11T10:00:00Z"}

    monkeypatch.setattr(pt, "_tenant_rows", fake_rows)
    monkeypatch.setattr(pt, "_tenant_stats", fake_stats)
    r = client.get("/api/platform/tenants", cookies=_admin_cookie(fake_redis))
    assert r.status_code == 200
    item = r.json()[0]
    assert item["slug"] == "acme" and item["sessions_count"] == 5


def test_create_tenant_returns_secrets_once(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt
    from app.provision_tenant import ProvisionResult

    def fake_provision(slug, name, email, password=""):
        return ProvisionResult(slug=slug, schema=f"t_{slug}", admin_email=email,
                               admin_password="genpw", api_key="sqa_xyz")

    monkeypatch.setattr(pt, "provision", fake_provision)
    r = client.post("/api/platform/tenants", cookies=_admin_cookie(fake_redis),
                    json={"slug": "newco", "display_name": "NewCo",
                          "admin_email": "a@n.co"})
    assert r.status_code == 201
    body = r.json()
    assert body["api_key"] == "sqa_xyz" and body["admin_password"] == "genpw"


def test_create_tenant_conflict_409(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt
    from app.provision_tenant import ProvisionError

    def fake_provision(*a, **k):
        raise ProvisionError("tenant 'x' already exists")

    monkeypatch.setattr(pt, "provision", fake_provision)
    r = client.post("/api/platform/tenants", cookies=_admin_cookie(fake_redis),
                    json={"slug": "x", "display_name": "", "admin_email": "a@b.c"})
    assert r.status_code == 409


def test_create_tenant_invalid_slug_422(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    def fake_provision(*a, **k):
        raise ValueError("invalid slug")

    monkeypatch.setattr(pt, "provision", fake_provision)
    r = client.post("/api/platform/tenants", cookies=_admin_cookie(fake_redis),
                    json={"slug": "Bad Slug!", "display_name": "", "admin_email": "a@b.c"})
    assert r.status_code == 422


def test_provision_result_repr_hides_secrets():
    from app.provision_tenant import ProvisionResult
    r = ProvisionResult(slug="acme", schema="t_acme", admin_email="a@b.c",
                        admin_password="topsecret", api_key="sqa_secret")
    text = repr(r)
    assert "topsecret" not in text and "sqa_secret" not in text
    assert "acme" in text


def test_patch_status_validates(client, fake_redis):
    r = client.patch("/api/platform/tenants/acme", cookies=_admin_cookie(fake_redis),
                     json={"status": "bogus"})
    assert r.status_code == 422


def test_patch_status_suspend(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt
    calls = {}

    async def fake_set_status(db, slug, status):
        calls["args"] = (slug, status)
        return True  # тенант найден

    monkeypatch.setattr(pt, "_set_status", fake_set_status)
    r = client.patch("/api/platform/tenants/acme", cookies=_admin_cookie(fake_redis),
                     json={"status": "suspended"})
    assert r.status_code == 200
    assert calls["args"] == ("acme", "suspended")


def test_rotate_key_returns_new_key_once(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_rotate(db, slug):
        return "sqa_new"  # None — тенант не найден

    monkeypatch.setattr(pt, "_rotate_key", fake_rotate)
    r = client.post("/api/platform/tenants/acme/rotate-key",
                    cookies=_admin_cookie(fake_redis))
    assert r.status_code == 200
    assert r.json()["api_key"] == "sqa_new"


def test_unknown_tenant_404(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_set_status(db, slug, status):
        return False

    monkeypatch.setattr(pt, "_set_status", fake_set_status)
    r = client.patch("/api/platform/tenants/ghost", cookies=_admin_cookie(fake_redis),
                     json={"status": "active"})
    assert r.status_code == 404
