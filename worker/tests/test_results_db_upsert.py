"""upsert/record: SQL-параметры, best-effort, идемпотентный ON CONFLICT."""
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import tasks.results_db as rdb


class FakeCursor:
    def __init__(self, session_row=None):
        self.executed = []
        self._session_row = session_row

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
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
