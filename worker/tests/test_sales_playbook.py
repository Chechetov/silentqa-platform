"""Unit tests for worker/prompts/sales_playbook.py."""
import re

from prompts.sales_playbook import (
    MEETING_PLAYBOOK,
    MEETING_PLAYBOOK_CATEGORIES,
    get_all_category_ids,
    get_meeting_playbook_prompt,
)


def test_playbook_has_expected_sections():
    assert "primary_approach" in MEETING_PLAYBOOK
    assert "objection_responses" in MEETING_PLAYBOOK
    assert "meeting_formats" in MEETING_PLAYBOOK


def test_primary_approach_categories_have_required_fields():
    for cat in MEETING_PLAYBOOK["primary_approach"]:
        assert cat["id"]
        assert cat["title"]
        assert cat["when_to_use"]
        assert isinstance(cat["examples"], list) and len(cat["examples"]) >= 1


def test_objection_response_categories_have_required_fields():
    for cat in MEETING_PLAYBOOK["objection_responses"]:
        assert cat["id"]
        assert cat["title"]
        assert isinstance(cat["triggers"], list) and len(cat["triggers"]) >= 1
        assert isinstance(cat["examples"], list) and len(cat["examples"]) >= 1


def test_meeting_formats_default_is_zoom():
    fmt = MEETING_PLAYBOOK["meeting_formats"]
    assert fmt["default"] == "zoom"
    assert set(fmt["options"].keys()) == {"zoom", "office", "client"}


def test_category_ids_are_unique():
    all_ids = [c["id"] for c in MEETING_PLAYBOOK["primary_approach"]] + [
        c["id"] for c in MEETING_PLAYBOOK["objection_responses"]
    ]
    assert len(all_ids) == len(set(all_ids)), "Duplicate category IDs in playbook"


def test_category_ids_are_snake_case():
    for cid in MEETING_PLAYBOOK_CATEGORIES:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", cid), f"Bad ID: {cid}"


def test_get_all_category_ids_matches_constant():
    assert get_all_category_ids() == MEETING_PLAYBOOK_CATEGORIES


def test_render_includes_all_categories():
    text = get_meeting_playbook_prompt()
    for cid in MEETING_PLAYBOOK_CATEGORIES:
        assert f"`{cid}`" in text, f"Category {cid} missing from rendered prompt"


def test_render_contains_meeting_formats():
    text = get_meeting_playbook_prompt()
    assert "zoom" in text
    assert "office" in text
    assert "client" in text


def test_render_token_budget_reasonable():
    """Sanity: rendered playbook should stay well under 4k tokens (~16k chars)."""
    text = get_meeting_playbook_prompt()
    assert len(text) < 16_000, f"Playbook too long: {len(text)} chars"
    assert len(text) > 1_000, "Playbook suspiciously short"


def test_render_is_deterministic():
    assert get_meeting_playbook_prompt() == get_meeting_playbook_prompt()


def test_render_respects_max_examples_cap():
    """max_examples_per_category=1 should produce shorter output than =3."""
    short = get_meeting_playbook_prompt(max_examples_per_category=1)
    long = get_meeting_playbook_prompt(max_examples_per_category=3)
    assert len(short) < len(long)
