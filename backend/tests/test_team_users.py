import asyncio
import pytest
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]
HOST = {"Host": "acme.silentqa.com"}


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def _cookie(fake_redis, role="admin", user_id="u1", email="a@x.io"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session(user_id, email, role)
        finally:
            reset_tenant_schema(token)

    return {SESSION_COOKIE: asyncio.run(seed())}


def test_users_admin_only(client, fake_redis):
    assert client.get("/api/users").status_code == 401
    assert client.get("/api/users",
                      cookies=_cookie(fake_redis, role="viewer")).status_code == 403


def test_cannot_delete_self(client, fake_redis, monkeypatch):
    r = client.delete("/api/users/u1", cookies=_cookie(fake_redis, user_id="u1"))
    assert r.status_code == 403
    assert r.json()["detail"] == "cannot_delete_self"


def test_cannot_demote_self(client, fake_redis):
    r = client.patch("/api/users/u1", cookies=_cookie(fake_redis, user_id="u1"),
                     json={"role": "viewer"})
    assert r.status_code == 403
    assert r.json()["detail"] == "cannot_demote_self"


# Жертва — валидный UUID: self-guards сравнивают строки, а uuid-валидация
# (см. Step 4) идёт ПОСЛЕ guards и ДО БД
VICTIM = "00000000-0000-0000-0000-000000000002"


def test_cannot_remove_last_admin(client, fake_redis, monkeypatch):
    from app.routes import users as users_mod

    async def fake_get_user(db, user_id):
        return {"id": VICTIM, "email": "b@x.io", "role": "admin"}

    async def fake_count_other_admins(db, user_id):
        return 0  # других админов нет

    monkeypatch.setattr(users_mod, "_get_user", fake_get_user)
    monkeypatch.setattr(users_mod, "_count_other_admins", fake_count_other_admins)
    r = client.delete(f"/api/users/{VICTIM}", cookies=_cookie(fake_redis, user_id="u1"))
    assert r.status_code == 403
    assert r.json()["detail"] == "last_admin"
    r = client.patch(f"/api/users/{VICTIM}", cookies=_cookie(fake_redis, user_id="u1"),
                     json={"role": "viewer"})
    assert r.status_code == 403


def test_delete_kills_user_sessions(client, fake_redis, monkeypatch):
    from app.routes import users as users_mod

    async def fake_get_user(db, user_id):
        return {"id": VICTIM, "email": "b@x.io", "role": "viewer"}

    async def fake_delete(db, user_id):
        return True

    monkeypatch.setattr(users_mod, "_get_user", fake_get_user)
    monkeypatch.setattr(users_mod, "_delete_user", fake_delete)
    victim = _cookie(fake_redis, role="viewer", user_id=VICTIM, email="b@x.io")
    r = client.delete(f"/api/users/{VICTIM}", cookies=_cookie(fake_redis, user_id="u1"))
    assert r.status_code == 204
    me = client.get("/api/user-auth/me", cookies=victim)
    assert me.status_code == 401  # сессии жертвы убиты


def test_garbage_user_id_404_not_500(client, fake_redis):
    r = client.delete("/api/users/not-a-uuid", cookies=_cookie(fake_redis, user_id="u1"))
    assert r.status_code == 404


SELF_UUID = "00000000-0000-0000-0000-00000000000a"  # канон. lowercase, с буквой


def test_cannot_delete_self_uppercase_uuid(client, fake_redis):
    # обход guard через неканонический (верхний регистр) UUID должен быть закрыт
    r = client.delete(f"/api/users/{SELF_UUID.upper()}",
                      cookies=_cookie(fake_redis, user_id=SELF_UUID))
    assert r.status_code == 403
    assert r.json()["detail"] == "cannot_delete_self"


def test_cannot_demote_self_uppercase_uuid(client, fake_redis):
    r = client.patch(f"/api/users/{SELF_UUID.upper()}",
                     cookies=_cookie(fake_redis, user_id=SELF_UUID),
                     json={"role": "viewer"})
    assert r.status_code == 403
    assert r.json()["detail"] == "cannot_demote_self"
