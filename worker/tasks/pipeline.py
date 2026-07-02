"""
Полный пайплайн обработки аудио:
1. Склейка чанков → один WAV файл
2. Транскрипция (faster-whisper)
3. Дiarизация (pyannote)
4. Объединение транскрипта со спикерами
5. Sentiment analysis (rubert)
6. Оценка качества (Claude LLM)
7. Сохранение результатов
"""
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from tenancy.context import get_tenant_schema, require_tenant_slug, reset_tenant_schema, set_tenant_schema
from tenancy.db import (
    get_sync_db_url,
    get_sync_dialect_url,
    tenant_connect,
    tenant_engine,
)
from tenancy.paths import tenant_audio_sessions_dir, tenant_results_dir
from tenancy.registry import tenant_amocrm_enabled

from tasks.celery_app import app
from tasks.transcribe import transcribe_audio
from tasks.diarize import diarize_audio
from tasks.sentiment import analyze_sentiment
from tasks.quality import assess_quality, plan_next_call
from tasks.prior_context import build_prior_context_for_session
from tasks.company_config import load_company_config, get_word_boost, get_protocol, get_custom_prompt, get_asr_engine, get_scenario, get_default_scenario_id, tenant_company_config_id
from tasks.amocrm_sync import find_lead_by_phone, create_enriched_note, update_note, format_enriched_note, tag_lead, format_next_call_plan, create_plain_note
from tasks.deal_summary import build_deal_summary, push_deal_summary
from tasks.lead_lock import lead_lock
from tasks.tenant_slots import try_acquire, release

logger = logging.getLogger(__name__)

SHORT_CALL_THRESHOLD_SEC = 20

# Нет свободного слота тенанта → retry с этим countdown (слот воркера
# освобождается для других тенантов).
TENANT_SLOT_RETRY_SEC = int(os.getenv("TENANT_SLOT_RETRY_SEC", "60"))

# Транзиентные ошибки (сеть/лимиты OpenAI, AmoCRM, httpx) — кандидаты на
# retry analyze-стадии. Матчим по имени класса, чтобы не тащить импорты
# всех клиентских SDK.
_TRANSIENT_MARKERS = ("Timeout", "Connection", "RateLimit",
                      "ServiceUnavailable", "InternalServerError", "TryAgain")


def _is_transient(exc: BaseException) -> bool:
    name = type(exc).__name__
    return any(m in name for m in _TRANSIENT_MARKERS)

# Silence-dominance gate. Protects against broken desktop-app recordings:
# when the mic stream dies mid-call (e.g. user toggled headphones) the
# MediaRecorder keeps emitting empty timeslices, producing a long WAV
# that is audible for the first few seconds and digital silence afterwards.
BROKEN_RECORDING_MIN_DURATION_SEC = 60
SILENCE_DB_THRESHOLD = -70.0  # below this a 10-sec window counts as silent


def _build_short_call_report(duration: float) -> dict:
    """Minimal quality report for calls too short for meaningful evaluation."""
    secs = int(duration)
    return {
        "overall_score": None,
        "skip_reason": "too_short",
        "audio_duration": round(duration, 1),
        "brief_summary": (
            f"⚠️ Короткий звонок ({secs}с). Оценка не проводилась — "
            f"вероятно, автоответчик или клиент не ответил."
        ),
        "summary": f"Короткий звонок ({secs}с), оценка пропущена.",
        "score_version": "short_call_v1",
    }


def _build_broken_recording_report(duration: float, stats: dict) -> dict:
    """Quality report for recordings where audio died after the start."""
    secs = int(duration)
    return {
        "overall_score": None,
        "skip_reason": "broken_client_recording",
        "audio_duration": round(duration, 1),
        "silence_stats": stats,
        "brief_summary": (
            f"⚠️ Битая запись ({secs}с): после начала разговора микрофон перестал "
            f"давать звук. Типичная причина — переключение аудио-устройства "
            f"(подключили/отключили наушники, Bluetooth-переключение) во время "
            f"записи. Оценка не проводилась — аудио по факту пустое."
        ),
        "summary": (
            f"Битая запись ({secs}с): {stats.get('silent_windows')}/{stats.get('total_windows')} "
            f"окон тишина после начала. Начало {stats.get('opening_db')} dB, "
            f"хвост {stats.get('tail_db')} dB."
        ),
        "score_version": "broken_recording_v1",
    }


def _probe_mean_volume_db(audio_path: str, start: float, length: float = 10.0) -> float | None:
    """Return mean volume in dB over a [start, start+length] window via ffmpeg volumedetect."""
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats",
                "-ss", str(start), "-t", str(length),
                "-i", audio_path,
                "-af", "volumedetect", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        logger.exception(f"volumedetect probe failed at {start}s for {audio_path}")
        return None
    for line in (proc.stderr or "").splitlines():
        if "mean_volume" in line:
            try:
                return float(line.split("mean_volume:")[1].strip().split()[0])
            except Exception:
                return None
    return None


def _detect_broken_recording(audio_path: str, duration: float) -> dict | None:
    """Detect recordings that went silent shortly after the start.

    Samples 5 ten-second windows across the timeline (0%, 25%, 50%, 75%, 90%).
    If the opening window has audible speech (> -70 dB) but at least 3 of the
    4 later windows are below -70 dB, the recording is considered broken.
    Returns stats dict if broken, otherwise None.
    """
    if duration < BROKEN_RECORDING_MIN_DURATION_SEC:
        return None
    fractions = [0.0, 0.25, 0.5, 0.75, 0.9]
    probes: list[tuple[float, float | None]] = []
    for f in fractions:
        start = max(0.0, duration * f - 5.0)
        db = _probe_mean_volume_db(audio_path, start=start)
        probes.append((start, db))
    opening = probes[0][1]
    tail_db = [db for _, db in probes[1:] if db is not None]
    if opening is None or not tail_db:
        return None
    silent_tail = sum(1 for db in tail_db if db < SILENCE_DB_THRESHOLD)
    if opening > SILENCE_DB_THRESHOLD and silent_tail >= 3:
        return {
            "silent_windows": silent_tail,
            "total_windows": len(tail_db),
            "opening_db": round(opening, 1),
            "tail_db": [round(db, 1) for db in tail_db],
            "silence_threshold_db": SILENCE_DB_THRESHOLD,
        }
    return None


