"""rewrite_eval_prompt: «пожелания» → gpt-5.4 structured output → {prompt, criteria}.

Pure unit: llm.egress.structured_completion замокан, сети нет.
"""
import pytest


def test_rewrite_builds_request_and_parses(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    captured = {}

    import app.eval_prompt_rewrite as mod

    def fake_sc(**kwargs):
        captured.update(kwargs)
        return {
            "rewritten_prompt": "новый промпт: при оценке слушания требуй ≥2 переспроса",
            "rationale": "учёл пожелание про активное слушание",
            "suggested_criteria": [{"id": "listening", "name": "Слушание", "description": "переспрашивает"}],
        }

    monkeypatch.setattr(mod, "structured_completion", fake_sc)

    out = mod.rewrite_eval_prompt("старый промпт", [{"id": "c1", "name": "Ясность"}],
                                  "хочу строже оценивать активное слушание")

    assert out["rewritten_prompt"].startswith("новый промпт")
    assert out["suggested_criteria"][0]["id"] == "listening"
    # structured-output контракт: strict json_schema через адаптер
    assert captured["schema_name"] == "eval_prompt_rewrite"
    assert captured["schema"] is mod._SCHEMA
    # модель из QUALITY_OPENAI_MODEL; api_key прокинут в адаптер
    assert captured["model"] == mod._MODEL
    assert captured["api_key"] == "sk-test"
    # пожелания и текущий контекст ушли в user-сообщение
    user_msg = captured["user"]
    assert "активное слушание" in user_msg and "старый промпт" in user_msg
    # D-1.1: потолок выхода + кэш-ключ статичного system-промпта
    assert captured["max_output_tokens"] == mod.EVAL_REWRITE_MAX_OUTPUT_TOKENS
    assert captured["cache_key"] == "sqa-eval-rewrite"


def test_rewrite_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from app.eval_prompt_rewrite import rewrite_eval_prompt
    with pytest.raises(RuntimeError):
        rewrite_eval_prompt(None, [], "пожелание")
