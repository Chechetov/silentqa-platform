"""Manual AmoCRM reprocess: find call notes on a lead and enqueue them."""
import logging
import os
import sys
from pathlib import Path

from celery import Celery
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

# Make worker/tasks importable (shared codebase for AmoCRM helpers and DB ops)
WORKER_PATH = Path(__file__).resolve().parents[3] / "worker"
if str(WORKER_PATH) not in sys.path:
    sys.path.insert(0, str(WORKER_PATH))

from tasks.amocrm_sync import get_lead_with_contacts, list_call_notes_on_entity, search_leads
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
    """Send the process_amocrm_call task to the worker broker.

    queue="transcription" matches the decorator on the worker-side task; without
    it the task lands on the default "celery" queue, which no one consumes.
    """
    res = _celery_app.send_task(
        "amocrm_poll.process_amocrm_call",
        args=[call_id],
        queue="transcription",
    )
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
                entity_type=entity_type,
                entity_id=entity_id,
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


@router.get("/search-leads")
async def amocrm_search_leads(
    q: str = Query(..., min_length=2, max_length=200, description="Имя, телефон или email"),
    limit: int = Query(20, ge=1, le=50),
):
    """Lookup AmoCRM leads by free-form query for the dashboard's lead-picker."""
    return {"items": search_leads(q, limit=limit)}
