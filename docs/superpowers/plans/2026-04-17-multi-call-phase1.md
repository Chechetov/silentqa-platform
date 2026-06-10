# Multi-call Context — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Phase 1 of multi-call evaluation: short-call filter, manual reprocess endpoint, prior-context aggregation, context-aware evaluation (V4 schema), and forward-looking next-call plan — delivered as a second AmoCRM note.

**Architecture:** Existing `_run_pipeline` gains a short-call gate at entry and a `_build_prior_context` step before the LLM. `assess_quality` gains a V4 schema path that accepts `prior_context`. A new `plan_next_call` performs a second LLM call for forward recommendations. A new FastAPI route exposes manual reprocess; a small admin form wires the UI.

**Tech Stack:** Python 3.11 (Celery workers, FastAPI backend), OpenAI Responses API (GPT-5.4 structured output), psycopg2 (sync DB), PostgreSQL, vanilla JS frontend, pytest (newly introduced).

**Spec:** `docs/superpowers/specs/2026-04-17-short-call-filter-and-reprocess-design.md`

---

## File Structure Overview

**New files:**
- `worker/tasks/prior_context.py` — pure helpers to build `prior_context` dict from DB + filesystem
- `worker/tests/__init__.py`, `worker/tests/conftest.py`, `worker/tests/test_prior_context.py`, `worker/tests/test_short_call.py`, `worker/tests/test_quality_schemas.py`
- `backend/app/routes/amocrm.py` — FastAPI router for `/api/amocrm/reprocess`
- `worker/pytest.ini` — pytest config

**Modified files:**
- `worker/tasks/pipeline.py` — short-call gate, prior_context hookup, plan hookup, metadata updates
- `worker/tasks/quality.py` — V4 schema, `assess_quality` signature (accept `prior_context`), `NEXT_CALL_PLAN_SCHEMA`, `plan_next_call` function
- `worker/tasks/amocrm_sync.py` — `format_enriched_note` short-call branch, `list_call_notes_on_entity`, `format_next_call_plan`, `create_plain_note`
- `worker/tasks/amocrm_poll.py` — no changes expected; reprocess endpoint calls `process_amocrm_call` directly
- `worker/requirements.txt` — add `pytest`, `pytest-mock`
- `backend/app/main.py` — register `amocrm` router
- `backend/static/index.html` — new nav link
- `backend/static/app.js` — new `#reprocess` route + form

---

## Task 0: Bootstrap pytest in worker

**Files:**
- Create: `worker/pytest.ini`
- Create: `worker/tests/__init__.py`
- Create: `worker/tests/conftest.py`
- Modify: `worker/requirements.txt`

- [ ] **Step 1: Add pytest to requirements**

Append to `worker/requirements.txt`:

```
pytest==8.3.4
pytest-mock==3.14.0
```

- [ ] **Step 2: Install**

Run: `source .venv/bin/activate && pip install pytest==8.3.4 pytest-mock==3.14.0`
Expected: successful install, both importable.

- [ ] **Step 3: Create pytest.ini**

Write `worker/pytest.ini`:

```ini
[pytest]
testpaths = tests
python_files = test_*.py
pythonpath = .
addopts = -v
```

- [ ] **Step 4: Create tests package**

Write empty `worker/tests/__init__.py`:

```python
```

Write `worker/tests/conftest.py`:

```python
"""Shared fixtures for worker tests."""
import sys
from pathlib import Path

# Make `tasks` package importable during tests
sys.path.insert(0, str(Path(__file__).parent.parent))
```

- [ ] **Step 5: Sanity-check pytest discovery**

Run: `cd worker && source ../.venv/bin/activate && pytest --collect-only`
Expected: `no tests ran in X.XXs` (no errors, just no tests yet).

- [ ] **Step 6: Commit**

```bash
git add worker/pytest.ini worker/tests/__init__.py worker/tests/conftest.py worker/requirements.txt
git commit -m "chore: bootstrap pytest in worker"
```

---

## Task 1: Short-call filter — pure helpers and schema

**Files:**
- Modify: `worker/tasks/pipeline.py` (add constants + builder helper)
- Create: `worker/tests/test_short_call.py`

**Design note:** we extract a tiny helper `_build_short_call_report(duration)` so tests don't need to touch the pipeline. The actual pipeline integration happens in Task 2.

- [ ] **Step 1: Write failing test for the short-call report builder**

Write `worker/tests/test_short_call.py`:

```python
"""Tests for the short-call report builder."""
from tasks.pipeline import _build_short_call_report, SHORT_CALL_THRESHOLD_SEC


def test_threshold_is_twenty_seconds():
    assert SHORT_CALL_THRESHOLD_SEC == 20


def test_short_call_report_shape():
    report = _build_short_call_report(12.3)
    assert report["overall_score"] is None
    assert report["skip_reason"] == "too_short"
    assert report["audio_duration"] == 12.3
    assert "12с" in report["brief_summary"]
    assert "автоответчик" in report["brief_summary"].lower()
    assert report["score_version"] == "short_call_v1"


def test_short_call_report_integer_seconds_in_text():
    report = _build_short_call_report(5.9)
    # Text must use integer seconds; 5.9s → "5с"
    assert "5с" in report["brief_summary"]
    assert "6с" not in report["brief_summary"]
```

- [ ] **Step 2: Run test, verify it fails**

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_short_call.py -v`
Expected: ImportError / AttributeError — `_build_short_call_report` not defined.

- [ ] **Step 3: Add constant and helper to pipeline.py**

In `worker/tasks/pipeline.py`, after the `logger = logging.getLogger(__name__)` line (around line 28), add:

```python
SHORT_CALL_THRESHOLD_SEC = 20


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
```

- [ ] **Step 4: Run test, verify it passes**

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_short_call.py -v`
Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/pipeline.py worker/tests/test_short_call.py
git commit -m "feat(quality): add short-call report builder (<20s threshold)"
```

---

## Task 2: Short-call gate in _run_pipeline + note short-circuit

**Files:**
- Modify: `worker/tasks/pipeline.py` (insert gate in `_run_pipeline` before step 2)
- Modify: `worker/tasks/amocrm_sync.py` (short-circuit in `format_enriched_note`)

**Design note:** the gate runs `_get_audio_duration` once, reuses the value later. If ffprobe fails (`0.0`), we fall through to the full pipeline — we don't swallow potentially-real audio.

- [ ] **Step 1: Modify `_run_pipeline` to insert short-call gate**

In `worker/tasks/pipeline.py`, locate the start of `_run_pipeline` (currently beginning around line 284). Replace the function body's first lines with:

```python
def _run_pipeline(task, session_id: str, audio_path: str, config: dict, company_config: dict, scenario: dict | None, session_meta: dict):
    """
    Shared pipeline: transcription → diarization → sentiment → quality → save → AmoCRM.
    Called by both process_session (browser recordings) and process_session_from_file (AmoCRM calls).
    """
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

    # === 2. Transcription ===
    task.update_state(state="PROGRESS", meta={"step": "transcribing", "progress": 15})
    # ... rest of existing function body unchanged ...
```

Keep the existing remainder of `_run_pipeline` unchanged (transcribe, diarize, sentiment, quality, speaker-roles, push-to-amocrm, return). The gate adds an early-exit at the top; nothing else moves.

- [ ] **Step 2: Add short-circuit to `format_enriched_note`**

In `worker/tasks/amocrm_sync.py`, at the very beginning of `format_enriched_note` (currently around line 358, right after the docstring), insert:

```python
def format_enriched_note(quality_report: dict, session_id: str, responsible_user_id: int = 0) -> str:
    """
    Формирует текст обогащённого примечания для AmoCRM.
    Формат адаптируется под тип звонка (brush-off / продуктивный / короткий).
    """
    # Short-circuit: calls too short for meaningful evaluation
    if quality_report.get("skip_reason") == "too_short":
        secs = int(quality_report.get("audio_duration", 0))
        return (
            f"⚠️ Короткий звонок ({secs}с).\n\n"
            f"Оценка не проводилась — вероятно, автоответчик "
            f"или клиент не ответил."
        )

    score = quality_report.get("overall_score")
    # ... rest of existing function unchanged ...
