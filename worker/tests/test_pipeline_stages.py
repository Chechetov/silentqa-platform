"""Разрез пайплайна: stage1 → handoff в analysis; гейты завершают сами; ретраи."""
import json

import pytest

import tasks.pipeline as pl
from tenancy.context import reset_tenant_schema, set_tenant_schema


class RetryCalled(Exception):
    def __init__(self, countdown=None):
        self.countdown = countdown


class StubTask:
    def __init__(self, retries=0):
        self.request = type("R", (), {"retries": retries})()

    def update_state(self, state=None, meta=None):
        pass

    def retry(self, countdown=None, exc=None, max_retries=None):
        raise RetryCalled(countdown)


@pytest.fixture
def tenant_ctx(tmp_path, monkeypatch):
    token = set_tenant_schema("t_acme")
    monkeypatch.setattr(pl, "RESULTS_PATH", str(tmp_path / "results"))
    monkeypatch.setattr(pl, "AUDIO_PATH", str(tmp_path / "audio"))
    statuses = []
    monkeypatch.setattr(pl, "update_session_status",
                        lambda sid, st, **kw: statuses.append(st))
    monkeypatch.setattr(pl, "_get_session_metadata", lambda sid: {})
    monkeypatch.setattr(pl, "try_acquire", lambda slug: ("k", "t"))
    monkeypatch.setattr(pl, "release", lambda key, token: None)
    monkeypatch.setattr(pl, "tenant_company_config_id", lambda: "default")
    monkeypatch.setattr(pl, "load_company_config", lambda cid: {"id": cid})
    monkeypatch.setattr(pl, "get_scenario", lambda cfg, sid: None)
    monkeypatch.setattr(pl, "get_default_scenario_id", lambda cfg: None)
    yield statuses
    reset_tenant_schema(token)


def _spy_send_task(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pl.app, "send_task",
        lambda name, kwargs=None, queue=None, **kw: calls.append(
            {"name": name, "kwargs": kwargs, "queue": queue}))
    return calls


def _results_dir(sid):
    return pl.tenant_results_dir(pl.RESULTS_PATH, "acme", sid)


def test_short_call_completes_without_handoff(tenant_ctx, monkeypatch, tmp_path):
    statuses = tenant_ctx
    calls = _spy_send_task(monkeypatch)
    monkeypatch.setattr(pl, "merge_chunks", lambda sid: str(tmp_path / "full.wav"))
    monkeypatch.setattr(pl, "_get_audio_duration", lambda p: 12.0)  # < 20с

    result = pl._process_session_body(StubTask(), "sid-1", None)

    assert result["status"] == "completed"
    assert statuses == ["processing", "completed"]
    assert calls == []                                   # analyze НЕ ставился
    q = json.loads((_results_dir("sid-1") / "quality.json").read_text())
    assert q["skip_reason"] == "too_short"


def test_full_call_hands_off_to_analysis(tenant_ctx, monkeypatch, tmp_path):
    statuses = tenant_ctx
    calls = _spy_send_task(monkeypatch)
    monkeypatch.setattr(pl, "merge_chunks", lambda sid: str(tmp_path / "full.wav"))
    monkeypatch.setattr(pl, "_get_audio_duration", lambda p: 300.0)
    monkeypatch.setattr(pl, "_detect_broken_recording", lambda p, d: None)
    monkeypatch.setattr(
        pl, "_transcribe_and_merge",
        lambda task, sid, path, cfg: [{"start": 0, "end": 1, "text": "х", "speaker": "S1"}])

    result = pl._process_session_body(StubTask(), "sid-2", None)

    assert result["status"] == "analyzing"
    assert statuses == ["processing"]                    # completed поставит analyze
    assert len(calls) == 1
    assert calls[0]["name"] == "pipeline.analyze_session"
    assert calls[0]["queue"] == "analysis"
    assert calls[0]["kwargs"]["tenant_schema"] == "t_acme"
    assert calls[0]["kwargs"]["session_id"] == "sid-2"
    assert (_results_dir("sid-2") / "transcript.json").exists()


def test_no_slot_retries(tenant_ctx, monkeypatch):
    monkeypatch.setattr(pl, "try_acquire", lambda slug: None)
    with pytest.raises(RetryCalled):
        pl._process_session_body(StubTask(), "sid-3", None)
    assert tenant_ctx == []                              # статус не трогали


