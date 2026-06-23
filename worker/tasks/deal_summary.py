"""
Build and render the rolling deal-summary note for a lead.

Deterministic aggregation of prior_context + the current call — no LLM.
Persistence (DB / AmoCRM note upsert) lives in the companion module entries
below; the builder/formatter here are pure so they can be unit-tested.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def build_deal_summary(
    prior_context: dict,
    current_quality_report: dict,
    stage_name: str | None,
    current_created_at: str,
) -> dict:
    """Aggregate prior_context + current call into a structured summary dict."""
    profile = dict(prior_context.get("client_profile") or {})

    # Count current call as part of history total
    total = len(prior_context.get("interactions_history") or []) + 1

    return {
        "stage": stage_name or "—",
        "total_interactions": total,
        "call_number": prior_context.get("call_number", 1),
        "client_profile": profile,
        "history": list(prior_context.get("interactions_history") or []),
        "open_objections": list(prior_context.get("open_objections") or []),
        "resolved_objections": list(prior_context.get("resolved_objections") or []),
        "open_objections_count": len(prior_context.get("open_objections") or []),
        "resolved_objections_count": len(prior_context.get("resolved_objections") or []),
        "last_call_plan": prior_context.get("last_call_plan"),
        "current_brief": current_quality_report.get("brief_summary") or "",
        "current_duration": int(current_quality_report.get("audio_duration") or 0),
        "updated_at": current_created_at,
    }


_PROFILE_LABELS = {
    "purchase_goal": "Цель",
    "locations": "Локация",
    "apartment_format": "Формат",
    "timeline": "Сроки",
    "budget": "Бюджет",
    "payment_form": "Оплата",
    "important_factors": "Важно",
    "what_viewed": "Смотрели",
    "current_situation": "Ситуация",
    "other": "Прочее",
}


def format_deal_summary(summary: dict) -> str:
    """Render the structured summary dict as the AmoCRM note markdown."""
    updated = (summary.get("updated_at") or "")[:10]
    lines: list[str] = [f"🏠 Сводка по сделке (AI, обновлена {updated})", ""]

    lines.append(f"📊 Этап: {summary['stage']}")
    lines.append(f"📞 Взаимодействий: {summary['total_interactions']}")
    lines.append("")

    profile = summary.get("client_profile") or {}
    if profile:
        lines.append("👤 Профиль клиента:")
        for key, label in _PROFILE_LABELS.items():
            val = profile.get(key)
            if val:
                lines.append(f"• {label}: {val}")
        lines.append("")

    history = summary.get("history") or []
    if history:
        lines.append("📅 История:")
        for h in history:
            mins = (h.get("duration") or 0) // 60
            type_label = {"call_in": "входящий звонок", "call_out": "исходящий звонок"}.get(h.get("type", ""), h.get("type", ""))
            lines.append(
                f"• {h.get('date', '')} ({type_label}, {mins} мин) — {h.get('brief', '')}"
            )
        # Add current call
        cur_mins = (summary.get("current_duration") or 0) // 60
        lines.append(
            f"• {updated} (текущий звонок, {cur_mins} мин) — {summary.get('current_brief', '')}"
        )
        lines.append("")

    open_obj = summary.get("open_objections") or []
    if open_obj:
        lines.append("❗ Открытые возражения:")
        for o in open_obj:
            lines.append(f"• «{o.get('text', '')}»")
        lines.append("")

    resolved = summary.get("resolved_objections") or []
    if resolved:
        lines.append("✅ Закрытые возражения:")
        for o in resolved:
            how = o.get("how") or ""
            when = o.get("resolved_at") or ""
            suffix = f" — {how}" if how else ""
            date_suffix = f" ({when})" if when else ""
            lines.append(f"• «{o.get('text', '')}»{suffix}{date_suffix}")
        lines.append("")

    plan = summary.get("last_call_plan")
    if plan:
        goals = sorted(plan.get("goals") or [], key=lambda g: g.get("priority", 99))
        if goals:
            lines.append("➡️ Следующие шаги (из последнего плана):")
            for g in goals:
                lines.append(f"• {g.get('text', '')}")
            lines.append("")

    return "\n".join(lines).strip()


# === Persistence ==================================================================

import json
from datetime import datetime, timezone

from tenancy.db import get_sync_db_url, tenant_connect

_get_sync_db_url = get_sync_db_url


def get_existing_summary_note_id(lead_id: int) -> int | None:
    if not _get_sync_db_url() or not lead_id:
        return None
    try:
        conn = tenant_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT amo_note_id FROM amocrm_deal_summaries WHERE lead_id=%s", (lead_id,))
                row = cur.fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:
        logger.exception(f"Failed to read deal summary for lead {lead_id}")
        return None


def upsert_summary_record(lead_id: int, amo_note_id: int, content: dict) -> None:
    if not _get_sync_db_url() or not lead_id:
        return
    try:
        conn = tenant_connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO amocrm_deal_summaries (lead_id, amo_note_id, updated_at, content_json)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (lead_id) DO UPDATE
                           SET amo_note_id = EXCLUDED.amo_note_id,
                               updated_at  = EXCLUDED.updated_at,
                               content_json = EXCLUDED.content_json
                        """,
                        (lead_id, amo_note_id, datetime.now(timezone.utc), json.dumps(content, ensure_ascii=False)),
                    )
        finally:
            conn.close()
    except Exception:
        logger.exception(f"Failed to upsert deal summary for lead {lead_id}")


def push_deal_summary(lead_id: int, summary: dict) -> int | None:
    """Format + create-or-update the AmoCRM summary note. Returns the note_id."""
    from tasks.amocrm_sync import create_plain_note, update_note
    text = format_deal_summary(summary)
    existing = get_existing_summary_note_id(lead_id)
    if existing:
        result = update_note(lead_id, existing, text)
        if result.get("ok"):
            upsert_summary_record(lead_id, existing, summary)
            logger.info(f"Updated deal summary note {existing} for lead {lead_id}")
            return existing
        logger.warning(f"Failed to update summary note {existing}, will try create: {result}")

    result = create_plain_note(lead_id, text)
    if result.get("ok"):
        note_id = result["note_id"]
        upsert_summary_record(lead_id, note_id, summary)
        logger.info(f"Created deal summary note {note_id} for lead {lead_id}")
        return note_id
    logger.warning(f"Failed to create summary note for lead {lead_id}: {result}")
    return None
