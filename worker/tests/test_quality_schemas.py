"""Schema-level sanity checks for V4 and next-call plan schemas."""
import pytest
from tasks.quality import QUALITY_JSON_SCHEMA_V3, QUALITY_JSON_SCHEMA_V4

try:
    from tasks.quality import NEXT_CALL_PLAN_SCHEMA
    HAS_PLAN = True
except ImportError:
    HAS_PLAN = False
    NEXT_CALL_PLAN_SCHEMA = None


def _required(schema):
    return set(schema["schema"]["required"])


def test_v4_is_superset_of_v3():
    v3_req = _required(QUALITY_JSON_SCHEMA_V3)
    v4_req = _required(QUALITY_JSON_SCHEMA_V4)
    assert v3_req.issubset(v4_req)


def test_v4_adds_stage_progression():
    v4_props = QUALITY_JSON_SCHEMA_V4["schema"]["properties"]
    assert "stage_progression" in v4_props
    sp = v4_props["stage_progression"]["properties"]
    assert "new_info_learned" in sp
    assert "objections_resolved" in sp
    assert "objections_raised" in sp
    assert "progress_delta" in sp
    assert "stage_advanced" in sp


def test_v4_strict_flag_set():
    assert QUALITY_JSON_SCHEMA_V4["strict"] is True
    assert QUALITY_JSON_SCHEMA_V4["schema"]["additionalProperties"] is False


@pytest.mark.skipif(not HAS_PLAN, reason="NEXT_CALL_PLAN_SCHEMA added in Task 8")
def test_next_call_plan_required_fields():
    req = _required(NEXT_CALL_PLAN_SCHEMA)
    for f in ("goals", "unresolved_objections", "information_gaps",
              "talking_points", "recommended_properties", "risks",
              "suggested_opener"):
        assert f in req
    assert NEXT_CALL_PLAN_SCHEMA["strict"] is True


def test_v4_has_follow_through_field():
    props = QUALITY_JSON_SCHEMA_V4["schema"]["properties"]
    assert "previous_recommendations_follow_through" in props
    ft = props["previous_recommendations_follow_through"]["properties"]
    assert "total_recommendations" in ft
    assert "executed_count" in ft
    assert "items" in ft
    item_props = ft["items"]["items"]["properties"]
    for field in ("recommendation_text", "executed", "evidence", "effectiveness"):
        assert field in item_props


def test_v4_has_meeting_argumentation_assessment():
    props = QUALITY_JSON_SCHEMA_V4["schema"]["properties"]
    assert "meeting_argumentation_assessment" in props
    maa = props["meeting_argumentation_assessment"]["properties"]
    for field in (
        "attempted", "arguments_used", "objections_faced",
        "missed_opportunities", "overall_push_quality",
        "meeting_formats_offered",
    ):
        assert field in maa, f"Missing {field} in meeting_argumentation_assessment"

    # arguments_used items
    au_props = maa["arguments_used"]["items"]["properties"]
    for field in ("category_id", "quote_from_broker", "effectiveness"):
        assert field in au_props

    # missed_opportunities items
    mo_props = maa["missed_opportunities"]["items"]["properties"]
    for field in ("trigger_quote", "recommended_category_id", "why"):
        assert field in mo_props


def test_v4_meeting_assessment_in_required():
    req = _required(QUALITY_JSON_SCHEMA_V4)
    assert "meeting_argumentation_assessment" in req


def test_plan_system_prompt_contains_playbook():
    from tasks.quality import PLAN_SYSTEM_PROMPT
    assert "Библиотека аргументов" in PLAN_SYSTEM_PROMPT
    # At least some known category IDs should be present
    for cid in ("saves_time", "portfolio_presentation", "buying_strategy"):
        assert cid in PLAN_SYSTEM_PROMPT


def test_system_prompt_v4_contains_playbook_and_assessment_rules():
    from tasks.quality import SYSTEM_PROMPT_V4
    assert "meeting_argumentation_assessment" in SYSTEM_PROMPT_V4
    assert "Библиотека аргументов" in SYSTEM_PROMPT_V4
    assert "overall_push_quality" in SYSTEM_PROMPT_V4


def test_v4_follow_through_in_required():
    req = set(QUALITY_JSON_SCHEMA_V4["schema"]["required"])
    assert "previous_recommendations_follow_through" in req
