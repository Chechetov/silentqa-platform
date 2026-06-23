"""ASR-сравнение (API): POST /transcribe-compare (admin) + GET /transcript-variants.

Pure unit: фейковый реестр тенанта + cookie-сессии; get_db переопределён фейком,
celery send_task замокан. Диск читается из monkeypatched settings.RESULTS_STORAGE_PATH.
"""
import asyncio
import json
import types
import uuid

from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE
from app.models import SessionStatus

SID = "00000000-0000-0000-0000-000000000001"


def _client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "fulldent", "schema_name": "t_fulldent", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True, "modules": {}}]

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


# ---- GET /transcript-variants ------------------------------------------------

def test_variants_requires_auth(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    r = c.get(f"/api/sessions/{SID}/transcript-variants")
    assert r.status_code == 401


def test_variants_empty_when_none(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    r = c.get(f"/api/sessions/{SID}/transcript-variants", cookies=_cookie("viewer"))
    assert r.status_code == 200
    assert r.json() == {"engines": {}, "variants": {}}


def test_variants_returns_saved(monkeypatch, fake_redis, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "RESULTS_STORAGE_PATH", str(tmp_path))
    rd = tmp_path / "fulldent" / SID
    rd.mkdir(parents=True)
    (rd / "transcript_variants.json").write_text(json.dumps(
        {"whisper": {"status": "ok", "segments": 1, "has_speakers": False}}))
    (rd / "transcript_whisper.json").write_text(json.dumps(
        [{"start": 0.0, "end": 1.0, "text": "привет"}]))

    c = _client(monkeypatch, fake_redis)
    r = c.get(f"/api/sessions/{SID}/transcript-variants", cookies=_cookie("viewer"))
    assert r.status_code == 200
    body = r.json()
    assert body["engines"]["whisper"]["segments"] == 1
    assert body["variants"]["whisper"][0]["text"] == "привет"


# ---- POST /transcribe-compare ------------------------------------------------

class _Res:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeDB:
    def __init__(self, sess):
        self._sess = sess

    async def execute(self, *a, **k):
        return _Res(self._sess)


def _override_db(sess):
    from app.main import app
    from app.database import get_db

    async def _dep():
        yield _FakeDB(sess)
    app.dependency_overrides[get_db] = _dep
    return app


def test_compare_requires_admin(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    r = c.post(f"/api/sessions/{SID}/transcribe-compare",
               json={"engines": ["whisper"]}, cookies=_cookie("viewer"))
    assert r.status_code == 403


def test_compare_enqueues_with_tenant_and_filtered_engines(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    sess = types.SimpleNamespace(id=uuid.UUID(SID), status=SessionStatus.completed)
    app = _override_db(sess)

    captured = {}
    from app.routes import sessions as sroute

    def _fake_send_task(name, args=None, kwargs=None, queue=None):
        captured.update(name=name, args=args, kwargs=kwargs, queue=queue)
    monkeypatch.setattr(sroute.celery_app, "send_task", _fake_send_task)

    try:
        r = c.post(f"/api/sessions/{SID}/transcribe-compare",
                   json={"engines": ["assemblyai", "bogus", "whisper"]},
                   cookies=_cookie("admin"))
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 202
    assert r.json()["engines"] == ["assemblyai", "whisper"]   # bogus filtered out, order kept
    assert captured["name"] == "pipeline.compare_transcripts"
    assert captured["kwargs"]["engines"] == ["assemblyai", "whisper"]
    assert captured["kwargs"]["tenant_schema"] == "t_fulldent"
    assert captured["queue"] == "transcription"


def test_compare_rejects_empty_engines(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    sess = types.SimpleNamespace(id=uuid.UUID(SID), status=SessionStatus.completed)
    app = _override_db(sess)
    try:
        r = c.post(f"/api/sessions/{SID}/transcribe-compare",
                   json={"engines": ["bogus"]}, cookies=_cookie("admin"))
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 400