_get_sync_db_url = get_sync_db_url


def update_session_status(session_id: str, status: str, **kwargs):
    """Update session status directly in PostgreSQL via psycopg2."""
    if not get_sync_db_url():
        logger.warning("DATABASE_URL not set, cannot update session status")
        return

    sets = ["status = %s"]
    values = [status]

    # Метка фактического старта стадии 1 — опора watchdog-правил 3a/3b
    # (НЕ created_at). Ставим только при переходе в processing; на прочих
    # статусах колонку не трогаем. Бэкенд сбрасывает её в NULL при постановке.
    if status == "processing":
        sets.append("processing_started_at = NOW()")

    for key, value in kwargs.items():
        sets.append(f"{key} = %s")
        values.append(value)

    values.append(session_id)
    query = f"UPDATE sessions SET {', '.join(sets)} WHERE id = %s"

    try:
        conn = tenant_connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(query, values)
        conn.close()
    except Exception:
        logger.exception(f"Failed to update session {session_id} status to {status}")

def _get_audio_duration(wav_path: str) -> float:
    """Get real audio duration via ffprobe (not processing time)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", wav_path],
            capture_output=True, text=True,
        )
        return float(result.stdout.strip()) if result.stdout.strip() else 0.0
    except Exception:
        logger.warning(f"Failed to get audio duration for {wav_path}")
        return 0.0


AUDIO_PATH = os.getenv("AUDIO_STORAGE_PATH", "./data/audio")
RESULTS_PATH = os.getenv("RESULTS_STORAGE_PATH", "./data/results")


def _get_session_metadata(session_id: str) -> dict:
    """Read session metadata from DB."""
    if not get_sync_db_url():
        return {}
    try:
        conn = tenant_connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT metadata FROM sessions WHERE id = %s", (session_id,))
                row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return row[0] if isinstance(row[0], dict) else json.loads(row[0])
    except Exception:
        logger.exception(f"Failed to read metadata for session {session_id}")
    return {}


def _load_template_kind(template_id: str | None) -> tuple[str | None, str]:
    """Resolve (template_id, kind) for the given metadata template_id.

    Returns ('', 'evaluation') when no template is set, when the id is malformed,
    or when the row doesn't exist — keeps the legacy evaluation path as the safe default.
    """
    if not template_id:
        return None, "evaluation"
    if not get_sync_dialect_url():
        return None, "evaluation"
    from sqlalchemy import text as _text
    eng = tenant_engine()
    try:
        try:
            with eng.connect() as conn:
                row = conn.execute(_text("SELECT id, kind FROM extraction_templates WHERE id=:id"),
                                   {"id": template_id}).first()
        except Exception:
            return None, "evaluation"
        if not row:
            return None, "evaluation"
        return str(row[0]), row[1]
    finally:
        eng.dispose()


def _load_evaluation_template_prompt(template_id: str) -> str | None:
    """Return the prompt of an evaluation template, or None on error."""
    if not get_sync_dialect_url():
        return None
    from sqlalchemy import text as _text
    eng = tenant_engine()
    try:
        with eng.connect() as conn:
            row = conn.execute(_text("SELECT prompt FROM extraction_templates WHERE id=:id"),
                               {"id": template_id}).first()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        eng.dispose()


def _load_evaluation_template_criteria(template_id: str) -> list[dict] | None:
    """Return the criteria list of an evaluation template, or None on error / NULL column."""
    if not get_sync_dialect_url():
        return None
    from sqlalchemy import text as _text
    eng = tenant_engine()
    try:
        with eng.connect() as conn:
            row = conn.execute(_text("SELECT criteria FROM extraction_templates WHERE id=:id"),
                               {"id": template_id}).first()
        if not row or not row[0]:
            return None
        return row[0] if isinstance(row[0], list) else None
    except Exception:
        return None
    finally:
        eng.dispose()


def _get_session_created_at(session_id: str):
    """Return created_at (datetime or None) for a session."""
    if not get_sync_db_url():
        return None
    try:
        conn = tenant_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT created_at FROM sessions WHERE id = %s", (session_id,))
                row = cur.fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:
        logger.exception(f"Failed to read created_at for session {session_id}")
        return None


def _merge_kb_keyterms(word_boost: list[str]) -> list[str]:
    """Union config word_boost with KB feeds_asr keyterms; dedup; survive KB errors."""
    try:
        from tasks.knowledge_base import cap_keyterms, kb_keyterms
        return cap_keyterms(list(word_boost or []) + kb_keyterms())
    except Exception:
        logger.exception("kb keyterms merge failed; using config word_boost only")
        return list(word_boost or [])


def _kb_glossary_safe() -> str:
    try:
        from tasks.knowledge_base import kb_glossary
        return kb_glossary()
    except Exception:
        logger.exception("kb glossary failed; using empty")
        return ""


def merge_chunks(session_id: str) -> str:
    """Склеивает WebM чанки в один WAV 16kHz mono (формат для Whisper).

    MediaRecorder с timeslice выдаёт чанки, которые являются частями
    одного непрерывного WebM-потока: заголовок только в первом чанке,
    остальные — продолжение без EBML header. Поэтому сначала склеиваем
    байты в один .webm, затем конвертируем в WAV через ffmpeg.
    """
    session_dir = tenant_audio_sessions_dir(AUDIO_PATH, require_tenant_slug(), session_id)
    output_path = session_dir / "full.wav"

    if output_path.exists():
        return str(output_path)

    # Находим все чанки, сортируем по номеру (числовая сортировка устойчива к
    # смене ширины паддинга: chunk_001.webm и chunk_000123.webm перемешаются
    # правильно, лексикографическая бы это сломала).
    def _chunk_num(p: Path) -> int:
        stem = p.stem
        digits = stem.rsplit("_", 1)[-1]
        try:
            return int(digits)
        except ValueError:
            return 0

    chunks = sorted(session_dir.glob("chunk_*.webm"), key=_chunk_num)
    if not chunks:
        raise FileNotFoundError(f"No chunks found for session {session_id}")

    # Склеиваем байты в один WebM (чанки — части одного потока)
    combined_webm = session_dir / "combined.webm"
    with open(combined_webm, "wb") as out:
        for chunk in chunks:
            out.write(chunk.read_bytes())

    # Сохраняем список чанков для справки
    list_file = session_dir / "chunks.txt"
    with open(list_file, "w") as f:
        for chunk in chunks:
            f.write(f"file '{chunk.name}'\n")

    # ffmpeg: WebM → WAV 16kHz mono
    cmd = [
        "ffmpeg", "-y",
        "-i", str(combined_webm),
        "-ar", "16000",      # 16kHz — стандарт для Whisper
        "-ac", "1",           # Mono
        "-c:a", "pcm_s16le",  # 16-bit PCM
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"ffmpeg error: {result.stderr}")
        raise RuntimeError(f"ffmpeg merge failed: {result.stderr}")

    logger.info(f"Merged {len(chunks)} chunks → {output_path} ({output_path.stat().st_size / 1024 / 1024:.1f} MB)")
    return str(output_path)


def merge_transcript_with_speakers(transcript: list, diarization: list) -> list:
    """
    Объединяет транскрипт (с таймстемпами) с дiarизацией (кто говорит когда).
    Каждому сегменту транскрипта присваивается спикер на основе
    максимального пересечения по времени.
    """
    for segment in transcript:
        seg_start = segment["start"]
        seg_end = segment["end"]
        seg_mid = (seg_start + seg_end) / 2

        # Находим спикера, в чьём интервале находится середина сегмента
        best_speaker = "UNKNOWN"
        for diar in diarization:
            if diar["start"] <= seg_mid <= diar["end"]:
                best_speaker = diar["speaker"]
                break

        segment["speaker"] = best_speaker

    return transcript


def save_results(session_id: str, key: str, data: dict | list):
    """Сохраняет результат обработки в JSON."""
    results_dir = tenant_results_dir(RESULTS_PATH, require_tenant_slug(), session_id)
    results_dir.mkdir(parents=True, exist_ok=True)
    output_file = results_dir / f"{key}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved {key} → {output_file}")


def delete_results(session_id: str, key: str) -> None:
    """Удалить ранее сохранённый результат (если есть). Идемпотентно."""
    results_dir = tenant_results_dir(RESULTS_PATH, require_tenant_slug(), session_id)
    (results_dir / f"{key}.json").unlink(missing_ok=True)


def _save_speaker_roles(session_id: str, speaker_roles: dict):
    """Save auto-detected speaker roles to session metadata (if no manual override exists)."""
    if not get_sync_db_url():
        return
    try:
        conn = tenant_connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT metadata FROM sessions WHERE id = %s", (session_id,))
                row = cur.fetchone()
                meta = (row[0] if isinstance(row[0], dict) else json.loads(row[0])) if row and row[0] else {}
                if "speaker_map" not in meta:
                    meta["speaker_map"] = speaker_roles
                    cur.execute("UPDATE sessions SET metadata = %s WHERE id = %s",
                                (json.dumps(meta, ensure_ascii=False), session_id))
        conn.close()
        logger.info(f"[{session_id}] Auto-saved speaker roles: {list(speaker_roles.keys())}")
    except Exception:
        logger.exception(f"[{session_id}] Failed to save speaker roles")


def _update_session_metadata(session_id: str, updates: dict):
    """Merge updates into session metadata."""
    if not get_sync_db_url():
        return
    try:
        conn = tenant_connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT metadata FROM sessions WHERE id = %s", (session_id,))
                row = cur.fetchone()
                meta = (row[0] if isinstance(row[0], dict) else json.loads(row[0])) if row and row[0] else {}
                meta.update(updates)
                cur.execute("UPDATE sessions SET metadata = %s WHERE id = %s",
                            (json.dumps(meta, ensure_ascii=False), session_id))
        conn.close()
        logger.info(f"[{session_id}] Updated metadata: {list(updates.keys())}")
    except Exception:
        logger.exception(f"[{session_id}] Failed to update metadata")


def _push_to_amocrm(
    session_id: str,
    quality_report: dict,
    session_meta: dict,
    audio_path: str,
    next_call_plan: dict | None = None,
    prior_context_for_summary: dict | None = None,
):
    """Push enriched note to AmoCRM deal. Finds lead_id from metadata or phone.

    Pushes whenever a lead_id is known — either set by the AmoCRM poller
    (source='amocrm'), assigned via the dashboard link-lead action
    (desktop/Zoom/upload sessions), or resolved by phone lookup. Stub
    reports (short calls, broken recordings, extraction-only) carry a
    `skip_reason` and are intentionally not published.
    """
    slug = require_tenant_slug()
    if not tenant_amocrm_enabled(slug):
        logger.info(
            f"[{session_id}] AmoCRM push skipped: tenant '{slug}' has no AmoCRM integration"
        )
        return

    lead_id = session_meta.get("lead_id")
    phone = session_meta.get("phone", "")
    amo_note_id = session_meta.get("amo_note_id")

    # Try to find lead by phone if not explicitly set (legacy AmoCRM-poll path).
    if not lead_id and phone:
        lead_id = find_lead_by_phone(phone)
        if lead_id:
            _update_session_metadata(session_id, {"lead_id": lead_id})

    if not lead_id:
        logger.info(f"[{session_id}] No lead_id found, skipping AmoCRM push")
        return

    skip_reason = quality_report.get("skip_reason")
    if skip_reason:
        logger.info(
            f"[{session_id}] skip_reason={skip_reason!r} — stub report, skipping AmoCRM push"
        )
        return

    duration = 0
    try:
        duration = int(_get_audio_duration(audio_path))
    except Exception:
        pass

    if amo_note_id:
        # Update existing note (re-processing)
        text = format_enriched_note(quality_report, session_id, responsible_user_id=session_meta.get("responsible_user_id", 0))
        result = update_note(lead_id, amo_note_id, text)
        if result.get("ok"):
            logger.info(f"[{session_id}] Updated AmoCRM note {amo_note_id}")
        else:
            logger.warning(f"[{session_id}] Failed to update AmoCRM note: {result}")
    else:
        # Create new note
        result = create_enriched_note(
            lead_id=lead_id,
            session_id=session_id,
            quality_report=quality_report,
            direction="out",
            duration=duration,
            phone=phone,
        )
        if result.get("ok"):
            _update_session_metadata(session_id, {"amo_note_id": result["note_id"]})
            logger.info(f"[{session_id}] Created AmoCRM note {result['note_id']} for lead {lead_id}")
        else:
            logger.warning(f"[{session_id}] Failed to create AmoCRM note: {result}")

    # Tag the lead
    score = quality_report.get("overall_score")
    if score is not None:
        tag_lead(lead_id, "AI оценка звонка")

    # Second note: next-call plan (if generated)
    if next_call_plan:
        plan_text = format_next_call_plan(next_call_plan)
        result = create_plain_note(lead_id, plan_text)
        if result.get("ok"):
            _update_session_metadata(session_id, {"plan_amo_note_id": result["note_id"]})
            logger.info(f"[{session_id}] Created plan note {result['note_id']} for lead {lead_id}")
        else:
            logger.warning(f"[{session_id}] Failed to create plan note: {result}")

    # Third note (singleton per lead): deal summary — update-in-place
    if quality_report.get("skip_reason") != "too_short":
        stage_name = (prior_context_for_summary or {}).get("current_deal_stage", {}).get("stage_name")
        summary = build_deal_summary(
            prior_context_for_summary or {},
            quality_report,
            stage_name=stage_name,
            current_created_at=session_meta.get("_current_created_at_iso", ""),
        )
        push_deal_summary(lead_id, summary)


def _run_gates(task, session_id: str, audio_path: str, config: dict, company_config: dict, scenario: dict | None, session_meta: dict) -> dict | None:
    """Пред-CPU гейты: короткий звонок / битая запись.

    Возвращает готовый результат (в том же контракте, что и полный прогон:
    transcript_with_speakers/quality_report/use_extended) либо None, если
    гейты пройдены и надо идти в транскрипцию. audio_duration вычисляется
    здесь (сигнатура совпадает с _run_pipeline)."""
    # === 1.5 Short-call gate ===
    audio_duration = _get_audio_duration(audio_path)
    if 0 < audio_duration < SHORT_CALL_THRESHOLD_SEC:
        logger.info(f"[{session_id}] Short call ({audio_duration:.1f}s < {SHORT_CALL_THRESHOLD_SEC}s), skipping full evaluation")
        quality_report = _build_short_call_report(audio_duration)
        save_results(session_id, "quality", quality_report)

        use_extended = bool(scenario and scenario.get("prompt"))
        if use_extended:
            task.update_state(state="PROGRESS", meta={"step": "amocrm_sync", "progress": 95})
            _push_to_amocrm(session_id, quality_report, session_meta, audio_path)

        return {
            "transcript_with_speakers": [],
            "quality_report": quality_report,
            "use_extended": use_extended,
        }

    # === 1.7 Broken-recording gate ===
    # Detect desktop-app recordings where the mic stream died mid-call and
    # everything after the first few seconds is digital silence.
    silence_stats = _detect_broken_recording(audio_path, audio_duration)
    if silence_stats:
        logger.warning(
            f"[{session_id}] Broken recording detected "
            f"({silence_stats['silent_windows']}/{silence_stats['total_windows']} silent windows, "
            f"opening={silence_stats['opening_db']}dB, tail={silence_stats['tail_db']})"
        )
        quality_report = _build_broken_recording_report(audio_duration, silence_stats)
        save_results(session_id, "quality", quality_report)

        use_extended = bool(scenario and scenario.get("prompt"))
        if use_extended:
            task.update_state(state="PROGRESS", meta={"step": "amocrm_sync", "progress": 95})
            _push_to_amocrm(session_id, quality_report, session_meta, audio_path)

        return {
            "transcript_with_speakers": [],
            "quality_report": quality_report,
            "use_extended": use_extended,
        }

    return None


def _transcribe_and_merge(task, session_id: str, audio_path: str, company_config: dict) -> list:
    """Шаги 2-4 (CPU): ASR (с кэшем transcript.json при reprocess) + диаризация + merge."""
    # === 2. Transcription (skipped if transcript already on disk — reprocess) ===
    transcript_path = tenant_results_dir(RESULTS_PATH, require_tenant_slug(), session_id) / "transcript.json"
    if transcript_path.exists():
        logger.info(f"[{session_id}] Step 2: Transcript already on disk, skipping transcription")
        with transcript_path.open() as f:
            cached = json.load(f)
        # `transcript.json` is already speaker-merged. Use it as transcript_with_speakers
        # below — set transcript = cached so the downstream check `has_speakers` short-circuits.
        transcript = cached
    else:
        task.update_state(state="PROGRESS", meta={"step": "transcribing", "progress": 15})
        word_boost = get_word_boost(company_config)
        word_boost = _merge_kb_keyterms(word_boost)        # KB layer-2 (best-effort)
        engine_override = get_asr_engine(company_config)
        logger.info(f"[{session_id}] Step 2: Transcribing (word_boost: {len(word_boost)} terms)...")
        transcript = transcribe_audio(audio_path, word_boost=word_boost, engine_override=engine_override)
        save_results(session_id, "transcript_raw", transcript)

    # === 3-4. Diarization + merge ===
    has_speakers = any(s.get("speaker") for s in transcript)
    if has_speakers:
        logger.info(f"[{session_id}] Step 3-4: Speakers already in transcript (from ASR), skipping diarization")
        task.update_state(state="PROGRESS", meta={"step": "merging_speakers", "progress": 70})
        transcript_with_speakers = transcript
    else:
        task.update_state(state="PROGRESS", meta={"step": "diarizing", "progress": 50})
        logger.info(f"[{session_id}] Step 3: Speaker diarization with pyannote...")
        diarization = diarize_audio(audio_path)
        save_results(session_id, "diarization", diarization)

        task.update_state(state="PROGRESS", meta={"step": "merging_speakers", "progress": 70})
        logger.info(f"[{session_id}] Step 4: Merging transcript with speakers...")
        transcript_with_speakers = merge_transcript_with_speakers(transcript, diarization)

    return transcript_with_speakers


def _run_stage1(task, session_id: str, audio_path: str, config: dict, company_config: dict, scenario: dict | None, session_meta: dict):
    """Стадия 1 (CPU): гейты → ASR+диаризация+merge → save transcript → handoff в analysis.

    Возвращает гейт-результат (сессия завершится в вызывающем body) либо
    {"handed_off": True} — тяжёлая CPU-часть закончена, IO/LLM ушли на
    очередь analysis."""
    gate = _run_gates(task, session_id, audio_path, config, company_config, scenario, session_meta)
    if gate is not None:
        return gate

    # === 2-4 (CPU): транскрипция + диаризация + merge ===
    transcript_with_speakers = _transcribe_and_merge(task, session_id, audio_path, company_config)
    # transcript.json ДО KB-нормализации: analyze нормализует идемпотентно
    # (KB layer-1, см. комментарий в _analyze_inner) и пересохранит.
    save_results(session_id, "transcript", transcript_with_speakers)

    # Handoff: CPU-часть кончилась; LLM/AmoCRM уходят на IO-очередь analysis,
    # не занимая слот тяжёлого воркера.
    app.send_task(
        "pipeline.analyze_session",
        kwargs={
            "session_id": session_id,
            "audio_path": audio_path,
            "config": config,
            "tenant_schema": get_tenant_schema(),
        },
        queue="analysis",
    )
    return {"handed_off": True}


def _run_pipeline(task, session_id: str, audio_path: str, config: dict, company_config: dict, scenario: dict | None, session_meta: dict):
    """Синхронный полный прогон БЕЗ handoff — путь AmoCRM-поллинга
    (amocrm_poll.py:473 читает quality_report/use_extended из результата).
    Контракт возврата НЕ менять.

    Called by process_amocrm_call (AmoCRM inbound/outbound). Browser and
    file-upload paths now go through stage-1 + analyze_session handoff.
    """
    gate = _run_gates(task, session_id, audio_path, config, company_config, scenario, session_meta)
    if gate is not None:
        return gate

    transcript_with_speakers = _transcribe_and_merge(task, session_id, audio_path, company_config)
    save_results(session_id, "transcript", transcript_with_speakers)

    # Serialise concurrent pipelines on the same lead (prevents prior_context
    # races when a manual reprocess overlaps with polling, etc.)
    with lead_lock(session_meta.get("lead_id")):
        return _analyze_inner(task, session_id, audio_path, config, company_config,
                              scenario, session_meta, transcript_with_speakers)


def _analyze_inner(task, session_id: str, audio_path: str, config: dict, company_config: dict,
                   scenario: dict | None, session_meta: dict, transcript_with_speakers: list):
    """KB → (extraction | sentiment → quality → card → plan → AmoCRM). Вызывается под lead_lock."""
    # KB layer-1: engine-agnostic correction + tagging. Idempotent → safe for reprocess.
    try:
        from tasks.knowledge_base import kb_build_matcher, normalize_transcript
        matcher = kb_build_matcher()
        transcript_with_speakers, _kb_hits = normalize_transcript(transcript_with_speakers, matcher)
    except Exception:
        logger.exception(f"[{session_id}] KB normalize failed; transcript unmodified")
        _kb_hits = []
    save_results(session_id, "transcript", transcript_with_speakers)  # normalized on disk
    try:
        from tasks.knowledge_base import kb_record_mentions
        kb_record_mentions(session_id, _kb_hits)
    except Exception:
        logger.exception(f"[{session_id}] KB record_mentions failed")

    # === Branch: extraction template skips quality+sentiment+amocrm ===
    template_id_meta = (session_meta or {}).get("template_id")
    template_id, template_kind = _load_template_kind(template_id_meta)
    if template_kind == "extraction" and template_id:
        from tasks.extract import run_extraction
        from sqlalchemy.orm import Session as _DbSession
        task.update_state(state="PROGRESS", meta={"step": "extracting", "progress": 85})
        logger.info(f"[{session_id}] Step 5e: LLM extraction with template {template_id}...")
        eng = tenant_engine()
        try:
            with eng.connect() as conn:
                with _DbSession(bind=conn, expire_on_commit=False) as db:
                    from tasks.knowledge_base import kb_glossary
                    run_extraction(db, session_id, template_id, glossary=kb_glossary())
            logger.info(f"[{session_id}] Extraction completed")
        finally:
            eng.dispose()
        return {
            "transcript_with_speakers": transcript_with_speakers,
            "quality_report": {"skip_reason": "extraction_template"},
            "use_extended": False,
        }

    # === 5. Sentiment Analysis ===
    task.update_state(state="PROGRESS", meta={"step": "sentiment", "progress": 80})
    logger.info(f"[{session_id}] Step 5: Sentiment analysis...")
    sentiment_results = analyze_sentiment(transcript_with_speakers)
    save_results(session_id, "sentiment", sentiment_results)

    # === 6. LLM Quality Assessment ===
    task.update_state(state="PROGRESS", meta={"step": "quality", "progress": 90})
    logger.info(f"[{session_id}] Step 6: LLM quality assessment...")
    quality_protocol = (scenario.get("protocol") if scenario else None) or config.get("protocol") or get_protocol(company_config)
    quality_prompt = (scenario.get("prompt") if scenario else None) or config.get("custom_prompt") or get_custom_prompt(company_config)
    quality_criteria = scenario.get("criteria") if scenario else None

    use_extended = bool(scenario and scenario.get("prompt"))

    # Per-tenant, per-scenario evaluation profile (DB) overrides the file-config
    # scenario criteria/prompt. Активный профиль настраивается в дашборде
    # (#evaluation, routes/eval_profiles.py). Evaluation-шаблон (ниже, при явном
    # reprocess) всё ещё главнее профиля.
    scenario_id_eff = scenario.get("id") if scenario else None
    if scenario_id_eff:
        try:
            from tasks.eval_profile import load_active_eval_profile
            _profile = load_active_eval_profile(scenario_id_eff)
        except Exception:
            logger.exception(f"[{session_id}] eval profile load failed; using file config")
            _profile = None
        if _profile:
            if _profile.get("criteria"):
                quality_criteria = _profile["criteria"]
            if _profile.get("prompt"):
                quality_prompt = _profile["prompt"]
                use_extended = True
            logger.info(
                f"[{session_id}] Using eval profile for scenario '{scenario_id_eff}' "
                f"(criteria: {len(_profile.get('criteria') or [])}, custom_prompt: {bool(_profile.get('prompt'))})"
            )

    # Evaluation template override: if the session was tagged with an evaluation
    # template, the template's prompt is self-contained (protocol + criteria +
    # scoring rules baked in by migration 005/008). Drop scenario-derived
    # custom_prompt and criteria so the LLM doesn't get contradictory guidance —
    # e.g. when reprocessing an AmoCRM outbound call (scenario_id =
    # outbound_residential, criteria like meeting_effort) with the Zoom-meeting
    # template, the scenario's outbound playbook would otherwise drown out the
    # template's Zoom protocol and skew the answer back to "meeting closing"
    # framing.
    template_driven = False
    if template_kind == "evaluation" and template_id:
        tpl_prompt = _load_evaluation_template_prompt(template_id)
        if tpl_prompt:
            quality_protocol = tpl_prompt
            quality_prompt = None
            tpl_criteria = _load_evaluation_template_criteria(template_id)
            quality_criteria = tpl_criteria  # falls through to DEFAULT_CRITERIA when None
            use_extended = True
            template_driven = True
            logger.info(
                f"[{session_id}] Using evaluation template {template_id} as protocol "
                f"(template criteria: {len(tpl_criteria) if tpl_criteria else 0}, template_driven=True)"
            )

    # Resolve lead_id up-front: calls attached to a contact (not a lead) land here
    # with lead_id=None; we need it for prior_context lookup AND for the plan gate.
    lead_id = session_meta.get("lead_id")
    phone = session_meta.get("phone", "")
    amocrm_enabled = tenant_amocrm_enabled(require_tenant_slug())
    if not lead_id and phone and amocrm_enabled:
        lead_id = find_lead_by_phone(phone)
        if lead_id:
            _update_session_metadata(session_id, {"lead_id": lead_id})
            session_meta["lead_id"] = lead_id
    current_created_at = _get_session_created_at(session_id)
    if current_created_at is not None:
        session_meta["_current_created_at_iso"] = (
            current_created_at.isoformat() if hasattr(current_created_at, "isoformat") else str(current_created_at)
        )
    # Fetch deal stage + events for stage-aware + offline-gap awareness (Phase 2)
    from tasks.amocrm_sync import get_lead_stage, fetch_lead_events
    deal_stage = get_lead_stage(lead_id) if (lead_id and amocrm_enabled) else None
    events = fetch_lead_events(lead_id) if (lead_id and amocrm_enabled) else []

    prior_context = (
        build_prior_context_for_session(lead_id, current_created_at, deal_stage=deal_stage, events=events)
        if lead_id else None
    )
    if prior_context and prior_context.get("previous_calls_count", 0) > 0:
        logger.info(f"[{session_id}] Prior context: call #{prior_context['call_number']}, "
                    f"{prior_context['previous_calls_count']} prior, "
                    f"{len(prior_context['open_objections'])} open objections")

    quality_report = assess_quality(
        transcript_with_speakers,
        sentiment_results,
        protocol=quality_protocol,
        custom_prompt=quality_prompt,
        criteria_config=quality_criteria,
        use_extended_schema=use_extended,
        prior_context=prior_context,
        template_driven=template_driven,
        glossary=_kb_glossary_safe(),
    )
    save_results(session_id, "quality", quality_report)

    # === 6b. Structured card (config-gated, generic; clinical only — QA above) ===
    from tasks.card import run_card_extraction, has_card_extraction
    card = run_card_extraction(transcript_with_speakers, company_config, scenario)
    if card is not None:
        save_results(session_id, "card", card)
        logger.info(f"[{session_id}] Card extraction saved")
    elif not has_card_extraction(company_config, scenario):
        # Для этого сценария карта НЕ сконфигурирована → убрать устаревшую card.json.
        # NB: при сбое извлечения (карта нужна, но упала) has_card_extraction=True →
        # старую card.json НЕ трогаем, чтобы транзиентная ошибка её не стёрла.
        delete_results(session_id, "card")

    # === 7. Auto-save speaker roles ===
    speaker_roles = quality_report.get("speaker_roles")
    if speaker_roles:
        _save_speaker_roles(session_id, speaker_roles)

    # === 7.5 Next-call plan (LLM call 2) ===
    next_call_plan = None
    classification = (quality_report.get("call_classification") or {}).get("type", "")
    can_plan = (
        use_extended
        and quality_report.get("skip_reason") != "too_short"
        and classification != "brushoff_short"
        and prior_context is not None
    )
    if can_plan:
        try:
            task.update_state(state="PROGRESS", meta={"step": "next_call_plan", "progress": 93})
            logger.info(f"[{session_id}] Step 7.5: Next-call plan...")
            stage_name = (deal_stage or {}).get("stage_name")
            next_call_plan = plan_next_call(prior_context, quality_report, deal_stage=stage_name)
            if next_call_plan:
                save_results(session_id, "next_call_plan", next_call_plan)
                _update_session_metadata(session_id, {"next_call_plan": next_call_plan})
        except Exception:
            logger.exception(f"[{session_id}] Next-call plan generation failed (continuing)")

    # === 8. Push to AmoCRM (if extended) ===
    if use_extended:
        task.update_state(state="PROGRESS", meta={"step": "amocrm_sync", "progress": 95})
        logger.info(f"[{session_id}] Step 8: AmoCRM sync...")
        _push_to_amocrm(
            session_id, quality_report, session_meta, audio_path,
            next_call_plan=next_call_plan,
            prior_context_for_summary=prior_context,
        )

    return {
        "transcript_with_speakers": transcript_with_speakers,
        "quality_report": quality_report,
        "use_extended": use_extended,
    }


def _analyze_session_body(task, session_id: str, audio_path: str, config: dict | None = None):
    """IO-стадия: грузит transcript.json со стадии 1 и доводит сессию до completed."""
    config = config or {}
    logger.info(f"[{session_id}] Analyze stage starting...")
    start_time = datetime.now(timezone.utc)

    session_meta = _get_session_metadata(session_id)
    company_id = config.get("company_id") or tenant_company_config_id()
    scenario_id = config.get("scenario_id")
    company_config = load_company_config(company_id)
    scenario = get_scenario(company_config, scenario_id or get_default_scenario_id(company_config))

    try:
        transcript_path = tenant_results_dir(RESULTS_PATH, require_tenant_slug(), session_id) / "transcript.json"
        with transcript_path.open(encoding="utf-8") as f:
            transcript_with_speakers = json.load(f)

        # lead_lock здесь, а не в стадии 1: только analyze трогает
        # prior_context/метаданные лида/AmoCRM.
        with lead_lock(session_meta.get("lead_id")):
            result = _analyze_inner(task, session_id, audio_path, config, company_config,
                                    scenario, session_meta, transcript_with_speakers)

        finished_at = datetime.now(timezone.utc)
        audio_duration = _get_audio_duration(audio_path)
        processing_duration = (finished_at - start_time).total_seconds()
        update_session_status(
            session_id, "completed",
            finished_at=finished_at,
            duration_seconds=audio_duration or processing_duration,
        )
        logger.info(f"[{session_id}] Analyze stage completed!")

        tws = result["transcript_with_speakers"]
        return {
            "session_id": session_id,
            "status": "completed",
            "segments_count": len(tws),
            "speakers_count": len(set(s.get("speaker", "") for s in tws)),
            "quality_score": result["quality_report"].get("overall_score"),
        }

    except Exception as e:
        if _is_transient(e) and task.request.retries < 2:
            logger.warning(f"[{session_id}] Analyze transient failure (retry {task.request.retries + 1}/2): {e}")
            raise task.retry(countdown=120, exc=e)
        update_session_status(session_id, "failed", finished_at=datetime.now(timezone.utc))
        logger.exception(f"[{session_id}] Analyze stage failed: {e}")
        raise


# acks_late: analyze идемпотентна (вход — transcript.json с диска) → at-least-once
# безопасен; убитый io-воркер не теряет задачу (ре-доставка после
# visibility_timeout Redis-брокера, по умолчанию 1 час).
@app.task(bind=True, queue="analysis", name="pipeline.analyze_session",
          acks_late=True, reject_on_worker_lost=True)
def analyze_session(self, session_id: str, audio_path: str, config: dict | None = None,
                    tenant_schema: str | None = None):
    if not tenant_schema:
        raise ValueError("tenant_schema is required (fail fast: a task without "
                         "tenant context would read/write the wrong schema)")
    token = set_tenant_schema(tenant_schema)
    try:
        return _analyze_session_body(self, session_id, audio_path, config)
    finally:
        reset_tenant_schema(token)


def _process_session_body(task, session_id: str, config: dict | None = None):
    """Стадия 1 для browser-записей: merge чанков + гейты + ASR + handoff."""
    config = config or {}
    # Fairness: не больше TENANT_MAX_CONCURRENT тяжёлых задач на тенанта.
    slot = try_acquire(require_tenant_slug())
    if slot is None:
        raise task.retry(countdown=TENANT_SLOT_RETRY_SEC, max_retries=None)
    try:
        logger.info(f"[{session_id}] Starting processing pipeline...")
        start_time = datetime.now(timezone.utc)
        update_session_status(session_id, "processing")

        session_meta = _get_session_metadata(session_id)
        # company/scenario — server-owned (спека 5.6): из клиентских метаданных
        # сессии НЕ читаются. company — из shared.tenants, scenario — только из
        # явного config (ops-скрипты/reprocess).
        company_id = config.get("company_id") or tenant_company_config_id()
        scenario_id = config.get("scenario_id")
        company_config = load_company_config(company_id)
        scenario = get_scenario(company_config, scenario_id or get_default_scenario_id(company_config))
        logger.info(f"[{session_id}] Company: {company_config.get('name', company_id)}, "
                    f"Scenario: {scenario.get('name') if scenario else 'default'}")

        try:
            # Step 1: Merge chunks
            task.update_state(state="PROGRESS", meta={"step": "merging", "progress": 5})
            logger.info(f"[{session_id}] Step 1: Merging audio chunks...")
            audio_path = merge_chunks(session_id)

            result = _run_stage1(task, session_id, audio_path, config, company_config, scenario, session_meta)

            if result.get("handed_off"):
                logger.info(f"[{session_id}] Stage 1 done — analyze queued")
                return {"session_id": session_id, "status": "analyzing"}

            # Гейт (short-call / broken recording) уже сохранил quality.json —
            # завершаем сессию прямо здесь, analyze не нужен.
            finished_at = datetime.now(timezone.utc)
            audio_duration = _get_audio_duration(audio_path)
            processing_duration = (finished_at - start_time).total_seconds()
            update_session_status(
                session_id, "completed",
                finished_at=finished_at,
                duration_seconds=audio_duration or processing_duration,
            )
            return {
                "session_id": session_id,
                "status": "completed",
                "segments_count": 0,
                "speakers_count": 0,
                "quality_score": result["quality_report"].get("overall_score"),
            }
        except Exception as e:
            update_session_status(session_id, "failed", finished_at=datetime.now(timezone.utc))
            logger.exception(f"[{session_id}] Pipeline stage 1 failed: {e}")
            raise
    finally:
        if slot is not None:
            release(*slot)


@app.task(bind=True, queue="transcription", name="pipeline.process_session")
def process_session(self, session_id: str, config: dict | None = None,
                    tenant_schema: str | None = None):
    if not tenant_schema:
        raise ValueError("tenant_schema is required (fail fast: a task without "
                         "tenant context would read/write the wrong schema)")
    token = set_tenant_schema(tenant_schema)
    try:
        return _process_session_body(self, session_id, config)
    finally:
        reset_tenant_schema(token)


def _process_session_from_file_body(task, session_id: str, audio_path: str, config: dict | None = None):
    """Стадия 1 для готовых аудиофайлов (uploaded files): гейты + ASR + handoff."""
    config = config or {}
    # Fairness: не больше TENANT_MAX_CONCURRENT тяжёлых задач на тенанта.
    slot = try_acquire(require_tenant_slug())
    if slot is None:
        raise task.retry(countdown=TENANT_SLOT_RETRY_SEC, max_retries=None)
    try:
        logger.info(f"[{session_id}] Starting file-based pipeline for {audio_path}...")
        start_time = datetime.now(timezone.utc)
        update_session_status(session_id, "processing")

        session_meta = _get_session_metadata(session_id)
        # company/scenario — server-owned (спека 5.6): клиентские фоллбеки из
        # метаданных сессии убраны; см. _process_session_body.
        company_id = config.get("company_id") or tenant_company_config_id()
        scenario_id = config.get("scenario_id")
        company_config = load_company_config(company_id)
        scenario = get_scenario(company_config, scenario_id or get_default_scenario_id(company_config))
        logger.info(f"[{session_id}] Company: {company_config.get('name', company_id)}, "
                    f"Scenario: {scenario.get('name') if scenario else 'default'}")

        try:
            result = _run_stage1(task, session_id, audio_path, config, company_config, scenario, session_meta)

            if result.get("handed_off"):
                logger.info(f"[{session_id}] Stage 1 done — analyze queued")
                return {"session_id": session_id, "status": "analyzing"}

            # Гейт (short-call / broken recording) уже сохранил quality.json —
            # завершаем сессию прямо здесь, analyze не нужен.
            finished_at = datetime.now(timezone.utc)
            audio_duration = _get_audio_duration(audio_path)
            processing_duration = (finished_at - start_time).total_seconds()
            update_session_status(
                session_id, "completed",
                finished_at=finished_at,
                duration_seconds=audio_duration or processing_duration,
            )
            return {
                "session_id": session_id,
                "status": "completed",
                "segments_count": 0,
                "speakers_count": 0,
                "quality_score": result["quality_report"].get("overall_score"),
            }
        except Exception as e:
            update_session_status(session_id, "failed", finished_at=datetime.now(timezone.utc))
            logger.exception(f"[{session_id}] Pipeline stage 1 failed: {e}")
            raise
    finally:
        if slot is not None:
            release(*slot)


@app.task(bind=True, queue="transcription", name="pipeline.process_session_from_file")
def process_session_from_file(self, session_id: str, audio_path: str, config: dict | None = None,
                              tenant_schema: str | None = None):
    if not tenant_schema:
        raise ValueError("tenant_schema is required (fail fast: a task without "
                         "tenant context would read/write the wrong schema)")
    token = set_tenant_schema(tenant_schema)
    try:
        return _process_session_from_file_body(self, session_id, audio_path, config)
    finally:
        reset_tenant_schema(token)
