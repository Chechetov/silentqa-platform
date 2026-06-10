"""
Polling AmoCRM for new call recordings.

Two tasks:
- poll_amocrm_calls: Celery Beat task (every 5 min), fetches new call events
- process_amocrm_call: Downloads recording, creates session, runs pipeline
"""
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psycopg2

from tasks.celery_app import app
from tasks.amocrm_sync import (
    get_recent_call_events,
    get_note_details,
    download_recording,
)

logger = logging.getLogger(__name__)

AUDIO_PATH = os.getenv("AUDIO_STORAGE_PATH", "./data/audio")
INITIAL_LOOKBACK_HOURS = int(os.getenv("AMOCRM_INITIAL_LOOKBACK_HOURS", "24"))
# AmoCRM events API lags behind note creation — an event may appear in the
# listing many minutes after its created_at timestamp (esp. long calls, whose
# event surfaces only once the recording is encoded). Always rewind the `since`
# cursor by this window so freshly-surfaced events are not filtered out. We've
# seen the lag exceed 15 min and silently drop a call (lead 12056961), so the
# default is 60 min. Duplicates are harmless: insertion is ON CONFLICT
# (amo_note_id). The reconcile task (amocrm_reconcile) is the deeper backstop
# for any event that surfaces even later than this window.
POLL_SAFETY_WINDOW_MINUTES = int(os.getenv("AMOCRM_POLL_SAFETY_WINDOW_MINUTES", "60"))
# UIS Com sometimes takes >15 min to publish a recording after a call ends —
# we've seen recordings appear an hour or more after the AmoCRM note. With a
# 5-min poll cadence, 36 retries gives ≈3h of catch-up, which covers the
# observed encoding delays. After that we stop to avoid hammering URLs that
# truly never resolve. The hard age cap (RETRY_MAX_AGE_HOURS) keeps long-stuck
# rows from being retried forever.
MAX_RETRIES = int(os.getenv("AMOCRM_MAX_RETRIES", "36"))
RETRY_MAX_AGE_HOURS = int(os.getenv("AMOCRM_RETRY_MAX_AGE_HOURS", "24"))


def _get_sync_db_url() -> str:
    url = os.getenv("DATABASE_URL_SYNC", "") or os.getenv("DATABASE_URL", "")
    url = url.replace("postgresql+psycopg2://", "postgresql://")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    return url


def _get_last_poll_timestamp() -> int:
    """Unix timestamp to use as `filter[created_at][from]` in the next poll.

    Rewinds the cursor by POLL_SAFETY_WINDOW_MINUTES so events that appear in
    the AmoCRM listing API with a delay are not missed. Duplicate notes are
    de-duplicated downstream by ON CONFLICT (amo_note_id).
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())
    safety_cutoff = now_ts - POLL_SAFETY_WINDOW_MINUTES * 60
    initial_cutoff = int((datetime.now(timezone.utc) - timedelta(hours=INITIAL_LOOKBACK_HOURS)).timestamp())

    db_url = _get_sync_db_url()
    if not db_url:
        return initial_cutoff

    try:
        conn = psycopg2.connect(db_url)
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(created_at) FROM amocrm_calls")
                row = cur.fetchone()
        conn.close()
        if row and row[0]:
            last_insert_ts = int(row[0].timestamp())
            # Rewind by safety window to catch late-arriving events.
            return min(last_insert_ts, safety_cutoff)
    except Exception:
        logger.exception("Failed to get last poll timestamp")

    return initial_cutoff


def _insert_call(amo_note_id: int, lead_id: int, phone: str, direction: str,
                 duration: int, recording_url: str, responsible_user_id: int = 0,
                 entity_type: str = "", entity_id: int = 0) -> int | None:
    """Insert new call into amocrm_calls. Returns row ID or None if duplicate.

    `recording_url` may be empty: a call is ingested as soon as its start-event
    appears in AmoCRM, which can precede the recording being published. The
    entity reference (`entity_type`/`entity_id`) lets process_amocrm_call
    re-read the note later to pick the recording up once it lands.
    """
    db_url = _get_sync_db_url()
    if not db_url:
        return None

    try:
        conn = psycopg2.connect(db_url)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO amocrm_calls
                       (amo_note_id, lead_id, contact_phone, direction, duration,
                        recording_url, responsible_user_id, entity_type, entity_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (amo_note_id) DO NOTHING
                       RETURNING id""",
                    (amo_note_id, lead_id, phone, direction, duration, recording_url,
                     responsible_user_id, entity_type or None, entity_id or None),
                )
                row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        logger.exception(f"Failed to insert amocrm_call for note {amo_note_id}")
        return None


