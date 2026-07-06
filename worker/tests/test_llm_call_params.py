"""D-1.1: все LLM-вызовы несут max_output_tokens и prompt_cache_key.

Функциональная проверка quality/plan/card (фейк-клиент ловит kwargs);
для extract.py — source-проверка (живой запуск требует БД и тенант-контекста).
"""
import json
from pathlib import Path
from types import SimpleNamespace

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


def test_quality_structured_v2_params():
    fake = FakeClient({"overall_score": 7})
    quality._assess_with_structured_output(fake, "транскрипт", "{}", None, "", "")
    kw = fake.responses.kwargs
    assert kw["max_output_tokens"] == quality.QUALITY_MAX_OUTPUT_TOKENS
    assert kw["prompt_cache_key"] == "sqa-quality-v2"


def test_quality_structured_v4_template_cache_key():
    fake = FakeClient({"overall_score": 7})
    quality._assess_with_structured_output(
        fake, "транскрипт", "{}", None, "", "",
        use_extended_schema=True, template_driven=True,
    )
    kw = fake.responses.kwargs
    assert kw["max_output_tokens"] == quality.QUALITY_MAX_OUTPUT_TOKENS
    assert kw["prompt_cache_key"] == "sqa-quality-v4-tpl"


def test_plan_next_call_params(monkeypatch):
    fake = FakeClient({"goals": []})
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(quality, "OpenAI", lambda api_key=None: fake)
    plan = quality.plan_next_call({"calls": []}, {"overall_score": 5})
    assert plan == {"goals": []}
    kw = fake.responses.kwargs
    assert kw["max_output_tokens"] == quality.PLAN_MAX_OUTPUT_TOKENS
    assert kw["prompt_cache_key"] == "sqa-plan"


def test_card_extraction_params(monkeypatch):
    fake = FakeClient({"result": "ok"})
    monkeypatch.setattr(card, "OpenAI", lambda: fake)
    cfg = {"card_extraction": {"prompt": "p", "json_schema": {"type": "object"}}}
    out = card.run_card_extraction([{"speaker": "A", "text": "привет"}], cfg)
    assert out == {"result": "ok"}
    kw = fake.responses.kwargs
    assert kw["max_output_tokens"] == card.CARD_MAX_OUTPUT_TOKENS
    assert kw["prompt_cache_key"] == "sqa-card"


def test_extract_source_has_params():
    src = (Path(__file__).resolve().parents[1] / "tasks" / "extract.py").read_text()
    assert "max_output_tokens=EXTRACT_MAX_OUTPUT_TOKENS" in src
    assert "prompt_cache_key" in src


def test_no_anthropic_in_requirements():
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    assert "anthropic" not in req.lower()
