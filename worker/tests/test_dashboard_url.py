"""Пер-тенант dashboard_base_url для ссылок в AmoCRM-нотах (спека 5.5)."""
from unittest import mock

import pytest

from tenancy.context import reset_tenant_schema, set_tenant_schema

import tasks.amocrm_sync as ams


@pytest.fixture(autouse=True)
def clear_cache():
    ams._DASHBOARD_URL_CACHE.clear()
    yield
    ams._DASHBOARD_URL_CACHE.clear()


def _fake_conn(value):
    conn = mock.MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (value,)
    return conn


def test_uses_tenant_row_value(monkeypatch):
    monkeypatch.setattr(ams, "shared_connect", lambda: _fake_conn("https://rogov.automate-it.fun"))
    token = set_tenant_schema("t_realestate")
    try:
        assert ams._dashboard_base_url() == "https://rogov.automate-it.fun"
    finally:
        reset_tenant_schema(token)


def test_falls_back_to_subdomain(monkeypatch):
    monkeypatch.setattr(ams, "shared_connect", lambda: _fake_conn(None))
    # недефолтный домен — чтобы тест пиновал чтение env, а не хардкод-дефолт
    monkeypatch.setenv("BASE_DOMAIN", "example.test")
    token = set_tenant_schema("t_acme")
    try:
        assert ams._dashboard_base_url() == "https://acme.example.test"
    finally:
        reset_tenant_schema(token)


def test_transient_lookup_failure_is_not_cached(monkeypatch):
    """Сбой shared_connect не должен пиновать subdomain-фоллбек в кеше навсегда."""
    monkeypatch.setenv("BASE_DOMAIN", "example.test")
    calls = {"n": 0}

    def flaky_connect():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        return _fake_conn("https://rogov.automate-it.fun")

    monkeypatch.setattr(ams, "shared_connect", flaky_connect)
    token = set_tenant_schema("t_realestate")
    try:
        # первый вызов: транзиентная ошибка → фоллбек, но БЕЗ кеширования
        assert ams._dashboard_base_url() == "https://realestate.example.test"
        # второй вызов: БД ожила → реальное значение из shared.tenants
        assert ams._dashboard_base_url() == "https://rogov.automate-it.fun"
    finally:
        reset_tenant_schema(token)


def test_no_tenant_context_uses_env_global():
    assert ams._dashboard_base_url() == ams.DASHBOARD_BASE_URL
