"""
One-shot backfill: push existing linked sessions to AmoCRM.

Until now, desktop/Zoom/upload sessions linked to a lead via the dashboard
never landed in AmoCRM because of a publish-gate in _push_to_amocrm that
required a Publish button which was never implemented. The gate is gone,
but old sessions that were linked under the previous behaviour still have
metadata.lead_id set with no metadata.amo_note_id — meaning the deal in
AmoCRM has no record of the call. This script walks those sessions and
publishes them.

Idempotent: a session is only picked up if amo_note_id is missing. Stub
quality reports (short call, broken recording, extraction-only) are
skipped — they carry a skip_reason and should not pollute the deal.

Deal summary is built with an empty prior_context (degraded). The
singleton deal_summary note gets a proper rebuild on the next live call
landing on that lead.

Usage (from /root/projects/realestate/worker):

    ../.venv/bin/python3 -m scripts.backfill_amocrm_push --tenant <slug>           # dry-run
    ../.venv/bin/python3 -m scripts.backfill_amocrm_push --tenant <slug> --apply   # actually push
    ../.venv/bin/python3 -m scripts.backfill_amocrm_push --tenant <slug> --apply --limit 5
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# `python -m scripts.backfill_amocrm_push` from the worker root puts cwd on
# sys.path automatically; the repo root (for `tenancy`) has to be added by hand.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tenancy.context import require_tenant_slug, set_tenant_schema  # noqa: E402
from tenancy.db import get_sync_db_url, tenant_connect  # noqa: E402
from tenancy.identifiers import schema_for_slug  # noqa: E402
from tenancy.paths import tenant_results_dir  # noqa: E402

from tasks.pipeline import _push_to_amocrm  # noqa: E402

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill_amocrm_push")


_get_sync_db_url = get_sync_db_url

RESULTS_ROOT = os.getenv("RESULTS_STORAGE_PATH", "./data/results")


def _find_candidates(limit: int | None) -> list[dict]:
    """Sessions with a linked lead, no AmoCRM note yet, completed pipeline."""
    sql = """
        SELECT id::text AS id, metadata, status, created_at
        FROM sessions
        WHERE status = 'completed'
          AND metadata ? 'lead_id'
          AND metadata->>'lead_id' IS NOT NULL
          AND (NOT metadata ? 'amo_note_id' OR metadata->>'amo_note_id' IS NULL)
        ORDER BY created_at DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with tenant_connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]


def _load_json(session_id: str, key: str) -> dict | None:
    path = tenant_results_dir(RESULTS_ROOT, require_tenant_slug(), session_id) / f"{key}.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception(f"[{session_id}] failed to load {key}.json")
        return None


def _classify(session_id: str, meta: dict) -> tuple[str, dict | None, dict | None]:
    """Return (status_label, quality, plan). status_label is 'push' or a skip reason."""
    quality = _load_json(session_id, "quality")
    if quality is None:
        return ("skip:no_quality_json", None, None)
    skip_reason = quality.get("skip_reason")
    if skip_reason:
        return (f"skip:stub_{skip_reason}", None, None)
    if not meta.get("lead_id"):
        return ("skip:no_lead_id", None, None)
    plan = _load_json(session_id, "next_call_plan")
    return ("push", quality, plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="tenant slug, e.g. realestate")
    parser.add_argument("--apply", action="store_true", help="Actually push to AmoCRM (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="Cap number of sessions processed")
    args = parser.parse_args()

    set_tenant_schema(schema_for_slug(args.tenant))

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info(f"Mode: {mode}")

    candidates = _find_candidates(args.limit)
    logger.info(f"Found {len(candidates)} candidate session(s) with lead_id and no amo_note_id")

    counts: dict[str, int] = {}
    pushed: list[str] = []
    failed: list[tuple[str, str]] = []

    for row in candidates:
        sid = row["id"]
        meta = row["metadata"] or {}
        lead_id = meta.get("lead_id")
        status_label, quality, plan = _classify(sid, meta)
        counts[status_label] = counts.get(status_label, 0) + 1

        if status_label != "push":
            logger.info(f"[{sid}] lead={lead_id} → {status_label}")
            continue

        logger.info(f"[{sid}] lead={lead_id} → ready (quality score={quality.get('overall_score')}, plan={'yes' if plan else 'no'})")

        if not args.apply:
            continue

        try:
            # audio_path empty: _get_audio_duration handles missing path silently
            # prior_context_for_summary None: deal_summary builds a degraded singleton
            _push_to_amocrm(
                session_id=sid,
                quality_report=quality,
                session_meta=meta,
                audio_path="",
                next_call_plan=plan,
                prior_context_for_summary=None,
            )
            pushed.append(sid)
        except Exception as e:
            logger.exception(f"[{sid}] push failed")
            failed.append((sid, str(e)[:200]))

    logger.info("---")
    logger.info(f"Summary ({mode}):")
    for label, n in sorted(counts.items()):
        logger.info(f"  {label}: {n}")
    if args.apply:
        logger.info(f"  pushed: {len(pushed)}")
        logger.info(f"  failed: {len(failed)}")
        for sid, err in failed:
            logger.warning(f"    {sid}: {err}")


if __name__ == "__main__":
    main()
