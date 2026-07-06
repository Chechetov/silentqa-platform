import asyncio
import json

from starlette.testclient import TestClient
from app.auth_sessions import SESSION_COOKIE


def _client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "fulldent", "schema_name": "t_fulldent", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True, "modules": {}}]

    async def fake_all(self):
        return rows
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://fulldent.silentqa.com")


def _viewer():
    from app import auth_sessions
    from tenancy.context import reset_tenant_schema, set_tenant_schema

    async def seed():
        t = set_tenant_schema("t_fulldent")
        try:
            return await auth_sessions.create_session("u", "v@t.io", "viewer")
        finally:
            reset_tenant_schema(t)
    return {SESSION_COOKIE: asyncio.run(seed())}


def test_coaching_requires_session(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    r = c.get("/api/sessions/00000000-0000-0000-0000-000000000001/coaching")
    assert r.status_code == 401


def test_coaching_404_when_no_file(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    r = c.get("/api/sessions/00000000-0000-0000-0000-000000000001/coaching", cookies=_viewer())
    assert r.status_code == 404


def test_coaching_200_with_body(monkeypatch, fake_redis, tmp_path):
    from app.config import settings
    from tenancy.paths import tenant_results_dir

    monkeypatch.setattr(settings, "RESULTS_STORAGE_PATH", str(tmp_path))
    session_id = "00000000-0000-0000-0000-000000000002"
    body = {"strengths": ["ясная презентация"], "growth": ["не выявил боль"]}
    result_dir = tenant_results_dir(str(tmp_path), "fulldent", session_id)
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "coaching.json").write_text(json.dumps(body), encoding="utf-8")

    c = _client(monkeypatch, fake_redis)
    r = c.get(f"/api/sessions/{session_id}/coaching", cookies=_viewer())
    assert r.status_code == 200
    assert r.json() == body
