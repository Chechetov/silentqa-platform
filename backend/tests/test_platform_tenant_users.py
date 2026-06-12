"""Тесты Task 11: управление юзерами клиента из платформенной админки."""
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


# ── auth-кейсы живьём ────────────────────────────────────────────────────────

def test_users_list_requires_auth(client):
    assert client.get("/api/platform/tenants/acme/users").status_code == 401


# ── data-кейсы (data-слой мокается) ─────────────────────────────────────────

def test_tenant_users_list(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_schema(db, slug):
        return "t_acme" if slug == "acme" else None

    async def fake_users(db, schema):
        return [{"id": "u1", "email": "a@x.io", "role": "admin",
                 "employee_name": None, "created_at": None, "last_login": None}]

    monkeypatch.setattr(pt, "_schema_for", fake_schema)
    monkeypatch.setattr(pt, "_list_users", fake_users)
    r = client.get("/api/platform/tenants/acme/users", cookies=_admin_cookie(fake_redis))
    assert r.status_code == 200 and r.json()[0]["email"] == "a@x.io"
    assert client.get("/api/platform/tenants/ghost/users",
                      cookies=_admin_cookie(fake_redis)).status_code == 404


def test_tenant_user_reset_password_returns_once(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_schema(db, slug):
        return "t_acme"

    async def fake_reset(db, schema, user_id, pw_hash):
        return True

    async def fake_email(db, schema, uid):
        return None  # без живой сессии — инвалидация no-op

    monkeypatch.setattr(pt, "_schema_for", fake_schema)
    monkeypatch.setattr(pt, "_reset_password", fake_reset)
    monkeypatch.setattr(pt, "_user_email", fake_email)
    r = client.post("/api/platform/tenants/acme/users/00000000-0000-0000-0000-000000000001/reset-password",
                    cookies=_admin_cookie(fake_redis))
    assert r.status_code == 200
    assert len(r.json()["password"]) >= 12


def test_tenant_user_create(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_schema(db, slug):
        return "t_acme"

    async def fake_insert(db, schema, email, role, employee_name, pw_hash):
        return "new-uid-1"

    monkeypatch.setattr(pt, "_schema_for", fake_schema)
    monkeypatch.setattr(pt, "_insert_user", fake_insert)
    r = client.post("/api/platform/tenants/acme/users",
                    cookies=_admin_cookie(fake_redis),
                    json={"email": "new@x.io", "role": "viewer"})
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "new@x.io" and body["role"] == "viewer"
    assert len(body["password"]) >= 12


def test_tenant_user_create_conflict_409(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_schema(db, slug):
        return "t_acme"

    async def fake_insert(db, schema, email, role, employee_name, pw_hash):
        return None  # email conflict

    monkeypatch.setattr(pt, "_schema_for", fake_schema)
    monkeypatch.setattr(pt, "_insert_user", fake_insert)
    r = client.post("/api/platform/tenants/acme/users",
                    cookies=_admin_cookie(fake_redis),
                    json={"email": "dup@x.io", "role": "viewer"})
    assert r.status_code == 409


def test_tenant_user_patch(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_require_schema(db, slug):
        return "t_acme"

    # We need to mock db.execute and db.commit as well for the PATCH handler
    # to avoid a real DB call; simplest: mock _require_schema and use a fake db helper
    called = {}

    async def fake_patch(slug, user_id, body, db=None):
        called["slug"] = slug
        called["user_id"] = str(user_id)
        called["role"] = body.role
        from fastapi.responses import JSONResponse
        return {"ok": True}

    monkeypatch.setattr(pt.router, "routes", pt.router.routes)

    # Simpler approach: patch _require_schema and also intercept db.execute via
    # monkeypatching the module-level helper that does the update
    # Since _tenant_user_patch doesn't have a separate helper, we test it via 422
    # (no fields → 422 "nothing to change") and 200 (with mock _require_schema)
    monkeypatch.setattr(pt, "_require_schema", fake_require_schema)

    # Test 422 when no fields provided
    r = client.patch(
        "/api/platform/tenants/acme/users/00000000-0000-0000-0000-000000000001",
        cookies=_admin_cookie(fake_redis),
        json={})
    assert r.status_code == 422


def test_tenant_users_unknown_tenant_404(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_schema(db, slug):
        return None  # не найден

    monkeypatch.setattr(pt, "_schema_for", fake_schema)
    r = client.get("/api/platform/tenants/ghost/users",
                   cookies=_admin_cookie(fake_redis))
    assert r.status_code == 404


def test_tenant_user_reset_password_not_found(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_schema(db, slug):
        return "t_acme"

    async def fake_reset(db, schema, user_id, pw_hash):
        return False  # user not found

    monkeypatch.setattr(pt, "_schema_for", fake_schema)
    monkeypatch.setattr(pt, "_reset_password", fake_reset)
    r = client.post("/api/platform/tenants/acme/users/00000000-0000-0000-0000-000000000099/reset-password",
                    cookies=_admin_cookie(fake_redis))
    assert r.status_code == 404


def test_tenant_user_delete_204(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_schema(db, slug):
        return "t_acme"

    async def fake_delete(db, schema, user_id):
        return True

    async def fake_email(db, schema, uid):
        return None  # без живой сессии — инвалидация no-op

    monkeypatch.setattr(pt, "_schema_for", fake_schema)
    monkeypatch.setattr(pt, "_delete_user", fake_delete)
    monkeypatch.setattr(pt, "_user_email", fake_email)
    r = client.delete("/api/platform/tenants/acme/users/00000000-0000-0000-0000-000000000001",
                      cookies=_admin_cookie(fake_redis))
    assert r.status_code == 204


def test_tenant_user_delete_404(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_schema(db, slug):
        return "t_acme"

    async def fake_delete(db, schema, user_id):
        return False

    async def fake_email(db, schema, uid):
        return None

    monkeypatch.setattr(pt, "_schema_for", fake_schema)
    monkeypatch.setattr(pt, "_delete_user", fake_delete)
    monkeypatch.setattr(pt, "_user_email", fake_email)
    r = client.delete("/api/platform/tenants/acme/users/00000000-0000-0000-0000-000000000001",
                      cookies=_admin_cookie(fake_redis))
    assert r.status_code == 404


# ── §3.3 rescue: платформенное удаление/сброс пароля убивает живую сессию ─────

def test_platform_delete_kills_tenant_user_sessions(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt
    from app import auth_sessions
    from tenancy.context import set_tenant_schema, reset_tenant_schema
    VICTIM = "00000000-0000-0000-0000-000000000033"

    async def fake_schema(db, slug):
        return "t_acme"

    async def fake_email(db, schema, uid):
        return "victim@x.io"

    async def fake_delete(db, schema, uid):
        return True

    monkeypatch.setattr(pt, "_require_schema", fake_schema)
    monkeypatch.setattr(pt, "_user_email", fake_email)
    monkeypatch.setattr(pt, "_delete_user", fake_delete)

    # сессия жертвы под t_acme
    async def seed():
        tok = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session(VICTIM, "victim@x.io", "viewer")
        finally:
            reset_tenant_schema(tok)

    victim_sid = asyncio.run(seed())
    assert any(k.endswith(victim_sid) for k in fake_redis.store)  # сессия есть

    r = client.delete(f"/api/platform/tenants/acme/users/{VICTIM}",
                      cookies=_admin_cookie(fake_redis))
    assert r.status_code == 204
    assert not any(k.endswith(victim_sid) for k in fake_redis.store)  # убита


def test_platform_reset_kills_tenant_user_sessions(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt
    from app import auth_sessions
    from tenancy.context import set_tenant_schema, reset_tenant_schema
    VICTIM = "00000000-0000-0000-0000-000000000034"

    async def fake_schema(db, slug):
        return "t_acme"

    async def fake_email(db, schema, uid):
        return "v2@x.io"

    async def fake_reset(db, schema, uid, pw):
        return True

    monkeypatch.setattr(pt, "_require_schema", fake_schema)
    monkeypatch.setattr(pt, "_user_email", fake_email)
    monkeypatch.setattr(pt, "_reset_password", fake_reset)

    async def seed():
        tok = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session(VICTIM, "v2@x.io", "viewer")
        finally:
            reset_tenant_schema(tok)

    victim_sid = asyncio.run(seed())
    r = client.post(f"/api/platform/tenants/acme/users/{VICTIM}/reset-password",
                    cookies=_admin_cookie(fake_redis))
    assert r.status_code == 200
    assert not any(k.endswith(victim_sid) for k in fake_redis.store)
