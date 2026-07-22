"""upsert/record: SQL-параметры, best-effort, идемпотентный ON CONFLICT."""
import json
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import tasks.results_db as rdb


class FakeRedis:
    """SET NX EX семантика для alert-once (как FakeRedis в test_tenant_slots)."""
    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


def _raiser(exc):
    def _f(*a, **k):
        raise exc
    return _f


@pytest.fixture(autouse=True)
def alert_redis(monkeypatch):
    """Каждому тесту — свой FakeRedis для risk-alert-once: изоляция от боевого Redis."""
    fake = FakeRedis()
    monkeypatch.setattr(rdb, "_alert_redis", lambda: fake)
    return fake


class FakeCursor:
    def __init__(self, session_row=None, inserted=True):
        self.executed = []
        self._session_row = session_row
        self._inserted = inserted
        self._last_sql = ""

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last_sql = sql

    def fetchone(self):
        # INSERT ... RETURNING (xmax = 0) → (inserted,); SELECT ... → session_row
        if "INSERT INTO quality_results" in self._last_sql:
            return (self._inserted,)
        return self._session_row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


SESSION_ROW = (
    {"employee": "Иванов", "scenario_id": "general"},   # metadata
    datetime(2026, 7, 1, tzinfo=timezone.utc),           # created_at
    123.0,                                               # duration_seconds
)


def _wire(monkeypatch, cursor):
    monkeypatch.setattr(rdb, "tenant_connect", lambda: FakeConn(cursor))
    monkeypatch.setattr(rdb, "get_tenant_slug", lambda: "acme")


def test_upsert_sql_has_on_conflict(monkeypatch):
    cur = FakeCursor()
    _wire(monkeypatch, cur)
    ok = rdb.upsert_quality_result(
        "sid-1", overall_score=7, version=4, scenario_id="general",
        employee="Иванов", session_created_at=SESSION_ROW[1],
        duration_seconds=123.0, skip_reason=None,
        criteria=[{"name": "politeness", "score": 8, "comment": ""}],
        objections=[], talk_metrics=None, sentiment_counts=None, risk_flags=[])
    assert ok is True
    sql, params = cur.executed[-1]
    assert "INSERT INTO quality_results" in sql
    assert "ON CONFLICT (session_id) DO UPDATE" in sql
    assert params["overall_score"] == 7
    assert json.loads(params["criteria"])[0]["name"] == "politeness"


def test_record_reads_session_and_upserts(monkeypatch):
    cur = FakeCursor(session_row=SESSION_ROW)
    _wire(monkeypatch, cur)
    sent = [{"sentiment": "negative"}] * 3 + [{"sentiment": "positive"}]
    flags = rdb.record_quality_result(
        "sid-1", {"overall_score": 3, "version": 4, "objections": []},
        transcript=[{"speaker": "A", "start": 0, "end": 5, "text": "х"}],
        sentiment_results=sent)
    assert "low_score" in flags and "negative_sentiment" in flags
    ins = [p for s, p in cur.executed if "INSERT INTO quality_results" in s]
    assert ins and ins[0]["employee"] == "Иванов"
    assert ins[0]["skip_reason"] is None


def test_record_reads_score_version_into_version(monkeypatch):
    # отчёт качества несёт ключ score_version — он должен попасть в колонку version
    cur = FakeCursor(session_row=SESSION_ROW)
    _wire(monkeypatch, cur)
    rdb.record_quality_result("sid-1", {"overall_score": 7, "score_version": 4})
    ins = [p for s, p in cur.executed if "INSERT INTO quality_results" in s]
    assert ins and ins[0]["version"] == 4


def test_record_skip_reason_no_risk_flags(monkeypatch):
    cur = FakeCursor(session_row=SESSION_ROW)
    _wire(monkeypatch, cur)
    flags = rdb.record_quality_result(
        "sid-1", {"overall_score": None, "skip_reason": "too_short"},
        skip_reason="too_short")
    assert flags == []
    ins = [p for s, p in cur.executed if "INSERT INTO quality_results" in s]
    assert ins[0]["skip_reason"] == "too_short"


def test_record_best_effort_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(rdb, "tenant_connect", boom)
    monkeypatch.setattr(rdb, "get_tenant_slug", lambda: "acme")
    assert rdb.record_quality_result("sid-1", {"overall_score": 5}) == []


def test_record_missing_session_row_warns_and_skips(monkeypatch, caplog):
    cur = FakeCursor(session_row=None)
    _wire(monkeypatch, cur)
    assert rdb.record_quality_result("sid-nope", {"overall_score": 5}) == []
    assert not [p for s, p in cur.executed if "INSERT INTO quality_results" in s]


def test_record_calls_alert_on_risk(monkeypatch):
    cur = FakeCursor(session_row=SESSION_ROW)
    _wire(monkeypatch, cur)
    called = {}
    monkeypatch.setattr(rdb, "_send_risk_alert_safe",
                        lambda **kw: called.update(kw))
    rdb.record_quality_result("sid-1", {"overall_score": 1, "version": 4})
    assert called["session_id"] == "sid-1" and "low_score" in called["flags"]


