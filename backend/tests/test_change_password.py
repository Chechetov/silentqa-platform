import asyncio
import pytest
from starlette.testclient import TestClient
from argon2 import PasswordHasher

from app.auth_sessions import SESSION_COOKIE

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


@pytest.fixture
def pw_store(monkeypatch):
    ph = PasswordHasher()
    store = {"hash": ph.hash("oldpw")}

    async def fake_get_hash(db, user_id):
        return store["hash"]

    async def fake_set_hash(db, user_id, new_hash):
        store["hash"] = new_hash

    from app.routes import user_auth
    monkeypatch.setattr(user_auth, "_get_password_hash", fake_get_hash)
    monkeypatch.setattr(user_auth, "_set_password_hash", fake_set_hash)
    return store


def _cookie(fake_redis, user_id="u1", email="a@x.io", role="viewer"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session(user_id, email, role)
        finally:
            reset_tenant_schema(token)

    return {SESSION_COOKIE: asyncio.run(seed())}


def test_change_password_wrong_old(client, fake_redis, pw_store):
    r = client.post("/api/user-auth/change-password", cookies=_cookie(fake_redis),
                    json={"old_password": "nope", "new_password": "newpw123"})
    assert r.status_code == 403


def test_change_password_kills_other_sessions(client, fake_redis, pw_store):
    mine = _cookie(fake_redis)
    other = _cookie(fake_redis)  # вторая сессия того же юзера
    r = client.post("/api/user-auth/change-password", cookies=mine,
                    json={"old_password": "oldpw", "new_password": "newpw123"})
    assert r.status_code == 200
    assert client.get("/api/user-auth/me", cookies=mine).status_code == 200
    assert client.get("/api/user-auth/me", cookies=other).status_code == 401


def test_change_password_short_new(client, fake_redis, pw_store):
    r = client.post("/api/user-auth/change-password", cookies=_cookie(fake_redis),
                    json={"old_password": "oldpw", "new_password": "short"})
    assert r.status_code == 422
    assert r.json()["detail"] == "password_too_short"
