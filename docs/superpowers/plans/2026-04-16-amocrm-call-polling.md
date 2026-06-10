# AmoCRM Call Polling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically poll AmoCRM for new call recordings, process them through the transcription/quality pipeline, and push enriched results back.

**Architecture:** Celery Beat polls AmoCRM every 5 minutes for new call notes. Each new call spawns a processing task that downloads the recording, creates a session, and runs the existing pipeline. Outbound calls get V3 extended evaluation and are pushed back to AmoCRM; all calls get V2 basic evaluation for the dashboard.

**Tech Stack:** Celery Beat, psycopg2, requests, ffmpeg, existing pipeline (whisper/pyannote/GPT)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `backend/alembic/versions/002_amocrm_calls.py` | Create | Migration: amocrm_calls table |
| `worker/tasks/amocrm_sync.py` | Modify | Add read functions: get_recent_call_events, get_note_details, download_recording |
| `worker/tasks/pipeline.py` | Modify | Refactor: extract `_run_pipeline()`, add `process_session_from_file` task |
| `worker/tasks/amocrm_poll.py` | Create | Polling task + process_amocrm_call task |
| `worker/tasks/celery_app.py` | Modify | Add beat_schedule, include amocrm_poll in task list |

---

### Task 1: Database migration — amocrm_calls table

**Files:**
- Create: `backend/alembic/versions/002_amocrm_calls.py`

- [ ] **Step 1: Create migration file**

```python
"""Add amocrm_calls table for tracking polled calls

Revision ID: 002
Revises: 001
Create Date: 2026-04-16
"""
from typing import Sequence, Union

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""CREATE TABLE amocrm_calls (
        id SERIAL PRIMARY KEY,
        amo_note_id BIGINT UNIQUE NOT NULL,
        lead_id BIGINT NOT NULL,
        contact_phone VARCHAR(20),
        direction VARCHAR(10),
        duration INTEGER,
        recording_url TEXT,
        session_id VARCHAR(64),
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        error_message TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        processed_at TIMESTAMPTZ
    )""")
    op.execute("CREATE INDEX idx_amocrm_calls_status ON amocrm_calls(status)")
    op.execute("CREATE INDEX idx_amocrm_calls_created ON amocrm_calls(created_at)")


def downgrade() -> None:
    op.drop_index("idx_amocrm_calls_status")
    op.drop_index("idx_amocrm_calls_created")
    op.drop_table("amocrm_calls")
```

- [ ] **Step 2: Verify migration file is valid Python**

Run: `python -c "import ast; ast.parse(open('backend/alembic/versions/002_amocrm_calls.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add backend/alembic/versions/002_amocrm_calls.py
git commit -m "feat: add amocrm_calls migration for call polling tracking"
```

---

### Task 2: Add read functions to amocrm_sync.py

**Files:**
- Modify: `worker/tasks/amocrm_sync.py`

- [ ] **Step 1: Add `get_recent_call_events()` function**

Add after the existing `_normalize_phone` function (before `find_lead_by_phone`):

```python
def get_recent_call_events(since_timestamp: int) -> list[dict]:
    """
    Fetch recent call events from AmoCRM since given unix timestamp.
    Returns list of dicts with: note_id, entity_id, entity_type, event_type.
    """
    if not AMOCRM_ACCESS_TOKEN:
        return []

    events = []
    page = 1
    while True:
        try:
            resp = requests.get(
                f"{AMOCRM_BASE_URL}/api/v4/events",
                params={
                    "filter[type][]": ["incoming_call", "outgoing_call"],
                    "filter[created_at][from]": since_timestamp,
                    "limit": 100,
                    "page": page,
                },
                headers=_headers(),
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"AmoCRM events fetch failed: {resp.status_code}")
                break

            data = resp.json()
            items = data.get("_embedded", {}).get("events", [])
            if not items:
                break

            for event in items:
                entity_id = event.get("entity_id")
                entity_type = event.get("entity_type")  # "lead", "contact", etc.
                event_type = event.get("type")  # "incoming_call", "outgoing_call"
                # value_after contains array with note info
                value_after = event.get("value_after", [])
                note_id = None
                for v in value_after:
                    if "note" in v:
                        note_id = v["note"].get("id")
                        break

                if note_id and entity_id:
                    events.append({
                        "note_id": note_id,
                        "entity_id": entity_id,
                        "entity_type": entity_type or "leads",
                        "event_type": event_type,
                    })

            # Check for next page
            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break
            page += 1

        except Exception:
            logger.exception("Error fetching AmoCRM events")
            break

    logger.info(f"Fetched {len(events)} call events from AmoCRM since {since_timestamp}")
    return events
```

