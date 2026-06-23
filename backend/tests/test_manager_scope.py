import asyncio
import pytest
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def _cookie(fake_redis, role="manager", employee=None, email="m@x.io"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session(
                "u9", email, role, employee_name=employee)
        finally:
            reset_tenant_schema(token)

    return {SESSION_COOKIE: asyncio.run(seed())}


def test_manager_foreign_stats_403(client, fake_redis):
    r = client.get("/api/managers/Чужой/sessions",
                   cookies=_cookie(fake_redis, employee="Иванов"))
    assert r.status_code == 403


def test_manager_own_name_passes_scope_gate(client, fake_redis, monkeypatch):
    """Своё имя НЕ отбивается 403 (дальше штатный 404 — звонков нет)."""
    from app.routes import managers as managers_mod

    async def fake_all_sessions(db):
        return []

    monkeypatch.setattr(managers_mod, "_all_sessions", fake_all_sessions)
    r = client.get("/api/managers/Иванов/sessions",
                   cookies=_cookie(fake_redis, employee="Иванов"))
    assert r.status_code == 404  # не 403: scope пройден, данных нет


def test_manager_unbound_sees_empty(client, fake_redis, monkeypatch):
    from app.routes import managers as managers_mod

    async def fake_all_sessions(db):
        class S:  # минимальный стаб Session
            metadata_ = {"employee": "Иванов"}
            created_at = None
        return [S()]

    monkeypatch.setattr(managers_mod, "_all_sessions", fake_all_sessions)
    r = client.get("/api/managers", cookies=_cookie(fake_redis, employee=None))
    assert r.status_code == 200
    assert r.json() == []


def test_manager_management_403(client, fake_redis):
    c = _cookie(fake_redis, employee="Иванов")
    assert client.delete(
        "/api/sessions/00000000-0000-0000-0000-000000000001", cookies=c
    ).status_code == 403  # require_admin
    assert client.get("/api/users", cookies=c).status_code == 403


# --- Прямые тесты ядра изоляции require_session_access (файловый GET) ---
# require_session_access делает `from .database import async_session` ВНУТРИ
# функции и `await db.get(SessionModel, sid)`. Чтобы доказать гейт без живой БД,
# патчим app.database.async_session фабрикой фейковой async-сессии.


class _FakeDB:
    def __init__(self, row):
        self._row = row

    async def get(self, model, sid):
        return self._row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Row:
    def __init__(self, employee):
        self.metadata_ = {"employee": employee}


def _patch_session_lookup(monkeypatch, row):
    monkeypatch.setattr("app.database.async_session", lambda: _FakeDB(row))


SID = "00000000-0000-0000-0000-000000000010"


def test_manager_foreign_transcript_404(client, fake_redis, monkeypatch):
    # чужая сессия (employee=Петров) для manager Иванов → require_session_access
    # отбивает 404 'Not found' ДО хендлера (не раскрываем существование файла).
    _patch_session_lookup(monkeypatch, _Row("Петров"))
    r = client.get(f"/api/sessions/{SID}/transcript",
                   cookies=_cookie(fake_redis, employee="Иванов"))
    assert r.status_code == 404
    # маркер гейта require_session_access (НЕ файловый 404 _read_result)
    assert r.json()["detail"] == "Not found"


def test_manager_own_transcript_passes_gate(client, fake_redis, monkeypatch):
    # своя сессия (employee=Иванов) → гейт пройден, дальше штатный файловый 404
    # (transcript.json нет на диске) с ДРУГИМ detail — доказывает, что гейт пропустил.
    _patch_session_lookup(monkeypatch, _Row("Иванов"))
    r = client.get(f"/api/sessions/{SID}/transcript",
                   cookies=_cookie(fake_redis, employee="Иванов"))
    assert r.status_code == 404
    # дошли до _read_result → файла нет: detail про файл, НЕ 'Not found'
    assert r.json()["detail"] != "Not found"
    assert "transcript.json" in r.json()["detail"]


def test_viewer_transcript_no_ownership_check(client, fake_redis, monkeypatch):
    # viewer: require_session_access возвращает user БЕЗ обращения к БД (ранний
    # return) → гейт не отбивает; дальше штатный файловый 404. Патч НЕ нужен.
    # Если упадёт с 'Not found' — require_session_access ошибочно гейтит viewer = БАГ.
    r = client.get(f"/api/sessions/{SID}/transcript",
                   cookies=_cookie(fake_redis, role="viewer", employee=None))
    assert r.status_code == 404
    assert r.json()["detail"] != "Not found"
    assert "transcript.json" in r.json()["detail"]
