"""Extract structured facts from a transcript using a template's JSON Schema.

Designed to be called from `pipeline.process_session` when the session's template
has kind='extraction'. Saves results/{session_id}/extraction.json on disk and
inserts a row into complex_extractions, then runs match_or_create_complex.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session as DbSession

from llm.egress import structured_completion
from tenancy.context import require_tenant_slug
from tenancy.paths import tenant_results_dir

from tasks.complex_match import match_or_create_complex

logger = logging.getLogger(__name__)

MAX_TRANSCRIPT_TOKENS = 500_000  # safety cap; gpt-5.4 context is 1.05M
RESULTS_PATH = Path(os.getenv("RESULTS_STORAGE_PATH", "./data/results"))

# Потолок выхода extract-вызова (D-1.1)
EXTRACT_MAX_OUTPUT_TOKENS = 6_000


def _approx_tokens(text_str: str) -> int:
    return len(text_str) // 4


def _flatten_transcript(transcript) -> str:
    """Build a plain-text transcript with speakers for the LLM."""
    if isinstance(transcript, list):
        segments = transcript
    elif isinstance(transcript, dict):
        segments = transcript.get("segments") or transcript.get("utterances") or []
    else:
        segments = []
    parts: list[str] = []
    for seg in segments:
        speaker = seg.get("speaker") or "UNKNOWN"
        content = seg.get("text") or seg.get("content") or ""
        if content.strip():
            parts.append(f"[{speaker}] {content.strip()}")
    return "\n".join(parts)


def run_extraction(db: DbSession, session_id, template_id, glossary: str | None = None) -> dict:
    """Run the LLM extraction step. Returns the extracted dict.

    Raises RuntimeError on safety-cap breach, missing transcript, or LLM error —
    caller marks session as failed.
    """
    template = db.execute(text(
        "SELECT id, name, prompt, json_schema FROM extraction_templates WHERE id=:id"
    ), {"id": template_id}).first()
    if not template:
        raise RuntimeError(f"template {template_id} not found")

    transcript_path = tenant_results_dir(RESULTS_PATH, require_tenant_slug(), session_id) / "transcript.json"
    if not transcript_path.exists():
        raise RuntimeError(f"transcript not found at {transcript_path}")
    with transcript_path.open() as f:
        transcript = json.load(f)
    flat = _flatten_transcript(transcript)
    if not flat.strip():
        raise RuntimeError("transcript is empty after flattening")

    if _approx_tokens(flat) > MAX_TRANSCRIPT_TOKENS:
        raise RuntimeError(
            f"transcript exceeds safety cap ({_approx_tokens(flat)} > {MAX_TRANSCRIPT_TOKENS} tokens) — "
            "looks corrupted"
        )

    logger.info("Running extraction with template '%s' for session %s", template.name, session_id)
    user_content = (glossary + "\n\n" if glossary else "") + f"Транскрипт:\n\n{flat}"
    extracted = structured_completion(
        system=template.prompt,
        user=user_content,
        schema=template.json_schema,
        schema_name="extraction",
        max_output_tokens=EXTRACT_MAX_OUTPUT_TOKENS,
        cache_key=f"sqa-extract-{template_id}",
        model=os.getenv("SQA_LLM_MODEL_EXTRACT", "gpt-5.4"),
    )

    out_dir = tenant_results_dir(RESULTS_PATH, require_tenant_slug(), session_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "extraction.json").open("w") as f:
        json.dump(extracted, f, ensure_ascii=False, indent=2)

    extraction_id = uuid.uuid4()
    db.execute(text("""
        INSERT INTO complex_extractions (id, session_id, template_id, raw_data)
        VALUES (:id, :sid, :tid, CAST(:raw AS jsonb))
    """), {
        "id": extraction_id,
        "sid": session_id,
        "tid": template_id,
        "raw": json.dumps(extracted, ensure_ascii=False),
    })

    try:
        match_or_create_complex(db, extraction_id)
    except Exception:
        logger.exception("match_or_create_complex failed for extraction %s", extraction_id)

    db.commit()
    return extracted