_ALLOWED_UPDATE_COLUMNS = {"error_message", "session_id", "retry_count", "processed_at",
                           "recording_url"}


def _update_call_status(call_id: int, status: str, **kwargs):
    """Update amocrm_calls row status and optional fields."""
    db_url = _get_sync_db_url()
    if not db_url:
        return

    sets = ["status = %s"]
    values = [status]
    for key, value in kwargs.items():
        if key not in _ALLOWED_UPDATE_COLUMNS:
            raise ValueError(f"Disallowed column in _update_call_status: {key}")
        sets.append(f"{key} = %s")
        values.append(value)
    values.append(call_id)

    try:
        conn = psycopg2.connect(db_url)
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE amocrm_calls SET {', '.join(sets)} WHERE id = %s", values)
        conn.close()
    except Exception:
        logger.exception(f"Failed to update amocrm_call {call_id}")


def reset_call_for_reprocess(call_id: int) -> bool:
    """Reset a call to 'created' status so it can be re-enqueued. Returns True if reset."""
    db_url = _get_sync_db_url()
    if not db_url:
        return False
    try:
        conn = psycopg2.connect(db_url)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE amocrm_calls SET status='created', retry_count=0, error_message=NULL "
                    "WHERE id=%s",
                    (call_id,),
                )
                return cur.rowcount > 0
    except Exception:
        logger.exception(f"Failed to reset amocrm_call {call_id} for reprocess")
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _get_retryable_calls() -> list[dict]:
    """Get retryable calls with retry_count < MAX_RETRIES.

    Covers both 'failed' (download/processing error) and 'awaiting_recording'
    (call ingested before its recording was published) — the latter is retried
    until the recording lands or the retry budget is exhausted.
    """
    db_url = _get_sync_db_url()
    if not db_url:
        return []

    try:
        conn = psycopg2.connect(db_url)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, amo_note_id, lead_id, contact_phone, direction,
                              duration, recording_url
                       FROM amocrm_calls
                       WHERE status IN ('failed', 'awaiting_recording')
                         AND retry_count < %s
                         AND created_at > now() - (%s || ' hours')::interval
                       ORDER BY created_at
                       LIMIT 10""",
                    (MAX_RETRIES, RETRY_MAX_AGE_HOURS),
                )
                rows = cur.fetchall()
        conn.close()
        return [
            {"id": r[0], "amo_note_id": r[1], "lead_id": r[2], "contact_phone": r[3],
             "direction": r[4], "duration": r[5], "recording_url": r[6]}
            for r in rows
        ]
    except Exception:
        logger.exception("Failed to get retryable calls")
        return []


def _create_session(metadata: dict) -> str:
    """Create a new session in the sessions table. Returns session_id."""
    db_url = _get_sync_db_url()
    session_id = str(uuid.uuid4())

    if not db_url:
        return session_id

    try:
        conn = psycopg2.connect(db_url)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sessions (id, status, metadata) VALUES (%s, 'created', %s)",
                    (session_id, json.dumps(metadata, ensure_ascii=False)),
                )
        conn.close()
        logger.info(f"Created session {session_id} for AmoCRM call")
    except Exception:
        logger.exception(f"Failed to create session {session_id}")

    return session_id


def _convert_to_wav(input_path: str, output_path: str) -> bool:
    """Convert audio file to WAV 16kHz mono via ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        logger.error(f"ffmpeg conversion timed out for {input_path}")
        return False
    if result.returncode != 0:
        logger.error(f"ffmpeg conversion failed: {result.stderr[:500]}")
        return False
    return True


