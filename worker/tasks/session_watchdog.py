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
  3-reenqueue. status=processing AND transcript.json exists AND quality.json does
     NOT AND processing_started_at > ANALYZE_REENQUEUE_AFTER_MIN ago
     → стадия 1 отработала (транскрипт на диске), но handoff-analyze потерялся.
     Ре-энкьюим `pipeline.analyze_session` на очередь analysis, а НЕ выбрасываем
     дорогой транскрипт под нож 3a. Максимум ANALYZE_REENQUEUE_MAX попыток
     (счётчик в metadata.analyze_reenqueue_count) с паузой ≥ ANALYZE_REENQUEUE_AFTER_MIN
     между ними (metadata.analyze_reenqueue_at); дальше правило молкнет и сессию
     добьют 3a/3b. Идёт ДО 3a/3b: пока счётчик не исчерпан, транскрипт спасаем.
     Вход analyze идемпотентен (guard: completed+quality.json → ранний return).
  3a. status=processing AND processing_started_at set > PROCESSING_STALE_HOURS ago
     → mark `failed`. Stage started but the worker died / task got lost. Keyed on
     the worker's start mark, NOT created_at, so reprocess of old calls and
     fairness-slot waiting don't get killed.
  3b. status=processing AND processing_started_at IS NULL and
      COALESCE(enqueued_at, created_at) > ENQUEUED_STALE_HOURS
     → mark `failed`. Enqueued but the stage never started (task lost pre-start).
     Keyed on enqueued_at (moment we flipped to processing), NOT created_at, so a
     reprocess of an old call waiting in the queue is not falsely killed by its
     ancient created_at. Fallback to created_at is only for legacy rows stuck in
     processing before migration 017 (enqueued_at still NULL).
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from tenancy.context import (
    get_tenant_schema,
    get_tenant_slug,
    reset_tenant_schema,
    set_tenant_schema,
)
from tenancy.db import get_sync_db_url, tenant_connect
from tenancy.paths import tenant_audio_sessions_dir, tenant_results_dir

from tasks.celery_app import app

logger = logging.getLogger(__name__)

AUDIO_PATH = os.getenv("AUDIO_STORAGE_PATH", "./data/audio")
RESULTS_PATH = os.getenv("RESULTS_STORAGE_PATH", "./data/results")

UPLOADING_STALE_MIN = int(os.getenv("SESSION_UPLOADING_STALE_MIN", "15"))
CREATED_STALE_MIN = int(os.getenv("SESSION_CREATED_STALE_MIN", "60"))
# Rule 3-reenqueue: транскрипт готов, но analyze потерян. Через сколько минут
# «фактического старта» (processing_started_at) ре-энкьюим потерянный analyze,
# и та же пауза — минимум между повторными ре-энкью.
ANALYZE_REENQUEUE_AFTER_MIN = int(os.getenv("ANALYZE_REENQUEUE_AFTER_MIN", "30"))
# Максимум ре-энкью на сессию: дальше правило молчит, добивают 3a/3b (6ч, failed).
ANALYZE_REENQUEUE_MAX = 2
# Rule 3a: стадия стартовала (processing_started_at проставлен воркером) и висит
# дольше N часов — воркер умер / задача потерялась. Порог щедрый: > hard limit
# 90 мин + ретраи analyze. НЕ по created_at: reprocess старых звонков и
# ожидание fairness-слота не должны попадать под нож.
PROCESSING_STALE_HOURS = int(os.getenv("SESSION_PROCESSING_STALE_HOURS", "6"))
# Rule 3b: в processing, но стадия так и не стартовала (NULL) — задача потеряна
# до старта. Порог суточный: ожидание слота при залпе легитимно длится часами.
ENQUEUED_STALE_HOURS = int(os.getenv("SESSION_ENQUEUED_STALE_HOURS", "24"))


_get_sync_db_url = get_sync_db_url


def _transcript_exists(session_id: str) -> bool:
    """transcript.json со стадии 1 лежит на диске (тенант-контекст выставлен)."""
    return (tenant_results_dir(RESULTS_PATH, get_tenant_slug(), session_id)
            / "transcript.json").exists()


def _quality_exists(session_id: str) -> bool:
    """quality.json уже записан (стадия 2 / гейт завершились) → ре-энкью не нужен."""
    return (tenant_results_dir(RESULTS_PATH, get_tenant_slug(), session_id)
            / "quality.json").exists()


def _reenqueue_audio_path(session_id: str) -> str:
    """Канонический merged-wav (тот же, что отдаёт merge_chunks на стадии 1).
    В analyze используется только для длительности (ffprobe), недостающий файл
    деградирует мягко."""
    return str(tenant_audio_sessions_dir(AUDIO_PATH, get_tenant_slug(), session_id)
               / "full.wav")


