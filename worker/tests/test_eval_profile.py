"""load_active_eval_profile: активный профиль оценки сценария из БД тенанта.

Pure unit: фейковый sync-коннект (без Postgres). Проверяем None-пути и коэрцию criteria.
"""
import tasks.eval_profile as ep


class _Cur:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, row):
        self._row = row
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self._row)

    def close(self):
        self.closed = True


def _patch(monkeypatch, row, db_url="postgresql://x"):
    monkeypatch.setattr(ep, "get_sync_db_url", lambda: db_url)
    monkeypatch.setattr(ep, "tenant_connect", lambda: _Conn(row))


def test_none_when_no_scenario(monkeypatch):
    _patch(monkeypatch, ("[]", None))
    assert ep.load_active_eval_profile(None) is None


def test_none_when_no_db_url(monkeypatch):
    _patch(monkeypatch, ([], None), db_url="")
    assert ep.load_active_eval_profile("general") is None


def test_none_when_no_active_row(monkeypatch):
    _patch(monkeypatch, None)
    assert ep.load_active_eval_profile("general") is None


def test_returns_profile(monkeypatch):
    crit = [{"id": "c1", "name": "Ясность"}]
    _patch(monkeypatch, (crit, "оцени строго"))
    out = ep.load_active_eval_profile("general")
    assert out == {"criteria": crit, "prompt": "оцени строго"}


def test_coerces_json_string_criteria(monkeypatch):
    _patch(monkeypatch, ('[{"id":"c1","name":"Ясность"}]', None))
    out = ep.load_active_eval_profile("general")
    assert out["criteria"] == [{"id": "c1", "name": "Ясность"}]
    assert out["prompt"] is None


def test_db_error_is_soft(monkeypatch):
    def _boom():
        raise RuntimeError("table missing")
    monkeypatch.setattr(ep, "get_sync_db_url", lambda: "postgresql://x")
    monkeypatch.setattr(ep, "tenant_connect", _boom)
    assert ep.load_active_eval_profile("general") is None
