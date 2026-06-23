import asyncio
import json

import pytest
from starlette.testclient import TestClient

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app)


def _admin_cookie(fake_redis):
    from app import platform_sessions as ps
    sid = asyncio.run(ps.create_platform_session("a1", "boss@x.io"))
    return {ps.PLATFORM_COOKIE: sid}


@pytest.fixture
def schema_mock(monkeypatch):
    """Impersonate-хендлер зовёт _require_schema (SELECT shared.tenants) —
    в юнит-окружении Postgres нет, мокаем как в test_platform_tenants."""
    from app.routes import platform_tenants as pt

    async def fake_require_schema(db, slug):
        if slug != "acme":
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="tenant not found")
        return "t_acme"

    monkeypatch.setattr(pt, "_require_schema", fake_require_schema)


def test_issue_token_and_exchange(client, fake_redis, schema_mock):
    r = client.post("/api/platform/tenants/acme/impersonate",
                    cookies=_admin_cookie(fake_redis),
                    headers={"Host": "admin.silentqa.com"})
    assert r.status_code == 200
    url = r.json()["url"]
    assert url.startswith("https://acme.silentqa.com/api/user-auth/impersonate?token=")
    token = url.split("token=")[1]
    assert fake_redis.ttls[f"platform:imp:{token}"] == 60

    ex = client.get(f"/api/user-auth/impersonate?token={token}",
                    headers={"Host": "acme.silentqa.com"}, follow_redirects=False)
    assert ex.status_code == 303
    assert "sqa_session" in ex.headers.get("set-cookie", "")

    me = client.get("/api/user-auth/me", headers={"Host": "acme.silentqa.com"},
                    cookies={"sqa_session": ex.cookies["sqa_session"]})
    assert me.json()["impersonated_by"] == "boss@x.io"
    assert me.json()["role"] == "admin"


def test_token_single_use(client, fake_redis, schema_mock):
    r = client.post("/api/platform/tenants/acme/impersonate",
                    cookies=_admin_cookie(fake_redis),
                    headers={"Host": "admin.silentqa.com"})
    token = r.json()["url"].split("token=")[1]
    ok = client.get(f"/api/user-auth/impersonate?token={token}",
                    headers={"Host": "acme.silentqa.com"}, follow_redirects=False)
    assert ok.status_code == 303
    again = client.get(f"/api/user-auth/impersonate?token={token}",
                       headers={"Host": "acme.silentqa.com"}, follow_redirects=False)
    assert again.status_code == 401


def test_token_slug_mismatch(client, fake_redis):
    """Токен на acme нельзя обменять... если бы был второй тенант. Стаб: кладём токен руками."""
    asyncio.run(fake_redis.set("platform:imp:tok1", json.dumps(
        {"slug": "other", "admin_email": "boss@x.io"}), ex=60))
    r = client.get("/api/user-auth/impersonate?token=tok1",
                   headers={"Host": "acme.silentqa.com"}, follow_redirects=False)
    assert r.status_code == 401
