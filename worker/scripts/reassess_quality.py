"""
Light reassess: re-run quality + next_call_plan on already-transcribed calls
using the new MEETING_PLAYBOOK prompts, WITHOUT re-transcribing.

Reads saved transcript.json + sentiment.json from RESULTS_STORAGE_PATH,
writes new results to quality_v2.json + next_call_plan_v2.json in the same
folder. Original quality.json / next_call_plan.json are left untouched for
side-by-side comparison.

Usage (from /root/projects/realestate/worker):

    ../.venv/bin/python3 -m scripts.reassess_quality --tenant <slug> [--limit N] [--only SESSION_ID]

После массовой переоценки прогони backfill_quality_results.py --apply — таблица quality_results обновится из файлов.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


# Load .env from repo root before importing worker tasks (they read env at import time)
REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

# Make sure tasks + tenancy packages are importable when run as `python -m scripts.reassess_quality`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO_ROOT))

from tenancy.context import set_tenant_schema  # noqa: E402
from tenancy.db import get_sync_db_url, tenant_connect  # noqa: E402
from tenancy.identifiers import schema_for_slug  # noqa: E402

from tasks.quality import assess_quality, plan_next_call  # noqa: E402
from tasks.prior_context import build_prior_context_for_session  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("reassess")
# Quiet the noisy info logs from tasks.quality
logging.getLogger("tasks.quality").setLevel(logging.WARNING)

RESULTS_ROOT = os.getenv("RESULTS_STORAGE_PATH", "./data/results")
# Per-tenant results dir (<RESULTS_ROOT>/<slug>); finalized in main() from --tenant.
RESULTS_PATH = Path(RESULTS_ROOT)

_sync_db_url = get_sync_db_url


def list_completed_sessions() -> list[tuple[str, Any, Any]]:
    """Return (session_id, created_at, metadata) for all completed sessions, oldest first."""
    db_url = _sync_db_url()
    if not db_url:
        raise RuntimeError("DATABASE_URL_SYNC not set")
    conn = tenant_connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, metadata
                FROM sessions
                WHERE status = 'completed'
                ORDER BY created_at ASC
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return rows


def _load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception(f"Failed to read {path}")
        return None


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def reassess_one(session_id: str, created_at: Any, metadata: Any) -> dict:
    sdir = RESULTS_PATH / session_id
    transcript = _load_json(sdir / "transcript.json")
    sentiment = _load_json(sdir / "sentiment.json")
    old_quality = _load_json(sdir / "quality.json") or {}

    if transcript is None or sentiment is None:
        return {"session_id": session_id, "status": "skipped", "reason": "missing_transcript_or_sentiment"}
    if old_quality.get("skip_reason") == "too_short":
        return {"session_id": session_id, "status": "skipped", "reason": "too_short"}

    meta = metadata if isinstance(metadata, dict) else (json.loads(metadata) if metadata else {})
    lead_id = meta.get("lead_id")

    prior_ctx = None
    if lead_id:
        try:
            prior_ctx = build_prior_context_for_session(lead_id, created_at)
        except Exception:
            logger.exception(f"[{session_id}] prior_context build failed, proceeding without it")

    try:
        new_quality = assess_quality(
            transcript=transcript,
            sentiment_results=sentiment,
            use_extended_schema=True,
            prior_context=prior_ctx,
        )
    except Exception as e:
        return {"session_id": session_id, "status": "error", "reason": f"assess_quality: {e}"}

    if new_quality.get("error"):
        return {"session_id": session_id, "status": "error", "reason": new_quality["error"]}

    _save_json(sdir / "quality_v2.json", new_quality)

    # Plan requires prior_context; for calls with no prior context we pass a minimal dict
    try:
        plan = plan_next_call(
            prior_context=prior_ctx or {"previous_calls_count": 0},
            current_quality_report=new_quality,
            deal_stage=None,
        )
    except Exception as e:
        logger.warning(f"[{session_id}] plan_next_call failed: {e}")
        plan = None

    if plan is not None:
        _save_json(sdir / "next_call_plan_v2.json", plan)

    return {
        "session_id": session_id,
        "status": "done",
        "overall_score_old": old_quality.get("overall_score"),
        "overall_score_new": new_quality.get("overall_score"),
        "maa_push_quality": (new_quality.get("meeting_argumentation_assessment") or {}).get("overall_push_quality"),
    }


def main() -> int:
    global RESULTS_PATH

    parser = argparse.ArgumentParser(description="Light reassess for quality+plan with new prompts")
    parser.add_argument("--tenant", required=True, help="tenant slug, e.g. realestate")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of sessions (for testing)")
    parser.add_argument("--only", type=str, default=None, help="Process only this session_id")
    parser.add_argument("--skip-existing", action="store_true", help="Skip sessions that already have quality_v2.json")
    parser.add_argument("--workers", type=int, default=1, help="Parallel LLM requests (be mindful of OpenAI rate limits)")
    args = parser.parse_args()

    tenant_schema = schema_for_slug(args.tenant)
    set_tenant_schema(tenant_schema)
    RESULTS_PATH = Path(RESULTS_ROOT) / args.tenant

    if not os.getenv("OPENAI_API_KEY"):
        logger.error("OPENAI_API_KEY is not set — cannot run LLM reassess")
        return 1

    sessions = list_completed_sessions()
    if args.only:
        sessions = [s for s in sessions if s[0] == args.only]
    if args.limit:
        sessions = sessions[: args.limit]

    total = len(sessions)
    logger.info(f"Sessions to process: {total}")
    logger.info(f"Results dir:         {RESULTS_PATH}")

    summary = {"done": 0, "skipped": 0, "error": 0, "skipped_existing": 0}
    results: list[dict] = []
    progress_lock = threading.Lock()
    progress = {"done": 0}
    t_start = time.time()

    def _task(i: int, sid: str, created: Any, meta: Any) -> dict:
        # ThreadPoolExecutor threads start with a fresh contextvars context —
        # re-set the tenant so DB access inside reassess_one stays scoped.
        set_tenant_schema(tenant_schema)
        sdir = RESULTS_PATH / sid
        if args.skip_existing and (sdir / "quality_v2.json").exists():
            return {"session_id": sid, "status": "skipped_existing", "_order": i}
        t = time.time()
        try:
            res = reassess_one(sid, created, meta)
        except Exception as e:
            logger.exception(f"[{sid}] unexpected error")
            res = {"session_id": sid, "status": "error", "reason": str(e)}
        res["_order"] = i
        res["_dt"] = time.time() - t
        return res

    if args.workers > 1:
        logger.info(f"Parallel mode: {args.workers} workers")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [
                pool.submit(_task, i, sid, created, meta)
                for i, (sid, created, meta) in enumerate(sessions, start=1)
            ]
            for fut in as_completed(futures):
                res = fut.result()
                results.append(res)
                status = res.get("status", "error")
                summary[status] = summary.get(status, 0) + 1
                with progress_lock:
                    progress["done"] += 1
                    n = progress["done"]
                extra = ""
                if status == "done":
                    extra = f"score {res.get('overall_score_old')} → {res.get('overall_score_new')} | push={res.get('maa_push_quality')}"
                elif status in ("skipped", "error"):
                    extra = res.get("reason", "")
                logger.info(f"[{n}/{total}] {status:16s} {res['session_id']} ({res.get('_dt',0):5.1f}s) {extra}")
    else:
        for i, (sid, created, meta) in enumerate(sessions, start=1):
            res = _task(i, sid, created, meta)
            results.append(res)
            status = res.get("status", "error")
            summary[status] = summary.get(status, 0) + 1
            extra = ""
            if status == "done":
                extra = f"score {res.get('overall_score_old')} → {res.get('overall_score_new')} | push={res.get('maa_push_quality')}"
            elif status in ("skipped", "error"):
                extra = res.get("reason", "")
            logger.info(f"[{i}/{total}] {status:16s} {sid} ({res.get('_dt',0):5.1f}s) {extra}")

    elapsed = time.time() - t_start
    logger.info("=" * 60)
    logger.info(f"Total: {total} | done: {summary.get('done',0)} | skipped: {summary.get('skipped',0)} | "
                f"skipped_existing: {summary.get('skipped_existing',0)} | error: {summary.get('error',0)}")
    logger.info(f"Elapsed: {elapsed:.0f}s ({elapsed/max(total,1):.1f}s/call avg)")

    # Dump run summary for later analysis
    summary_path = RESULTS_PATH / "_reassess_summary.json"
    _save_json(summary_path, {
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": total,
        "counts": summary,
        "elapsed_sec": elapsed,
        "results": results,
    })
    logger.info(f"Summary saved to {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
