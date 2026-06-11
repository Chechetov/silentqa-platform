"""Tenant propagation: enqueue tasks require tenant_schema; beat iterates."""
import pytest

import tasks.pipeline as pl
from tenancy.context import get_tenant_schema


def test_process_session_requires_tenant_schema():
    with pytest.raises(ValueError, match="tenant_schema"):
        pl.process_session.run("sid-1", None)  # tenant_schema omitted


def test_process_session_sets_and_resets_context(monkeypatch):
    seen = {}

    def _fake_body(task, session_id, config):
        seen["schema"] = get_tenant_schema()
        return {"ok": True}

    monkeypatch.setattr(pl, "_process_session_body", _fake_body)
    pl.process_session.run("sid-1", None, tenant_schema="t_acme")
    assert seen["schema"] == "t_acme"
    assert get_tenant_schema() is None  # reset after task


def test_process_amocrm_call_requires_tenant_schema():
    import tasks.amocrm_poll as ap

    with pytest.raises(ValueError, match="tenant_schema"):
        ap.process_amocrm_call.run(1)
