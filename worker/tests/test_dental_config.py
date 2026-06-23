import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dental_config_valid_and_wired():
    cfg = json.loads((ROOT / "companies" / "dental.json").read_text())
    assert cfg["asr"]["engine"] == "elevenlabs"
    assert cfg["default_scenario_id"] == "consultation"
    ce = cfg["card_extraction"]
    assert ce["label"] == "Карта приёма"
    sch = ce["json_schema"]
    assert sch["type"] == "object" and sch.get("additionalProperties") is False
    # dental tooth chart present
    assert "dental_status" in sch["properties"]
    # consultation scenario has criteria and NO prompt (keeps QA on the lean V2 path)
    cons = next(s for s in cfg["scenarios"] if s["id"] == "consultation")
    assert cons.get("prompt") in (None, "") and len(cons["criteria"]) >= 5
