"""Tests for the broken-recording silence-detection gate."""
from unittest.mock import patch

from tasks.pipeline import (
    BROKEN_RECORDING_MIN_DURATION_SEC,
    SILENCE_DB_THRESHOLD,
    _build_broken_recording_report,
    _detect_broken_recording,
)


def test_short_recordings_are_not_flagged():
    """Recordings shorter than the min duration are never broken-flagged
    (they'd be caught by the short-call gate earlier)."""
    result = _detect_broken_recording("/fake", duration=BROKEN_RECORDING_MIN_DURATION_SEC - 1)
    assert result is None


def test_long_audible_recording_is_not_flagged():
    """All probe windows audible (louder than threshold) → not broken."""
    with patch("tasks.pipeline._probe_mean_volume_db", return_value=-25.0):
        result = _detect_broken_recording("/fake", duration=300.0)
    assert result is None


def test_recording_with_silent_tail_is_flagged():
    """Opening loud, tail mostly silent → broken."""
    # probes called 5 times; return -20 for first, -90 for rest
    values = iter([-20.0, -90.0, -90.0, -90.0, -90.0])
    with patch("tasks.pipeline._probe_mean_volume_db", side_effect=lambda *a, **kw: next(values)):
        result = _detect_broken_recording("/fake", duration=4500.0)
    assert result is not None
    assert result["silent_windows"] == 4
    assert result["total_windows"] == 4
    assert result["opening_db"] == -20.0
    assert result["silence_threshold_db"] == SILENCE_DB_THRESHOLD


def test_recording_with_mostly_silent_tail_is_flagged():
    """3 of 4 tail windows silent → broken (boundary case)."""
    values = iter([-20.0, -90.0, -90.0, -90.0, -30.0])
    with patch("tasks.pipeline._probe_mean_volume_db", side_effect=lambda *a, **kw: next(values)):
        result = _detect_broken_recording("/fake", duration=4500.0)
    assert result is not None
    assert result["silent_windows"] == 3


def test_recording_with_only_2_silent_windows_is_not_flagged():
    """2 of 4 tail windows silent → not enough evidence, not flagged."""
    values = iter([-20.0, -90.0, -90.0, -30.0, -30.0])
    with patch("tasks.pipeline._probe_mean_volume_db", side_effect=lambda *a, **kw: next(values)):
        result = _detect_broken_recording("/fake", duration=4500.0)
    assert result is None


def test_quiet_opening_is_not_flagged_even_if_tail_silent():
    """If opening itself is silent (e.g. pure silence recording) — not broken,
    just silent. Might be too_short or autoresponder semantically."""
    values = iter([-90.0, -90.0, -90.0, -90.0, -90.0])
    with patch("tasks.pipeline._probe_mean_volume_db", side_effect=lambda *a, **kw: next(values)):
        result = _detect_broken_recording("/fake", duration=4500.0)
    assert result is None


def test_probe_failure_returns_none_gracefully():
    """If volumedetect can't read the file, we don't flag it — let transcription try."""
    with patch("tasks.pipeline._probe_mean_volume_db", return_value=None):
        result = _detect_broken_recording("/fake", duration=4500.0)
    assert result is None


def test_build_report_shape():
    stats = {"silent_windows": 4, "total_windows": 4, "opening_db": -20.0,
             "tail_db": [-90.0, -90.0, -90.0, -90.0], "silence_threshold_db": -70.0}
    r = _build_broken_recording_report(duration=4682.0, stats=stats)
    assert r["skip_reason"] == "broken_client_recording"
    assert r["overall_score"] is None
    assert r["silence_stats"] == stats
    assert "битая" in r["brief_summary"].lower()
    assert r["score_version"] == "broken_recording_v1"
