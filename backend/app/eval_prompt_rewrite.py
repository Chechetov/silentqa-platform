"""LLM-переработка eval-промпта из свободных «пожеланий» (gpt-5.4, structured output).

Вызывается синхронно из роута через run_in_executor (редкое интерактивное действие
админа). Идиома `client.responses.create(... text.format json_schema strict)` — та же,
что в worker/tasks/quality.py и card.py. openai объявлен в backend/requirements.txt.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_SYSTEM = (
    "Ты — эксперт по prompt-engineering для LLM-оценки качества звонков и встреч.\n"
    "На входе: текущий eval-промпт (опционально), текущие критерии оценки (опционально) "
    "и свободные пожелания пользователя.\n"
    "Задача: переписать eval-промпт так, чтобы он интегрировал пожелания в логику оценки — "
    "конкретно и проверяемо (не «будь внимательнее», а, напр., «при оценке слушания убедись, "
    "что менеджер переспросил клиента не менее 2 раз»). Сохрани полезную структуру текущего "
    "промпта. При необходимости предложи обновлённый список критериев (id — латиницей в snake_case).\n"
    "Отвечай строго по схеме, тексты — на русском."
)

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rewritten_prompt", "rationale", "suggested_criteria"],
    "properties": {
        "rewritten_prompt": {"type": "string"},
        "rationale": {"type": "string"},
        "suggested_criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "name", "description"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
    },
}

_MODEL = os.getenv("QUALITY_OPENAI_MODEL", "gpt-5.4")


def rewrite_eval_prompt(current_prompt: str | None,
                        current_criteria: list | None,
                        wishes: str) -> dict:
    """Вернуть {rewritten_prompt, rationale, suggested_criteria[]}. Бросает при отсутствии ключа."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    crit_txt = json.dumps(current_criteria or [], ensure_ascii=False, indent=2)
    user = (
        f"Текущий eval-промпт:\n{current_prompt or '(не задан — построй с нуля)'}\n\n"
        f"Текущие критерии:\n{crit_txt}\n\n"
        f"Пожелания пользователя:\n{wishes}"
    )
    resp = client.responses.create(
        model=_MODEL,
        input=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "eval_prompt_rewrite",
                "strict": True,
                "schema": _SCHEMA,
            }
        },
    )
    return json.loads(resp.output_text)
