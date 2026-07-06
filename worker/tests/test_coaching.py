"""Юниты коучинг-инсайта (F-5): generate_coaching + вайринг в pipeline.

structured_completion капчерим монки-патчем — реальный LLM не зовётся.
"""
import pytest

import tasks.coaching as coaching
import tasks.pipeline as pl
from tenancy.context import reset_tenant_schema, set_tenant_schema


_TRANSCRIPT = [
    {"speaker": "manager", "start": 0.0, "end": 3.0, "text": "Здравствуйте, меня зовут Анна."},
    {"speaker": "client", "start": 3.5, "end": 6.0, "text": "Добрый день."},
    {"speaker": "manager", "start": 12.34, "end": 15.0, "text": "Расскажите про бюджет."},
]

_VALID_OUT = {"headline": "h", "strengths": [], "growth_areas": [], "drill": "d"}


def _patch_capture(monkeypatch):
    """Патчит structured_completion капчером; возвращает список kwargs-вызовов."""
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return dict(_VALID_OUT)

    monkeypatch.setattr(coaching, "structured_completion", fake)
    return calls


# === Параметры LLM-вызова ===

def test_model_default(monkeypatch):
    monkeypatch.delenv("SQA_LLM_MODEL_COACH", raising=False)
    calls = _patch_capture(monkeypatch)
    coaching.generate_coaching(_TRANSCRIPT, {"overall_score": 5})
    assert calls[0]["model"] == "gpt-5.4"


def test_model_override(monkeypatch):
    monkeypatch.setenv("SQA_LLM_MODEL_COACH", "gpt-5.4-mini")
    calls = _patch_capture(monkeypatch)
    coaching.generate_coaching(_TRANSCRIPT, {"overall_score": 5})
    assert calls[0]["model"] == "gpt-5.4-mini"


def test_call_params(monkeypatch):
    calls = _patch_capture(monkeypatch)
    coaching.generate_coaching(_TRANSCRIPT, {"overall_score": 5})
    kw = calls[0]
    assert kw["cache_key"] == "sqa-coach"
    assert kw["max_output_tokens"] == 4_000
    assert kw["schema_name"] == "coaching"


def test_schema_strict_and_nullable(monkeypatch):
    calls = _patch_capture(monkeypatch)
    coaching.generate_coaching(_TRANSCRIPT, {"overall_score": 5})
    schema = calls[0]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"headline", "strengths", "growth_areas", "drill"}

    ga = schema["properties"]["growth_areas"]["items"]
    assert ga["additionalProperties"] is False
    assert set(ga["required"]) == {"moment_quote", "why_it_matters", "better_version", "time"}
    assert ga["properties"]["time"]["type"] == ["number", "null"]

    st = schema["properties"]["strengths"]["items"]
    assert st["additionalProperties"] is False
    assert st["properties"]["quote"]["type"] == ["string", "null"]

    # Обёртка объявляет strict — как NEXT_CALL_PLAN_SCHEMA.
    assert coaching._COACH_SCHEMA["strict"] is True


# === Формирование user-промпта ===

def test_transcript_timecodes(monkeypatch):
    calls = _patch_capture(monkeypatch)
    coaching.generate_coaching(_TRANSCRIPT, {"overall_score": 5})
    user = calls[0]["user"]
    assert "[0.0] manager: Здравствуйте, меня зовут Анна." in user
    assert "[12.3] manager: Расскажите про бюджет." in user


def test_transcript_truncation(monkeypatch):
    calls = _patch_capture(monkeypatch)
    big = [
        {"speaker": "m", "start": float(i), "end": float(i) + 1,
         "text": f"реплика {i} " + "x" * 200}
        for i in range(300)  # ~300 строк × ~215 симв ≈ 64К > 24К
    ]
    big[0]["text"] = "ПЕРВАЯ_СТАРАЯ_РЕПЛИКА " + "y" * 200
    big[-1]["text"] = "ПОСЛЕДНЯЯ_НОВАЯ_РЕПЛИКА " + "z" * 200

    coaching.generate_coaching(big, {"overall_score": 5})
    user = calls[0]["user"]
    assert "(начало усечено)" in user
    assert "ПОСЛЕДНЯЯ_НОВАЯ_РЕПЛИКА" in user      # новейшее сохранено
    assert "ПЕРВАЯ_СТАРАЯ_РЕПЛИКА" not in user     # старейшее срезано
    # Кап уважается (с запасом на пометку усечения).
    assert len(user) < coaching.COACH_MAX_TRANSCRIPT_CHARS + 500


def test_compact_report_filters(monkeypatch):
    calls = _patch_capture(monkeypatch)
    report = {
        "overall_score": 5,
        "criteria": [
            {"name": "СЛАБЫЙ_КРИТЕРИЙ", "score": 3, "comment": "плохо"},
            {"name": "ГРАНИЧНЫЙ_КРИТЕРИЙ", "score": 6, "comment": "средне"},
            {"name": "СИЛЬНЫЙ_КРИТЕРИЙ", "score": 9, "comment": "отлично"},
            {"name": "НЕПРИМЕНИМЫЙ", "score": None, "comment": "N/A"},
        ],
        "objections": [
            {"text": "ОТКРЫТОЕ_ВОЗРАЖЕНИЕ", "broker_response": "ответ", "resolved": False},
            {"text": "ЗАКРЫТОЕ_ВОЗРАЖЕНИЕ", "broker_response": "ответ2", "resolved": True},
        ],
    }
    coaching.generate_coaching(_TRANSCRIPT, report)
    user = calls[0]["user"]
    assert "СЛАБЫЙ_КРИТЕРИЙ" in user
    assert "ГРАНИЧНЫЙ_КРИТЕРИЙ" in user            # score==6 — граница включительно
    assert "СИЛЬНЫЙ_КРИТЕРИЙ" not in user           # score 9 — не проседает
    assert "НЕПРИМЕНИМЫЙ" not in user               # score None — не проседает
    assert "ОТКРЫТОЕ_ВОЗРАЖЕНИЕ" in user
    assert "ЗАКРЫТОЕ_ВОЗРАЖЕНИЕ" not in user        # resolved — не тянем


