"""Eval-profiles API: auth-гейтинг мутаций + сериализация GET (active/history).

Pure unit: фейковый реестр тенанта + cookie-сессии; get_db переопределён фейком.
"""
import asyncio
import datetime as dt
import uuid

from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE


def _client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "fulldent", "schema_name": "t_fulldent", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": {}, "company_config_id": None}]

    async def fake_all(self):
        return rows
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://fulldent.silentqa.com")


def _cookie(role: str):
    from app import auth_sessions
    from tenancy.context import reset_tenant_schema, set_tenant_schema

    async def seed():
        t = set_tenant_schema("t_fulldent")
        try:
            return await auth_sessions.create_session("u", f"{role}@t.io", role)
        finally:
            reset_tenant_schema(t)
    return {SESSION_COOKIE: asyncio.run(seed())}


# ---- auth gating (mutations require admin; no DB I/O before the 403) ----

def test_get_requires_auth(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    r = c.get("/api/eval-profiles?scenario_id=general")
    assert r.status_code == 401


def test_create_version_requires_admin(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    r = c.post("/api/eval-profiles/general/versions",
               json={"criteria": [], "prompt": "x"}, cookies=_cookie("viewer"))
    assert r.status_code == 403


def test_rewrite_requires_admin(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    r = c.post("/api/eval-profiles/general/rewrite",
               json={"wishes": "строже"}, cookies=_cookie("viewer"))
    assert r.status_code == 403


def test_activate_requires_admin(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    vid = "00000000-0000-0000-0000-0000000000aa"
    r = c.patch(f"/api/eval-profiles/versions/{vid}/activate", cookies=_cookie("viewer"))
    assert r.status_code == 403


def test_delete_requires_admin(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    vid = "00000000-0000-0000-0000-0000000000aa"
    r = c.delete(f"/api/eval-profiles/versions/{vid}", cookies=_cookie("viewer"))
    assert r.status_code == 403


# ---- GET serialization with a fake DB ----

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *a, **k):
        return _Result(self._rows)


def test_get_returns_active_and_history(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    from app.main import app
    from app.database import get_db

    now = dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc)
    # _COLS order: id, scenario_id, version, criteria, prompt, source, wishes_input, rewrite_meta, is_active, created_at
    rows = [
        (uuid.uuid4(), "general", 2, [{"id": "c1", "name": "Ясность"}], "v2 prompt",
         "llm_rewrite", "строже", {"rationale": "r"}, True, now),
        (uuid.uuid4(), "general", 1, [], None, "manual", None, None, False, now),
    ]

    async def _dep():
        yield _FakeDB(rows)
    app.dependency_overrides[get_db] = _dep
    try:
        r = c.get("/api/eval-profiles?scenario_id=general", cookies=_cookie("admin"))
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 200
    body = r.json()
    assert body["scenario_id"] == "general"
    assert body["active"]["version"] == 2
    assert body["active"]["source"] == "llm_rewrite"
    assert len(body["history"]) == 2
    assert body["file_fallback"] is None   # company_config_id is None for this tenant
