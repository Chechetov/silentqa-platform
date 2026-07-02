"""CORS: allow_origins берётся из settings.ALLOWED_ORIGINS, а не хардкод."""
from fastapi.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient

import app.main as main


def _cors_kwargs():
    entries = [m for m in main.app.user_middleware if m.cls is CORSMiddleware]
    assert len(entries) == 1
    return entries[0].kwargs


def test_allow_origins_uses_settings():
    # плумбинг: в мидлварь уходит именно распарсенный settings-список
    assert _cors_kwargs()["allow_origins"] == main.origins


def test_default_stays_wildcard():
    # дефолт ALLOWED_ORIGINS="*" сохраняет текущее поведение
    assert main.origins == ["*"]


def test_preflight_wildcard(monkeypatch):
    # запрос идёт через внешний TenantResolutionMiddleware (реестр → Postgres),
    # поэтому реестр стабим пустым списком — как в остальных TestClient-тестах
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return []

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)

    c = TestClient(main.app)
    r = c.options("/health", headers={
        "Origin": "https://anywhere.example",
        "Access-Control-Request-Method": "GET",
    })
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
