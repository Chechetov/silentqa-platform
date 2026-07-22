"""Старт бэкенда: миграции НЕ применяются; head-check только логирует."""
import logging

from starlette.testclient import TestClient

import app.main as main


def _stub_registry(monkeypatch):
    # Любой HTTP-запрос идёт через внешний TenantResolutionMiddleware
    # (реестр → Postgres); реестр стабим пустым списком — как в остальных
    # TestClient-тестах (см. tests/test_cors_config.py).
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return []

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)


def test_mismatch_warns_but_starts(monkeypatch, caplog):
    _stub_registry(monkeypatch)
    monkeypatch.setattr(main, "migrate_check", lambda: ["t_lag"])
    with caplog.at_level(logging.CRITICAL):
        with TestClient(main.app) as c:  # контекст-менеджер гоняет lifespan
            assert c.get("/health").json() == {"status": "ok"}
    assert any("t_lag" in r.getMessage() for r in caplog.records)


def test_clean_start_silent(monkeypatch, caplog):
    monkeypatch.setattr(main, "migrate_check", lambda: [])
    with caplog.at_level(logging.CRITICAL):
        with TestClient(main.app):
            pass
    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]


def test_db_down_still_starts(monkeypatch, caplog):
    _stub_registry(monkeypatch)

    def boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(main, "migrate_check", boom)
    with caplog.at_level(logging.CRITICAL):
        with TestClient(main.app) as c:
            assert c.get("/health").status_code == 200
    assert any("migrate check" in r.getMessage() for r in caplog.records)


def test_ready_ok(monkeypatch):
    _stub_registry(monkeypatch)

    async def ping_ok():
        return None
    monkeypatch.setattr(main, "_db_ping", ping_ok)
    assert TestClient(main.app).get("/health/ready").status_code == 200


def test_ready_degraded_when_db_down(monkeypatch):
    _stub_registry(monkeypatch)

    async def ping_boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(main, "_db_ping", ping_boom)
    r = TestClient(main.app).get("/health/ready")
    assert r.status_code == 503
    assert r.json()["db"] == "unreachable"
