"""Generic structured-card extraction (config-driven).

If the tenant's company config has a `card_extraction` block ({label, prompt,
json_schema}), run one gpt-5.4 strict structured-output call over the transcript
and return the card dict. Domain-agnostic: the schema/prompt come from config,
NOT from a DB template, and there is NO complex matching. Degrades to None on any
error so it never fails the pipeline. QA scoring is handled separately in quality.py."""
from __future__ import annotations

import json
import logging

from openai import OpenAI

logger = logging.getLogger(__name__)


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
        client = OpenAI()
        resp = client.responses.create(
            model="gpt-5.4",
            input=[{"role": "system", "content": cfg["prompt"]},
                   {"role": "user", "content": "Транскрипт разговора:\n\n" + flat}],
            text={"format": {"type": "json_schema", "name": "card",
                             "strict": True, "schema": cfg["json_schema"]}},
        )
        return json.loads(resp.output_text)
    except Exception:
        logger.exception("card extraction failed; skipping card")
        return None