- [ ] **Step 2: Add `get_note_details()` function**

Add after `get_recent_call_events`:

```python
def get_note_details(entity_type: str, entity_id: int, note_id: int) -> dict | None:
    """Fetch full note details from AmoCRM. Returns note dict or None."""
    if not AMOCRM_ACCESS_TOKEN:
        return None

    # Normalize entity type to plural for API path
    entity_path = entity_type if entity_type.endswith("s") else f"{entity_type}s"

    try:
        resp = requests.get(
            f"{AMOCRM_BASE_URL}/api/v4/{entity_path}/{entity_id}/notes/{note_id}",
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"Failed to get note {note_id}: {resp.status_code}")
        return None
    except Exception:
        logger.exception(f"Error fetching note {note_id}")
        return None
```

- [ ] **Step 3: Add `download_recording()` function**

Add after `get_note_details`:

```python
def download_recording(url: str, dest_path: str) -> bool:
    """Download call recording file. Returns True on success."""
    if not url:
        return False

    try:
        resp = requests.get(url, stream=True, timeout=60)
        if resp.status_code == 404:
            logger.warning(f"Recording not found (expired?): {url}")
            return False
        resp.raise_for_status()

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        file_size = os.path.getsize(dest_path)
        logger.info(f"Downloaded recording: {dest_path} ({file_size / 1024:.0f} KB)")
        return True

    except Exception:
        logger.exception(f"Failed to download recording from {url}")
        return False
```

- [ ] **Step 4: Verify syntax**

Run: `python -c "import ast; ast.parse(open('worker/tasks/amocrm_sync.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/amocrm_sync.py
git commit -m "feat: add AmoCRM read functions for call polling"
```

---

### Task 3: Refactor pipeline.py — extract _run_pipeline and add process_session_from_file

**Files:**
- Modify: `worker/tasks/pipeline.py`

- [ ] **Step 1: Extract `_run_pipeline()` from `process_session()`**

Replace the try block inside `process_session` (lines 301-399) with a call to a new shared function. Add this function before `process_session`:

```python
def _run_pipeline(task, session_id: str, audio_path: str, config: dict, company_config: dict, scenario: dict | None, session_meta: dict):
    """
    Shared pipeline: transcription → diarization → sentiment → quality → save → AmoCRM.
    Called by both process_session (browser recordings) and process_session_from_file (AmoCRM calls).
    """
    # === 2. Transcription ===
    task.update_state(state="PROGRESS", meta={"step": "transcribing", "progress": 15})
    word_boost = get_word_boost(company_config)
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

    save_results(session_id, "transcript", transcript_with_speakers)

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

    quality_report = assess_quality(
        transcript_with_speakers,
        sentiment_results,
        protocol=quality_protocol,
        custom_prompt=quality_prompt,
        criteria_config=quality_criteria,
        use_extended_schema=use_extended,
    )
    save_results(session_id, "quality", quality_report)

    # === 7. Auto-save speaker roles ===
    speaker_roles = quality_report.get("speaker_roles")
    if speaker_roles:
        _save_speaker_roles(session_id, speaker_roles)

    # === 8. Push to AmoCRM (if extended) ===
    if use_extended:
        task.update_state(state="PROGRESS", meta={"step": "amocrm_sync", "progress": 95})
        logger.info(f"[{session_id}] Step 8: AmoCRM sync...")
        _push_to_amocrm(session_id, quality_report, session_meta, audio_path)

    return {
        "transcript_with_speakers": transcript_with_speakers,
        "quality_report": quality_report,
        "use_extended": use_extended,
    }
```

- [ ] **Step 2: Simplify `process_session` to call `_run_pipeline`**

Replace the body of `process_session` (keep the function signature and decorator):

