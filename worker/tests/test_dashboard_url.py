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
    monkeypatch.setenv("BASE_DOMAIN", "silentqa.com")
    token = set_tenant_schema("t_acme")
    try:
        assert ams._dashboard_base_url() == "https://acme.silentqa.com"
    finally:
        reset_tenant_schema(token)


def test_no_tenant_context_uses_env_global():
    assert ams._dashboard_base_url() == ams.DASHBOARD_BASE_URL
