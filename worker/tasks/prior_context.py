"""
Build `prior_context` for context-aware evaluation and next-call planning.

Pure in-memory helpers + one DB/filesystem orchestrator. Tests cover
the pure helpers; the orchestrator is exercised via integration smoke.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
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


def extend_history_with_events(
    interactions: list[dict],
    events: list[dict],
    before_iso: str,
) -> list[dict]:
    """
    Merge AmoCRM events (status changes, external notes) into the interactions list
    and mark status-change-without-call as offline_gap_inferred.
    Returns a new sorted-by-date list.
    """
    import datetime as _dt
    call_dates = {h.get("date") for h in interactions if h.get("type", "").startswith("call_")}
    before_date = before_iso.split("T")[0] if "T" in before_iso else before_iso[:10]

    extra: list[dict] = []
    for ev in events:
        ev_type = ev.get("type", "")
        created = ev.get("created_at", 0)
        if not isinstance(created, (int, float)):
            continue
        # Unix ts → YYYY-MM-DD (UTC)
        d = _dt.datetime.fromtimestamp(int(created), tz=_dt.timezone.utc).date().isoformat()
        if d >= before_date:
            continue  # skip events after the current call

        if ev_type == "lead_status_changed":
            note = {
                "date": d,
                "type": "offline_gap_inferred" if d not in call_dates else "stage_change",
                "has_data": d in call_dates,
                "detail": "Этап сменился",
            }
            extra.append(note)

    merged = sorted(interactions + extra, key=lambda x: x.get("date", ""))
    return merged


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
    last_call_plan = None

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

        if item.get("plan"):
            last_call_plan = item["plan"]

    client_profile = merge_client_profiles(profile_entries)

    return {
        "call_number": call_number,
        "previous_calls_count": previous_count,
        "client_profile": client_profile,
        "interactions_history": interactions_history,
        "open_objections": open_objections,
        "resolved_objections": resolved_objections,
        "last_call_plan": last_call_plan,
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


def _load_plan(session_id: str) -> dict | None:
    path = Path(RESULTS_PATH) / session_id / "next_call_plan.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception(f"Failed to read {path}")
        return None


def load_past_sessions_for_lead(lead_id: int, before_created_at: datetime | str) -> list[dict]:
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
        plan = _load_plan(session_id)
        entries.append({
            "created_at_iso": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
            "duration": duration,
            "direction": direction,
            "report": report,
            "plan": plan,
        })
    return entries


def build_prior_context_for_session(
    lead_id: int,
    current_created_at: datetime | str,
    deal_stage: dict | None = None,
    events: list[dict] | None = None,
) -> dict | None:
    """Top-level orchestrator used by pipeline. Returns None when lead_id is missing."""
    if not lead_id:
        return None
    past = load_past_sessions_for_lead(lead_id, current_created_at)
    iso_now = current_created_at.isoformat() if hasattr(current_created_at, "isoformat") else str(current_created_at)
    ctx = build_prior_context_dict(past, iso_now)
    if events:
        ctx["interactions_history"] = extend_history_with_events(
            ctx["interactions_history"], events, iso_now
        )
    if deal_stage:
        ctx["current_deal_stage"] = deal_stage
    return ctx
