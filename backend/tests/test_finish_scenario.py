"""valid_scenario: санкционирование клиентского appointment_type → scenario_id.

Чистый хелпер читает COMPANIES_PATH/<config_id>.json и возвращает scenario_id,
только если он есть среди сценариев конфига. finish_session использует это, чтобы
RE-safe плюмбить config={"scenario_id": ...} в enqueue (нет/невалид → None)."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def companies_dir(tmp_path, monkeypatch):
    (tmp_path / "dental.json").write_text(json.dumps({
        "id": "dental",
        "scenarios": [
            {"id": "consultation", "name": "Консультация"},
            {"id": "treatment_plan"},
        ],
    }), encoding="utf-8")
    monkeypatch.setenv("COMPANIES_PATH", str(tmp_path))
    # хелперы кешируют через lru_cache — сбросить между тестами (env поменялся)
    import app.company_scenarios as cs
    cs._scenarios_for_config.cache_clear()
    cs._scenario_list_for_config.cache_clear()
    return tmp_path


def test_valid_scenario_accepts_known(companies_dir):
    from app.company_scenarios import valid_scenario
    assert valid_scenario("dental", "consultation") == "consultation"
    assert valid_scenario("dental", "treatment_plan") == "treatment_plan"


def test_valid_scenario_rejects_unknown(companies_dir):
    from app.company_scenarios import valid_scenario
    assert valid_scenario("dental", "evil") is None
    assert valid_scenario("dental", None) is None
    assert valid_scenario(None, "consultation") is None
    assert valid_scenario("nonexistent_cfg", "consultation") is None


def test_scenarios_for_returns_id_and_name(companies_dir):
    from app.company_scenarios import scenarios_for
    assert scenarios_for("dental") == [
        {"id": "consultation", "name": "Консультация"},
        {"id": "treatment_plan", "name": "treatment_plan"},  # name → id when absent
    ]


def test_scenarios_for_tolerant_of_missing(companies_dir):
    from app.company_scenarios import scenarios_for
    assert scenarios_for("nonexistent_cfg") == []
    assert scenarios_for(None) == []
