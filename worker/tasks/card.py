"""Generic structured-card extraction (config-driven).

If the tenant's company config has a `card_extraction` block ({label, prompt,
json_schema}), run one strict structured-output call (llm.egress adapter; model from
SQA_LLM_MODEL_CARD, default gpt-5.4-mini) over the transcript and return the card dict.
Domain-agnostic: the schema/prompt come from config,
NOT from a DB template, and there is NO complex matching. Degrades to None on any
error so it never fails the pipeline. QA scoring is handled separately in quality.py."""
from __future__ import annotations

import logging
import os

from llm.egress import structured_completion

logger = logging.getLogger(__name__)

# Потолок выхода card-вызова (D-1.1)
CARD_MAX_OUTPUT_TOKENS = 6_000


def _flatten_transcript(transcript: list[dict]) -> str:
    parts = []
    for seg in transcript or []:
        speaker = seg.get("speaker") or "UNKNOWN"
        text = (seg.get("text") or seg.get("content") or "").strip()
        if text:
            parts.append(f"[{speaker}] {text}")
    return "\n".join(parts)


def _resolve_card_cfg(company_config: dict, scenario: dict | None) -> dict | None:
    """Какой card_extraction применять. Ключ у сценария (в т.ч. явный null/{} —
    отказ от карты) ИМЕЕТ ПРИОРИТЕТ; иначе — config-level."""
    if scenario and "card_extraction" in scenario:
        return scenario["card_extraction"]
    return company_config.get("card_extraction")


def has_card_extraction(company_config: dict, scenario: dict | None = None) -> bool:
    """Сконфигурирована ли карта для этого прогона (scenario-override или config-level).

    Нужно пайплайну, чтобы отличить «карта не нужна» (→ убрать устаревшую card.json)
    от «карта нужна, но извлечение упало» (→ старую card.json НЕ трогать)."""
    return bool(_resolve_card_cfg(company_config, scenario))


def run_card_extraction(transcript: list[dict], company_config: dict,
                        scenario: dict | None = None) -> dict | None:
    cfg = _resolve_card_cfg(company_config, scenario)
    if not cfg:
        return None
    flat = _flatten_transcript(transcript)
    if not flat.strip():
        return None
    try:
        return structured_completion(
            system=cfg["prompt"],
            user="Транскрипт разговора:\n\n" + flat,
            schema=cfg["json_schema"],
            schema_name="card",
            max_output_tokens=CARD_MAX_OUTPUT_TOKENS,
            cache_key="sqa-card",
            model=os.getenv("SQA_LLM_MODEL_CARD", "gpt-5.4-mini"),
        )
    except Exception:
        logger.exception("card extraction failed; skipping card")
        return None