# === Гейт пустого транскрипта ===

def test_empty_transcript_returns_none(monkeypatch):
    calls = _patch_capture(monkeypatch)
    assert coaching.generate_coaching([], {"overall_score": 5}) is None
    assert calls == []                              # LLM не звался


def test_speechless_transcript_returns_none(monkeypatch):
    calls = _patch_capture(monkeypatch)
    speechless = [{"speaker": "m", "start": 0, "end": 1, "text": "   "}]
    assert coaching.generate_coaching(speechless, {"overall_score": 5}) is None
    assert calls == []


# === Вайринг в pipeline (_analyze_inner) ===

class _StubTask:
    def __init__(self):
        self.request = type("R", (), {"retries": 0})()

    def update_state(self, state=None, meta=None):
        pass

    def retry(self, countdown=None, exc=None, max_retries=None):
        raise AssertionError("unexpected retry")


@pytest.fixture
def tenant_ctx(tmp_path, monkeypatch):
    token = set_tenant_schema("t_acme")
    monkeypatch.setattr(pl, "RESULTS_PATH", str(tmp_path / "results"))
    monkeypatch.setattr(pl, "AUDIO_PATH", str(tmp_path / "audio"))
    monkeypatch.setattr(pl, "update_session_status", lambda sid, st, **kw: None)
    monkeypatch.setattr(pl, "_get_session_metadata", lambda sid: {})
    monkeypatch.setattr(pl, "tenant_company_config_id", lambda: "default")
    monkeypatch.setattr(pl, "load_company_config", lambda cid: {"id": cid})
    monkeypatch.setattr(pl, "get_scenario", lambda cfg, sid: None)
    monkeypatch.setattr(pl, "get_default_scenario_id", lambda cfg: None)
    yield tmp_path
    reset_tenant_schema(token)


def _stub_analyze_deps(monkeypatch, quality_result):
    # Зеркало _stub_analyze_deps из test_pipeline_stages.py (тот файл не трогаем).
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
    monkeypatch.setattr(pl, "assess_quality", lambda *a, **kw: quality_result)
    monkeypatch.setattr(card_mod, "run_card_extraction", lambda *a: None)
    monkeypatch.setattr(card_mod, "has_card_extraction", lambda *a: False)
    monkeypatch.setattr(pl, "tenant_amocrm_enabled", lambda slug: False)
    monkeypatch.setattr(pl, "_get_audio_duration", lambda p: 300.0)
    monkeypatch.setattr(pl, "_get_session_created_at", lambda sid: None)
    monkeypatch.setattr(pl, "_get_session_status", lambda sid: None)


def _write_transcript(sid):
    rd = pl.tenant_results_dir(pl.RESULTS_PATH, "acme", sid)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "transcript.json").write_text(
        '[{"start": 0, "end": 1, "text": "х", "speaker": "S1"}]', encoding="utf-8")
    return rd


def _spy_coaching(monkeypatch):
    import tasks.coaching as coaching_mod
    calls = []

    def fake(transcript, report):
        calls.append((transcript, report))
        return dict(_VALID_OUT)

    monkeypatch.setattr(coaching_mod, "generate_coaching", fake)
    return calls


def test_pipeline_calls_coaching_when_scored(tenant_ctx, monkeypatch):
    _stub_analyze_deps(monkeypatch, {"overall_score": 7})
    rd = _write_transcript("sid-c1")
    calls = _spy_coaching(monkeypatch)

    pl._analyze_session_body(_StubTask(), "sid-c1", "x.wav", None)

    assert len(calls) == 1
    assert (rd / "coaching.json").exists()


def test_pipeline_skips_coaching_when_no_score(tenant_ctx, monkeypatch):
    _stub_analyze_deps(
        monkeypatch,
        {"overall_score": None, "skip_reason": "llm_error", "score_version": 4})
    rd = _write_transcript("sid-c2")
    calls = _spy_coaching(monkeypatch)

    pl._analyze_session_body(_StubTask(), "sid-c2", "x.wav", None)

    assert calls == []
    assert not (rd / "coaching.json").exists()


def test_pipeline_skips_coaching_when_killswitch(tenant_ctx, monkeypatch):
    monkeypatch.setenv("SQA_COACHING", "0")
    _stub_analyze_deps(monkeypatch, {"overall_score": 7})
    rd = _write_transcript("sid-c3")
    calls = _spy_coaching(monkeypatch)

    pl._analyze_session_body(_StubTask(), "sid-c3", "x.wav", None)

    assert calls == []
    assert not (rd / "coaching.json").exists()