def test_upsert_returns_inserted_flag(monkeypatch):
    # первая вставка (xmax=0 → True) → inserted=True; UPDATE по конфликту → False
    ins = FakeCursor(inserted=True)
    _wire(monkeypatch, ins)
    assert rdb.upsert_quality_result(
        "sid-1", overall_score=7, version=4, scenario_id=None, employee=None,
        session_created_at=SESSION_ROW[1], duration_seconds=1.0, skip_reason=None,
        criteria=None, objections=[], talk_metrics=None, sentiment_counts=None,
        risk_flags=[]) is True
    upd = FakeCursor(inserted=False)
    _wire(monkeypatch, upd)
    assert rdb.upsert_quality_result(
        "sid-1", overall_score=7, version=4, scenario_id=None, employee=None,
        session_created_at=SESSION_ROW[1], duration_seconds=1.0, skip_reason=None,
        criteria=None, objections=[], talk_metrics=None, sentiment_counts=None,
        risk_flags=[]) is False


def test_record_reprocess_no_double_alert(monkeypatch):
    # редоставка/reprocess той же сессии: alert-once по Redis-ключу — второй раз молчим.
    # inserted=False (DB-UPDATE) на первом вызове НЕ мешает алерту — решение за Redis.
    cur = FakeCursor(session_row=SESSION_ROW, inserted=False)
    _wire(monkeypatch, cur)
    sends = []
    monkeypatch.setattr(rdb, "_send_risk_alert_safe", lambda **kw: sends.append(kw))
    f1 = rdb.record_quality_result("sid-1", {"overall_score": 1, "version": 4})
    f2 = rdb.record_quality_result("sid-1", {"overall_score": 1, "version": 4})
    assert "low_score" in f1 and "low_score" in f2   # флаги возвращаются всегда
    assert len(sends) == 1                            # алерт ровно один


def test_record_suppress_alert_even_when_inserted(monkeypatch, alert_redis):
    cur = FakeCursor(session_row=SESSION_ROW, inserted=True)
    _wire(monkeypatch, cur)
    called = {}
    monkeypatch.setattr(rdb, "_send_risk_alert_safe", lambda **kw: called.update(kw))
    flags = rdb.record_quality_result(
        "sid-1", {"overall_score": 1, "version": 4}, suppress_alert=True)
    assert "low_score" in flags
    assert not called
    assert alert_redis.store == {}   # suppress → Redis-ключ не ставим


def test_record_alerts_even_when_upsert_fails(monkeypatch):
    # Ядро фикса: upsert падает — риск-алерт всё равно уходит (Redis alert-once).
    cur = FakeCursor(session_row=SESSION_ROW)
    _wire(monkeypatch, cur)
    monkeypatch.setattr(rdb, "upsert_quality_result", _raiser(RuntimeError("upsert down")))
    sends = []
    monkeypatch.setattr(rdb, "_send_risk_alert_safe", lambda **kw: sends.append(kw))
    flags = rdb.record_quality_result("sid-1", {"overall_score": 1, "version": 4})
    assert "low_score" in flags        # возврат флагов не сломан провалом upsert
    assert len(sends) == 1 and sends[0]["session_id"] == "sid-1"


def test_record_redis_down_alerts_when_inserted(monkeypatch):
    # Redis недоступен → фолбэк на xmax: первая вставка (inserted=True) → алерт
    cur = FakeCursor(session_row=SESSION_ROW, inserted=True)
    _wire(monkeypatch, cur)
    monkeypatch.setattr(rdb, "_alert_redis", _raiser(RuntimeError("redis down")))
    sends = []
    monkeypatch.setattr(rdb, "_send_risk_alert_safe", lambda **kw: sends.append(kw))
    rdb.record_quality_result("sid-1", {"overall_score": 1, "version": 4})
    assert len(sends) == 1


def test_record_redis_down_no_alert_on_update(monkeypatch):
    # Redis недоступен → фолбэк на xmax: UPDATE по конфликту (inserted=False) → без алерта
    cur = FakeCursor(session_row=SESSION_ROW, inserted=False)
    _wire(monkeypatch, cur)
    monkeypatch.setattr(rdb, "_alert_redis", _raiser(RuntimeError("redis down")))
    sends = []
    monkeypatch.setattr(rdb, "_send_risk_alert_safe", lambda **kw: sends.append(kw))
    rdb.record_quality_result("sid-1", {"overall_score": 1, "version": 4})
    assert not sends


def test_record_redis_down_and_upsert_down_alerts_with_warning(monkeypatch, caplog):
    # Двойной сбой: Redis недоступен И upsert упал → алерт безусловно + WARNING
    cur = FakeCursor(session_row=SESSION_ROW)
    _wire(monkeypatch, cur)
    monkeypatch.setattr(rdb, "upsert_quality_result", _raiser(RuntimeError("upsert down")))
    monkeypatch.setattr(rdb, "_alert_redis", _raiser(RuntimeError("redis down")))
    sends = []
    monkeypatch.setattr(rdb, "_send_risk_alert_safe", lambda **kw: sends.append(kw))
    with caplog.at_level(logging.WARNING, logger="tasks.results_db"):
        rdb.record_quality_result("sid-1", {"overall_score": 1, "version": 4})
    assert len(sends) == 1
    assert any("безусловно" in r.message for r in caplog.records)
