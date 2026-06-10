"""Tests for incrementally merging an extraction into the aggregated profile.

Rules (per spec):
- Scalar: latest non-null wins (newest extraction first in input list).
- List of strings: union + dedup (case-insensitive after normalize).
- Dict: recursive.
- additional_info[]: each element wrapped with {note, session_id, recorded_at} — never deduped.
"""
from datetime import datetime, timezone

import pytest

from tasks.complex_match import merge_extractions


def _ext(raw, when, session_id="sess1"):
    return {"raw_data": raw, "created_at": when, "session_id": session_id}


def test_merge_single_extraction_returns_same_data_with_attribution():
    ext = _ext({"name": "Шагал", "additional_info": ["новая школа рядом"]},
               datetime(2026, 4, 1, tzinfo=timezone.utc), "s1")
    agg = merge_extractions([ext])
    assert agg["name"] == "Шагал"
    assert agg["additional_info"] == [
        {"note": "новая школа рядом", "session_id": "s1",
         "recorded_at": "2026-04-01T00:00:00+00:00"}
    ]


def test_scalar_latest_wins():
    new = _ext({"name": "Шагал-2"}, datetime(2026, 4, 10, tzinfo=timezone.utc), "s2")
    old = _ext({"name": "Шагал"}, datetime(2026, 4, 1, tzinfo=timezone.utc), "s1")
    agg = merge_extractions([new, old])  # newer first
    assert agg["name"] == "Шагал-2"


def test_scalar_falls_back_when_newest_is_null():
    new = _ext({"name": None, "developer": "Эталон"}, datetime(2026, 4, 10, tzinfo=timezone.utc))
    old = _ext({"name": "Шагал"}, datetime(2026, 4, 1, tzinfo=timezone.utc))
    agg = merge_extractions([new, old])
    assert agg["name"] == "Шагал"
    assert agg["developer"] == "Эталон"


def test_list_union_dedup_case_insensitive():
    e1 = _ext({"location": {"parks": ["Парк Победы", "Воробьёвы горы"]}},
              datetime(2026, 4, 1, tzinfo=timezone.utc))
    e2 = _ext({"location": {"parks": ["парк победы", "Парк Горького"]}},
              datetime(2026, 4, 5, tzinfo=timezone.utc))
    agg = merge_extractions([e2, e1])  # newer first
    parks = agg["location"]["parks"]
    assert len(parks) == 3
    assert "Парк Победы" in parks  # originals preserved
    assert "Воробьёвы горы" in parks
    assert "Парк Горького" in parks


def test_nested_dicts_merge_recursively():
    e1 = _ext({"pricing": {"cash": "от 25 млн"}}, datetime(2026, 4, 1, tzinfo=timezone.utc))
    e2 = _ext({"pricing": {"mortgage": "5% на 30 лет"}}, datetime(2026, 4, 5, tzinfo=timezone.utc))
    agg = merge_extractions([e2, e1])
    assert agg["pricing"]["cash"] == "от 25 млн"
    assert agg["pricing"]["mortgage"] == "5% на 30 лет"


def test_additional_info_collected_with_attribution_no_dedup():
    e1 = _ext({"additional_info": ["скидка 5% по промо", "офис продаж в ТЦ"]},
              datetime(2026, 4, 1, tzinfo=timezone.utc), "s1")
    e2 = _ext({"additional_info": ["скидка 5% по промо"]},  # same string, kept twice
              datetime(2026, 4, 5, tzinfo=timezone.utc), "s2")
    agg = merge_extractions([e2, e1])
    notes = agg["additional_info"]
    assert len(notes) == 3
    assert {n["session_id"] for n in notes} == {"s1", "s2"}
    # chronological order: oldest extraction's notes appear first
    assert [n["session_id"] for n in notes] == ["s1", "s1", "s2"]


def test_empty_input_returns_empty_dict():
    assert merge_extractions([]) == {}


def test_empty_strings_treated_as_null_for_scalars():
    new = _ext({"name": "", "developer": "Эталон"}, datetime(2026, 4, 10, tzinfo=timezone.utc))
    old = _ext({"name": "Шагал"}, datetime(2026, 4, 1, tzinfo=timezone.utc))
    agg = merge_extractions([new, old])
    assert agg["name"] == "Шагал"
