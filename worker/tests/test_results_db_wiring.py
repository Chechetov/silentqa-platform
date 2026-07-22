"""Пайплайн зовёт record_quality_result: analyze-хвост и оба гейта."""
from unittest.mock import MagicMock

import tasks.pipeline as pipeline


def test_run_gates_short_call_records_skip(monkeypatch):
    calls = {}
    monkeypatch.setattr(pipeline, "record_quality_result",
                        lambda sid, report, **kw: calls.update(sid=sid, kw=kw) or [])
    monkeypatch.setattr(pipeline, "_get_audio_duration", lambda p: 5.0)
    monkeypatch.setattr(pipeline, "save_results", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_push_to_amocrm", lambda *a, **k: None)
    task = MagicMock()
    res = pipeline._run_gates(task, "sid-1", "/tmp/a.wav", {}, {}, None, {})
    assert res is not None
    assert calls["sid"] == "sid-1"
    assert calls["kw"]["skip_reason"] == "too_short"
    assert calls["kw"]["duration_seconds"] == 5.0


def test_run_gates_broken_recording_records_skip(monkeypatch):
    calls = {}
    monkeypatch.setattr(pipeline, "record_quality_result",
                        lambda sid, report, **kw: calls.update(kw=kw) or [])
    monkeypatch.setattr(pipeline, "_get_audio_duration", lambda p: 300.0)
    monkeypatch.setattr(pipeline, "_detect_broken_recording",
                        lambda p, d: {"silent_windows": 3, "total_windows": 4,
                                      "opening_db": -20.0, "tail_db": [-80.0]})
    monkeypatch.setattr(pipeline, "save_results", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_push_to_amocrm", lambda *a, **k: None)
    res = pipeline._run_gates(MagicMock(), "sid-2", "/tmp/a.wav", {}, {}, None, {})
    assert res is not None
    assert calls["kw"]["skip_reason"] == "broken_recording"


def test_run_gates_passed_no_record(monkeypatch):
    called = []
    monkeypatch.setattr(pipeline, "record_quality_result",
                        lambda *a, **k: called.append(1) or [])
    monkeypatch.setattr(pipeline, "_get_audio_duration", lambda p: 300.0)
    monkeypatch.setattr(pipeline, "_detect_broken_recording", lambda p, d: None)
    assert pipeline._run_gates(MagicMock(), "sid-3", "/tmp/a.wav", {}, {}, None, {}) is None
    assert not called


def test_analyze_inner_source_calls_record():
    import inspect
    src = inspect.getsource(pipeline._analyze_inner)
    assert "record_quality_result(" in src
    for kwarg in ("card=", "transcript=", "sentiment_results=", "company_config="):
        assert kwarg in src, f"_analyze_inner должен передавать {kwarg}"
