"""processing_started_at СБРАСЫВАЕТСЯ в NULL при постановке в processing.

Reprocess старого звонка и finish ставят status='processing' до фактического старта
стадии в воркере. Если оставить прошлую метку старта (или любую непустую), watchdog
правило 3a убьёт сессию, пока она ждёт fairness-слот. Поэтому бэкенд ОБЯЗАН обнулять
processing_started_at везде, где сам ставит processing — воркер проставит NOW() на старте.
"""
import asyncio
import datetime as _dt
import uuid

import pytest
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE
from app.models import Session, SessionStatus

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True, "modules": {}}]
SID = uuid.UUID("00000000-0000-0000-0000-000000000030")
_STALE = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)


def _cookie(fake_redis, role="admin", email="a@x.io"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u1", email, role)
        finally:
            reset_tenant_schema(token)
    return {SESSION_COOKIE: asyncio.run(seed())}


class _Res:
    def __init__(self, scalar_one=None, first=None):
        self._s = scalar_one
        self._f = first

    def scalar_one_or_none(self):
        return self._s

    def first(self):
        return self._f


class _FakeDB:
    """1-й execute → select(Session); последующие → SELECT company_config_id."""

    def __init__(self, session, cfg_id="chechetov"):
        self.session = session
        self.cfg_id = cfg_id
        self.n = 0

    async def execute(self, *a, **k):
        self.n += 1
        if self.n == 1:
            return _Res(scalar_one=self.session)
        return _Res(first=(self.cfg_id,))

    async def commit(self):
        pass

    async def refresh(self, obj):
        pass

    async def scalar(self, *a, **k):
        return 0


@pytest.fixture(autouse=True)
def _clear():
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
    return TestClient(app, base_url="https://acme.silentqa.com")


def _capture_send(monkeypatch):
    from app.routes import sessions as m

    def fake(name, args=None, kwargs=None, queue=None):
        pass
    monkeypatch.setattr(m.celery_app, "send_task", fake)


def test_reprocess_resets_processing_started_at(monkeypatch, fake_redis):
    _capture_send(monkeypatch)
    sess = Session(id=SID, status=SessionStatus.completed,
                   created_at=_dt.datetime(2026, 6, 27, tzinfo=_dt.timezone.utc),
                   processing_started_at=_STALE, metadata_={})
    c = _client(monkeypatch, fake_redis, sess)
    r = c.post(f"/api/sessions/{SID}/reprocess", json={}, cookies=_cookie(fake_redis))
    assert r.status_code == 200, r.text
    assert sess.status == SessionStatus.processing
    assert sess.processing_started_at is None


def test_finish_resets_processing_started_at(monkeypatch, fake_redis):
    _capture_send(monkeypatch)
    sess = Session(id=SID, status=SessionStatus.uploading,
                   created_at=_dt.datetime(2026, 6, 27, tzinfo=_dt.timezone.utc),
                   processing_started_at=_STALE, metadata_={})
    c = _client(monkeypatch, fake_redis, sess)
    r = c.post(f"/api/sessions/{SID}/finish", cookies=_cookie(fake_redis))
    assert r.status_code == 200, r.text
    assert sess.status == SessionStatus.processing
    assert sess.processing_started_at is None
