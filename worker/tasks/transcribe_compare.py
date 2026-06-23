"""
ASR-сравнение (админ-инструмент тюнинга).

Прогоняет аудио уже записанного звонка несколькими движками транскрибации сразу,
чтобы подобрать лучший под тенант/домен. Варианты складываются рядом с каноном
на диске как ``transcript_<engine>.json`` плюс индекс ``transcript_variants.json``
со статистикой. Канонический ``transcript.json`` и БД НЕ трогаются — это чисто
сравнительные артефакты.

Паттерн thread-pool + per-thread tenant-context повторяет scripts/reassess_quality.py.
"""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tenancy.context import (
    require_tenant_schema,
    require_tenant_slug,
    reset_tenant_schema,
    set_tenant_schema,
)
from tenancy.paths import tenant_audio_sessions_dir, tenant_results_dir

from tasks.celery_app import app
from tasks.company_config import (
    get_word_boost,
    load_company_config,
    tenant_company_config_id,
)
from tasks.transcribe import transcribe_audio

logger = logging.getLogger(__name__)

# Module globals so worker tests can monkeypatch (см. tenancy/paths.py docstring).
AUDIO_PATH = os.getenv("AUDIO_STORAGE_PATH", "./data/audio")
RESULTS_PATH = os.getenv("RESULTS_STORAGE_PATH", "./data/results")

SUPPORTED_ENGINES = ("whisper", "assemblyai", "elevenlabs")


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _variant_stats(segments: list[dict]) -> dict:
    speakers = sorted({s["speaker"] for s in segments if s.get("speaker")})
    return {
        "segments": len(segments),
        "has_speakers": bool(speakers),
        "speakers": speakers,
        "chars": sum(len(s.get("text", "")) for s in segments),
    }


def transcribe_one(session_id: str, engine: str, audio_path: str,
                   word_boost: list[str] | None) -> dict:
    """Прогнать ровно один движок (без fallback) и записать transcript_<engine>.json.

    Best-effort: ошибка движка не пробрасывается — возвращается статус 'error'.
    """
    t0 = time.time()
    try:
        segments = transcribe_audio(
            audio_path, word_boost=word_boost, engine_override=engine, fallback=False
        )
        out = tenant_results_dir(RESULTS_PATH, require_tenant_slug(), session_id) / f"transcript_{engine}.json"
        _save_json(out, segments)
        stats = _variant_stats(segments)
        stats.update(status="ok", elapsed_s=round(time.time() - t0, 1))
        logger.info(f"[{session_id}] ASR compare '{engine}': {stats['segments']} segments "
                    f"in {stats['elapsed_s']}s")
        return stats
    except Exception as e:  # noqa: BLE001 — изолируем сбой одного движка
        logger.warning(f"[{session_id}] ASR compare engine '{engine}' failed: {e}")
        return {"status": "error", "error": str(e), "elapsed_s": round(time.time() - t0, 1),
                "segments": 0, "has_speakers": False, "speakers": [], "chars": 0}


def run_compare(session_id: str, engines: list[str]) -> dict:
    """Ядро сравнения (контекст тенанта должен быть выставлен вызывающим).

    Возвращает индекс {engine: {status, segments, has_speakers, ...}} и сохраняет
    его в transcript_variants.json.
    """
    engines = [e for e in (engines or []) if e in SUPPORTED_ENGINES]
    if not engines:
        raise ValueError("no supported engines requested")

    tenant_schema = require_tenant_schema()
    slug = require_tenant_slug()
    audio_path = str(tenant_audio_sessions_dir(AUDIO_PATH, slug, session_id) / "full.wav")
    if not Path(audio_path).exists():
        raise FileNotFoundError(
            f"merged audio not found for session {session_id}: {audio_path}"
        )

    company_config = load_company_config(tenant_company_config_id())
    word_boost = get_word_boost(company_config)

    def _task(engine: str) -> tuple[str, dict]:
        # ThreadPoolExecutor threads start with a fresh contextvars context —
        # re-set the tenant so path/DB lookups inside transcribe_one stay scoped.
        set_tenant_schema(tenant_schema)
        return engine, transcribe_one(session_id, engine, audio_path, word_boost)

    index: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(3, len(engines))) as pool:
        for fut in as_completed([pool.submit(_task, e) for e in engines]):
            engine, stats = fut.result()
            index[engine] = stats

    _save_json(tenant_results_dir(RESULTS_PATH, slug, session_id) / "transcript_variants.json", index)
    logger.info(f"[{session_id}] ASR compare done: {', '.join(sorted(index))}")
    return index


@app.task(bind=True, queue="transcription", name="pipeline.compare_transcripts")
def compare_transcripts(self, session_id: str, engines: list[str],
                        tenant_schema: str | None = None):
    if not tenant_schema:
        raise ValueError("tenant_schema is required (fail fast: a task without "
                         "tenant context would read/write the wrong schema)")
    token = set_tenant_schema(tenant_schema)
    try:
        return run_compare(session_id, engines)
    finally:
        reset_tenant_schema(token)
