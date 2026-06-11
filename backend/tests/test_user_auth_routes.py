"""/api/user-auth/*: login sets cookie, logout invalidates, me reports role."""
import hashlib

from argon2 import PasswordHasher
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE
from app.database import get_db
from app.routes import user_auth
from app.tenancy_http import TenantResolutionMiddleware

_ph = PasswordHasher()
GOOD_HASH = _ph.hash("correct-horse")


class _Row:
    def __init__(self):
        self.id = "11111111-1111-1111-1111-111111111111"
        self.email = "admin@t.io"
        self.password_hash = GOOD_HASH
        self.role = "admin"


class StubDB:
    """Возвращает заданный row на SELECT, проглатывает UPDATE/commit."""

    def __init__(self, row):
        self._row = row
        self.committed = False

    async def execute(self, *a, **kw):
        class _Res:
            def __init__(self, row):
                self._row = row

            def first(self):
                return self._row

        return _Res(self._row)

    async def commit(self):
        self.committed = True


class StubRegistry:
    def __init__(self):
        self.rows = [{
            "slug": "realestate", "schema_name": "t_realestate",
            "status": "active", "custom_domains": [],
            "api_key_hash": None, "api_key_required": False,
        }]

    async def all_tenants(self):
        return self.rows


def make_client(row) -> TestClient:
    app = FastAPI()
    app.include_router(user_auth.router)
    app.dependency_overrides[get_db] = lambda: StubDB(row)
    app.add_middleware(
        TenantResolutionMiddleware,
        registry=StubRegistry(),
        base_domain="silentqa.com",
        default_tenant="",
    )
    return TestClient(app, base_url="https://realestate.silentqa.com")


def test_login_success_sets_cookie_and_me_works(fake_redis):
    c = make_client(_Row())
    r = c.post("/api/user-auth/login",
               json={"email": "admin@t.io", "password": "correct-horse"})
    assert r.status_code == 200
    assert r.json() == {"email": "admin@t.io", "role": "admin"}
    cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE in cookie and "HttpOnly" in cookie
    assert "SameSite=lax" in cookie or "samesite=lax" in cookie
    assert "Domain=" not in cookie  # host-only (инвариант 11)
    me = c.get("/api/user-auth/me")
    assert me.status_code == 200 and me.json()["role"] == "admin"


def test_login_wrong_password_401_generic(fake_redis):
    c = make_client(_Row())
    r = c.post("/api/user-auth/login",
               json={"email": "admin@t.io", "password": "nope"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


def test_login_unknown_email_401_same_detail(fake_redis):
    c = make_client(None)
    r = c.post("/api/user-auth/login",
               json={"email": "ghost@t.io", "password": "whatever"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


def test_logout_invalidates_session(fake_redis):
    c = make_client(_Row())
    c.post("/api/user-auth/login",
           json={"email": "admin@t.io", "password": "correct-horse"})
    assert c.get("/api/user-auth/me").status_code == 200
    c.post("/api/user-auth/logout")
    assert c.get("/api/user-auth/me").status_code == 401


def test_login_rate_limited_429(fake_redis):
    from app.config import settings

    c = make_client(_Row())
    for _ in range(settings.LOGIN_RATE_MAX_ATTEMPTS):
        c.post("/api/user-auth/login",
               json={"email": "admin@t.io", "password": "nope"})
    r = c.post("/api/user-auth/login",
               json={"email": "admin@t.io", "password": "correct-horse"})
    assert r.status_code == 429


def test_login_on_platform_contour_404(fake_redis):
    c_platform = TestClient(_platform_app(), base_url="https://silentqa.com")
    r = c_platform.post("/api/user-auth/login",
                        json={"email": "a@b.c", "password": "x"})
    assert r.status_code == 404


def _platform_app() -> FastAPI:
    app = FastAPI()
    app.include_router(user_auth.router)
    app.add_middleware(
        TenantResolutionMiddleware,
        registry=StubRegistry(),
        base_domain="silentqa.com",
        default_tenant="",
    )
    return app
