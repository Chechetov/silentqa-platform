import asyncio
import uuid
import datetime as _dt

import pytest
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE
from app.models import Session, SessionStatus

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True, "modules": {}}]
SID = uuid.UUID("00000000-0000-0000-0000-000000000010")


def _cookie(fake_redis, role="admin", employee=None, email="a@x.io"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u1", email, role, employee_name=employee)
        finally:
            reset_tenant_schema(token)
    return {SESSION_COOKIE: asyncio.run(seed())}


class _Result:
    def __init__(self, val):
        self._val = val

    def scalar_one_or_none(self):
        return self._val


class _FakeDB:
    def __init__(self, session):
        self.session = session

    async def execute(self, *a, **k):
        return _Result(self.session)

    async def commit(self):
        pass

    async def refresh(self, obj):
        pass

    async def scalar(self, *a, **k):
        return 0


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app
    app.dependency_overrides.clear()


def _client(monkeypatch, fake_redis, session):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    from app.database import get_db
    fake = _FakeDB(session)

    async def _ovr():
        yield fake
    app.dependency_overrides[get_db] = _ovr
    return TestClient(app, base_url="https://acme.silentqa.com"), fake


def _session(meta=None):
    return Session(id=SID, status=SessionStatus.completed,
                   created_at=_dt.datetime(2026, 6, 27, tzinfo=_dt.timezone.utc),
                   metadata_=meta)


def test_admin_rename_ok(monkeypatch, fake_redis):
    s = _session({"phone": "+700"})
    c, _ = _client(monkeypatch, fake_redis, s)
    r = c.patch(f"/api/sessions/{SID}/title", json={"title": "  Демо-созвон  "},
                cookies=_cookie(fake_redis))
    assert r.status_code == 200, r.text
    assert r.json()["metadata"]["title"] == "Демо-созвон"
    assert s.metadata_["phone"] == "+700"


@pytest.mark.parametrize("body", [{"title": None}, {"title": ""}, {"title": "   "}])
def test_clear_title(monkeypatch, fake_redis, body):
    s = _session({"title": "old", "phone": "+700"})
    c, _ = _client(monkeypatch, fake_redis, s)
    r = c.patch(f"/api/sessions/{SID}/title", json=body, cookies=_cookie(fake_redis))
    assert r.status_code == 200, r.text
    assert "title" not in r.json()["metadata"]
    assert s.metadata_["phone"] == "+700"


def test_truncate_200(monkeypatch, fake_redis):
    c, _ = _client(monkeypatch, fake_redis, _session())
    r = c.patch(f"/api/sessions/{SID}/title", json={"title": "x" * 500},
                cookies=_cookie(fake_redis))
    assert r.status_code == 200
    assert len(r.json()["metadata"]["title"]) == 200


def test_unauthorized_401(monkeypatch, fake_redis):
    c, _ = _client(monkeypatch, fake_redis, _session())
    r = c.patch(f"/api/sessions/{SID}/title", json={"title": "x"})
    assert r.status_code == 401


def test_viewer_403(monkeypatch, fake_redis):
    c, _ = _client(monkeypatch, fake_redis, _session())
    r = c.patch(f"/api/sessions/{SID}/title", json={"title": "x"},
                cookies=_cookie(fake_redis, role="viewer"))
    assert r.status_code == 403


def test_manager_403(monkeypatch, fake_redis):
    c, _ = _client(monkeypatch, fake_redis, _session())
    r = c.patch(f"/api/sessions/{SID}/title", json={"title": "x"},
                cookies=_cookie(fake_redis, role="manager", employee="Иванов"))
    assert r.status_code == 403


def test_not_found_404(monkeypatch, fake_redis):
    c, _ = _client(monkeypatch, fake_redis, None)
    r = c.patch(f"/api/sessions/{SID}/title", json={"title": "x"},
                cookies=_cookie(fake_redis))
    assert r.status_code == 404


def test_title_stripped_on_create():
    from app.routes.sessions import build_session_metadata
    meta = build_session_metadata({"title": "Hacked", "employee": "Иван"}, None)
    assert "title" not in meta
    assert meta["employee"] == "Иван"