```python
@app.task(bind=True, queue="transcription", name="pipeline.process_session")
def process_session(self, session_id: str, config: dict | None = None):
    """Full pipeline for browser-recorded sessions (WebM chunks)."""
    config = config or {}
    logger.info(f"[{session_id}] Starting processing pipeline...")
    start_time = datetime.now(timezone.utc)
    update_session_status(session_id, "processing")

    session_meta = _get_session_metadata(session_id) if not config.get("company_id") else {}
    company_id = config.get("company_id") or session_meta.get("company_id")
    scenario_id = config.get("scenario_id") or session_meta.get("scenario_id")
    company_config = load_company_config(company_id)
    scenario = get_scenario(company_config, scenario_id)
    logger.info(f"[{session_id}] Company: {company_config.get('name', company_id)}, Scenario: {scenario.get('name') if scenario else 'default'}")

    try:
        # Step 1: Merge chunks
        self.update_state(state="PROGRESS", meta={"step": "merging", "progress": 5})
        logger.info(f"[{session_id}] Step 1: Merging audio chunks...")
        audio_path = merge_chunks(session_id)

        # Steps 2-8: shared pipeline
        result = _run_pipeline(self, session_id, audio_path, config, company_config, scenario, session_meta)

        # Done
        finished_at = datetime.now(timezone.utc)
        audio_duration = _get_audio_duration(audio_path)
        processing_duration = (finished_at - start_time).total_seconds()
        logger.info(f"[{session_id}] Audio duration: {audio_duration:.1f}s, processing time: {processing_duration:.1f}s")
        update_session_status(
            session_id, "completed",
            finished_at=finished_at,
            duration_seconds=audio_duration or processing_duration,
        )
        logger.info(f"[{session_id}] Pipeline completed successfully!")

        transcript_with_speakers = result["transcript_with_speakers"]
        return {
            "session_id": session_id,
            "status": "completed",
            "segments_count": len(transcript_with_speakers),
            "speakers_count": len(set(s.get("speaker", "") for s in transcript_with_speakers)),
            "quality_score": result["quality_report"].get("overall_score"),
        }

    except Exception as e:
        update_session_status(session_id, "failed", finished_at=datetime.now(timezone.utc))
        logger.exception(f"[{session_id}] Pipeline failed: {e}")
        raise
```

- [ ] **Step 3: Add `process_session_from_file` task**

Add after `process_session`:

```python
@app.task(bind=True, queue="transcription", name="pipeline.process_session_from_file")
def process_session_from_file(self, session_id: str, audio_path: str, config: dict | None = None):
    """Pipeline for pre-existing audio files (AmoCRM calls, uploaded files)."""
    config = config or {}
    logger.info(f"[{session_id}] Starting file-based pipeline for {audio_path}...")
    start_time = datetime.now(timezone.utc)
    update_session_status(session_id, "processing")

    session_meta = _get_session_metadata(session_id)
    company_id = config.get("company_id") or session_meta.get("company_id")
    scenario_id = config.get("scenario_id") or session_meta.get("scenario_id")
    company_config = load_company_config(company_id)
    scenario = get_scenario(company_config, scenario_id)
    logger.info(f"[{session_id}] Company: {company_config.get('name', company_id)}, Scenario: {scenario.get('name') if scenario else 'default'}")

    try:
        result = _run_pipeline(self, session_id, audio_path, config, company_config, scenario, session_meta)

        finished_at = datetime.now(timezone.utc)
        audio_duration = _get_audio_duration(audio_path)
        processing_duration = (finished_at - start_time).total_seconds()
        logger.info(f"[{session_id}] Audio duration: {audio_duration:.1f}s, processing time: {processing_duration:.1f}s")
        update_session_status(
            session_id, "completed",
            finished_at=finished_at,
            duration_seconds=audio_duration or processing_duration,
        )
        logger.info(f"[{session_id}] File-based pipeline completed!")

        transcript_with_speakers = result["transcript_with_speakers"]
        return {
            "session_id": session_id,
            "status": "completed",
            "segments_count": len(transcript_with_speakers),
            "speakers_count": len(set(s.get("speaker", "") for s in transcript_with_speakers)),
            "quality_score": result["quality_report"].get("overall_score"),
        }

    except Exception as e:
        update_session_status(session_id, "failed", finished_at=datetime.now(timezone.utc))
        logger.exception(f"[{session_id}] File-based pipeline failed: {e}")
        raise
```

- [ ] **Step 4: Verify syntax**

Run: `python -c "import ast; ast.parse(open('worker/tasks/pipeline.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/pipeline.py
git commit -m "refactor: extract _run_pipeline, add process_session_from_file entry point"
```

