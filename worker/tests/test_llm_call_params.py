"""D-1.1: все LLM-вызовы несут max_output_tokens и cache_key.

quality/plan/card — через адаптер llm.egress.structured_completion (капчер-фейк
ловит kwargs: cache_key, max_output_tokens, model); для extract.py —
source-проверка (живой запуск требует БД и тенант-контекста).
"""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest

import tasks.card as card
import tasks.quality as quality


class FakeResponses:
    def __init__(self, payload):
        self._payload = payload
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(output_text=json.dumps(self._payload), status="completed")


class FakeClient:
    def __init__(self, payload):
        self.responses = FakeResponses(payload)


class CaptureStructured:
    """Замена llm.egress.structured_completion: ловит kwargs, отдаёт payload или кидает exc."""

    def __init__(self, payload=None, exc=None):
        self._payload = {"overall_score": 7} if payload is None else payload
        self._exc = exc
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        if self._exc is not None:
            raise self._exc
        return dict(self._payload)


def test_quality_structured_v2_params(monkeypatch):
    cap = CaptureStructured({"overall_score": 7})
    monkeypatch.setattr(quality, "structured_completion", cap)
    quality._assess_with_structured_output("транскрипт", "{}", None, "", "")
    kw = cap.kwargs
    assert kw["max_output_tokens"] == quality.QUALITY_MAX_OUTPUT_TOKENS
    assert kw["cache_key"] == "sqa-quality-v2"
    assert kw["model"] == "gpt-5.4"


def test_quality_structured_v4_template_cache_key(monkeypatch):
    cap = CaptureStructured({"overall_score": 7})
    monkeypatch.setattr(quality, "structured_completion", cap)
    quality._assess_with_structured_output(
        "транскрипт", "{}", None, "", "",
        use_extended_schema=True, template_driven=True,
    )
    kw = cap.kwargs
    assert kw["max_output_tokens"] == quality.QUALITY_MAX_OUTPUT_TOKENS
    assert kw["cache_key"] == "sqa-quality-v4-tpl"


def test_quality_model_env_override(monkeypatch):
    monkeypatch.setenv("SQA_LLM_MODEL_QUALITY", "custom-q")
    cap = CaptureStructured({"overall_score": 7})
    monkeypatch.setattr(quality, "structured_completion", cap)
    quality._assess_with_structured_output("транскрипт", "{}", None, "", "")
    assert cap.kwargs["model"] == "custom-q"


def test_plan_next_call_params(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    cap = CaptureStructured({"goals": []})
    monkeypatch.setattr(quality, "structured_completion", cap)
    plan = quality.plan_next_call({"calls": []}, {"overall_score": 5})
    assert plan == {"goals": []}
    kw = cap.kwargs
    assert kw["max_output_tokens"] == quality.PLAN_MAX_OUTPUT_TOKENS
    assert kw["cache_key"] == "sqa-plan"
    assert kw["model"] == "gpt-5.4-mini"


def test_plan_model_env_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("SQA_LLM_MODEL_PLAN", "custom-plan")
    cap = CaptureStructured({"goals": []})
    monkeypatch.setattr(quality, "structured_completion", cap)
    quality.plan_next_call({"calls": []}, {"overall_score": 5})
    assert cap.kwargs["model"] == "custom-plan"


def _transient():
    """Транзиентное SDK-исключение (конструктор — только request)."""
    return openai.APITimeoutError(httpx.Request("POST", "https://api.openai.com/v1/responses"))


def _mini_transcript():
    return [{"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0, "text": "привет"}]


def test_assess_quality_bad_output_skips_v4(monkeypatch):
    """Детерминированная LLM-ошибка → честный skip_reason=llm_error (без даунгрейда V4→V2)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        quality, "structured_completion",
        CaptureStructured(exc=quality.LLMBadOutput("невалидный JSON")),
    )
    result = quality.assess_quality(_mini_transcript(), [], use_extended_schema=True)
    assert result["overall_score"] is None
    assert result["skip_reason"] == "llm_error"
    assert result["score_version"] == 4
    assert "невалидный JSON" in result["error"]


def test_assess_quality_bad_output_skips_v2(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        quality, "structured_completion",
        CaptureStructured(exc=quality.LLMBadOutput("bad")),
    )
    result = quality.assess_quality(_mini_transcript(), [])
    assert result["skip_reason"] == "llm_error"
    assert result["score_version"] == 2


def test_assess_quality_transient_propagates(monkeypatch):
    """Транзиентное исключение НЕ глотается — всплывает в task-retry пайплайна."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        quality, "structured_completion", CaptureStructured(exc=_transient()),
    )
    with pytest.raises(openai.APITimeoutError):
        quality.assess_quality(_mini_transcript(), [])


def test_card_extraction_params(monkeypatch):
    monkeypatch.delenv("SQA_LLM_MODEL_CARD", raising=False)
    captured = {}
    monkeypatch.setattr(card, "structured_completion",
                        lambda **kw: captured.update(kw) or {"result": "ok"})
    cfg = {"card_extraction": {"prompt": "p", "json_schema": {"type": "object"}}}
    out = card.run_card_extraction([{"speaker": "A", "text": "привет"}], cfg)
    assert out == {"result": "ok"}
    assert captured["max_output_tokens"] == card.CARD_MAX_OUTPUT_TOKENS
    assert captured["cache_key"] == "sqa-card"
    assert captured["schema_name"] == "card"
    assert captured["schema"] == {"type": "object"}
    assert captured["model"] == "gpt-5.4-mini"  # env-дефолт card/plan


def test_card_model_env_override(monkeypatch):
    captured = {}
    monkeypatch.setattr(card, "structured_completion",
                        lambda **kw: captured.update(kw) or {"result": "ok"})
    monkeypatch.setenv("SQA_LLM_MODEL_CARD", "gpt-5.4")
    cfg = {"card_extraction": {"prompt": "p", "json_schema": {"type": "object"}}}
    card.run_card_extraction([{"speaker": "A", "text": "привет"}], cfg)
    assert captured["model"] == "gpt-5.4"


def test_extract_source_has_params():
    src = (Path(__file__).resolve().parents[1] / "tasks" / "extract.py").read_text()
    assert "structured_completion(" in src
    assert "max_output_tokens=EXTRACT_MAX_OUTPUT_TOKENS" in src
    assert "cache_key=" in src
    assert "SQA_LLM_MODEL_EXTRACT" in src
    assert "OpenAI(" not in src


def test_no_anthropic_in_requirements():
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    assert "anthropic" not in req.lower()
