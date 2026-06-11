"""Tests for tenancy.db — the single sync-connection factory."""
import pytest

import tenancy.db as tdb
from tenancy.context import reset_tenant_schema, set_tenant_schema, TenantContextError


@pytest.fixture()
def fake_connect(monkeypatch):
    calls = []

    def _connect(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(tdb.psycopg2, "connect", _connect)
    monkeypatch.setenv("DATABASE_URL_SYNC", "postgresql+psycopg2://u:p@h:5/db")
    return calls


def test_get_sync_db_url_strips_dialect(monkeypatch):
    monkeypatch.setenv("DATABASE_URL_SYNC", "postgresql+psycopg2://u:p@h:5/db")
    assert tdb.get_sync_db_url() == "postgresql://u:p@h:5/db"
    monkeypatch.delenv("DATABASE_URL_SYNC")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5/db")
    assert tdb.get_sync_db_url() == "postgresql://u:p@h:5/db"
    monkeypatch.delenv("DATABASE_URL")
    assert tdb.get_sync_db_url() == ""


def test_tenant_connect_injects_search_path(fake_connect):
    token = set_tenant_schema("t_acme")
    try:
        tdb.tenant_connect()
    finally:
        reset_tenant_schema(token)
    url, kwargs = fake_connect[0]
    assert url == "postgresql://u:p@h:5/db"
    assert kwargs["options"] == "-c search_path=t_acme,shared,public"


def test_tenant_connect_requires_context(fake_connect):
    with pytest.raises(TenantContextError):
        tdb.tenant_connect()


def test_tenant_connect_passes_kwargs(fake_connect):
    token = set_tenant_schema("t_acme")
    try:
        tdb.tenant_connect(connect_timeout=5)
    finally:
        reset_tenant_schema(token)
    assert fake_connect[0][1]["connect_timeout"] == 5


def test_shared_connect(fake_connect):
    tdb.shared_connect()
    assert fake_connect[0][1]["options"] == "-c search_path=shared,public"


def test_tenant_engine_options(monkeypatch):
    monkeypatch.setenv("DATABASE_URL_SYNC", "postgresql+psycopg2://u:p@h:5/db")
    captured = {}

    def _fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(tdb, "_create_engine", _fake_create_engine)
    token = set_tenant_schema("t_acme")
    try:
        tdb.tenant_engine()
    finally:
        reset_tenant_schema(token)
    assert captured["url"] == "postgresql+psycopg2://u:p@h:5/db"
    assert captured["kwargs"]["connect_args"] == {
        "options": "-c search_path=t_acme,shared,public"
    }
