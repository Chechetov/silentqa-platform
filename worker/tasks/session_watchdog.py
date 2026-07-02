"""
Watchdog for stuck desktop-app sessions.

Desktop client uploads chunks every 10s and calls /finish when the user stops.
If the client crashes / loses network / the laptop sleeps mid-call, the session
sits in `uploading` (or `created`) forever — the chunks we already received
never get processed and the operator sees a permanent "uploading" badge.

Recovery rules:
  1. Has at least one chunk AND last chunk uploaded > UPLOADING_STALE_MIN ago
     → kick the same pipeline that /finish would have, on whatever audio we have.
  2. No chunks AND created > CREATED_STALE_MIN ago
     → mark `failed`. Nothing to process.
  3a. status=processing AND processing_started_at set > PROCESSING_STALE_HOURS ago
     → mark `failed`. Stage started but the worker died / task got lost. Keyed on
     the worker's start mark, NOT created_at, so reprocess of old calls and
     fairness-slot waiting don't get killed.
  3b. status=processing AND processing_started_at IS NULL and created > ENQUEUED_STALE_HOURS
     → mark `failed`. Enqueued but the stage never started (task lost pre-start).
"""
import logging
import os
from datetime import datetime, timezone

from tenancy.context import get_tenant_schema, reset_tenant_schema, set_tenant_schema
from tenancy.db import get_sync_db_url, tenant_connect

from tasks.celery_app import app

logger = logging.getLogger(__name__)

UPLOADING_STALE_MIN = int(os.getenv("SESSION_UPLOADING_STALE_MIN", "15"))
CREATED_STALE_MIN = int(os.getenv("SESSION_CREATED_STALE_MIN", "60"))
# Rule 3a: стадия стартовала (processing_started_at проставлен воркером) и висит
# дольше N часов — воркер умер / задача потерялась. Порог щедрый: > hard limit
# 90 мин + ретраи analyze. НЕ по created_at: reprocess старых звонков и
# ожидание fairness-слота не должны попадать под нож.
PROCESSING_STALE_HOURS = int(os.getenv("SESSION_PROCESSING_STALE_HOURS", "6"))
# Rule 3b: в processing, но стадия так и не стартовала (NULL) — задача потеряна
# до старта. Порог суточный: ожидание слота при залпе легитимно длится часами.
ENQUEUED_STALE_HOURS = int(os.getenv("SESSION_ENQUEUED_STALE_HOURS", "24"))


_get_sync_db_url = get_sync_db_url


def _sweep_for_current_tenant():
    """Find stuck desktop-app sessions and either finalize or fail them."""
    if not _get_sync_db_url():
        logger.warning("DATABASE_URL not set, watchdog skipped")
        return {"finalized": 0, "failed": 0, "stale_processing": 0, "stale_enqueued": 0}

    finalized: list[str] = []
    failed: list[str] = []
    stale_processing: list[str] = []
    stale_enqueued: list[str] = []

    try:
        conn = tenant_connect()
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

                # 3a. Стадия стартовала и висит > N часов → failed
                cur.execute(
                    """
                    UPDATE sessions
                    SET status = 'failed', finished_at = %s
                    WHERE status = 'processing'
                      AND processing_started_at IS NOT NULL
                      AND processing_started_at < NOW() - (%s || ' hours')::interval
                    RETURNING id::text
                    """,
                    (now, PROCESSING_STALE_HOURS),
                )
                stale_processing = [r[0] for r in cur.fetchall()]

                # 3b. Поставлена в обработку, но стадия не стартовала > N часов → failed
                cur.execute(
                    """
                    UPDATE sessions
                    SET status = 'failed', finished_at = %s
                    WHERE status = 'processing'
                      AND processing_started_at IS NULL
                      AND created_at < NOW() - (%s || ' hours')::interval
                    RETURNING id::text
                    """,
                    (now, ENQUEUED_STALE_HOURS),
                )
                stale_enqueued = [r[0] for r in cur.fetchall()]
        conn.close()
    except Exception:
        logger.exception("Watchdog DB sweep failed")
        return {"finalized": 0, "failed": 0, "stale_processing": 0,
                "stale_enqueued": 0, "error": True}

    # Queue pipeline tasks AFTER the DB transaction committed.
    for sid in finalized:
        try:
            app.send_task(
                "pipeline.process_session",
                args=[sid],
                kwargs={"tenant_schema": get_tenant_schema()},
                queue="transcription",
            )
            logger.warning("[watchdog] finalized stuck session %s — pipeline queued", sid)
        except Exception:
            logger.exception("[watchdog] failed to queue pipeline for %s", sid)

    for sid in failed:
        logger.warning("[watchdog] failed empty session %s (no chunks)", sid)

    for sid in stale_processing:
        logger.warning(
            "[watchdog] failed stale processing session %s (started >%sh ago)",
            sid, PROCESSING_STALE_HOURS,
        )

    for sid in stale_enqueued:
        logger.warning(
            "[watchdog] failed stuck-enqueued session %s (never started >%sh)",
            sid, ENQUEUED_STALE_HOURS,
        )

    return {
        "finalized": len(finalized),
        "failed": len(failed),
        "stale_processing": len(stale_processing),
        "stale_enqueued": len(stale_enqueued),
    }


@app.task(name="session_watchdog.sweep_stuck_sessions")
def sweep_stuck_sessions():
    """Beat task: run the stuck-session sweep for every active tenant."""
    from tenancy.registry import iter_active_tenants
    for t in iter_active_tenants():
        token = set_tenant_schema(t["schema_name"])
        try:
            _sweep_for_current_tenant()
        except Exception:
            logger.exception(f"watchdog sweep failed for tenant {t['slug']}")
        finally:
            reset_tenant_schema(token)
