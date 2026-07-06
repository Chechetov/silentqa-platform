"""Watchdog rules 3a/3b: processing по processing_started_at, НЕ по created_at."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import tasks.session_watchdog as sw


class FakeCursor:
    def __init__(self):
        self.executed = []
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.executed.append(s)
        if s.startswith("UPDATE sessions") and "'processing'" in s:
            if "processing_started_at IS NULL" in s:
                self._rows = [("lost-1",)]                 # 3b: один потерянный до старта
            else:
                self._rows = [("dead-1",), ("dead-2",)]    # 3a: два зависших после старта
        else:
            self._rows = []                                # rules 1-2: пусто

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_stale_rules(monkeypatch):
    cur = FakeCursor()
    monkeypatch.setattr(sw, "_get_sync_db_url", lambda: "postgresql://x")
    monkeypatch.setattr(sw, "tenant_connect", lambda: FakeConn(cur))

    out = sw._sweep_for_current_tenant()

    assert out["stale_processing"] == 2
    assert out["stale_enqueued"] == 1
    joined = " | ".join(cur.executed)
    # 3a — по метке старта стадии, НЕ по created_at
    assert "processing_started_at IS NOT NULL" in joined
    assert "processing_started_at <" in joined
    assert "'failed'" in joined

    # 3b — изолируем именно этот UPDATE (единственный с IS NULL), чтобы не путаться
    # с created_at из правила 2 (SELECT пустых сессий).
    stmt_3b = next(
        s for s in cur.executed
        if s.startswith("UPDATE sessions")
        and "'processing'" in s
        and "processing_started_at IS NULL" in s
    )
    # Якорь 3b — enqueued_at с fallback на created_at, а НЕ голый created_at
    assert "COALESCE(enqueued_at, created_at)" in stmt_3b
    assert "processing_started_at IS NULL" in stmt_3b
    assert "created_at <" not in stmt_3b  # голого created_at < в 3b быть не должно


def test_default_thresholds():
    assert sw.PROCESSING_STALE_HOURS == 6
    assert sw.ENQUEUED_STALE_HOURS == 24


def test_reenqueue_default_threshold():
    assert sw.ANALYZE_REENQUEUE_AFTER_MIN == 120
    assert sw.ANALYZE_REENQUEUE_MAX == 2


# --- B-1: watchdog ре-энкьюит потерянный analyze вместо выброса транскрипта ---

class ReenqCursor:
    """Фейк-курсор для правила ре-энкью: двухколоночный SELECT кандидатов
    отдаёт заданный список, метадата-UPDATE капчерится, всё прочее — пусто
    (rules 1-2 SELECT, finalize/fail и 3a/3b UPDATE → 0 строк)."""

    def __init__(self, candidates):
        self.candidates = candidates            # list[(sid, meta_dict|None)]
        self.executed = []                      # list[(sql, params)]
        self.metadata_updates = []              # params метадата-UPDATE
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.executed.append((s, params))
        if s.startswith("SELECT s.id::text, s.metadata"):
            self._rows = list(self.candidates)
            self.rowcount = len(self._rows)
        elif s.startswith("UPDATE sessions SET metadata"):
            self.metadata_updates.append(params)
            self._rows = []
            self.rowcount = 1
        else:
            self._rows = []
            self.rowcount = 0

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ReenqConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_reenqueue(monkeypatch, tmp_path, candidates,
                   transcript_age_min=200, quality=False):
    """Прогнать sweep под тенант-контекстом t_acme, вернуть (out, cur, sent).

    Для каждого кандидата кладём РЕАЛЬНЫЙ transcript.json под tmp RESULTS_PATH и
    выставляем его mtime = now - transcript_age_min мин (os.utime) — так реально
    проверяется mtime-порог _transcript_stale. transcript_age_min=None → файла нет.
    Дефолт 200 мин > порога 120 → «стадия 1 давно сделала handoff».
    """
    cur = ReenqCursor(candidates)
    sent = []
    monkeypatch.setattr(sw, "_get_sync_db_url", lambda: "postgresql://x")
    monkeypatch.setattr(sw, "tenant_connect", lambda: ReenqConn(cur))
    monkeypatch.setattr(sw, "RESULTS_PATH", str(tmp_path))
    monkeypatch.setattr(sw, "_quality_exists", lambda sid: quality)
    monkeypatch.setattr(sw.app, "send_task",
                        lambda name, **kw: sent.append((name, kw)))
    token = sw.set_tenant_schema("t_acme")
    try:
        if transcript_age_min is not None:
            for sid, _meta in candidates:
                p = sw._transcript_path(sid)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}")
                old = time.time() - transcript_age_min * 60
                os.utime(p, (old, old))
        out = sw._sweep_for_current_tenant()
    finally:
        sw.reset_tenant_schema(token)
    return out, cur, sent


def test_reenqueue_when_transcript_ready(monkeypatch, tmp_path):
    out, cur, sent = _run_reenqueue(monkeypatch, tmp_path, [("sess-1", {})])

    assert out["reenqueued"] == 1
    # send_task ушёл на очередь analysis с полным составом kwargs
    assert len(sent) == 1
    name, kw = sent[0]
    assert name == "pipeline.analyze_session"
    assert kw["queue"] == "analysis"
    assert kw["kwargs"]["session_id"] == "sess-1"
    assert kw["kwargs"]["tenant_schema"] == "t_acme"
    assert kw["kwargs"]["config"] == {}          # без appointment_type → дефолт
    assert kw["kwargs"]["audio_path"].endswith("full.wav")
    assert "acme" in kw["kwargs"]["audio_path"]
    # счётчик записан в метадату = 1, поставлен timestamp
    assert len(cur.metadata_updates) == 1
    meta = json.loads(cur.metadata_updates[0][0])
    assert meta["analyze_reenqueue_count"] == 1
    assert "analyze_reenqueue_at" in meta


def test_reenqueue_restores_scenario_from_appointment(monkeypatch, tmp_path):
    # Fix A: appointment_type в metadata восстанавливает config={"scenario_id": ...}
    out, cur, sent = _run_reenqueue(
        monkeypatch, tmp_path, [("sess-1", {"appointment_type": "X"})])

    assert out["reenqueued"] == 1
    assert len(sent) == 1
    _name, kw = sent[0]
    assert kw["kwargs"]["config"] == {"scenario_id": "X"}


def test_reenqueue_increments_existing_count(monkeypatch, tmp_path):
    # пауза с прошлого ре-энкью > порога (120 мин) → повтор разрешён
    old = (datetime.now(timezone.utc) - timedelta(minutes=200)).isoformat()
    out, cur, sent = _run_reenqueue(
        monkeypatch, tmp_path,
        [("sess-1", {"analyze_reenqueue_count": 1, "analyze_reenqueue_at": old})],
    )
    assert out["reenqueued"] == 1
    assert len(sent) == 1
    meta = json.loads(cur.metadata_updates[0][0])
    assert meta["analyze_reenqueue_count"] == 2


def test_no_reenqueue_when_count_maxed(monkeypatch, tmp_path):
    out, cur, sent = _run_reenqueue(
        monkeypatch, tmp_path, [("sess-1", {"analyze_reenqueue_count": 2})])

    assert out["reenqueued"] == 0
    assert sent == []
    assert cur.metadata_updates == []


def test_no_reenqueue_when_quality_exists(monkeypatch, tmp_path):
    out, cur, sent = _run_reenqueue(
        monkeypatch, tmp_path, [("sess-1", {})], quality=True)

    assert out["reenqueued"] == 0
    assert sent == []


def test_no_reenqueue_when_transcript_missing(monkeypatch, tmp_path):
    # transcript_age_min=None → файл не создаём → _transcript_stale=False
    out, cur, sent = _run_reenqueue(
        monkeypatch, tmp_path, [("sess-1", {})], transcript_age_min=None)

    assert out["reenqueued"] == 0
    assert sent == []


def test_no_reenqueue_when_transcript_fresh(monkeypatch, tmp_path):
    # Fix C: свежий transcript.json (mtime 5 мин < порог 120) → analyze может быть
    # жив, молчим
    out, cur, sent = _run_reenqueue(
        monkeypatch, tmp_path, [("sess-1", {})], transcript_age_min=5)

    assert out["reenqueued"] == 0
    assert sent == []
    assert cur.metadata_updates == []


def test_no_reenqueue_when_recent(monkeypatch, tmp_path):
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    out, cur, sent = _run_reenqueue(
        monkeypatch, tmp_path,
        [("sess-1", {"analyze_reenqueue_count": 1, "analyze_reenqueue_at": fresh})],
    )
    assert out["reenqueued"] == 0
    assert sent == []
    assert cur.metadata_updates == []