---

### Task 4: Create amocrm_poll.py — polling and processing tasks

**Files:**
- Create: `worker/tasks/amocrm_poll.py`

- [ ] **Step 1: Create the polling module**

```python
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
MAX_RETRIES = 3


def _get_sync_db_url() -> str:
    url = os.getenv("DATABASE_URL_SYNC", "") or os.getenv("DATABASE_URL", "")
    url = url.replace("postgresql+psycopg2://", "postgresql://")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    return url


def _get_last_poll_timestamp() -> int:
    """Get unix timestamp of the most recent polled call, or fallback to lookback window."""
    db_url = _get_sync_db_url()
    if not db_url:
        return int((datetime.now(timezone.utc) - timedelta(hours=INITIAL_LOOKBACK_HOURS)).timestamp())

    try:
        conn = psycopg2.connect(db_url)
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(created_at) FROM amocrm_calls")
                row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return int(row[0].timestamp())
    except Exception:
        logger.exception("Failed to get last poll timestamp")

    return int((datetime.now(timezone.utc) - timedelta(hours=INITIAL_LOOKBACK_HOURS)).timestamp())


def _insert_call(amo_note_id: int, lead_id: int, phone: str, direction: str,
                 duration: int, recording_url: str) -> int | None:
    """Insert new call into amocrm_calls. Returns row ID or None if duplicate."""
    db_url = _get_sync_db_url()
    if not db_url:
        return None

    try:
        conn = psycopg2.connect(db_url)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO amocrm_calls
                       (amo_note_id, lead_id, contact_phone, direction, duration, recording_url)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (amo_note_id) DO NOTHING
                       RETURNING id""",
                    (amo_note_id, lead_id, phone, direction, duration, recording_url),
                )
                row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        logger.exception(f"Failed to insert amocrm_call for note {amo_note_id}")
        return None


def _update_call_status(call_id: int, status: str, **kwargs):
    """Update amocrm_calls row status and optional fields."""
    db_url = _get_sync_db_url()
    if not db_url:
        return

    sets = ["status = %s"]
    values = [status]
    for key, value in kwargs.items():
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


def _get_retryable_calls() -> list[dict]:
    """Get failed calls with retry_count < MAX_RETRIES."""
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
                       WHERE status = 'failed' AND retry_count < %s
                       ORDER BY created_at
                       LIMIT 10""",
                    (MAX_RETRIES,),
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
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"ffmpeg conversion failed: {result.stderr[:500]}")
        return False
    return True


@app.task(name="amocrm_poll.poll_amocrm_calls")
def poll_amocrm_calls():
    """Celery Beat task: poll AmoCRM for new call recordings every 5 minutes."""
    logger.info("Polling AmoCRM for new calls...")
    since = _get_last_poll_timestamp()
    events = get_recent_call_events(since)

    new_count = 0
    for event in events:
        note_id = event["note_id"]
        entity_id = event["entity_id"]
        entity_type = event["entity_type"]
        event_type = event["event_type"]

        # Fetch full note details
        note = get_note_details(entity_type, entity_id, note_id)
        if not note:
            continue

        note_type = note.get("note_type", "")
        params = note.get("params", {})
        recording_url = params.get("link", "")
        phone = params.get("phone", "")
        duration = params.get("duration", 0)
        source = params.get("source", "")

        # Skip our own notes
        if source == "Rogov AI":
            continue

        # Skip notes without recordings
        if not recording_url:
            logger.debug(f"Skipping note {note_id}: no recording URL")
            continue

        # Determine direction
        direction = "out" if note_type == "call_out" or event_type == "outgoing_call" else "in"

        # Determine lead_id: entity_id if entity is a lead, otherwise search by phone
        lead_id = entity_id if entity_type in ("lead", "leads") else 0

        # Insert (deduplicated by amo_note_id)
        call_id = _insert_call(
            amo_note_id=note_id,
            lead_id=lead_id,
            phone=phone,
            direction=direction,
            duration=duration,
            recording_url=recording_url,
        )
        if call_id:
            new_count += 1
            process_amocrm_call.delay(call_id)

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
                              duration, recording_url, retry_count
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

    amo_note_id, lead_id, phone, direction, duration, recording_url, retry_count = row
    logger.info(f"[amo:{amo_note_id}] Processing call (direction={direction}, phone={phone})")

    _update_call_status(call_id, "downloading", retry_count=retry_count + 1)

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
        _update_call_status(call_id, "failed", error_message="download_failed")
        logger.warning(f"[amo:{amo_note_id}] Recording download failed")
        return

    # === 2. Convert to WAV 16kHz mono ===
    wav_path = str(call_dir / "full.wav")
    if original_ext == "wav":
        # Still convert to normalize format
        pass
    if not _convert_to_wav(original_path, wav_path):
        _update_call_status(call_id, "failed", error_message="conversion_failed")
        return

    # === 3. Create session ===
    # Determine scenario: outbound → outbound_residential
    scenario_id = "outbound_residential" if direction == "out" else None

    session_metadata = {
        "company_id": "realestate",
        "source": "amocrm",
        "amo_note_id": amo_note_id,
        "lead_id": lead_id if lead_id else None,
        "phone": phone,
        "direction": direction,
        "scenario_id": scenario_id,
    }
    session_id = _create_session(session_metadata)
    _update_call_status(call_id, "processing", session_id=session_id)

    # === 4. Run pipeline ===
    try:
        from tasks.pipeline import process_session_from_file
        result = process_session_from_file(session_id, wav_path, {
            "company_id": "realestate",
            "scenario_id": scenario_id,
        })

        _update_call_status(
            call_id, "completed",
            processed_at=datetime.now(timezone.utc),
        )
        logger.info(f"[amo:{amo_note_id}] Pipeline completed, session {session_id}")
        return result

    except Exception as e:
        _update_call_status(call_id, "failed", error_message=str(e)[:500])
        logger.exception(f"[amo:{amo_note_id}] Pipeline failed: {e}")
        raise
```