```

- [ ] **Step 3: Smoke-check that pipeline imports cleanly**

Run: `cd worker && source ../.venv/bin/activate && python -c "from tasks.pipeline import _run_pipeline, _build_short_call_report, SHORT_CALL_THRESHOLD_SEC; print('ok')"`
Expected: `ok`.

Run: `cd worker && source ../.venv/bin/activate && python -c "from tasks.amocrm_sync import format_enriched_note; print(format_enriched_note({'skip_reason': 'too_short', 'audio_duration': 7.4}, 'sess-1'))"`
Expected: output contains `⚠️ Короткий звонок (7с).` and "автоответчик".

- [ ] **Step 4: Commit**

```bash
git add worker/tasks/pipeline.py worker/tasks/amocrm_sync.py
git commit -m "feat(pipeline): gate short calls before LLM and use short note format"
```

---

## Task 3: AmoCRM helpers for reprocess

**Files:**
- Modify: `worker/tasks/amocrm_sync.py` (add `list_call_notes_on_entity`, `get_lead_with_contacts`)

These helpers are used by the reprocess endpoint. They have no cross-dependencies, so it's safe to land them separately.

- [ ] **Step 1: Add helpers to `amocrm_sync.py`**

In `worker/tasks/amocrm_sync.py`, after `get_note_details` (currently ends around line 207), append:

```python
def get_lead_with_contacts(lead_id: int) -> dict | None:
    """Fetch a lead with linked contacts. Returns dict with 'contact_ids' key or None."""
    if not _get_access_token():
        return None
    try:
        resp = requests.get(
            f"{AMOCRM_BASE_URL}/api/v4/leads/{lead_id}",
            params={"with": "contacts"},
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"Failed to fetch lead {lead_id}: {resp.status_code}")
            return None
        data = resp.json()
        contact_ids = [c["id"] for c in data.get("_embedded", {}).get("contacts", [])]
        return {"lead_id": lead_id, "contact_ids": contact_ids, "raw": data}
    except Exception:
        logger.exception(f"Error fetching lead {lead_id}")
        return None


def list_call_notes_on_entity(entity_type: str, entity_id: int) -> list[dict]:
    """
    List call_in/call_out notes on a given entity (lead or contact).
    Returns raw note dicts from AmoCRM (id, note_type, params, etc.). Empty list on 204/errors.
    """
    if not _get_access_token():
        return []
    entity_path = entity_type if entity_type.endswith("s") else f"{entity_type}s"
    try:
        resp = requests.get(
            f"{AMOCRM_BASE_URL}/api/v4/{entity_path}/{entity_id}/notes",
            params={
                "limit": 100,
                "filter[note_type][]": ["call_in", "call_out"],
            },
            headers=_headers(),
            timeout=15,
        )
        if resp.status_code == 204:
            return []
        if resp.status_code != 200:
            logger.warning(f"Failed to list notes on {entity_type}/{entity_id}: {resp.status_code}")
            return []
        return resp.json().get("_embedded", {}).get("notes", [])
    except Exception:
        logger.exception(f"Error listing notes on {entity_type}/{entity_id}")
        return []
```

- [ ] **Step 2: Smoke-check import and call**

Run:
```bash
cd worker && source ../.venv/bin/activate && \
  set -a && source ../.env && set +a && \
  python -c "from tasks.amocrm_sync import get_lead_with_contacts, list_call_notes_on_entity; \
             r = get_lead_with_contacts(11425275); \
             print('contact_ids:', r['contact_ids']); \
             notes = list_call_notes_on_entity('contacts', r['contact_ids'][0]); \
             print('call notes:', [(n['id'], n.get('params', {}).get('duration')) for n in notes])"
```

Expected: prints `contact_ids: [11099691]` and two call notes `(46048433, 5)` and `(46240441, 1050)`.

- [ ] **Step 3: Commit**

```bash
git add worker/tasks/amocrm_sync.py
git commit -m "feat(amocrm): helpers to fetch lead contacts and call notes"
```

---

## Task 4: Reprocess endpoint + reset logic

**Files:**
- Modify: `worker/tasks/amocrm_poll.py` (expose `reset_call_for_reprocess` helper)
- Create: `backend/app/routes/amocrm.py`
- Modify: `backend/app/main.py` (register router)
- Modify: `backend/requirements.txt` (add celery/redis if missing)

**Design note:** the endpoint lives in the backend process. To enqueue into the worker's Celery broker, we use `send_task` with the string name — no worker imports from backend.

- [ ] **Step 1: Add reset helper to `amocrm_poll.py`**

In `worker/tasks/amocrm_poll.py`, after `_update_call_status` (ends around line 112), append:

```python
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
```

- [ ] **Step 2: Verify celery and redis are in backend/requirements.txt**

Run: `grep -iE "^celery|^redis" backend/requirements.txt`
If `celery` is not present, append to `backend/requirements.txt`:

```
celery[redis]==5.4.0
```

Then run: `source .venv/bin/activate && pip install 'celery[redis]==5.4.0'` (idempotent if already installed).

Expected: both celery and redis importable.

- [ ] **Step 3: Create backend router**

Write `backend/app/routes/amocrm.py`:

```python
"""Manual AmoCRM reprocess: find call notes on a lead and enqueue them."""
import logging
import os
import sys
from pathlib import Path

from celery import Celery
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# Make worker/tasks importable (shared codebase for AmoCRM helpers and DB ops)
WORKER_PATH = Path(__file__).resolve().parents[3] / "worker"
if str(WORKER_PATH) not in sys.path:
    sys.path.insert(0, str(WORKER_PATH))

from tasks.amocrm_sync import get_lead_with_contacts, list_call_notes_on_entity
from tasks.amocrm_poll import _insert_call, reset_call_for_reprocess

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/amocrm", tags=["amocrm"])

_redis_url = os.getenv("REDIS_URL", "redis://localhost:6381/0")
_celery_app = Celery("reprocess_dispatcher", broker=_redis_url, backend=_redis_url)
_celery_app.conf.update(task_serializer="json", accept_content=["json"])


class ReprocessRequest(BaseModel):
    lead_id: int
    force: bool = False


class ReprocessItem(BaseModel):
    call_id: int | None = None
    note_id: int
    duration: int | None = None
    direction: str | None = None


class ReprocessResponse(BaseModel):
    lead_id: int
    queued: list[ReprocessItem]
    already_processed: list[ReprocessItem]
    missing_recording: list[ReprocessItem]


def _enqueue_process_call(call_id: int) -> str:
    """Send the process_amocrm_call task to the worker broker."""
    res = _celery_app.send_task("amocrm_poll.process_amocrm_call", args=[call_id])
    return res.id


