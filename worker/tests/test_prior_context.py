"""Tests for prior_context building: merge logic and structure."""
from tasks.prior_context import merge_client_profiles, summarise_interaction, build_prior_context_dict


def test_merge_empty_profiles_returns_empty():
    assert merge_client_profiles([]) == {}


def test_merge_newer_overrides_older():
    older = {"date": "2026-04-01", "client_info": {"budget": "20 млн", "locations": "Хамовники"}}
    newer = {"date": "2026-04-10", "client_info": {"budget": "25 млн"}}
    result = merge_client_profiles([older, newer])
    # budget changed — must note both versions
    assert "25 млн" in result["budget"]
    assert "20" in result["budget"] or "пересмотр" in result["budget"].lower()
    # locations unchanged — passes through
    assert result["locations"] == "Хамовники"


def test_merge_preserves_stable_field():
    a = {"date": "2026-04-01", "client_info": {"purchase_goal": "жильё"}}
    b = {"date": "2026-04-10", "client_info": {"purchase_goal": "жильё"}}
    result = merge_client_profiles([a, b])
    assert result["purchase_goal"] == "жильё"


def test_merge_skips_nulls():
    a = {"date": "2026-04-01", "client_info": {"budget": "20 млн"}}
    b = {"date": "2026-04-10", "client_info": {"budget": None}}
    result = merge_client_profiles([a, b])
    assert result["budget"] == "20 млн"


def test_summarise_interaction_extracts_core_fields():
    report = {
        "brief_summary": "Первый контакт",
        "call_classification": {"type": "partial"},
        "conversation_outcome": {"result": "info_provided"},
    }
    s = summarise_interaction(report, created_at_iso="2026-04-03T10:00:00Z", duration=840, direction="out")
    assert s["date"] == "2026-04-03"
    assert s["type"] == "call_out"
    assert s["duration"] == 840
    assert s["classification"] == "partial"
    assert s["outcome"] == "info_provided"
    assert s["brief"] == "Первый контакт"


def test_build_prior_context_first_call():
    ctx = build_prior_context_dict(past_reports=[], current_created_at="2026-04-17T10:00:00Z")
    assert ctx["call_number"] == 1
    assert ctx["previous_calls_count"] == 0
    assert ctx["client_profile"] == {}
    assert ctx["interactions_history"] == []
    assert ctx["open_objections"] == []
    assert ctx["resolved_objections"] == []


def test_build_prior_context_aggregates_objections():
    r1 = {
        "created_at_iso": "2026-04-01T10:00:00Z",
        "duration": 900,
        "direction": "out",
        "report": {
            "brief_summary": "первый",
            "client_info": {"budget": "20 млн"},
            "objections": [
                {"text": "Дорого", "resolved": False, "category": "too_expensive"}
            ],
        },
    }
    r2 = {
        "created_at_iso": "2026-04-10T10:00:00Z",
        "duration": 1200,
        "direction": "out",
        "report": {
            "brief_summary": "второй",
            "client_info": {"budget": "25 млн"},
            "objections": [
                {"text": "Дорого", "resolved": True, "category": "too_expensive"}
            ],
        },
    }
    ctx = build_prior_context_dict(past_reports=[r1, r2], current_created_at="2026-04-17T10:00:00Z")
    assert ctx["call_number"] == 3
    assert ctx["previous_calls_count"] == 2
    assert len(ctx["interactions_history"]) == 2
    # Дорого was raised in #1 and resolved in #2 — must end up in resolved_objections only
    assert any("Дорого" in o["text"] for o in ctx["resolved_objections"])
    assert not any("Дорого" in o["text"] for o in ctx["open_objections"])


def test_build_prior_context_surfaces_last_call_plan():
    r1 = {
        "created_at_iso": "2026-04-01T10:00:00Z",
        "duration": 900,
        "direction": "out",
        "report": {"brief_summary": "первый", "client_info": {}},
        "plan": None,
    }
    plan = {
        "goals": [{"priority": 1, "text": "Назначить показ"}],
        "talking_points": [{"topic": "Виды", "argument": "хорошие", "personalized_hook": ""}],
        "unresolved_objections": [],
        "information_gaps": [],
        "recommended_properties": [],
        "risks": [],
        "suggested_opener": "Иван, добрый день!",
    }
    r2 = {
        "created_at_iso": "2026-04-10T10:00:00Z",
        "duration": 1200,
        "direction": "out",
        "report": {"brief_summary": "второй", "client_info": {}},
        "plan": plan,
    }
    ctx = build_prior_context_dict(past_reports=[r1, r2], current_created_at="2026-04-17T10:00:00Z")
    # last_call_plan must reflect the plan from the most-recent prior call that has one
    assert ctx["last_call_plan"] == plan


def test_build_prior_context_last_call_plan_none_when_missing():
    r1 = {
        "created_at_iso": "2026-04-10T10:00:00Z",
        "duration": 1200,
        "direction": "out",
        "report": {"brief_summary": "первый", "client_info": {}},
        "plan": None,
    }
    ctx = build_prior_context_dict(past_reports=[r1], current_created_at="2026-04-17T10:00:00Z")
    assert ctx.get("last_call_plan") is None


def test_extend_history_with_events_marks_offline_gap():
    from tasks.prior_context import extend_history_with_events
    import datetime as _dt
    call_ts = _dt.datetime(2026, 4, 3, tzinfo=_dt.timezone.utc)
    gap_ts = _dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc)
    interactions = [{"date": "2026-04-03", "type": "call_out"}]
    events = [
        {"type": "lead_status_changed", "created_at": int(gap_ts.timestamp())},
        {"type": "lead_status_changed", "created_at": int(call_ts.timestamp())},  # same day as call → not a gap
    ]
    merged = extend_history_with_events(interactions, events, before_iso="2026-04-17T00:00:00Z")
    gap_entries = [e for e in merged if e.get("type") == "offline_gap_inferred"]
    assert len(gap_entries) == 1
    assert gap_entries[0]["date"] == "2026-04-10"
    assert gap_entries[0]["has_data"] is False
