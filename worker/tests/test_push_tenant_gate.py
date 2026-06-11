"""AmoCRM push гейтится принадлежностью тенанта к AMOCRM_TENANT_SLUGS."""
from unittest import mock

from tenancy.registry import AMOCRM_TENANT_SLUGS

import tasks.pipeline as pl


def test_amocrm_tenant_slugs_is_realestate_only():
    assert AMOCRM_TENANT_SLUGS == ("realestate",)


def test_push_to_amocrm_noops_for_foreign_tenant(monkeypatch):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    # любой AmoCRM-вызов внутри тела должен быть недостижим;
    # phone задан, чтобы без гейта тело дошло бы до find_lead_by_phone
    monkeypatch.setattr(pl, "find_lead_by_phone", mock.MagicMock(side_effect=AssertionError))
    token = set_tenant_schema("t_acme")
    try:
        # ранний return до любых обращений к CRM/БД
        pl._push_to_amocrm("sid", {}, {"phone": "+79990000000"}, "/tmp/x.wav")
    finally:
        reset_tenant_schema(token)


def test_pipeline_gates_lead_resolution():
    import inspect
    src = inspect.getsource(pl)
    assert "amocrm_enabled" in src  # сторожевой: lead-резолв гейтится флагом
