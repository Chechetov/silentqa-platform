"""Storage paths gain the tenant slug component."""
from pathlib import Path

import pytest

import tasks.pipeline as pl
from tenancy.context import reset_tenant_schema, set_tenant_schema


@pytest.fixture()
def acme_ctx():
    token = set_tenant_schema("t_acme")
    yield
    reset_tenant_schema(token)


def test_save_results_under_tenant_slug(tmp_path, monkeypatch, acme_ctx):
    monkeypatch.setattr(pl, "RESULTS_PATH", str(tmp_path))
    pl.save_results("sid-1", "quality", {"x": 1})
    assert (tmp_path / "acme" / "sid-1" / "quality.json").exists()


def test_merge_chunks_reads_tenant_dir(tmp_path, monkeypatch, acme_ctx):
    monkeypatch.setattr(pl, "AUDIO_PATH", str(tmp_path))
    sdir = tmp_path / "acme" / "sessions" / "sid-1"
    sdir.mkdir(parents=True)
    with pytest.raises(Exception, match="[Nn]o chunks"):
        pl.merge_chunks("sid-1")  # пустая директория → прежняя ошибка "no chunks"
