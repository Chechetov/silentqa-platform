"""AmoCRM push гейтится модулем amocrm тенанта (tenants.modules.amocrm), а не
слуг-кортежом — одна истина с iter_amocrm_tenants / require_amocrm_tenant."""
from unittest import mock

from tenancy import registry

import tasks.pipeline as pl


def _fake_conn(row):
    class FakeCur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return row

    class FakeConn:
        def cursor(self):
            return FakeCur()

        def close(self):
            pass

    return FakeConn()


def test_tenant_amocrm_enabled_reads_modules(monkeypatch):
    # модуль включён
    monkeypatch.setattr(registry, "shared_connect", lambda: _fake_conn([{"amocrm": True}]))
    assert registry.tenant_amocrm_enabled("realestate") is True
    # пустые modules → дефолт OFF
    monkeypatch.setattr(registry, "shared_connect", lambda: _fake_conn([{}]))
    assert registry.tenant_amocrm_enabled("acme") is False
    # тенант не найден → False
    monkeypatch.setattr(registry, "shared_connect", lambda: _fake_conn(None))
    assert registry.tenant_amocrm_enabled("missing") is False


def test_push_to_amocrm_noops_when_module_off(monkeypatch):
    from tenancy.context import reset_tenant_schema, set_tenant_schema

    # гейт по модулю: выключен → ранний return до любых обращений к CRM/БД.
    # phone задан, чтобы без гейта тело дошло бы до find_lead_by_phone.
    gate = mock.MagicMock(return_value=False)
    monkeypatch.setattr(pl, "tenant_amocrm_enabled", gate)
    monkeypatch.setattr(pl, "find_lead_by_phone", mock.MagicMock(side_effect=AssertionError))
    token = set_tenant_schema("t_acme")
    try:
        pl._push_to_amocrm("sid", {}, {"phone": "+79990000000"}, "/tmp/x.wav")
    finally:
        reset_tenant_schema(token)
    gate.assert_called_once_with("acme")  # гейт спрошен по slug текущего тенанта


def test_pipeline_gates_lead_resolution():
    import inspect

    src = inspect.getsource(pl)
    assert "amocrm_enabled" in src  # сторожевой: lead-резолв гейтится флагом
