"""Smoke-tests for meeting_argumentation_assessment rendering in AmoCRM note."""
from tasks.amocrm_sync import format_enriched_note


def _base_report(**overrides) -> dict:
    report = {
        "overall_score": 5,
        "call_classification": {"type": "brushoff_with_attempt"},
        "brief_summary": "Клиент попросил подборку",
        "client_info": {},
        "client_info_summary": "",
        "summary": "",
        "protocol_checklist": [],
        "objections": [],
        "conversation_outcome": {"result": "send_info", "description": ""},
        "improvement_suggestions": [],
    }
    report.update(overrides)
    return report


def test_weak_push_renders_missed_opportunities():
    report = _base_report(
        meeting_argumentation_assessment={
            "attempted": True,
            "arguments_used": [],
            "objections_faced": [],
            "missed_opportunities": [
                {
                    "trigger_quote": "пришлите подборку",
                    "recommended_category_id": "saves_time",
                    "why": "Экономия времени — ключевой триггер",
                }
            ],
            "overall_push_quality": "weak",
            "meeting_formats_offered": [],
        }
    )
    note = format_enriched_note(report, session_id="t-1")
    assert "Аргументация встречи: слабая" in note
    assert "saves_time" in note
    assert "пришлите подборку" in note


def test_adequate_push_does_not_render_block():
    report = _base_report(
        meeting_argumentation_assessment={
            "attempted": True,
            "arguments_used": [],
            "objections_faced": [],
            "missed_opportunities": [
                {
                    "trigger_quote": "x",
                    "recommended_category_id": "saves_time",
                    "why": "y",
                }
            ],
            "overall_push_quality": "adequate",
            "meeting_formats_offered": ["zoom"],
        }
    )
    note = format_enriched_note(report, session_id="t-2")
    assert "Аргументация встречи" not in note


def test_not_applicable_does_not_render_block():
    report = _base_report(
        meeting_argumentation_assessment={
            "attempted": False,
            "arguments_used": [],
            "objections_faced": [],
            "missed_opportunities": [],
            "overall_push_quality": "not_applicable",
            "meeting_formats_offered": [],
        }
    )
    note = format_enriched_note(report, session_id="t-3")
    assert "Аргументация встречи" not in note


def test_weak_but_no_missed_opportunities_does_not_render_block():
    """If LLM marked weak but didn't provide missed opportunities — nothing to show."""
    report = _base_report(
        meeting_argumentation_assessment={
            "attempted": False,
            "arguments_used": [],
            "objections_faced": [],
            "missed_opportunities": [],
            "overall_push_quality": "weak",
            "meeting_formats_offered": [],
        }
    )
    note = format_enriched_note(report, session_id="t-4")
    assert "Аргументация встречи" not in note


def test_report_without_assessment_field_does_not_break():
    """Backward compat: old reports without the V4 field render fine."""
    report = _base_report()
    note = format_enriched_note(report, session_id="t-5")
    assert "Оценка:" in note
    assert "Аргументация встречи" not in note


def test_missed_opportunities_capped_at_three():
    report = _base_report(
        meeting_argumentation_assessment={
            "attempted": True,
            "arguments_used": [],
            "objections_faced": [],
            "missed_opportunities": [
                {"trigger_quote": f"q{i}", "recommended_category_id": f"cat_{i}", "why": f"w{i}"}
                for i in range(10)
            ],
            "overall_push_quality": "weak",
            "meeting_formats_offered": [],
        }
    )
    note = format_enriched_note(report, session_id="t-6")
    assert "cat_0" in note
    assert "cat_2" in note
    assert "cat_3" not in note
