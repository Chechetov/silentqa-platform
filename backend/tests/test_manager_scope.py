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


# --- Фильтр GET /api/sessions?employee=... ---
# Пул юнит-тестов без БД: фейковый get_db, отдающий Session-строки в память, и
# крошечный вычислитель WHERE-критериев (JSON `metadata ->> 'employee' = :val`),
# чтобы доказать, что параметр реально сужает выборку, а не только принимается.
import operator as _operator
import uuid as _uuid
from datetime import datetime as _dt, timezone as _tz

from sqlalchemy.sql.elements import BinaryExpression as _BinExpr

from app.models import Session as _Session, SessionStatus as _SessionStatus


def _clause_matches(clause, meta):
    # Единственный критерий в этом тесте: metadata ->> 'employee' == :val (eq).
    if isinstance(clause, _BinExpr) and clause.operator is _operator.eq:
        key = clause.left.right.value      # 'employee' из `metadata ->> 'employee'`
        return meta.get(key) == clause.right.value
    return True                            # незнакомый критерий — не сужаем


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ExecResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)

    def all(self):
        return self._rows


class _FilterFakeDB:
    """Мини-async-сессия: держит Session-строки, применяет WHERE-критерии select-а."""

    def __init__(self, sessions):
        self._sessions = sessions

    def _matched(self, query):
        return [s for s in self._sessions
                if all(_clause_matches(c, s.metadata_ or {})
                       for c in query._where_criteria)]

    async def scalar(self, query):
        # count(*) по chunks на сессию → 0; иначе — total по sessions.
        if "chunks" in str(query):
            return 0
        return len(self._matched(query))

    async def execute(self, query, params=None):
        # Список сессий (текстовый tpl-запрос в этом тесте не срабатывает — нет template_id).
        if hasattr(query, "_where_criteria"):
            return _ExecResult(self._matched(query))
        return _ExecResult([])


def _mk_session(employee):
    s = _Session(metadata_={"employee": employee})
    s.id = _uuid.uuid4()
    s.status = _SessionStatus.completed
    s.created_at = _dt(2026, 7, 1, tzinfo=_tz.utc)
    s.finished_at = None
    s.duration_seconds = None
    s.file_size_bytes = None
    return s


def test_sessions_employee_filter(client, fake_redis, monkeypatch):
    from app.main import app
    from app.database import get_db

    fake = _FilterFakeDB([_mk_session("Иванов"), _mk_session("Петров")])

    async def _override():
        yield fake

    app.dependency_overrides[get_db] = _override
    try:
        r = client.get("/api/sessions?employee=Иванов",
                       cookies=_cookie(fake_redis, role="viewer", employee=None))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 1
        assert [i["metadata"]["employee"] for i in body["items"]] == ["Иванов"]
    finally:
        app.dependency_overrides.clear()


def test_manager_foreign_stats_403(client, fake_redis):
    r = client.get("/api/managers/Чужой/sessions",
                   cookies=_cookie(fake_redis, employee="Иванов"))
    assert r.status_code == 403


def test_manager_own_name_passes_scope_gate(client, fake_redis, monkeypatch):
    """Своё имя НЕ отбивается 403 (дальше штатный 404 — звонков нет)."""
    from app.routes import managers as managers_mod

    async def fake_sessions_for(db, name):
        return []

    monkeypatch.setattr(managers_mod, "_sessions_for", fake_sessions_for)
    r = client.get("/api/managers/Иванов/sessions",
                   cookies=_cookie(fake_redis, employee="Иванов"))
    assert r.status_code == 404  # не 403: scope пройден, данных нет


def test_manager_unbound_sees_empty(client, fake_redis, monkeypatch):
    from app.routes import managers as managers_mod

    async def fake_agg_rows(db, scope):
        return []

    monkeypatch.setattr(managers_mod, "_manager_agg_rows", fake_agg_rows)
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
