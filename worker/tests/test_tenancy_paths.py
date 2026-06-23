"""Tests for tenancy.paths and tenancy.registry."""
from pathlib import Path

import tenancy.registry as treg
from tenancy.paths import (
    tenant_audio_amocrm_dir,
    tenant_audio_sessions_dir,
    tenant_results_dir,
)


def test_path_layouts():
    assert tenant_audio_sessions_dir("./data/audio", "acme", "sid-1") == Path(
        "./data/audio/acme/sessions/sid-1"
    )
    assert tenant_audio_amocrm_dir("./data/audio", "acme", 123) == Path(
        "./data/audio/acme/amocrm/123"
    )
    assert tenant_results_dir("./data/results", "acme", "sid-1") == Path(
        "./data/results/acme/sid-1"
    )


def test_iter_active_tenants(monkeypatch):
    rows = [("realestate", "t_realestate", {"amocrm": True}), ("acme", "t_acme", {})]

    class _Cur:
        def execute(self, sql, params=None):
            self._sql = sql
        def fetchall(self):
            return rows
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(treg, "shared_connect", lambda: _Conn())
    tenants = treg.iter_active_tenants()
    assert [t["slug"] for t in tenants] == ["realestate", "acme"]
    assert tenants[0]["schema_name"] == "t_realestate"
    # AmoCRM gated by modules.amocrm (realestate has it on)
    amo = treg.iter_amocrm_tenants()
    assert [t["slug"] for t in amo] == ["realestate"]
