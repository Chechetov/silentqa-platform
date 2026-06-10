"""
Watchdog for stuck desktop-app sessions.

Desktop client uploads chunks every 10s and calls /finish when the user stops.
If the client crashes / loses network / the laptop sleeps mid-call, the session
sits in `uploading` (or `created`) forever — the chunks we already received
never get processed and the operator sees a permanent "uploading" badge.

Two recovery rules:
  1. Has at least one chunk AND last chunk uploaded > UPLOADING_STALE_MIN ago
     → kick the same pipeline that /finish would have, on whatever audio we have.
  2. No chunks AND created > CREATED_STALE_MIN ago
     → mark `failed`. Nothing to process.
"""
import logging
import os
from datetime import datetime, timezone

import psycopg2

from tasks.celery_app import app

logger = logging.getLogger(__name__)

UPLOADING_STALE_MIN = int(os.getenv("SESSION_UPLOADING_STALE_MIN", "15"))
CREATED_STALE_MIN = int(os.getenv("SESSION_CREATED_STALE_MIN", "60"))


def _get_sync_db_url() -> str:
    url = os.getenv("DATABASE_URL_SYNC", "") or os.getenv("DATABASE_URL", "")
    url = url.replace("postgresql+psycopg2://", "postgresql://")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    return url


@app.task(name="session_watchdog.sweep_stuck_sessions")
def sweep_stuck_sessions():
    """Find stuck desktop-app sessions and either finalize or fail them."""
    db_url = _get_sync_db_url()
    if not db_url:
        logger.warning("DATABASE_URL not set, watchdog skipped")
        return {"finalized": 0, "failed": 0}

    finalized: list[str] = []
    failed: list[str] = []

    try:
        conn = psycopg2.connect(db_url)
        with conn:
            with conn.cursor() as cur:
                # 1. uploading sessions with at least 1 chunk, no chunk for N min
                cur.execute(
                    """
                    SELECT s.id::text
                    FROM sessions s
                    WHERE s.status = 'uploading'
                      AND EXISTS (SELECT 1 FROM chunks c WHERE c.session_id = s.id)
                      AND NOT EXISTS (
                        SELECT 1 FROM chunks c
                        WHERE c.session_id = s.id
                          AND c.uploaded_at > NOW() - (%s || ' minutes')::interval
                      )
                    """,
                    (UPLOADING_STALE_MIN,),
                )
                stuck_uploading = [r[0] for r in cur.fetchall()]

                # 2. created/uploading sessions with NO chunks, older than N min
                cur.execute(
                    """
                    SELECT s.id::text
                    FROM sessions s
                    WHERE s.status IN ('created', 'uploading')
                      AND s.created_at < NOW() - (%s || ' minutes')::interval
                      AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.session_id = s.id)
                    """,
                    (CREATED_STALE_MIN,),
                )
                empty_stuck = [r[0] for r in cur.fetchall()]

                # Finalize: flip to processing + queue pipeline
                for sid in stuck_uploading:
                    cur.execute(
                        "UPDATE sessions SET status = 'processing' WHERE id = %s AND status = 'uploading'",
                        (sid,),
                    )
                    if cur.rowcount:
                        finalized.append(sid)

                # Fail empty sessions
                now = datetime.now(timezone.utc)
                for sid in empty_stuck:
                    cur.execute(
                        "UPDATE sessions SET status = 'failed', finished_at = %s WHERE id = %s AND status IN ('created','uploading')",
                        (now, sid),
                    )
                    if cur.rowcount:
                        failed.append(sid)
        conn.close()
    except Exception:
        logger.exception("Watchdog DB sweep failed")
        return {"finalized": 0, "failed": 0, "error": True}

    # Queue pipeline tasks AFTER the DB transaction committed.
    for sid in finalized:
        try:
            app.send_task("pipeline.process_session", args=[sid], queue="transcription")
            logger.warning("[watchdog] finalized stuck session %s — pipeline queued", sid)
        except Exception:
            logger.exception("[watchdog] failed to queue pipeline for %s", sid)

    for sid in failed:
        logger.warning("[watchdog] failed empty session %s (no chunks)", sid)

    return {"finalized": len(finalized), "failed": len(failed)}
