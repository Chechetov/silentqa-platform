"""Пакет 2 / Task 1: shared-адаптер llm.egress.structured_completion.

Фейк-клиент — идиома FakeResponses/FakeClient из test_llm_call_params.py,
расширенная управляемой очередью транзиентных исключений (ретрай-кейсы).
llm.egress.OpenAI и llm.egress.time.sleep monkeypatch-ятся, задержки капчерятся.
"""
import json
from types import SimpleNamespace

import httpx
import openai
import pytest

import llm.egress as egress
from llm.egress import (  # noqa: F401 — контрактный импорт публичных имён
    LLMBadOutput,
    LLMEgressError,
    LLMTruncated,
    structured_completion,
)

SCHEMA = {
    "type": "object",
    "properties": {"overall_score": {"type": "integer"}},
    "required": ["overall_score"],
    "additionalProperties": False,
}


class FakeResponses:
    """Ловит kwargs; отдаёт очередь транзиентных исключений, потом успех."""

    def __init__(self, output_text="{}", status="completed", errors=()):
        self.output_text = output_text
        self.status = status
        self.errors = list(errors)
        self.kwargs = None
        self.calls = 0

    def create(self, **kwargs):
        self.kwargs = kwargs
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(output_text=self.output_text, status=self.status)


class FakeClient:
    def __init__(self, responses):
        self.responses = responses


def _transient():
    """Простейшее транзиентное исключение SDK (конструктор — только request)."""
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.APITimeoutError(req)


@pytest.fixture
def wire(monkeypatch):
    """Возвращает (install, sleeps): install(fake_responses) -> подменяет OpenAI+sleep."""
    sleeps = []
    monkeypatch.setattr(egress.time, "sleep", lambda d: sleeps.append(d))

    def install(fake_responses):
        monkeypatch.setattr(
            egress, "OpenAI", lambda api_key=None: FakeClient(fake_responses)
        )
        return fake_responses

    return SimpleNamespace(install=install, sleeps=sleeps)


def test_happy_path_kwargs(wire):
    fake = wire.install(FakeResponses(output_text=json.dumps({"overall_score": 7})))
    out = structured_completion(
        system="sys",
        user="usr",
        schema=SCHEMA,
        schema_name="myschema",
        max_output_tokens=1234,
        cache_key="sqa-test",
        model="gpt-x",
    )
    assert out == {"overall_score": 7}
    kw = fake.kwargs
    assert kw["model"] == "gpt-x"
    assert kw["max_output_tokens"] == 1234
    assert kw["prompt_cache_key"] == "sqa-test"
    fmt = kw["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["name"] == "myschema"
    assert fmt["strict"] is True
    assert fmt["schema"] == SCHEMA
    assert kw["input"][0] == {"role": "system", "content": "sys"}
    assert kw["input"][1] == {"role": "user", "content": "usr"}
    assert wire.sleeps == []


def test_two_transient_then_success(wire):
    fake = wire.install(
        FakeResponses(
            output_text=json.dumps({"overall_score": 9}),
            errors=[_transient(), _transient()],
        )
    )
    out = structured_completion(
        system="s",
        user="u",
        schema=SCHEMA,
        schema_name="sc",
        max_output_tokens=10,
        cache_key="k",
        retries=2,
    )
    assert out == {"overall_score": 9}
    assert fake.calls == 3
    assert wire.sleeps == [2.0, 8.0]


def test_transient_exhausted_reraises(wire):
    fake = wire.install(
        FakeResponses(errors=[_transient(), _transient(), _transient()])
    )
    with pytest.raises(openai.APITimeoutError):
        structured_completion(
            system="s",
            user="u",
            schema=SCHEMA,
            schema_name="sc",
            max_output_tokens=10,
            cache_key="k",
            retries=2,
        )
    assert fake.calls == 3
    assert wire.sleeps == [2.0, 8.0]


def test_incomplete_raises_truncated(wire):
    wire.install(FakeResponses(output_text="{}", status="incomplete"))
    with pytest.raises(LLMTruncated):
        structured_completion(
            system="s",
            user="u",
            schema=SCHEMA,
            schema_name="sc",
            max_output_tokens=5,
            cache_key="k",
        )


def test_garbage_output_raises_bad_output(wire):
    wire.install(FakeResponses(output_text="не-json <<<"))
    with pytest.raises(LLMBadOutput):
        structured_completion(
            system="s",
            user="u",
            schema=SCHEMA,
            schema_name="sc",
            max_output_tokens=5,
            cache_key="k",
        )


def test_model_none_uses_env_default(wire, monkeypatch):
    monkeypatch.setenv("SQA_LLM_MODEL_DEFAULT", "my-custom-model")
    fake = wire.install(FakeResponses(output_text=json.dumps({})))
    structured_completion(
        system="s",
        user="u",
        schema=SCHEMA,
        schema_name="sc",
        max_output_tokens=5,
        cache_key="k",
        model=None,
    )
    assert fake.kwargs["model"] == "my-custom-model"


def test_error_hierarchy():
    assert issubclass(LLMTruncated, LLMEgressError)
    assert issubclass(LLMBadOutput, LLMEgressError)
    assert issubclass(LLMEgressError, Exception)
