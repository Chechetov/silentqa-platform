"""Reconciliation backstop for AmoCRM call ingestion — the self-healing net.

Why this exists
---------------
The 5-min poll (amocrm_poll) discovers calls via the AmoCRM events API using a
short `now - safety_window` cursor. When an event surfaces in the events API
LATER than that window (long calls: the event/recording is published minutes
after the call ends, and the cursor has since advanced past its created_at),
the note is never ingested — and since no amocrm_calls row exists, the retry
layer has nothing to retry. The score is silently lost (observed: lead 12056961,
a 506s call whose event surfaced ~16 min late).

What it does
------------
Every AMOCRM_RECONCILE_INTERVAL_SEC (default 30 min) this task:

  1. DEEP-SWEEP (heal): re-scans the events API over a WIDE lookback
     (RECONCILE_LOOKBACK_HOURS, default 12h) — long enough that any late event
     has surfaced by now — and ingests every note not already in amocrm_calls,
     reusing the poll's own `_ingest_call_event` (so ingestion is identical and
     deduped by ON CONFLICT(amo_note_id); re-scanning the same window is a
     no-op). This does NOT depend on the events API being timely.

  2. DETECT (catch errors): classifies terminally-stuck calls and reports the
     last-ingest age, so silent failures surface in a structured metrics line.
     A recovered>0 result means the fast poll missed something — that fires an
     alert (the actionable signal). Stuck-terminal counts are observable in the
     metrics line but don't spam alerts (they're bounded/known states).

It deliberately does NOT re-drive rows the retry layer already owns ('failed' /
'awaiting_recording' within budget are retried by the 5-min poll), to avoid a
second push path (create_enriched_note is not idempotent). Its only write action
is ingesting MISSING notes. Mirrors session_watchdog's safety contract: one
outer try/except, never raises into Beat.
"""
import logging
import os
from datetime import datetime, timezone, timedelta

from tenancy.context import reset_tenant_schema, set_tenant_schema
from tenancy.db import tenant_connect

from tasks.celery_app import app
from tasks.amocrm_poll import (
    _get_sync_db_url,
    _ingest_call_event,
    MAX_RETRIES,
    RETRY_MAX_AGE_HOURS,
)
from tasks.amocrm_sync import get_recent_call_events
from tasks.amocrm_alerts import send_alert

logger = logging.getLogger(__name__)

RECONCILE_LOOKBACK_HOURS = int(os.getenv("AMOCRM_RECONCILE_LOOKBACK_HOURS", "12"))
# Backpressure: cap how many missed calls we ingest+enqueue per cycle. After an
# outage the wide lookback can surface a large backlog; without a cap we'd burst
# the whole thing onto the transcription queue and hammer the AmoCRM API at once.
# Anything over the cap is left for the next cycle (logged), so the backlog
# drains steadily instead of in one spike.
RECONCILE_BATCH_LIMIT = int(os.getenv("AMOCRM_RECONCILE_BATCH_LIMIT", "50"))


def _existing_note_ids(note_ids: list[int]) -> set[int]:
    """Which of these amo_note_ids are already in amocrm_calls (so the deep-sweep
    skips them before the per-note get_note_details call — keeps it cheap)."""
    if not note_ids:
        return set()
    if not _get_sync_db_url():
        return set()
    conn = tenant_connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT amo_note_id FROM amocrm_calls WHERE amo_note_id = ANY(%s)",
                (note_ids,),
            )
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def _stuck_counts() -> dict:
    """Counts of terminally-stuck calls the retry layer can no longer help, plus
    the age (minutes) of the most recent ingest. Read-only observability."""
    if not _get_sync_db_url():
        return {"recording_timeout": 0, "failed_terminal": 0, "last_ingest_age_min": None}
    conn = tenant_connect()
    try:
        with conn, conn.cursor() as cur:
            # awaiting_recording that exhausted its retry budget or aged out
            cur.execute(
                """SELECT count(*) FROM amocrm_calls
                   WHERE status = 'awaiting_recording'
                     AND (retry_count >= %s
                          OR created_at < now() - (%s || ' hours')::interval)""",
                (MAX_RETRIES, RETRY_MAX_AGE_HOURS),
            )
            recording_timeout = cur.fetchone()[0]

            # failed past budget, excluding expected 'missed call, no recording'
            # rows (those are normal — a missed call has nothing to transcribe).
            cur.execute(
                """SELECT count(*) FROM amocrm_calls
                   WHERE status = 'failed'
                     AND retry_count >= %s
                     AND error_message IS DISTINCT FROM 'missed_call_no_recording_skipped'""",
                (MAX_RETRIES,),
            )
            failed_terminal = cur.fetchone()[0]

            cur.execute(
                "SELECT EXTRACT(EPOCH FROM (now() - MAX(created_at))) / 60 FROM amocrm_calls"
            )
            row = cur.fetchone()
            last_age = round(float(row[0]), 1) if row and row[0] is not None else None
    finally:
        conn.close()
    return {
        "recording_timeout": recording_timeout,
        "failed_terminal": failed_terminal,
        "last_ingest_age_min": last_age,
    }


def _reconcile_for_current_tenant():
    """Deep-sweep for missed calls + stuck-call detection (tenant context set)."""
    try:
        # 1. Deep-sweep: re-scan a wide window, ingest notes missing from the DB.
        since = int(
            (datetime.now(timezone.utc) - timedelta(hours=RECONCILE_LOOKBACK_HOURS)).timestamp()
        )
        events = get_recent_call_events(since)
        known = _existing_note_ids([e["note_id"] for e in events if e.get("note_id")])

        recovered = 0
        capped = False
        for event in events:
            if event.get("note_id") in known:
                continue  # already ingested by the poll — skip the get_note_details call
            if recovered >= RECONCILE_BATCH_LIMIT:
                capped = True
                break  # drain the rest next cycle — avoid a burst onto the queue/API
            if _ingest_call_event(event):
                recovered += 1
                logger.warning(
                    f"[reconcile] recovered missed call note {event.get('note_id')} "
                    f"(poll dropped it — event surfaced after the {RECONCILE_LOOKBACK_HOURS}h-swept window opened)"
                )
        if capped:
            logger.warning(
                f"[reconcile] hit batch cap of {RECONCILE_BATCH_LIMIT} this cycle "
                f"— backlog remains, will continue next cycle"
            )

        # 2. Detect terminally-stuck calls / observability.
        stuck = _stuck_counts()
        metrics = {"scanned": len(events), "recovered": recovered, "capped": capped, **stuck}
        logger.info(f"[reconcile] {metrics}")

        # 3. Alert only on the actionable signal: the poll missed something.
        if recovered:
            send_alert(
                f"recovered {recovered} missed call(s) the 5-min poll dropped "
                f"(now queued). Stuck: {stuck['recording_timeout']} recording-timeout, "
                f"{stuck['failed_terminal']} failed-terminal."
            )

        return metrics
    except Exception:
        logger.exception("[reconcile] sweep failed")
        return {"error": True}


@app.task(name="amocrm_reconcile.reconcile_amocrm_calls")
def reconcile_amocrm_calls():
    """Beat task: run the reconcile sweep for each AmoCRM tenant."""
    from tenancy.registry import iter_amocrm_tenants
    for t in iter_amocrm_tenants():
        token = set_tenant_schema(t["schema_name"])
        try:
            _reconcile_for_current_tenant()
        except Exception:
            logger.exception(f"reconcile failed for tenant {t['slug']}")
        finally:
            reset_tenant_schema(token)
