"""app.migrate --check: read-only сверка head'ов без применения миграций."""
import pytest

import app.migrate as m


class FakeCursor:
    def __init__(self, versions, tenants):
        self.versions = versions   # schema -> version_num | None (None = нет таблицы)
        self.tenants = tenants     # список schema_name активных тенантов
        self._rows = []

    def execute(self, sql, params=None):
        if "to_regclass" in sql:
            schema = params[0].split(".")[0]
            self._rows = [("x" if self.versions.get(schema) is not None else None,)]
        elif "FROM shared.tenants" in sql:
            self._rows = [(s,) for s in self.tenants]
        elif "alembic_version" in sql:
            schema = sql.split('"')[1]
            self._rows = [(self.versions[schema],)]

    def fetchone(self):
        return self._rows[0]

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


def _wire(monkeypatch, versions, tenants):
    monkeypatch.setattr(m, "shared_connect",
                        lambda: FakeConn(FakeCursor(versions, tenants)))
    monkeypatch.setattr(m, "_script_head",
                        lambda ini, d: "H_SHARED" if "shared" in ini else "H_TENANT")


def test_all_current(monkeypatch):
    _wire(monkeypatch, {"shared": "H_SHARED", "t_acme": "H_TENANT"}, ["t_acme"])
    assert m.check() == []


def test_lagging_tenant(monkeypatch):
    _wire(monkeypatch, {"shared": "H_SHARED", "t_acme": "old"}, ["t_acme"])
    assert m.check() == ["t_acme"]


def test_lagging_shared(monkeypatch):
    _wire(monkeypatch, {"shared": "old", "t_acme": "H_TENANT"}, ["t_acme"])
    assert m.check() == ["shared"]


def test_missing_version_table_counts_as_lagging(monkeypatch):
    # свежепровиженный тенант без alembic_version — тоже «отстаёт»
    _wire(monkeypatch, {"shared": "H_SHARED", "t_new": None}, ["t_new"])
    assert m.check() == ["t_new"]


def test_invalid_schema_from_db_fails_loud(monkeypatch):
    _wire(monkeypatch, {"shared": "H_SHARED"}, ["t_acme; DROP TABLE x"])
    with pytest.raises(ValueError):
        m.check()


def test_cli_check_exit_codes(monkeypatch, capsys):
    _wire(monkeypatch, {"shared": "H_SHARED", "t_acme": "H_TENANT"}, ["t_acme"])
    m.main(["--check"])  # не бросает
    assert "heads OK" in capsys.readouterr().out

    _wire(monkeypatch, {"shared": "H_SHARED", "t_acme": "old"}, ["t_acme"])
    with pytest.raises(SystemExit) as e:
        m.main(["--check"])
    assert e.value.code == 1
    assert "t_acme" in capsys.readouterr().out
