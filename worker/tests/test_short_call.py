"""Tests for the short-call report builder."""
from tasks.pipeline import _build_short_call_report, SHORT_CALL_THRESHOLD_SEC


def test_threshold_is_twenty_seconds():
    assert SHORT_CALL_THRESHOLD_SEC == 20


def test_short_call_report_shape():
    report = _build_short_call_report(12.3)
    assert report["overall_score"] is None
    assert report["skip_reason"] == "too_short"
    assert report["audio_duration"] == 12.3
    assert "12с" in report["brief_summary"]
    assert "автоответчик" in report["brief_summary"].lower()
    assert report["score_version"] == "short_call_v1"


def test_short_call_report_integer_seconds_in_text():
    report = _build_short_call_report(5.9)
    # Text must use integer seconds; 5.9s → "5с"
    assert "5с" in report["brief_summary"]
    assert "6с" not in report["brief_summary"]
