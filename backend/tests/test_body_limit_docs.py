"""C-2: глобальный лимит тела + пер-роутные капы; C-3: /docs выключены по умолчанию."""
import asyncio

import pytest
from starlette.testclient import TestClient

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app)


# --- BodySizeLimitMiddleware как чистый ASGI-юнит ---

def _scope(headers):
    return {"type": "http", "method": "POST", "path": "/x", "headers": headers}


async def _run(mw, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await mw(scope, receive, send)
    return sent


def test_middleware_rejects_oversized():
    from app.body_limit import BodySizeLimitMiddleware

    async def inner_app(scope, receive, send):
        raise AssertionError("до приложения дойти не должно")

    mw = BodySizeLimitMiddleware(inner_app, max_bytes=10)
    sent = asyncio.run(_run(mw, _scope([(b"content-length", b"11")])))
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_middleware_passes_at_limit_and_without_length():
    from app.body_limit import BodySizeLimitMiddleware
    reached = []

    async def inner_app(scope, receive, send):
        reached.append(scope["path"])

    mw = BodySizeLimitMiddleware(inner_app, max_bytes=10)
    asyncio.run(_run(mw, _scope([(b"content-length", b"10")])))
    asyncio.run(_run(mw, _scope([])))          # chunked / нет заголовка — пропускаем
    asyncio.run(_run(mw, _scope([(b"content-length", b"garbage")])))  # мусор — пропускаем
    assert len(reached) == 3


# --- вайринг в приложение ---

def test_app_has_body_limit_middleware(client):
    from app.body_limit import BodySizeLimitMiddleware
    from app.main import app
    assert any(m.cls is BodySizeLimitMiddleware for m in app.user_middleware)


# --- пер-роутный кап ингеста ---

def test_enforce_upload_cap():
    from fastapi import HTTPException
    from app.routes.chunks import _enforce_upload_cap

    _enforce_upload_cap(5, 1, "chunk")      # 5 байт < 1 МБ — ок
    _enforce_upload_cap(None, 1, "chunk")   # размер неизвестен — пропускаем
    with pytest.raises(HTTPException) as exc:
        _enforce_upload_cap(2 * 1024 * 1024, 1, "chunk")
    assert exc.value.status_code == 413


# --- C-3: schema-эндпоинты ---

def test_docs_disabled_by_default(client):
    for host in ("acme.silentqa.com", "admin.silentqa.com"):
        for path in ("/docs", "/redoc", "/openapi.json"):
            r = client.get(path, headers={"Host": host})
            assert r.status_code == 404, f"{host}{path}"


def test_enable_api_docs_flag_parses(monkeypatch):
    monkeypatch.setenv("ENABLE_API_DOCS", "1")
    from app.config import Settings
    assert Settings().ENABLE_API_DOCS is True
