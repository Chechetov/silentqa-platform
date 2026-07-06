"""rewrite_eval_prompt: «пожелания» → gpt-5.4 structured output → {prompt, criteria}.

Pure unit: openai.OpenAI замокан, сети нет.
"""
import json
import sys
import types

import pytest


def _install_fake_openai(monkeypatch, captured):
    fake = types.ModuleType("openai")

    class _Resp:
        output_text = json.dumps({
            "rewritten_prompt": "новый промпт: при оценке слушания требуй ≥2 переспроса",
            "rationale": "учёл пожелание про активное слушание",
            "suggested_criteria": [{"id": "listening", "name": "Слушание", "description": "переспрашивает"}],
        })

    class _Client:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key
            self.responses = self

        def create(self, **kwargs):
            captured["kwargs"] = kwargs
            return _Resp()

    fake.OpenAI = _Client
    monkeypatch.setitem(sys.modules, "openai", fake)


def test_rewrite_builds_request_and_parses(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    captured = {}
    _install_fake_openai(monkeypatch, captured)

    from app.eval_prompt_rewrite import rewrite_eval_prompt
    out = rewrite_eval_prompt("старый промпт", [{"id": "c1", "name": "Ясность"}],
                              "хочу строже оценивать активное слушание")

    assert out["rewritten_prompt"].startswith("новый промпт")
    assert out["suggested_criteria"][0]["id"] == "listening"
    # structured-output контракт: strict json_schema
    fmt = captured["kwargs"]["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert captured["kwargs"]["model"]  # модель задана
    # пожелания и текущий контекст ушли в user-сообщение
    user_msg = captured["kwargs"]["input"][-1]["content"]
    assert "активное слушание" in user_msg and "старый промпт" in user_msg
    # D-1.1: потолок выхода + кэш-ключ статичного system-промпта
    from app.eval_prompt_rewrite import EVAL_REWRITE_MAX_OUTPUT_TOKENS
    assert captured["kwargs"]["max_output_tokens"] == EVAL_REWRITE_MAX_OUTPUT_TOKENS
    assert captured["kwargs"]["prompt_cache_key"] == "sqa-eval-rewrite"


def test_rewrite_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from app.eval_prompt_rewrite import rewrite_eval_prompt
    with pytest.raises(RuntimeError):
        rewrite_eval_prompt(None, [], "пожелание")