def _as_meta(raw) -> dict:
    """Нормализовать metadata (jsonb dict / json-строка / NULL) в свежий dict."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _reenqueue_is_recent(meta: dict, now: datetime) -> bool:
    """С прошлого ре-энкью прошло < ANALYZE_REENQUEUE_AFTER_MIN → повтор рано."""
    last = meta.get("analyze_reenqueue_at")
    if not last:
        return False
    try:
        dt = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt) < timedelta(minutes=ANALYZE_REENQUEUE_AFTER_MIN)


def _sweep_for_current_tenant():
    """Find stuck desktop-app sessions and either finalize or fail them."""
    if not _get_sync_db_url():
        logger.warning("DATABASE_URL not set, watchdog skipped")
        return {"finalized": 0, "failed": 0, "stale_processing": 0,
                "stale_enqueued": 0, "reenqueued": 0}

    finalized: list[str] = []
    failed: list[str] = []
    reenqueue: list[str] = []
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

                # 3-reenqueue. Транскрипт готов, но handoff-analyze потерян →
                # ре-энкью на analysis (ДО 3a/3b-фейла), чтобы не выбрасывать
                # дорогой транскрипт. Кандидаты — по метке старта стадии 1
                # (processing_started_at), тот же якорь, что у 3a.
                cur.execute(
                    """
                    SELECT s.id::text, s.metadata
                    FROM sessions s
                    WHERE s.status = 'processing'
                      AND s.processing_started_at IS NOT NULL
                      AND s.processing_started_at < NOW() - (%s || ' minutes')::interval
                    """,
                    (ANALYZE_REENQUEUE_AFTER_MIN,),
                )
                reenqueue_candidates = cur.fetchall()
                for sid, meta_raw in reenqueue_candidates:
                    meta = _as_meta(meta_raw)
                    count = int(meta.get("analyze_reenqueue_count") or 0)
                    if count >= ANALYZE_REENQUEUE_MAX:
                        continue                    # исчерпан — молчим, добьют 3a/3b
                    if _reenqueue_is_recent(meta, now):
                        continue                    # прошлый ре-энкью слишком свежий
                    if not _transcript_exists(sid) or _quality_exists(sid):
                        continue                    # стадии 1 нет / стадия 2 уже готова
                    meta["analyze_reenqueue_count"] = count + 1
                    meta["analyze_reenqueue_at"] = now.isoformat()
                    cur.execute(
                        "UPDATE sessions SET metadata = %s "
                        "WHERE id = %s AND status = 'processing'",
                        (json.dumps(meta, ensure_ascii=False), sid),
                    )
                    if cur.rowcount:
                        reenqueue.append(sid)

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

                # 3b. Поставлена в обработку, но стадия не стартовала > N часов → failed.
                # Якорь — enqueued_at (момент постановки в обработку), НЕ created_at:
                # reprocess старого звонка в очереди не убивается по древнему created_at,
                # пока ждёт слот. COALESCE-fallback на created_at — только для легаси-строк,
                # зависших в processing до миграции 017 (enqueued_at ещё NULL): их гасим
                # по created_at как исторически зависшие.
                cur.execute(
                    """
                    UPDATE sessions
                    SET status = 'failed', finished_at = %s
                    WHERE status = 'processing'
                      AND processing_started_at IS NULL
                      AND COALESCE(enqueued_at, created_at) < NOW() - (%s || ' hours')::interval
                    RETURNING id::text
                    """,
                    (now, ENQUEUED_STALE_HOURS),
                )
                stale_enqueued = [r[0] for r in cur.fetchall()]
        conn.close()
    except Exception:
        logger.exception("Watchdog DB sweep failed")
        return {"finalized": 0, "failed": 0, "stale_processing": 0,
                "stale_enqueued": 0, "reenqueued": 0, "error": True}

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

    # Re-enqueue lost analyze AFTER the DB transaction committed (metadata-счётчик
    # уже инкрементнут). config={} — company/scenario резолвятся server-side из
    # тенанта (тот же дефолт, что при исходном browser/upload-прогоне).
    for sid in reenqueue:
        try:
            app.send_task(
                "pipeline.analyze_session",
                kwargs={
                    "session_id": sid,
                    "audio_path": _reenqueue_audio_path(sid),
                    "config": {},
                    "tenant_schema": get_tenant_schema(),
                },
                queue="analysis",
            )
            logger.warning(
                "[watchdog] re-enqueued lost analyze for session %s (transcript ready, quality missing)",
                sid,
            )
        except Exception:
            logger.exception("[watchdog] failed to re-enqueue analyze for %s", sid)

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
        "reenqueued": len(reenqueue),
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
