import tasks.company_config as cc


def test_get_card_extraction_present():
    cfg = {"card_extraction": {"label": "Карта приёма", "prompt": "p", "json_schema": {"type": "object"}}}
    assert cc.get_card_extraction(cfg)["label"] == "Карта приёма"


def test_get_card_extraction_absent():
    assert cc.get_card_extraction({"id": "realestate"}) is None


def test_default_scenario_id():
    assert cc.get_default_scenario_id({"default_scenario_id": "consultation"}) == "consultation"
    assert cc.get_default_scenario_id({}) is None
