"""Watchdog rules 3a/3b: processing по processing_started_at, НЕ по created_at."""
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
    # 3b — потерянные до старта: по created_at, но ТОЛЬКО при IS NULL
    assert "processing_started_at IS NULL" in joined
    assert "'failed'" in joined


def test_default_thresholds():
    assert sw.PROCESSING_STALE_HOURS == 6
    assert sw.ENQUEUED_STALE_HOURS == 24
