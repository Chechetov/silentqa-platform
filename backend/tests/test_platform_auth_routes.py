"""Логин платформенного админа. БД мокается: _fetch_admin → словарь."""
import pytest
from starlette.testclient import TestClient

from argon2 import PasswordHasher

ROWS = []  # тенанты не нужны; платформенный хост


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://admin.silentqa.com")


@pytest.fixture
def admin_row(monkeypatch):
    ph = PasswordHasher()
    row = {"id": "a1", "email": "boss@x.io", "password_hash": ph.hash("s3cret")}

    async def fake_fetch(db, email):
        return row if email.lower() == row["email"] else None

    from app.routes import platform_auth
    monkeypatch.setattr(platform_auth, "_fetch_admin", fake_fetch)
    # last_login-апдейт без БД:
    async def fake_touch(db, admin_id):
        return None
    monkeypatch.setattr(platform_auth, "_touch_last_login", fake_touch)
    return row


def test_login_sets_cookie_and_me(client, admin_row):
    r = client.post("/api/platform/auth/login",
                    json={"email": "boss@x.io", "password": "s3cret"})
    assert r.status_code == 200
    assert "sqa_admin" in r.cookies
    me = client.get("/api/platform/auth/me")
    assert me.status_code == 200
    assert me.json() == {"email": "boss@x.io"}


def test_login_wrong_password_401(client, admin_row):
    r = client.post("/api/platform/auth/login",
                    json={"email": "boss@x.io", "password": "nope"})
    assert r.status_code == 401


def test_me_without_session_401(client):
    r = client.get("/api/platform/auth/me")
    assert r.status_code == 401
    assert r.json()["detail"] == "platform_auth_required"


def test_logout_kills_session(client, admin_row):
    client.post("/api/platform/auth/login",
                json={"email": "boss@x.io", "password": "s3cret"})
    client.post("/api/platform/auth/logout")
    assert client.get("/api/platform/auth/me").status_code == 401


def test_login_unknown_email_401(client, admin_row):
    r = client.post("/api/platform/auth/login",
                    json={"email": "ghost@x.io", "password": "whatever"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


def test_login_rate_limited_429(client, admin_row):
    from app.config import settings

    for _ in range(settings.LOGIN_RATE_MAX_ATTEMPTS):
        client.post("/api/platform/auth/login",
                    json={"email": "boss@x.io", "password": "nope"})
    r = client.post("/api/platform/auth/login",
                    json={"email": "boss@x.io", "password": "s3cret"})
    assert r.status_code == 429
    assert r.json()["detail"] == "too_many_attempts"