@router.post("/reprocess", response_model=ReprocessResponse)
async def reprocess(body: ReprocessRequest):
    lead = get_lead_with_contacts(body.lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found in AmoCRM")

    entities: list[tuple[str, int]] = [("leads", body.lead_id)]
    entities += [("contacts", cid) for cid in lead["contact_ids"]]

    queued: list[ReprocessItem] = []
    already: list[ReprocessItem] = []
    missing: list[ReprocessItem] = []

    for entity_type, entity_id in entities:
        notes = list_call_notes_on_entity(entity_type, entity_id)
        for note in notes:
            note_id = note["id"]
            params = note.get("params", {}) or {}
            source = params.get("source", "")
            recording_url = params.get("link", "")
            phone_raw = params.get("phone", "")
            phone = phone_raw.split(",")[0].strip()
            duration = params.get("duration", 0)
            note_type = note.get("note_type", "")
            direction = "out" if note_type == "call_out" else "in"

            if source == "Rogov AI":
                continue
            if not recording_url:
                missing.append(ReprocessItem(note_id=note_id, duration=duration, direction=direction))
                continue

            # We always know lead_id from the reprocess request — even when the call note
            # is attached to a contact. Persist it so prior_context lookups work correctly.
            call_id = _insert_call(
                amo_note_id=note_id,
                lead_id=body.lead_id,
                phone=phone,
                direction=direction,
                duration=duration,
                recording_url=recording_url,
                responsible_user_id=note.get("responsible_user_id", 0),
            )

            if call_id is None:
                # Already in DB (ON CONFLICT DO NOTHING returned no row)
                if body.force:
                    # We don't know the row id without another query; use amo_note_id to look it up
                    from tasks.amocrm_poll import _get_sync_db_url
                    import psycopg2
                    conn = psycopg2.connect(_get_sync_db_url())
                    try:
                        with conn, conn.cursor() as cur:
                            cur.execute(
                                "SELECT id FROM amocrm_calls WHERE amo_note_id=%s",
                                (note_id,),
                            )
                            row = cur.fetchone()
                    finally:
                        conn.close()
                    if row and reset_call_for_reprocess(row[0]):
                        _enqueue_process_call(row[0])
                        queued.append(ReprocessItem(call_id=row[0], note_id=note_id, duration=duration, direction=direction))
                    else:
                        already.append(ReprocessItem(note_id=note_id, duration=duration, direction=direction))
                else:
                    already.append(ReprocessItem(note_id=note_id, duration=duration, direction=direction))
            else:
                _enqueue_process_call(call_id)
                queued.append(ReprocessItem(call_id=call_id, note_id=note_id, duration=duration, direction=direction))

    return ReprocessResponse(
        lead_id=body.lead_id,
        queued=queued,
        already_processed=already,
        missing_recording=missing,
    )
```

- [ ] **Step 4: Register router in `backend/app/main.py`**

Modify `backend/app/main.py`. Change the import line (currently line 15):

```python
from app.routes import chunks, sessions, companies, transcripts, analysis, managers, webhooks
```

To:

```python
from app.routes import chunks, sessions, companies, transcripts, analysis, managers, webhooks, amocrm
```

And add after `app.include_router(webhooks.router)` (currently line 88):

```python
app.include_router(amocrm.router)
```

- [ ] **Step 5: Restart backend and smoke-test**

Run (foreground smoke; stop with Ctrl+C when verified):

```bash
cd backend && source ../.venv/bin/activate && \
  set -a && source ../.env && set +a && \
  uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload &
sleep 3
curl -s -X POST http://127.0.0.1:8765/api/amocrm/reprocess \
  -H 'Content-Type: application/json' \
  -d '{"lead_id": 11425275, "force": false}' | head -60
```

Expected: JSON response containing `queued` array with 1 or 2 call items for notes `46048433`/`46240441`. (Exact count depends on whether AmoCRM polling has since caught up.) If both already in DB — empty `queued`, both in `already_processed`; retry with `force: true`.

Kill the background uvicorn: `pkill -f "uvicorn app.main:app.*8765"` (only the ad-hoc instance).

- [ ] **Step 6: Commit**

```bash
git add worker/tasks/amocrm_poll.py backend/app/routes/amocrm.py backend/app/main.py backend/requirements.txt
git commit -m "feat(backend): POST /api/amocrm/reprocess to re-queue historical calls"
```

---

## Task 5: Admin form for reprocess

**Files:**
- Modify: `backend/static/index.html` (new nav link)
- Modify: `backend/static/app.js` (route + renderer)

- [ ] **Step 1: Add nav link to `index.html`**

In `backend/static/index.html`, inside `<ul class="nav-links">`, after the `#upload` `<li>` (currently around line 26-29), insert:

```html
<li><a href="#reprocess" data-route="reprocess" class="nav-link">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><polyline points="23 20 23 14 17 14"/><path d="M20.49 9A9 9 0 005.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 013.51 15"/></svg>
  <span>Повторный прогон</span>
</a></li>
```

- [ ] **Step 2: Add route handler and renderer to `app.js`**

In `backend/static/app.js`, locate the router dispatch (grep for `data-route` handling or the `switch (route)` block). Add a case for `'reprocess'` that calls a new `renderReprocess()` function.

If you can't locate it exactly, search for the first `async function render` definition — most route functions live next to each other. Append this function at the end of the file (before any closing IIFE if present):

```javascript
async function renderReprocess() {
  const app = document.getElementById('app');
  app.innerHTML = `
    <div class="page-header">
      <h1>Повторный прогон звонков</h1>
      <p class="page-subtitle">Укажите ID сделки — подтянем все записанные звонки с её контактов и прогоним через оценку.</p>
    </div>
    <div class="card" style="max-width:600px">
      <form id="reprocessForm" style="display:flex;flex-direction:column;gap:12px">
        <label style="display:flex;flex-direction:column;gap:6px">
          <span>ID сделки AmoCRM</span>
          <input type="number" id="leadIdInput" required placeholder="11425275" style="padding:8px;border-radius:6px;border:1px solid var(--border)">
        </label>
        <label style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="forceInput">
          <span>Перезапустить уже обработанные</span>
        </label>
        <button type="submit" class="btn btn-primary" style="align-self:flex-start">Запустить</button>
      </form>
      <div id="reprocessResult" style="margin-top:16px"></div>
    </div>
  `;

  document.getElementById('reprocessForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const leadId = parseInt(document.getElementById('leadIdInput').value, 10);
    const force = document.getElementById('forceInput').checked;
    const resultEl = document.getElementById('reprocessResult');
    resultEl.innerHTML = '<em>Отправляем запрос…</em>';
    try {
      const resp = await fetch('/api/amocrm/reprocess', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({lead_id: leadId, force}),
      });
      if (!resp.ok) {
        const errText = await resp.text();
        resultEl.innerHTML = `<div style="color:#f85149">Ошибка ${resp.status}: ${escapeHtml(errText.slice(0, 200))}</div>`;
        return;
      }
      const data = await resp.json();
      const rows = (title, items, color) => {
        if (!items || items.length === 0) return '';
        const listItems = items.map(it => `<li>note ${it.note_id}${it.duration != null ? ` (${it.duration}s)` : ''}${it.call_id != null ? ` → call_id ${it.call_id}` : ''}</li>`).join('');
        return `<div style="margin-top:8px"><strong style="color:${color}">${title} (${items.length}):</strong><ul>${listItems}</ul></div>`;
      };
      resultEl.innerHTML =
        rows('В очередь поставлено', data.queued, '#4CAF50') +
        rows('Уже были обработаны', data.already_processed, '#58a6ff') +
        rows('Без записи (скипнуто)', data.missing_recording, '#d29922');
    } catch (err) {
      resultEl.innerHTML = `<div style="color:#f85149">Сбой: ${escapeHtml(err.message)}</div>`;
    }
  });
}
```

Then locate the dispatcher (search `app.js` for `'upload'`, `'companies'`, or similar route strings). Add a matching branch for `'reprocess'`. The existing pattern is likely something like:

```javascript
if (route === 'upload') return renderUpload();
if (route === 'companies') return renderCompanies();
```

Add alongside: `if (route === 'reprocess') return renderReprocess();`.

- [ ] **Step 3: Manual browser smoke-test**

Run (background):

```bash
cd backend && source ../.venv/bin/activate && \
  set -a && source ../.env && set +a && \
  uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

In a browser, open `http://127.0.0.1:8765/` (basic auth prompts — use `AUTH_USERNAME`/`AUTH_PASSWORD` from `.env`). Navigate to «Повторный прогон», enter `11425275`, submit. Expect to see one of:
- `queued: 2 items` (if first time)
- `already_processed: 2 items` (if the previous task already enqueued them)
- error with specific message (note it; verify via curl as in Task 4)

Kill the backend: `pkill -f "uvicorn app.main:app.*8765"`.

- [ ] **Step 4: Commit**

```bash
git add backend/static/index.html backend/static/app.js
git commit -m "feat(admin): add reprocess page for historical AmoCRM calls"
```

---

## Task 6: Prior context builder module

**Files:**
- Create: `worker/tasks/prior_context.py`
- Create: `worker/tests/test_prior_context.py`

**Design note:** pure in-memory helpers only. DB query and filesystem read are exposed as separate functions so tests can mock them by passing prepared data.

- [ ] **Step 1: Write failing tests for profile merge**

Write `worker/tests/test_prior_context.py`:

```python
"""Tests for prior_context building: merge logic and structure."""
from tasks.prior_context import merge_client_profiles, summarise_interaction, build_prior_context_dict


def test_merge_empty_profiles_returns_empty():
    assert merge_client_profiles([]) == {}


def test_merge_newer_overrides_older():
    older = {"date": "2026-04-01", "client_info": {"budget": "20 млн", "locations": "Хамовники"}}
    newer = {"date": "2026-04-10", "client_info": {"budget": "25 млн"}}
    result = merge_client_profiles([older, newer])
    # budget changed — must note both versions
    assert "25 млн" in result["budget"]
    assert "20" in result["budget"] or "пересмотр" in result["budget"].lower()
    # locations unchanged — passes through
    assert result["locations"] == "Хамовники"


def test_merge_preserves_stable_field():
    a = {"date": "2026-04-01", "client_info": {"purchase_goal": "жильё"}}
    b = {"date": "2026-04-10", "client_info": {"purchase_goal": "жильё"}}
    result = merge_client_profiles([a, b])
    assert result["purchase_goal"] == "жильё"


def test_merge_skips_nulls():
    a = {"date": "2026-04-01", "client_info": {"budget": "20 млн"}}
    b = {"date": "2026-04-10", "client_info": {"budget": None}}
    result = merge_client_profiles([a, b])
    assert result["budget"] == "20 млн"


def test_summarise_interaction_extracts_core_fields():
    report = {
        "brief_summary": "Первый контакт",
        "call_classification": {"type": "partial"},
        "conversation_outcome": {"result": "info_provided"},
    }
    s = summarise_interaction(report, created_at_iso="2026-04-03T10:00:00Z", duration=840, direction="out")
    assert s["date"] == "2026-04-03"
    assert s["type"] == "call_out"
    assert s["duration"] == 840
    assert s["classification"] == "partial"
    assert s["outcome"] == "info_provided"
    assert s["brief"] == "Первый контакт"


def test_build_prior_context_first_call():
    ctx = build_prior_context_dict(past_reports=[], current_created_at="2026-04-17T10:00:00Z")
    assert ctx["call_number"] == 1
    assert ctx["previous_calls_count"] == 0
    assert ctx["client_profile"] == {}
    assert ctx["interactions_history"] == []
    assert ctx["open_objections"] == []
    assert ctx["resolved_objections"] == []


def test_build_prior_context_aggregates_objections():
    r1 = {
        "created_at_iso": "2026-04-01T10:00:00Z",
        "duration": 900,
        "direction": "out",
        "report": {
            "brief_summary": "первый",
            "client_info": {"budget": "20 млн"},
            "objections": [
                {"text": "Дорого", "resolved": False, "category": "too_expensive"}
            ],
        },
    }
    r2 = {
        "created_at_iso": "2026-04-10T10:00:00Z",
        "duration": 1200,
        "direction": "out",
        "report": {
            "brief_summary": "второй",
            "client_info": {"budget": "25 млн"},
            "objections": [
                {"text": "Дорого", "resolved": True, "category": "too_expensive"}
            ],
        },
    }
    ctx = build_prior_context_dict(past_reports=[r1, r2], current_created_at="2026-04-17T10:00:00Z")
    assert ctx["call_number"] == 3
    assert ctx["previous_calls_count"] == 2
    assert len(ctx["interactions_history"]) == 2
    # Дорого was raised in #1 and resolved in #2 — must end up in resolved_objections only
    assert any("Дорого" in o["text"] for o in ctx["resolved_objections"])
    assert not any("Дорого" in o["text"] for o in ctx["open_objections"])
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_prior_context.py -v`
Expected: ImportError on `tasks.prior_context`.

- [ ] **Step 3: Implement `prior_context.py`**

Write `worker/tasks/prior_context.py`:

```python
"""
Build `prior_context` for context-aware evaluation and next-call planning.

Pure in-memory helpers + one DB/filesystem orchestrator. Tests cover
the pure helpers; the orchestrator is exercised via integration smoke.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import psycopg2

logger = logging.getLogger(__name__)

RESULTS_PATH = os.getenv("RESULTS_STORAGE_PATH", "./data/results")

# Fields of client_info we aggregate
_CLIENT_INFO_FIELDS = [
    "purchase_goal", "locations", "apartment_format", "timeline",
    "budget", "payment_form", "important_factors", "what_viewed",
    "current_situation", "objections_voiced", "other",
]


def merge_client_profiles(entries: list[dict]) -> dict:
    """
    Merge client_info across interactions chronologically.

    entries: list of {"date": ISO-ish string, "client_info": {field: value}}
             sorted oldest-first.
    Returns a merged dict. Field that changed is expressed as
    "new (ранее: old, пересмотрено 2026-04-10)".
    """
    if not entries:
        return {}
    # Keep (value, date) for each field; only ingest non-None/non-empty
    seen: dict[str, list[tuple[str, str]]] = {}
    for entry in entries:
        date = entry.get("date", "")
        info = entry.get("client_info", {}) or {}
        for field in _CLIENT_INFO_FIELDS:
            val = info.get(field)
            if val in (None, ""):
                continue
            seen.setdefault(field, []).append((str(val), date))

    merged: dict[str, str] = {}
    for field, history in seen.items():
        if not history:
            continue
        latest_val, latest_date = history[-1]
        # Any previous different value?
        prior_differing = [(v, d) for (v, d) in history[:-1] if v != latest_val]
        if prior_differing:
            old_val, old_date = prior_differing[-1]
            merged[field] = f"{latest_val} (ранее: {old_val}, пересмотрено {latest_date})"
        else:
            merged[field] = latest_val
    return merged


def summarise_interaction(report: dict, created_at_iso: str, duration: int, direction: str) -> dict:
    """Extract the compact per-interaction summary used inside prior_context."""
    classification = (report.get("call_classification") or {}).get("type", "")
    outcome = (report.get("conversation_outcome") or {}).get("result", "")
    brief = report.get("brief_summary") or report.get("summary") or ""
    call_type = "call_out" if direction == "out" else "call_in"
    date = created_at_iso.split("T")[0] if "T" in created_at_iso else created_at_iso[:10]
    return {
        "date": date,
        "type": call_type,
        "duration": duration,
        "classification": classification,
        "outcome": outcome,
        "brief": brief,
    }


def build_prior_context_dict(past_reports: list[dict], current_created_at: str) -> dict:
    """
    Shape the final prior_context dict for the LLM.

    past_reports: list of {"created_at_iso", "duration", "direction", "report"}
                  sorted oldest-first; each "report" is the saved quality.json dict.
    current_created_at: ISO timestamp of the current session.
    """
    previous_count = len(past_reports)
    call_number = previous_count + 1

    interactions_history = []
    profile_entries = []
    open_objections: list[dict] = []
    resolved_objections: list[dict] = []

    for item in past_reports:
        report = item["report"]
        date = item["created_at_iso"].split("T")[0] if "T" in item["created_at_iso"] else item["created_at_iso"][:10]
        interactions_history.append(
            summarise_interaction(report, item["created_at_iso"], item["duration"], item["direction"])
        )
        profile_entries.append({"date": date, "client_info": report.get("client_info") or {}})

        for obj in report.get("objections") or []:
            text = obj.get("text", "")
            if not text:
                continue
            entry = {
                "text": text,
                "category": obj.get("category", ""),
                "raised_at": date,
            }
            if obj.get("resolved"):
                entry["resolved_at"] = date
                entry["how"] = obj.get("broker_response", "")
                resolved_objections.append(entry)
                # Remove from open if previously raised
                open_objections = [o for o in open_objections if o["text"] != text]
            else:
                # Only keep if not already resolved later (we process chronologically;
                # if it gets resolved in a later call, we'll drop it there)
                open_objections.append(entry)

    client_profile = merge_client_profiles(profile_entries)

    return {
        "call_number": call_number,
        "previous_calls_count": previous_count,
        "client_profile": client_profile,
        "interactions_history": interactions_history,
        "open_objections": open_objections,
        "resolved_objections": resolved_objections,
    }


# === Orchestrator: pulls past reports from DB + filesystem =========================

def _get_sync_db_url() -> str:
    url = os.getenv("DATABASE_URL_SYNC", "") or os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace("postgresql+asyncpg://", "postgresql://")


def _load_quality_report(session_id: str) -> dict | None:
    path = Path(RESULTS_PATH) / session_id / "quality.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception(f"Failed to read {path}")
        return None


def load_past_sessions_for_lead(lead_id: int, before_created_at) -> list[dict]:
    """
    Fetch completed sessions for a lead that started before the given timestamp.
    Returns entries ready for build_prior_context_dict.
    """
    db_url = _get_sync_db_url()
    if not db_url or not lead_id:
        return []
    entries: list[dict] = []
    try:
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.id, s.created_at, s.metadata
                    FROM sessions s
                    WHERE (s.metadata->>'lead_id')::bigint = %s
                      AND s.status = 'completed'
                      AND s.created_at < %s
                    ORDER BY s.created_at ASC
                    """,
                    (lead_id, before_created_at),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        logger.exception(f"Failed to load past sessions for lead {lead_id}")
        return []

    for session_id, created_at, metadata in rows:
        report = _load_quality_report(session_id)
        if not report:
            continue
        # Short-call reports have no useful data for context — skip
        if report.get("skip_reason") == "too_short":
            continue
        meta = metadata if isinstance(metadata, dict) else json.loads(metadata or "{}")
        direction = meta.get("direction", "out")
        duration = int(report.get("audio_duration") or 0)
        entries.append({
            "created_at_iso": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
            "duration": duration,
            "direction": direction,
            "report": report,
        })
    return entries


def build_prior_context_for_session(lead_id: int, current_created_at) -> dict | None:
    """Top-level orchestrator used by pipeline. Returns None when lead_id is missing."""
    if not lead_id:
        return None
    past = load_past_sessions_for_lead(lead_id, current_created_at)
    iso_now = current_created_at.isoformat() if hasattr(current_created_at, "isoformat") else str(current_created_at)
    return build_prior_context_dict(past, iso_now)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_prior_context.py -v`
Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/prior_context.py worker/tests/test_prior_context.py
git commit -m "feat(prior-context): aggregate past calls into structured context"
```

---

## Task 7: V4 quality schema and context-aware eval

**Files:**
- Modify: `worker/tasks/quality.py` (add V4 schema, V4 system prompt, update `assess_quality` and `_assess_with_structured_output`)
- Create: `worker/tests/test_quality_schemas.py`

**Design note:** V4 is V3 plus two new top-level fields (`stage_progression`, `applicable_checklist_items`) and a `prior_context` section in the user prompt. The system prompt gains a dedicated «Контекст взаимодействий» section that tells the model how to read the context.

- [ ] **Step 1: Write failing schema-shape tests**

Write `worker/tests/test_quality_schemas.py`:

```python
"""Schema-level sanity checks for V4 and next-call plan schemas."""
from tasks.quality import (
    QUALITY_JSON_SCHEMA_V3,
    QUALITY_JSON_SCHEMA_V4,
    NEXT_CALL_PLAN_SCHEMA,
)


def _required(schema):
    return set(schema["schema"]["required"])


def test_v4_is_superset_of_v3():
    v3_req = _required(QUALITY_JSON_SCHEMA_V3)
    v4_req = _required(QUALITY_JSON_SCHEMA_V4)
    assert v3_req.issubset(v4_req)


def test_v4_adds_stage_progression():
    v4_props = QUALITY_JSON_SCHEMA_V4["schema"]["properties"]
    assert "stage_progression" in v4_props
    sp = v4_props["stage_progression"]["properties"]
    assert "new_info_learned" in sp
    assert "objections_resolved" in sp
    assert "objections_raised" in sp
    assert "progress_delta" in sp
    assert "stage_advanced" in sp


def test_v4_strict_flag_set():
    assert QUALITY_JSON_SCHEMA_V4["strict"] is True
    assert QUALITY_JSON_SCHEMA_V4["schema"]["additionalProperties"] is False


def test_next_call_plan_required_fields():
    req = _required(NEXT_CALL_PLAN_SCHEMA)
    for f in ("goals", "unresolved_objections", "information_gaps",
              "talking_points", "recommended_properties", "risks",
              "suggested_opener"):
        assert f in req
    assert NEXT_CALL_PLAN_SCHEMA["strict"] is True
```

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_quality_schemas.py -v`
Expected: ImportError — schemas not defined yet.

- [ ] **Step 2: Add V4 schema to `quality.py`**

In `worker/tasks/quality.py`, after `QUALITY_JSON_SCHEMA_V3` (ends around line 302), append:

```python
# === V4 Schema: context-aware evaluation with stage progression ===

QUALITY_JSON_SCHEMA_V4 = {
    "name": "quality_assessment_v4",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            **{k: v for k, v in QUALITY_JSON_SCHEMA_V3["schema"]["properties"].items()},
            "stage_progression": {
                "type": "object",
                "description": "Как этот звонок продвинул сделку по сравнению с прошлыми",
                "properties": {
                    "new_info_learned": {"type": "array", "items": {"type": "string"}},
                    "objections_resolved": {"type": "array", "items": {"type": "string"}},
                    "objections_raised": {"type": "array", "items": {"type": "string"}},
                    "profile_updates": {"type": "array", "items": {"type": "string"}},
                    "progress_delta": {"type": "string"},
                    "stage_advanced": {"type": "boolean"},
                },
                "required": ["new_info_learned", "objections_resolved", "objections_raised",
                             "profile_updates", "progress_delta", "stage_advanced"],
                "additionalProperties": False,
            },
            "applicable_checklist_items": {
                "type": "object",
                "description": "Какие пункты чек-листа пропущены, потому что уже сделаны ранее",
                "properties": {
                    "skipped_as_already_done": {"type": "array", "items": {"type": "string"}},
                    "newly_applicable": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["skipped_as_already_done", "newly_applicable"],
                "additionalProperties": False,
            },
        },
        "required": QUALITY_JSON_SCHEMA_V3["schema"]["required"] + [
            "stage_progression", "applicable_checklist_items"
        ],
        "additionalProperties": False,
    },
}
```

- [ ] **Step 3: Extend the system prompt with context section**

In `worker/tasks/quality.py`, define a new constant right after `SYSTEM_PROMPT_V3` (ends around line 410):

```python
CONTEXT_AWARE_INSTRUCTIONS = """

## Контекст взаимодействий с клиентом

Этот звонок может быть не первым. В блоке prior_context (в user-промпте)
есть:
- call_number — номер текущего звонка (1 = первый).
- client_profile — накопленный профиль клиента (бюджет, локации, сроки,
  и т.д.), собранный из прошлых звонков. Если значение изменилось,
  формат "новое (ранее: старое, пересмотрено DATE)".
- interactions_history — краткое описание прошлых звонков.
- open_objections / resolved_objections — история возражений.

Правила при наличии prior_context:
- Если call_number > 1 — НЕ штрафуй брокера за пропуск приветствия,
  представления, упоминания ЖК (пункты greeting_by_name, introduced_self,
  mentioned_project в чек-листе). Ставь им status="not_applicable"
  с comment вида "Уже сделано в звонке от <дата>".
- Если информация уже известна (бюджет, локация, сроки) — не требуй
  задавать эти вопросы заново. Status="not_applicable" + объяснение.
- Оценивай прогресс относительно prior_context. Заполни блок
  stage_progression: что нового узнали, какие возражения сняли/подняли,
  продвинулась ли сделка.
- Если клиент изменил позицию (например, бюджет уменьшился) — зафиксируй
  в stage_progression.profile_updates.
- В applicable_checklist_items.skipped_as_already_done перечисли ID
  пунктов, которые ты проставил not_applicable по причине уже-сделано.
- Используй реальные имена, проекты, детали из prior_context.
"""

SYSTEM_PROMPT_V4 = SYSTEM_PROMPT_V3 + CONTEXT_AWARE_INSTRUCTIONS
```

- [ ] **Step 4: Update `assess_quality` to accept `prior_context`**

In `worker/tasks/quality.py`, modify the `assess_quality` signature (currently line 521) to:

```python
def assess_quality(
    transcript: list[dict],
    sentiment_results: list[dict],
    protocol: str | None = None,
    custom_prompt: str | None = None,
    criteria_config: list[dict] | None = None,
    use_extended_schema: bool = False,
    prior_context: dict | None = None,
) -> dict:
```

Propagate `prior_context` into `_assess_with_structured_output`. Replace the call site (currently around line 573):

```python
    try:
        result = _assess_with_structured_output(
            client, transcript_text, sentiment_json, protocol,
            custom_instructions, criteria_instructions,
            use_extended_schema=use_extended_schema,
            prior_context=prior_context,
        )
```

And update `_assess_with_structured_output` signature (currently line 588):

```python
def _assess_with_structured_output(
    client, transcript_text, sentiment_json, protocol, custom_instructions,
    criteria_instructions="", use_extended_schema=False,
    prior_context=None,
):
    """Assess using structured output (JSON schema) via GPT-5.4 Responses API."""
    if not criteria_instructions:
        criteria_instructions = _build_criteria_instructions(None)

    if use_extended_schema and prior_context is not None and prior_context.get("previous_calls_count", 0) > 0:
        system = SYSTEM_PROMPT_V4.format(
            custom_instructions=custom_instructions,
            criteria_instructions=criteria_instructions,
        )
        schema = QUALITY_JSON_SCHEMA_V4
        schema_name = "quality_assessment_v4"
        version = 4
    elif use_extended_schema:
        system = SYSTEM_PROMPT_V3.format(
            custom_instructions=custom_instructions,
            criteria_instructions=criteria_instructions,
        )
        schema = QUALITY_JSON_SCHEMA_V3
        schema_name = "quality_assessment_v3"
        version = 3
    else:
        system = SYSTEM_PROMPT.format(
            custom_instructions=custom_instructions,
            criteria_instructions=criteria_instructions,
        )
        schema = QUALITY_JSON_SCHEMA
        schema_name = "quality_assessment"
        version = 2
```

Then modify the `user` prompt assembly in the same function (currently around line 613) to include prior_context:

```python
    user_parts = [
        USER_PROMPT.format(
            protocol=protocol or DEFAULT_PROTOCOL,
            transcript=transcript_text,
            sentiment_summary=sentiment_json,
        )
    ]
    if prior_context is not None:
        user_parts.append("\n## Prior context\n" + json.dumps(prior_context, ensure_ascii=False, indent=2))
    user = "\n".join(user_parts)
```

Keep the rest of `_assess_with_structured_output` (the Responses API call with `text={"format": {...}}` etc.) unchanged — but make sure `schema_name` and `schema["schema"]` are used consistently (they already are).

- [ ] **Step 5: Run schema tests, verify they pass**

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_quality_schemas.py -v`
Expected: 4 tests pass.

- [ ] **Step 6: Smoke-test imports**

Run: `cd worker && source ../.venv/bin/activate && python -c "from tasks.quality import assess_quality, QUALITY_JSON_SCHEMA_V4, SYSTEM_PROMPT_V4; print('ok')"`
Expected: `ok`.

- [ ] **Step 7: Commit**

```bash
git add worker/tasks/quality.py worker/tests/test_quality_schemas.py
git commit -m "feat(quality): V4 schema with stage_progression and prior-context awareness"
```

---

## Task 8: Next-call plan LLM function

**Files:**
- Modify: `worker/tasks/quality.py` (add `NEXT_CALL_PLAN_SCHEMA`, `PLAN_SYSTEM_PROMPT`, `plan_next_call`)
- Modify: `worker/tests/test_quality_schemas.py` (tests already added in Task 7 cover the plan schema)

- [ ] **Step 1: Add the plan schema and function**

In `worker/tasks/quality.py`, after `QUALITY_JSON_SCHEMA_V4`, append:

```python
# === Next-call plan schema (separate LLM call) ===

NEXT_CALL_PLAN_SCHEMA = {
    "name": "next_call_plan",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "goals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "priority": {"type": "integer"},
                        "text": {"type": "string"},
                    },
                    "required": ["priority", "text"],
                    "additionalProperties": False,
                },
            },
            "unresolved_objections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "suggested_response": {"type": "string"},
                        "priority": {"type": "string"},
                    },
                    "required": ["text", "suggested_response", "priority"],
                    "additionalProperties": False,
                },
            },
            "information_gaps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "why": {"type": "string"},
                    },
                    "required": ["field", "why"],
                    "additionalProperties": False,
                },
            },
            "talking_points": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "argument": {"type": "string"},
                        "personalized_hook": {"type": "string"},
                    },
                    "required": ["topic", "argument", "personalized_hook"],
                    "additionalProperties": False,
                },
            },
            "recommended_properties": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["project", "reason"],
                    "additionalProperties": False,
                },
            },
            "risks": {"type": "array", "items": {"type": "string"}},
            "suggested_opener": {"type": "string"},
        },
        "required": ["goals", "unresolved_objections", "information_gaps",
                     "talking_points", "recommended_properties",
                     "risks", "suggested_opener"],
        "additionalProperties": False,
    },
}