def _stub_analyze_deps(monkeypatch):
    # Фикстуры ниже используют минимальный односпикерный транскрипт ("х"),
    # который пост-ASR гейт «нет диалога» (D-2) закоротил бы до skip_reason —
    # а эти тесты проверяют именно полный analyze-путь (sentiment→quality).
    # Отключаем гейт (легитимно: тест не про него).
    monkeypatch.setenv("NO_DIALOGUE_GATE", "0")
    import tasks.card as card_mod
    import tasks.knowledge_base as kb
    monkeypatch.setattr(kb, "kb_build_matcher", lambda: None)
    monkeypatch.setattr(kb, "normalize_transcript", lambda t, m: (t, []))
    monkeypatch.setattr(kb, "kb_record_mentions", lambda sid, hits: None)
    monkeypatch.setattr(pl, "_kb_glossary_safe", lambda: "")
    monkeypatch.setattr(pl, "_load_template_kind", lambda t: (None, "evaluation"))
    monkeypatch.setattr(pl, "get_protocol", lambda c: None)
    monkeypatch.setattr(pl, "get_custom_prompt", lambda c: None)
    monkeypatch.setattr(pl, "analyze_sentiment", lambda t: [])
    monkeypatch.setattr(pl, "assess_quality", lambda *a, **kw: {"overall_score": 7})
    monkeypatch.setattr(card_mod, "run_card_extraction", lambda *a: None)
    monkeypatch.setattr(card_mod, "has_card_extraction", lambda *a: False)
    monkeypatch.setattr(pl, "tenant_amocrm_enabled", lambda slug: False)
    monkeypatch.setattr(pl, "_get_audio_duration", lambda p: 300.0)
    monkeypatch.setattr(pl, "_get_session_created_at", lambda sid: None)


def _write_transcript(sid):
    rd = _results_dir(sid)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "transcript.json").write_text(
        '[{"start": 0, "end": 1, "text": "х", "speaker": "S1"}]', encoding="utf-8")
    return rd


def test_analyze_body_completes(tenant_ctx, monkeypatch, tmp_path):
    statuses = tenant_ctx
    rd = _write_transcript("sid-4")
    _stub_analyze_deps(monkeypatch)

    result = pl._analyze_session_body(StubTask(), "sid-4", str(tmp_path / "full.wav"), None)

    assert result["status"] == "completed"
    assert statuses[-1] == "completed"
    assert json.loads((rd / "quality.json").read_text())["overall_score"] == 7


def test_analyze_transient_error_retries(tenant_ctx, monkeypatch, tmp_path):
    statuses = tenant_ctx
    _write_transcript("sid-5")
    _stub_analyze_deps(monkeypatch)

    class APITimeoutError(Exception):
        pass

    def boom(t):
        raise APITimeoutError("connect timeout")

    monkeypatch.setattr(pl, "analyze_sentiment", boom)
    with pytest.raises(RetryCalled):
        pl._analyze_session_body(StubTask(retries=0), "sid-5", str(tmp_path / "f.wav"), None)
    assert "failed" not in statuses                      # ретрай ≠ провал


def test_analyze_deterministic_error_fails(tenant_ctx, monkeypatch, tmp_path):
    statuses = tenant_ctx
    _write_transcript("sid-6")
    _stub_analyze_deps(monkeypatch)

    def boom(t):
        raise ValueError("bad schema")

    monkeypatch.setattr(pl, "analyze_sentiment", boom)
    with pytest.raises(ValueError):
        pl._analyze_session_body(StubTask(), "sid-6", str(tmp_path / "f.wav"), None)
    assert statuses[-1] == "failed"


def test_is_transient_classification():
    class APITimeoutError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    class ValidationError(Exception):
        pass

    assert pl._is_transient(APITimeoutError())
    assert pl._is_transient(RateLimitError())
    assert pl._is_transient(APIConnectionError())
    assert pl._is_transient(TimeoutError())
    assert pl._is_transient(ConnectionError())
    assert not pl._is_transient(ValidationError())
    assert not pl._is_transient(ValueError())


def test_run_pipeline_compat_for_amocrm(tenant_ctx, monkeypatch, tmp_path):
    # amocrm_poll.py:473 зовёт _run_pipeline напрямую и читает
    # quality_report/use_extended — синхронный контракт сохранён, handoff'а нет.
    import contextlib
    calls = _spy_send_task(monkeypatch)
    _stub_analyze_deps(monkeypatch)
    monkeypatch.setattr(pl, "_detect_broken_recording", lambda p, d: None)
    monkeypatch.setattr(pl, "lead_lock", lambda _lid: contextlib.nullcontext())
    monkeypatch.setattr(
        pl, "_transcribe_and_merge",
        lambda task, sid, path, cfg: [{"start": 0, "end": 1, "text": "х", "speaker": "S1"}])

    # сигнатуру вызова сверить с фактической _run_pipeline (планом не меняется)
    result = pl._run_pipeline(StubTask(), "sid-8", str(tmp_path / "f.wav"), {},
                              {"id": "default"}, None, {})

    assert calls == []                                    # БЕЗ handoff
    assert "quality_report" in result and "use_extended" in result
    assert "transcript_with_speakers" in result