- [ ] **Step 2: Verify syntax**

Run: `python -c "import ast; ast.parse(open('worker/tasks/amocrm_poll.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add worker/tasks/amocrm_poll.py
git commit -m "feat: add AmoCRM call polling and processing tasks"
```

---

### Task 5: Update celery_app.py — beat schedule and task registration

**Files:**
- Modify: `worker/tasks/celery_app.py`

- [ ] **Step 1: Add amocrm_poll to includes and add beat_schedule**

In the `include` list, add `"tasks.amocrm_poll"`:

```python
include=[
    "tasks.pipeline",
    "tasks.transcribe",
    "tasks.diarize",
    "tasks.sentiment",
    "tasks.quality",
    "tasks.amocrm_poll",
],
```

Add to `app.conf.update(...)`:

```python
    # Celery Beat — periodic tasks
    beat_schedule={
        "poll-amocrm-calls": {
            "task": "amocrm_poll.poll_amocrm_calls",
            "schedule": 300,  # every 5 minutes
        },
    },
```

- [ ] **Step 2: Verify syntax**

Run: `python -c "import ast; ast.parse(open('worker/tasks/celery_app.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add worker/tasks/celery_app.py
git commit -m "feat: register amocrm_poll tasks and add beat schedule"
```

---

### Task 6: Integration smoke test

- [ ] **Step 1: Verify all modules import cleanly**

Run from the worker directory:
```bash
cd /root/projects/realestate/worker && python -c "
from tasks.celery_app import app
from tasks.amocrm_poll import poll_amocrm_calls, process_amocrm_call
from tasks.amocrm_sync import get_recent_call_events, get_note_details, download_recording
from tasks.pipeline import process_session, process_session_from_file, _run_pipeline
print('All imports OK')
print('Beat schedule:', app.conf.beat_schedule)
print('Registered tasks will be available after worker starts')
"
```

Expected: `All imports OK` with beat schedule showing `poll-amocrm-calls`.

- [ ] **Step 2: Verify migration file chain**

```bash
python -c "
import ast
for f in ['backend/alembic/versions/001_initial.py', 'backend/alembic/versions/002_amocrm_calls.py']:
    m = ast.parse(open(f).read())
    for node in ast.walk(m):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if hasattr(t, 'id') and t.id in ('revision', 'down_revision'):
                    val = node.value
                    if isinstance(val, ast.Constant):
                        print(f'{f}: {t.id} = {val.value}')
"
```

Expected: 002's `down_revision` is `"001"`.

- [ ] **Step 3: Final commit with all files**

```bash
git add -A
git status
# If any unstaged changes remain, add them
git commit -m "feat: complete AmoCRM call polling integration"
```
