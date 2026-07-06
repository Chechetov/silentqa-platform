"""Tests for prior_context building: merge logic and structure."""
import inspect
import json

import tasks.prior_context as pc
from tasks.prior_context import merge_client_profiles, summarise_interaction, build_prior_context_dict
from tasks.prior_context import build_prior_context_for_session, load_past_sessions_for_lead


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


# === D-1.5: cap prior_context (SQL LIMIT + token budget) =========================


def test_sql_query_has_order_by_and_limit():
    """The lead-history SQL must bound its result set: ORDER BY ... DESC LIMIT."""
    src = inspect.getsource(load_past_sessions_for_lead)
    assert "ORDER BY" in src
    assert "LIMIT" in src
    # newest-first slice, then restore chronological order for build_prior_context_dict
    assert "DESC" in src
    assert "revers" in src.lower()
    assert "PRIOR_CONTEXT_MAX_CALLS" in src


def _make_past(n, brief_pad=1500):
    """n past reports, oldest-first, each with a unique MARKER in brief_summary."""
    past = []
    for i in range(n):
        past.append({
            "created_at_iso": f"2026-03-{(i % 28) + 1:02d}T10:00:00Z",
            "duration": 600,
            "direction": "out",
            "report": {
                "brief_summary": f"MARKER{i:03d} " + "x" * brief_pad,
                "client_info": {},
                "objections": [],
            },
            "plan": None,
        })
    return past


def test_token_budget_drops_oldest_keeps_newest(monkeypatch):
    monkeypatch.setenv("PRIOR_CONTEXT_MAX_CHARS", "8000")
    monkeypatch.setenv("PRIOR_CONTEXT_MAX_OBJECTIONS", "10")
    past = _make_past(30)
    monkeypatch.setattr(pc, "load_past_sessions_for_lead", lambda lead_id, before: past)

    ctx = build_prior_context_for_session(lead_id=123, current_created_at="2026-04-01T10:00:00Z")
    assert ctx is not None

    serialized = json.dumps(ctx, ensure_ascii=False)
    assert len(serialized) <= 8000

    joined = " ".join(h.get("brief", "") for h in ctx["interactions_history"])
    # newest interaction survives, oldest is dropped
    assert "MARKER029" in joined
    assert "MARKER000" not in joined
    # something was actually dropped
    assert len(ctx["interactions_history"]) < 30
    # never emptied entirely
    assert len(ctx["interactions_history"]) >= 1


def test_open_objections_capped_to_last_ten(monkeypatch):
    monkeypatch.setenv("PRIOR_CONTEXT_MAX_CHARS", "16000")
    monkeypatch.setenv("PRIOR_CONTEXT_MAX_OBJECTIONS", "10")
    past = []
    for i in range(15):
        past.append({
            "created_at_iso": f"2026-03-{i + 1:02d}T10:00:00Z",
            "duration": 600,
            "direction": "out",
            "report": {
                "brief_summary": f"c{i}",
                "client_info": {},
                "objections": [{"text": f"OBJ{i:03d}", "resolved": False, "category": "x"}],
            },
            "plan": None,
        })
    monkeypatch.setattr(pc, "load_past_sessions_for_lead", lambda lead_id, before: past)

    ctx = build_prior_context_for_session(lead_id=1, current_created_at="2026-04-01T10:00:00Z")
    texts = [o["text"] for o in ctx["open_objections"]]
    assert len(texts) == 10
    assert texts == [f"OBJ{i:03d}" for i in range(5, 15)]  # last 10 kept, oldest 5 dropped


def test_small_context_untouched(monkeypatch):
    monkeypatch.setenv("PRIOR_CONTEXT_MAX_CHARS", "16000")
    monkeypatch.setenv("PRIOR_CONTEXT_MAX_OBJECTIONS", "10")
    past = [
        {"created_at_iso": "2026-03-01T10:00:00Z", "duration": 600, "direction": "out",
         "report": {"brief_summary": "a", "client_info": {},
                    "objections": [{"text": "O1", "resolved": False, "category": "x"}]},
         "plan": None},
        {"created_at_iso": "2026-03-05T10:00:00Z", "duration": 600, "direction": "out",
         "report": {"brief_summary": "b", "client_info": {},
                    "objections": [{"text": "O2", "resolved": False, "category": "x"}]},
         "plan": None},
    ]
    monkeypatch.setattr(pc, "load_past_sessions_for_lead", lambda lead_id, before: past)

    ctx = build_prior_context_for_session(lead_id=1, current_created_at="2026-04-01T10:00:00Z")
    assert len(ctx["interactions_history"]) == 2
    assert len(ctx["open_objections"]) == 2
