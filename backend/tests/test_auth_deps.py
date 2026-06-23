"""Auth dependencies: cookie session, roles, dual ingestion auth."""
import hashlib

import pytest
from fastapi import Depends, FastAPI
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE
from app.auth_user import (
    UserCtx,
    get_current_user,
    require_admin,
    require_ingestion_auth,
    require_viewer,
)
from app.tenancy_http import TenantResolutionMiddleware

API_KEY = "sqa_test_key"
API_KEY_HASH = hashlib.sha256(API_KEY.encode()).hexdigest()


class StubRegistry:
    def __init__(self, api_key_required: bool):
        self.rows = [{
            "slug": "realestate",
            "schema_name": "t_realestate",
            "status": "active",
            "custom_domains": [],
            "api_key_hash": API_KEY_HASH,
            "api_key_required": api_key_required,
        }]

    async def all_tenants(self):
        return self.rows


def make_client(api_key_required: bool) -> TestClient:
    app = FastAPI()

    @app.get("/viewer", dependencies=[Depends(require_viewer)])
    async def viewer_ep():
        return {"ok": True}

    @app.get("/admin", dependencies=[Depends(require_admin)])
    async def admin_ep():
        return {"ok": True}

    @app.post("/ingest", dependencies=[Depends(require_ingestion_auth)])
    async def ingest_ep():
        return {"ok": True}

    @app.get("/me")
    async def me_ep(user: UserCtx | None = Depends(get_current_user)):
        return {"user": user.email if user else None}

    app.add_middleware(
        TenantResolutionMiddleware,
        registry=StubRegistry(api_key_required),
        base_domain="silentqa.com",
        default_tenant="",
    )
    return TestClient(app, base_url="https://realestate.silentqa.com")


async def _seed_session(role: str) -> str:
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    token = set_tenant_schema("t_realestate")
    try:
        return await auth_sessions.create_session("u1", "u@t.io", role)
    finally:
        reset_tenant_schema(token)


def _sid(fake_redis, role: str) -> str:
    import asyncio
    return asyncio.run(_seed_session(role))  # НЕ get_event_loop (инвариант 13)


def test_viewer_requires_session(fake_redis):
    c = make_client(api_key_required=True)
    assert c.get("/viewer").status_code == 401
    assert c.get("/viewer").json()["detail"] == "auth_required"
    sid = _sid(fake_redis, "viewer")
    assert c.get("/viewer", cookies={SESSION_COOKIE: sid}).status_code == 200


def test_admin_requires_admin_role(fake_redis):
    c = make_client(api_key_required=True)
    sid_v = _sid(fake_redis, "viewer")
    sid_a = _sid(fake_redis, "admin")
    assert c.get("/admin", cookies={SESSION_COOKIE: sid_v}).status_code == 403
    assert c.get("/admin", cookies={SESSION_COOKIE: sid_a}).status_code == 200


def test_ingest_dual_auth_matrix(fake_redis):
    c = make_client(api_key_required=True)
    # без ничего → 401
    assert c.post("/ingest").status_code == 401
    # валидный ключ → 200
    assert c.post("/ingest", headers={"X-API-Key": API_KEY}).status_code == 200
    # невалидный ключ → 403 (даже не 401: ключ предъявлен, но чужой/битый)
    assert c.post("/ingest", headers={"X-API-Key": "sqa_wrong"}).status_code == 403
    # cookie-сессия (viewer достаточно) → 200
    sid = _sid(fake_redis, "viewer")
    assert c.post("/ingest", cookies={SESSION_COOKIE: sid}).status_code == 200


def test_ingest_open_while_key_not_required(fake_redis):
    c = make_client(api_key_required=False)
    # rollout-окно (спека 6.4): без ключа пускаем
    assert c.post("/ingest").status_code == 200
    # но предъявленный НЕВЕРНЫЙ ключ всё равно отбивается
    assert c.post("/ingest", headers={"X-API-Key": "sqa_wrong"}).status_code == 403


def test_stale_cookie_is_anonymous(fake_redis):
    c = make_client(api_key_required=True)
    assert c.get("/me", cookies={SESSION_COOKIE: "ghost"}).json()["user"] is None
