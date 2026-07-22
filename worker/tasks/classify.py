"""Авто-классификация типа созвона (сценария) по транскрипту.

Если в конфиге тенанта ≥2 сценариев с подсказкой `classify.hint` и тип не задан
явно (запись без выбора), один дешёвый structured-вызов выбирает наиболее
подходящий сценарий по содержанию разговора. Любая ошибка → None: пайплайн
тогда откатывается на default_scenario_id. Явный выбор (рекордер/override/AmoCRM)
классификацию не запускает."""
from __future__ import annotations

import logging
import os

from llm.egress import structured_completion

logger = logging.getLogger(__name__)

# Классификация — короткий ответ (type/confidence/reason), потолок мал.
CLASSIFY_MAX_OUTPUT_TOKENS = 400


def _flatten_transcript(transcript: list[dict]) -> str:
    parts = []
    for seg in transcript or []:
        text = (seg.get("text") or seg.get("content") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def classify_call_type(transcript: list[dict], scenarios: list[dict]) -> dict | None:
    """Выбрать тип созвона среди классифицируемых сценариев.

    `scenarios` — сценарии с `classify.hint`. Возвращает {type, confidence, reason}
    (type — id одного из сценариев) или None (нечего/невозможно классифицировать).
    """
    classifiable = [s for s in (scenarios or []) if (s.get("classify") or {}).get("hint")]
    if len(classifiable) < 2:
        return None
    flat = _flatten_transcript(transcript)
    if not flat.strip():
        return None

    ids = [s["id"] for s in classifiable]
    options = "\n".join(
        f'- {s["id"]}: {s.get("name", s["id"])} — {s["classify"]["hint"]}'
        for s in classifiable
    )
    system = (
        "Ты классифицируешь тип рабочего созвона по его транскрипту. "
        "Выбери РОВНО один тип из списка, наиболее подходящий по содержанию разговора. "
        "Отвечай строго по схеме.\n\nТипы:\n" + options
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "confidence", "reason"],
        "properties": {
            "type": {"type": "string", "enum": ids},
            "confidence": {"type": "number"},
            "reason": {"type": ["string", "null"]},
        },
    }
    try:
        res = structured_completion(
            system=system,
            user="Транскрипт разговора:\n\n" + flat,
            schema=schema,
            schema_name="call_type",
            max_output_tokens=CLASSIFY_MAX_OUTPUT_TOKENS,
            cache_key="sqa-classify",
            model=os.getenv("SQA_LLM_MODEL_CLASSIFY", os.getenv("SQA_LLM_MODEL_CARD", "gpt-5.4-mini")),
        )
        if res and res.get("type") in ids:
            return res
        logger.warning("classify_call_type: пустой/невалидный ответ (%s)", res)
        return None
    except Exception:
        logger.exception("call-type classification failed; fallback to default scenario")
        return None
