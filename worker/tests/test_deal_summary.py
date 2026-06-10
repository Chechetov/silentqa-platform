"""Tests for the deal-summary builder and formatter."""
from tasks.deal_summary import build_deal_summary, format_deal_summary


def _ctx(**overrides):
    base = {
        "call_number": 3,
        "previous_calls_count": 2,
        "client_profile": {
            "budget": "20-25 млн (ранее: 18 млн, пересмотрено 2026-04-10)",
            "locations": "Хамовники",
            "purchase_goal": "жильё",
        },
        "interactions_history": [
            {"date": "2026-04-03", "type": "call_out", "duration": 840,
             "classification": "partial", "outcome": "info_provided", "brief": "Первый контакт"},
            {"date": "2026-04-10", "type": "call_out", "duration": 1200,
             "classification": "productive", "outcome": "appointment", "brief": "Назначили встречу"},
        ],
        "open_objections": [{"text": "Хочет южную сторону", "raised_at": "2026-04-10", "category": "other"}],
        "resolved_objections": [{"text": "Дорого", "resolved_at": "2026-04-10", "how": "показали рассрочку"}],
        "last_call_plan": None,
    }
    base.update(overrides)
    return base


def test_build_summary_counts_interactions_and_current_call():
    ctx = _ctx()
    current = {"brief_summary": "обсуждали впечатления", "audio_duration": 1020}
    s = build_deal_summary(ctx, current, stage_name="Квалификация", current_created_at="2026-04-17T14:23:00Z")
    assert s["stage"] == "Квалификация"
    assert s["total_interactions"] == 3  # 2 prior + 1 current
    assert s["open_objections_count"] == 1
    assert s["resolved_objections_count"] == 1


def test_format_summary_contains_expected_sections():
    ctx = _ctx()
    current = {"brief_summary": "обсуждали впечатления", "audio_duration": 1020}
    s = build_deal_summary(ctx, current, stage_name="Квалификация", current_created_at="2026-04-17T14:23:00Z")
    text = format_deal_summary(s)
    assert "🏠 Сводка по сделке" in text
    assert "Квалификация" in text
    assert "Хамовники" in text
    assert "Дорого" in text  # resolved
    assert "Хочет южную сторону" in text  # open
    assert "2026-04-17" in text  # updated_at


def test_format_summary_without_last_plan():
    ctx = _ctx(last_call_plan=None)
    current = {"brief_summary": "X", "audio_duration": 100}
    s = build_deal_summary(ctx, current, stage_name=None, current_created_at="2026-04-17T14:23:00Z")
    text = format_deal_summary(s)
    assert "Следующие шаги" not in text  # section omitted when no plan


def test_format_summary_with_last_plan():
    plan = {
        "goals": [{"priority": 1, "text": "Показ ЖК X в четверг"}],
        "talking_points": [], "unresolved_objections": [], "information_gaps": [],
        "recommended_properties": [], "risks": [], "suggested_opener": "",
    }
    ctx = _ctx(last_call_plan=plan)
    current = {"brief_summary": "X", "audio_duration": 100}
    s = build_deal_summary(ctx, current, stage_name=None, current_created_at="2026-04-17T14:23:00Z")
    text = format_deal_summary(s)
    assert "Следующие шаги" in text
    assert "Показ ЖК X" in text
