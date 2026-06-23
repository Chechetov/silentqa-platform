"""Воркер берёт company_config_id из shared.tenants, не из метаданных клиента."""
from unittest import mock

import pytest

from tenancy.context import reset_tenant_schema, set_tenant_schema

import tasks.company_config as cc


@pytest.fixture(autouse=True)
def clear_cache():
    cc._TENANT_COMPANY_CACHE.clear()
    yield
    cc._TENANT_COMPANY_CACHE.clear()


def _fake_conn(value):
    conn = mock.MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (value,)
    return conn


def test_reads_company_config_id_for_current_tenant(monkeypatch):
    monkeypatch.setattr(cc, "shared_connect", lambda: _fake_conn("realestate"))
    token = set_tenant_schema("t_realestate")
    try:
        assert cc.tenant_company_config_id() == "realestate"
        # второй вызов — из кеша, без нового соединения
        monkeypatch.setattr(cc, "shared_connect", mock.MagicMock(side_effect=AssertionError))
        assert cc.tenant_company_config_id() == "realestate"
    finally:
        reset_tenant_schema(token)


def test_requires_tenant_context():
    from tenancy.context import TenantContextError
    with pytest.raises(TenantContextError):
        cc.tenant_company_config_id()