def _ingest_call_event(event: dict) -> int | None:
    """Fetch a call note for one events-API event, insert it (deduped by
    amo_note_id) and enqueue processing. Returns the new call_id, or None if the
    note can't be fetched, is one of our own notes, or was already ingested.

    Shared by the 5-min poll AND the reconcile deep-sweep so both ingest calls
    identically. Re-running over an already-seen note is a no-op: _insert_call
    uses ON CONFLICT (amo_note_id) DO NOTHING and only a fresh row returns an id.

    NB: we intentionally do NOT skip notes without a recording URL. The call
    start-event surfaces at call-start time, but for a long call the recording
    is attached only after the call ends — by which point the event has aged
    past the poll cursor. So we ingest now (empty URL is fine) and
    process_amocrm_call resolves the recording later via the retry layer, using
    the persisted entity reference to re-read the note.
    """
    note_id = event["note_id"]
    entity_id = event["entity_id"]
    entity_type = event["entity_type"]
    event_type = event["event_type"]

    note = get_note_details(entity_type, entity_id, note_id)
    if not note:
        return None

    note_type = note.get("note_type", "")
    params = note.get("params", {})
    recording_url = params.get("link", "")
    phone_raw = params.get("phone", "")
    phone = phone_raw.split(",")[0].strip()  # Strip "Статус: ..." suffix
    duration = params.get("duration", 0)
    source = params.get("source", "")
    responsible_user_id = note.get("responsible_user_id", 0)

    # Skip our own notes
    if source == "Rogov AI":
        return None

    direction = "out" if note_type == "call_out" or event_type == "outgoing_call" else "in"
    lead_id = entity_id if entity_type in ("lead", "leads") else 0

    call_id = _insert_call(
        amo_note_id=note_id,
        lead_id=lead_id,
        phone=phone,
        direction=direction,
        duration=duration,
        recording_url=recording_url,
        responsible_user_id=responsible_user_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    if call_id:
        process_amocrm_call.delay(call_id)
    return call_id


@app.task(name="amocrm_poll.poll_amocrm_calls")
def poll_amocrm_calls():
    """Celery Beat task: poll AmoCRM for new call recordings every 5 minutes."""
    logger.info("Polling AmoCRM for new calls...")
    since = _get_last_poll_timestamp()
    events = get_recent_call_events(since)

    new_count = 0
    for event in events:
        if _ingest_call_event(event):
            new_count += 1

    # Also retry failed calls
    retryable = _get_retryable_calls()
    for call in retryable:
        logger.info(f"Retrying failed call {call['id']} (note {call['amo_note_id']})")
        process_amocrm_call.delay(call["id"])

    logger.info(f"Poll complete: {new_count} new calls, {len(retryable)} retries")
    return {"new": new_count, "retries": len(retryable)}


@app.task(bind=True, queue="transcription", name="amocrm_poll.process_amocrm_call")
def process_amocrm_call(self, call_id: int):
    """Download recording from AmoCRM note and run through pipeline."""
    # Read call data from DB
    db_url = _get_sync_db_url()
    if not db_url:
        logger.error("DATABASE_URL not set")
        return

    try:
        conn = psycopg2.connect(db_url)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT amo_note_id, lead_id, contact_phone, direction,
                              duration, recording_url, retry_count, responsible_user_id,
                              entity_type, entity_id
                       FROM amocrm_calls WHERE id = %s""",
                    (call_id,),
                )
                row = cur.fetchone()
        conn.close()
    except Exception:
        logger.exception(f"Failed to read amocrm_call {call_id}")
        return

    if not row:
        logger.warning(f"amocrm_call {call_id} not found")
        return

    (amo_note_id, lead_id, phone, direction, duration, recording_url, retry_count,
     responsible_user_id, entity_type, entity_id) = row
    logger.info(f"[amo:{amo_note_id}] Processing call (direction={direction}, phone={phone})")

    # === Inbound calls are not scored ===
    # There is no inbound evaluation rubric — only the outbound_residential
    # scenario exists, and scoring an inbound call with an outbound sales
    # playbook would be wrong-genre (see call-protocols rule). So inbound calls
    # are skipped explicitly here, before any download/transcription, rather
    # than silently producing a default-protocol score that never gets pushed.
    if direction != "out":
        _update_call_status(call_id, "skipped_inbound", processed_at=datetime.now(timezone.utc))
        logger.info(f"[amo:{amo_note_id}] Inbound call — not scored (no inbound rubric), skipping")
        return

    # === 0. Resolve recording URL ===
    # The poll ingests a call as soon as its start-event appears, which can be
    # before the recording is published (long calls: the recording is attached
    # only after the call ends). When the URL is missing, re-read the note now
    # — the recording may have landed since the last attempt. If it still
    # hasn't, park the row as 'awaiting_recording' so the retry layer tries
    # again next poll, until the recording lands or the retry budget runs out.
    if not recording_url:
        note = (
            get_note_details(entity_type, entity_id, amo_note_id)
            if entity_type and entity_id
            else None
        )
        recording_url = ((note or {}).get("params") or {}).get("link") or ""
        if not recording_url:
            _update_call_status(
                call_id, "awaiting_recording",
                error_message="recording_not_published",
                retry_count=retry_count + 1,
            )
            logger.info(
                f"[amo:{amo_note_id}] Recording not published yet "
                f"(attempt {retry_count + 1}/{MAX_RETRIES}) — will retry"
            )
            return
        _update_call_status(call_id, "downloading", recording_url=recording_url,
                            error_message=None)
        logger.info(f"[amo:{amo_note_id}] Recording URL resolved on retry")
    else:
        _update_call_status(call_id, "downloading")

    # === 1. Download recording ===
    call_dir = Path(AUDIO_PATH) / "amocrm" / str(amo_note_id)
    call_dir.mkdir(parents=True, exist_ok=True)

    # Determine file extension from URL or default to mp3
    original_ext = "mp3"
    if recording_url:
        for ext in ("wav", "mp3", "ogg"):
            if ext in recording_url.lower():
                original_ext = ext
                break

    original_path = str(call_dir / f"original.{original_ext}")
    if not download_recording(recording_url, original_path):
        _update_call_status(call_id, "failed", error_message="download_failed", retry_count=retry_count + 1)
        logger.warning(f"[amo:{amo_note_id}] Recording download failed")
        return

    # === 2. Convert to WAV 16kHz mono ===
    wav_path = str(call_dir / "full.wav")
    if not _convert_to_wav(original_path, wav_path):
        _update_call_status(call_id, "failed", error_message="conversion_failed", retry_count=retry_count + 1)
        return

    # === 3. Create session ===
    # Determine scenario: outbound → outbound_residential
    scenario_id = "outbound_residential" if direction == "out" else None

    session_metadata = {
        "company_id": "realestate",
        "source": "amocrm",
        "amo_source_note_id": amo_note_id,
        "responsible_user_id": responsible_user_id or None,
        "lead_id": lead_id if lead_id else None,
        "phone": phone,
        "direction": direction,
        "scenario_id": scenario_id,
    }
    session_id = _create_session(session_metadata)
    _update_call_status(call_id, "processing", session_id=session_id)

    # === 4. Run pipeline directly (not via Celery to avoid result.get() deadlock) ===
    try:
        from tasks.pipeline import _run_pipeline, update_session_status, _get_session_metadata, _get_audio_duration
        from tasks.company_config import load_company_config, get_scenario

        update_session_status(session_id, "processing")
        session_meta = _get_session_metadata(session_id)
        company_config = load_company_config("realestate")
        scenario = get_scenario(company_config, scenario_id)

        result = _run_pipeline(self, session_id, wav_path, {
            "company_id": "realestate",
            "scenario_id": scenario_id,
        }, company_config, scenario, session_meta)

        audio_duration = _get_audio_duration(wav_path)
        update_session_status(
            session_id, "completed",
            finished_at=datetime.now(timezone.utc),
            duration_seconds=audio_duration or 0,
        )

        # === Verify the AmoCRM push actually landed ===
        # The push runs inside the pipeline and its failure used to be swallowed
        # (logged as a warning) while the call was still marked 'completed' —
        # silently losing the score. When a push was expected (extended scenario,
        # no stub skip_reason) but no note id was persisted, mark the call
        # 'failed' so the retry layer re-runs it. The evaluation itself is done,
        # so the session stays 'completed'; only the call is re-queued. The
        # transcript is cached on disk, so the retry skips re-transcription.
        quality_report = (result or {}).get("quality_report") or {}
        push_expected = (result or {}).get("use_extended") and not quality_report.get("skip_reason")
        if push_expected and not (_get_session_metadata(session_id) or {}).get("amo_note_id"):
            _update_call_status(
                call_id, "failed",
                error_message="amocrm_push_failed",
                retry_count=retry_count + 1,
            )
            logger.warning(
                f"[amo:{amo_note_id}] Pipeline done but AmoCRM push did not land "
                f"— marked for retry ({retry_count + 1}/{MAX_RETRIES}), session {session_id}"
            )
            return result

        _update_call_status(
            call_id, "completed",
            processed_at=datetime.now(timezone.utc),
        )
        logger.info(f"[amo:{amo_note_id}] Pipeline completed, session {session_id}")
        return result

    except Exception as e:
        _update_call_status(call_id, "failed", error_message=str(e)[:500], retry_count=retry_count + 1)
        logger.exception(f"[amo:{amo_note_id}] Pipeline failed: {e}")
        raise