PLAN_SYSTEM_PROMPT = """Ты — consultative-sales coach для брокера элитной недвижимости Москвы.

Твоя задача: на основе всей истории взаимодействий с клиентом и результата последнего
звонка — построить конкретный план следующего взаимодействия.

Правила:
- Goals: 1-3 цели, упорядоченные по приоритету.
- Unresolved_objections: перечисли ТОЛЬКО возражения, которые сейчас в open
  (resolved опускай). Предложи конкретный ответ брокера.
- Information_gaps: ключевые недостающие поля клиентского профиля, с обоснованием "зачем".
- Talking_points: аргументы, привязанные к конкретным фактам о клиенте
  (используй поля из client_profile как personalized_hook).
- Recommended_properties: если контекст указывает на конкретные ЖК — добавь
  их с обоснованием. НЕ ВЫДУМЫВАЙ факты о ЖК — используй только то,
  что было упомянуто в прошлых звонках или профиле клиента.
- Risks: что может пойти не так, на что обратить внимание.
- Suggested_opener: одна короткая фраза для начала следующего звонка, с именем клиента
  и конкретной зацепкой.

Все тексты — по-русски, естественные формулировки."""


def plan_next_call(
    prior_context: dict,
    current_quality_report: dict,
    deal_stage: str | None = None,
) -> dict | None:
    """
    Generate a forward-looking plan via a second LLM call.

    Returns the parsed plan dict, or None on failure (caller must not block on this).
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set, skipping next-call plan")
        return None

    client = OpenAI(api_key=api_key)

    # Strip heavy fields from current report — plan doesn't need transcript/key_moments
    compact_current = {
        k: current_quality_report.get(k)
        for k in (
            "brief_summary", "summary", "overall_score", "call_classification",
            "conversation_outcome", "objections", "client_info",
            "improvement_suggestions", "stage_progression",
        )
        if current_quality_report.get(k) is not None
    }

    user_payload = {
        "prior_context": prior_context,
        "current_call": compact_current,
        "deal_stage": deal_stage,
    }

    try:
        response = client.responses.create(
            model="gpt-5.4",
            input=[
                {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": NEXT_CALL_PLAN_SCHEMA["name"],
                    "strict": True,
                    "schema": NEXT_CALL_PLAN_SCHEMA["schema"],
                }
            },
        )
        plan = json.loads(response.output_text)
        logger.info(f"Next-call plan generated with {len(plan.get('goals', []))} goals")
        return plan
    except Exception:
        logger.exception("Failed to generate next-call plan")
        return None
```

- [ ] **Step 2: Run existing schema tests, verify plan schema passes**

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_quality_schemas.py -v`
Expected: all 4 tests pass (the plan test added in Task 7 now has its schema).

- [ ] **Step 3: Commit**

```bash
git add worker/tasks/quality.py
git commit -m "feat(quality): add plan_next_call with dedicated schema and system prompt"
```

---

## Task 9: Plan note formatter + AmoCRM plain-note helper

**Files:**
- Modify: `worker/tasks/amocrm_sync.py` (add `format_next_call_plan`, `create_plain_note`)

- [ ] **Step 1: Add `format_next_call_plan`**

In `worker/tasks/amocrm_sync.py`, after `format_enriched_note` (currently ends around line 510), append:

```python
def format_next_call_plan(plan: dict) -> str:
    """Render a next-call plan dict as markdown-like text for an AmoCRM note."""
    lines: list[str] = ["📋 План следующего звонка (AI)", ""]

    goals = sorted(plan.get("goals") or [], key=lambda g: g.get("priority", 99))
    if goals:
        lines.append("🎯 Цели:")
        for g in goals:
            lines.append(f"{g.get('priority', 1)}. {g.get('text', '')}")
        lines.append("")

    obj = plan.get("unresolved_objections") or []
    if obj:
        lines.append("❗ Открытые возражения:")
        for o in obj:
            pri = o.get("priority", "средний")
            lines.append(f"• «{o.get('text','')}» ({pri} приоритет)")
            if o.get("suggested_response"):
                lines.append(f"  → {o['suggested_response']}")
        lines.append("")

    gaps = plan.get("information_gaps") or []
    if gaps:
        lines.append("🔍 Чего не хватает:")
        for g in gaps:
            field = g.get("field", "")
            why = g.get("why", "")
            lines.append(f"• {field}" + (f" — {why}" if why else ""))
        lines.append("")

    tp = plan.get("talking_points") or []
    if tp:
        lines.append("💬 Аргументы:")
        for t in tp:
            lines.append(f"• {t.get('topic', '')}")
            if t.get("argument"):
                lines.append(f"  {t['argument']}")
            if t.get("personalized_hook"):
                lines.append(f"  ({t['personalized_hook']})")
        lines.append("")

    props = plan.get("recommended_properties") or []
    if props:
        lines.append("🏢 Рекомендуем показать:")
        for p in props:
            lines.append(f"• {p.get('project', '')} — {p.get('reason', '')}")
        lines.append("")

    risks = plan.get("risks") or []
    if risks:
        lines.append("⚠️ Риски:")
        for r in risks:
            lines.append(f"• {r}")
        lines.append("")

    if plan.get("suggested_opener"):
        lines.append("🗣️ Фраза для начала:")
        lines.append(f"«{plan['suggested_opener']}»")

    return "\n".join(lines).strip()


def create_plain_note(lead_id: int, text: str) -> dict:
    """Create a common (non-call) note on a lead. Used for plan notes."""
    if not _get_access_token():
        return {"ok": False, "error": "AmoCRM token not configured"}
    payload = [{"note_type": "common", "params": {"text": text}}]
    try:
        resp = requests.post(
            f"{AMOCRM_BASE_URL}/api/v4/leads/{lead_id}/notes",
            json=payload,
            headers=_headers(),
            timeout=15,
        )
        if resp.status_code == 200:
            note_id = resp.json()["_embedded"]["notes"][0]["id"]
            logger.info(f"Created plain note {note_id} for lead {lead_id}")
            return {"ok": True, "note_id": note_id}
        logger.warning(f"Failed to create plain note: {resp.status_code} {resp.text[:200]}")
        return {"ok": False, "error": resp.text[:200], "status": resp.status_code}
    except Exception:
        logger.exception(f"Error creating plain note for lead {lead_id}")
        return {"ok": False, "error": "Request failed"}
```

- [ ] **Step 2: Smoke-check formatter with a minimal plan**

Run:
```bash
cd worker && source ../.venv/bin/activate && \
  python -c "
from tasks.amocrm_sync import format_next_call_plan
plan = {
  'goals': [{'priority': 1, 'text': 'Назначить показ'}],
  'unresolved_objections': [{'text': 'Дорого', 'suggested_response': 'Показать рассрочку', 'priority': 'high'}],
  'information_gaps': [{'field': 'бюджет', 'why': 'не озвучен'}],
  'talking_points': [{'topic': 'Виды', 'argument': 'Хорошая панорама', 'personalized_hook': 'клиент любит виды'}],
  'recommended_properties': [{'project': 'ЖК X', 'reason': 'локация совпадает'}],
  'risks': ['клиент смотрит альтернативы'],
  'suggested_opener': 'Добрый день, Иван!'
}
print(format_next_call_plan(plan))
"
```

Expected: output includes «📋 План следующего звонка», all sections, and the opener quoted.

- [ ] **Step 3: Commit**

```bash
git add worker/tasks/amocrm_sync.py
git commit -m "feat(amocrm): format next-call plan and create plain-note helper"
```

---

## Task 10: Wire prior_context and plan into the pipeline

**Files:**
- Modify: `worker/tasks/pipeline.py` (build prior_context, call plan_next_call, update `_push_to_amocrm` to post second note, persist plan_amo_note_id)

**Design note:** the plan runs inside `_run_pipeline` after `assess_quality`; its output plus the AmoCRM note_id both go into `sessions.metadata.next_call_plan` and `sessions.metadata.plan_amo_note_id` (Phase 2 reads these for follow-through).

- [ ] **Step 1: Import helpers at the top of `pipeline.py`**

In `worker/tasks/pipeline.py`, near the other `from tasks.*` imports (around line 20-26), add:

```python
from tasks.prior_context import build_prior_context_for_session
from tasks.quality import plan_next_call
```

- [ ] **Step 2: Add `_get_session_created_at` helper**

In `worker/tasks/pipeline.py`, near `_get_session_metadata` (around line 83), append:

```python
def _get_session_created_at(session_id: str):
    """Return created_at (datetime or None) for a session."""
    db_url = _get_sync_db_url()
    if not db_url:
        return None
    try:
        conn = psycopg2.connect(db_url)
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
```

- [ ] **Step 3: Build prior_context before `assess_quality`**

In `worker/tasks/pipeline.py`, inside `_run_pipeline`, locate the block `# === 6. LLM Quality Assessment ===` (currently around line 321). Replace the `quality_report = assess_quality(...)` call site with:

```python
    # === 6. LLM Quality Assessment ===
    task.update_state(state="PROGRESS", meta={"step": "quality", "progress": 90})
    logger.info(f"[{session_id}] Step 6: LLM quality assessment...")
    quality_protocol = (scenario.get("protocol") if scenario else None) or config.get("protocol") or get_protocol(company_config)
    quality_prompt = (scenario.get("prompt") if scenario else None) or config.get("custom_prompt") or get_custom_prompt(company_config)
    quality_criteria = scenario.get("criteria") if scenario else None

    use_extended = bool(scenario and scenario.get("prompt"))

    lead_id = session_meta.get("lead_id")
    current_created_at = _get_session_created_at(session_id)
    prior_context = build_prior_context_for_session(lead_id, current_created_at) if lead_id else None
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
    )
    save_results(session_id, "quality", quality_report)
```

- [ ] **Step 4: Generate plan and store it in metadata**

In `worker/tasks/pipeline.py`, inside `_run_pipeline`, after the existing `# === 7. Auto-save speaker roles ===` block and BEFORE `# === 8. Push to AmoCRM ===`, insert:

```python
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
            next_call_plan = plan_next_call(prior_context, quality_report)
            if next_call_plan:
                save_results(session_id, "next_call_plan", next_call_plan)
                _update_session_metadata(session_id, {"next_call_plan": next_call_plan})
        except Exception:
            logger.exception(f"[{session_id}] Next-call plan generation failed (continuing)")
```

- [ ] **Step 5: Post plan as a second AmoCRM note in `_push_to_amocrm`**

In `worker/tasks/pipeline.py`, modify the signature of `_push_to_amocrm` (currently line 232) to accept the plan:

```python
def _push_to_amocrm(session_id: str, quality_report: dict, session_meta: dict, audio_path: str, next_call_plan: dict | None = None):
```

Then at the very end of that function (after tag_lead), append:

```python
    # Second note: next-call plan (if generated)
    if next_call_plan:
        from tasks.amocrm_sync import format_next_call_plan, create_plain_note
        plan_text = format_next_call_plan(next_call_plan)
        result = create_plain_note(lead_id, plan_text)
        if result.get("ok"):
            _update_session_metadata(session_id, {"plan_amo_note_id": result["note_id"]})
            logger.info(f"[{session_id}] Created plan note {result['note_id']} for lead {lead_id}")
        else:
            logger.warning(f"[{session_id}] Failed to create plan note: {result}")
```

Update the call site in `_run_pipeline` (inside the `if use_extended:` block, currently line 346 area):

```python
    if use_extended:
        task.update_state(state="PROGRESS", meta={"step": "amocrm_sync", "progress": 95})
        logger.info(f"[{session_id}] Step 8: AmoCRM sync...")
        _push_to_amocrm(session_id, quality_report, session_meta, audio_path, next_call_plan=next_call_plan)
```

Also update the short-call gate's `_push_to_amocrm` call (added in Task 2) — it should NOT pass a plan (plan is skipped for short calls). The existing call already omits the kwarg, so nothing to change there.

- [ ] **Step 6: Smoke-check imports**

Run: `cd worker && source ../.venv/bin/activate && python -c "from tasks.pipeline import _run_pipeline, _push_to_amocrm, _get_session_created_at; print('ok')"`
Expected: `ok`.

- [ ] **Step 7: Commit**

```bash
git add worker/tasks/pipeline.py
git commit -m "feat(pipeline): wire prior_context and next-call plan; post plan as second AmoCRM note"
```

---

## Task 11: End-to-end smoke test

**Files:** no code changes; this task validates the full flow.

- [ ] **Step 1: Restart worker and backend to pick up new code**

Tell the user via the conversation: worker restart requires their action (cached tokens and running celery processes). Explicitly confirm before proceeding. If authorised:

```bash
# Full Celery restart (dev host)
pkill -f "celery -A tasks.celery_app" || true
sleep 2
cd /root/projects/realestate/worker && source ../.venv/bin/activate && \
  set -a && source ../.env && set +a && \
  nohup celery -A tasks.celery_app worker --loglevel=info --concurrency=4 --max-tasks-per-child=10 -Q default,transcription -B > /tmp/celery-realestate.log 2>&1 &
```

Verify: `tail -n 20 /tmp/celery-realestate.log` shows the beat scheduler and workers ready.

- [ ] **Step 2: Enqueue lead 11425275 via the reprocess endpoint**

If backend isn't running, start it the same way as Task 4 Step 5. Then:

```bash
curl -s -X POST http://127.0.0.1:8765/api/amocrm/reprocess \
  -u "$AUTH_USERNAME:$AUTH_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"lead_id": 11425275, "force": true}' | jq
```

Expected: `queued` contains 2 entries — `note_id=46048433` (5s) and `note_id=46240441` (1050s).

- [ ] **Step 3: Watch processing**

```bash
tail -f /tmp/celery-realestate.log | grep -E "amo:46048433|amo:46240441"
```

Expected sequence for each note:
- For `46048433` (5s): log line `"Short call (5.xs < 20s), skipping full evaluation"`, then AmoCRM note creation for the short-call comment.
- For `46240441` (1050s, ~17 min): full pipeline (transcribing → diarizing → merging_speakers → sentiment → quality → next_call_plan → amocrm_sync). Plan note creation log at the end.

- [ ] **Step 4: Verify AmoCRM content**

Using the existing AmoCRM helpers (from worker python shell):

```bash
cd worker && source ../.venv/bin/activate && \
  set -a && source ../.env && set +a && \
  python -c "
from tasks.amocrm_sync import list_call_notes_on_entity, get_lead_with_contacts, _headers, AMOCRM_BASE_URL
import requests
lead = get_lead_with_contacts(11425275)
print('lead contacts:', lead['contact_ids'])
resp = requests.get(f'{AMOCRM_BASE_URL}/api/v4/leads/11425275/notes', params={'limit': 50}, headers=_headers(), timeout=15)
for n in resp.json()['_embedded']['notes']:
    p = n.get('params', {}) or {}
    text_preview = (p.get('text') or '')[:80].replace('\n', ' | ')
    print(n['id'], n.get('note_type'), '->', text_preview)
"
```

Expected: at least one note for the short-call (text contains «Короткий звонок» + «автоответчик») and two notes for the 17-minute call (the enriched eval + the plan starting with «📋 План следующего звонка»).

- [ ] **Step 5: Re-run the long call to trigger prior_context**

```bash
curl -s -X POST http://127.0.0.1:8765/api/amocrm/reprocess \
  -u "$AUTH_USERNAME:$AUTH_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"lead_id": 11425275, "force": true}' | jq
```

Tail logs for `prior_context`:

```bash
grep -E "Prior context|not_applicable" /tmp/celery-realestate.log | tail -20
```

Expected: at least the 1050s call logs `"Prior context: call #2, 1 prior, N open objections"` (since the 5s short-call is excluded). If the 5s call ran first this time, it's skipped-as-short and doesn't enter context. The quality.json for the long call should contain the `stage_progression` block.

Inspect the saved quality:

```bash
ls data/results/*/quality.json | xargs -I {} sh -c 'echo "== {}"; cat {} | python -c "import json,sys; d=json.load(sys.stdin); print(\"version:\", d.get(\"score_version\"), \"classification:\", (d.get(\"call_classification\") or {}).get(\"type\"), \"stage_progression:\", bool(d.get(\"stage_progression\")))"'
```

Expected: one record with `score_version: 4` and `stage_progression: True`.

- [ ] **Step 6: Commit nothing (this task is verification only)**

If everything passes, no commit needed. If a regression was found, fix it inline, add a regression test where sensible, commit with a descriptive message.

---

## Task 12: Update user-facing documentation

**Files:**
- Modify: `docs/superpowers/specs/2026-04-17-short-call-filter-and-reprocess-design.md` (add «Implementation status» note with the link to this plan and commit hash)
- Modify: `docs/superpowers/plans/2026-04-17-multi-call-phase1.md` (mark status as Completed once Task 11 passes)

- [ ] **Step 1: Append status block to spec**

At the very bottom of `docs/superpowers/specs/2026-04-17-short-call-filter-and-reprocess-design.md`, append:

```markdown

---

## Implementation status

- **Phase 1:** Completed per plan `docs/superpowers/plans/2026-04-17-multi-call-phase1.md`.
  Verified end-to-end on lead 11425275.
- **Phase 2:** Pending.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-04-17-short-call-filter-and-reprocess-design.md docs/superpowers/plans/2026-04-17-multi-call-phase1.md
git commit -m "docs: mark phase 1 complete"
```

---

## Self-review notes (for the executing agent)

- Tasks 1 → 10 each end in a working state with a commit. If a later task reveals a design issue, `git revert` only the affected commit rather than rolling back everything.
- Token check (gotcha from `project_amocrm_token_refresh_gotcha.md`): the worker caches AmoCRM tokens in module globals. After ANY env change or token rotation, **restart the worker** — otherwise even correct code uses stale credentials.
- If Task 11 step 3 stalls on the 1050s call, check `/tmp/celery-realestate.log` for AmoCRM 401s (token revoked). The reprocess endpoint itself will succeed in enqueueing, but the worker can't download the recording without a live token. The spec notes token restoration as **out of scope** — escalate to the user.
- If `plan_next_call` times out or errors, step 7.5 logs and moves on; the evaluation note still publishes. Intentional — the plan must never block the eval.
