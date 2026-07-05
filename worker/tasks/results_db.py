"""Аналитическая проекция отчётов качества → тенант-таблица quality_results.

Файл quality.json остаётся источником истины; здесь — денормализованная
строка для SQL-агрегаций дашборда (/api/stats). Все внешние вызовы
best-effort: сбой записи НИКОГДА не валит пайплайн.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from tenancy.context import get_tenant_slug
from tenancy.db import tenant_connect

logger = logging.getLogger(__name__)

RISK_DEFAULTS = {"risk_score_below": 4, "negative_sentiment_ratio": 0.4}


def extract_objections(quality_report: dict | None, card: dict | None) -> list[dict]:
    """Нормализует возражения из v4-quality и card-extraction к единому виду.

    Приоритет — quality (там category-enum); карточные берём, только если
    quality-список пуст (у card нет category → "other")."""
    out: list[dict] = []
    for raw in (quality_report or {}).get("objections") or []:
        if not isinstance(raw, dict) or not raw.get("text"):
            continue
        out.append({
            "text": str(raw["text"]),
            "category": str(raw.get("category") or "other"),
            "resolved": raw.get("resolved") if isinstance(raw.get("resolved"), bool) else None,
            "handling_quality": raw.get("handling_quality")
            if isinstance(raw.get("handling_quality"), int) else None,
        })
    if out:
        return out
    for raw in (card or {}).get("objections") or []:
        if not isinstance(raw, dict) or not raw.get("objection"):
            continue
        out.append({
            "text": str(raw["objection"]),
            "category": "other",
            "resolved": raw.get("handled") if isinstance(raw.get("handled"), bool) else None,
            "handling_quality": None,
        })
    return out


def compute_talk_metrics(transcript: list | None, speaker_roles: dict | None) -> dict | None:
    """Суммы говорения по ролям из сегментов {speaker,start,end}.

    Роли — из speaker_roles (LLM: manager/client/...); без ролей — топ-2
    спикера по времени занимают слоты manager/client с пометкой unattributed."""
    segs = [s for s in (transcript or [])
            if isinstance(s, dict) and isinstance(s.get("start"), (int, float))
            and isinstance(s.get("end"), (int, float)) and s["end"] > s["start"]]
    if not segs:
        return None

    per_speaker: dict[str, float] = {}
    for s in segs:
        sp = str(s.get("speaker") or "UNKNOWN")
        per_speaker[sp] = per_speaker.get(sp, 0.0) + (s["end"] - s["start"])

    # Реальная форма ролей — {spk: {"role": ..., "name": ...}}; терпим и плоскую
    # {spk: "manager"}. Из объекта берём ключ role, строку — как есть.
    roles = {}
    for k, v in (speaker_roles or {}).items():
        role_val = v.get("role") if isinstance(v, dict) else v
        roles[str(k)] = str(role_val or "")
    unattributed = not any(v == "manager" for v in roles.values())
    if unattributed:
        ranked = sorted(per_speaker, key=per_speaker.get, reverse=True)
        roles = {}
        if ranked:
            roles[ranked[0]] = "manager"
        if len(ranked) > 1:
            roles[ranked[1]] = "client"

    manager_sec = client_sec = other_sec = 0.0
    for sp, sec in per_speaker.items():
        role = roles.get(sp)
        if role == "manager":
            manager_sec += sec
        elif role == "client":
            client_sec += sec
        else:
            other_sec += sec

    talked = manager_sec + client_sec
    longest, cur, cur_sp = 0.0, 0.0, None
    for s in segs:  # сегменты идут в хронологии транскрипта
        sp = str(s.get("speaker") or "UNKNOWN")
        cur = cur + (s["end"] - s["start"]) if sp == cur_sp else (s["end"] - s["start"])
        cur_sp = sp
        longest = max(longest, cur)

    return {
        "manager_sec": round(manager_sec, 2),
        "client_sec": round(client_sec, 2),
        "other_sec": round(other_sec, 2),
        "talk_ratio": round(manager_sec / talked, 4) if talked > 0 else None,
        "longest_monologue_sec": round(longest, 2),
        "unattributed": unattributed,
    }


def sentiment_counts_from(sentiment_results: list | None) -> dict | None:
    if sentiment_results is None:
        return None
    counts = {"positive": 0, "neutral": 0, "negative": 0}
    for r in sentiment_results:
        label = (r.get("sentiment") or "").lower() if isinstance(r, dict) else ""
        if label in counts:
            counts[label] += 1
    return counts


def compute_risk_flags(overall_score, sentiment_counts, objections, thresholds: dict | None) -> list[str]:
    th = {**RISK_DEFAULTS, **(thresholds or {})}
    flags: list[str] = []
    if isinstance(overall_score, (int, float)) and overall_score < th["risk_score_below"]:
        flags.append("low_score")
    if sentiment_counts:
        total = sum(sentiment_counts.values())
        if total and sentiment_counts.get("negative", 0) / total > th["negative_sentiment_ratio"]:
            flags.append("negative_sentiment")
    if any(o.get("resolved") is False for o in (objections or [])):
        flags.append("unresolved_objections")
    return flags


_UPSERT_SQL = """
INSERT INTO quality_results (
    session_id, overall_score, version, scenario_id, employee,
    session_created_at, duration_seconds, skip_reason,
    criteria, objections, talk_metrics, sentiment_counts, risk_flags, updated_at
) VALUES (
    %(session_id)s, %(overall_score)s, %(version)s, %(scenario_id)s, %(employee)s,
    %(session_created_at)s, %(duration_seconds)s, %(skip_reason)s,
    %(criteria)s, %(objections)s, %(talk_metrics)s, %(sentiment_counts)s,
    %(risk_flags)s, now()
)
ON CONFLICT (session_id) DO UPDATE SET
    overall_score = EXCLUDED.overall_score,
    version = EXCLUDED.version,
    scenario_id = EXCLUDED.scenario_id,
    employee = EXCLUDED.employee,
    session_created_at = EXCLUDED.session_created_at,
    duration_seconds = EXCLUDED.duration_seconds,
    skip_reason = EXCLUDED.skip_reason,
    criteria = EXCLUDED.criteria,
    objections = EXCLUDED.objections,
    talk_metrics = EXCLUDED.talk_metrics,
    sentiment_counts = EXCLUDED.sentiment_counts,
    risk_flags = EXCLUDED.risk_flags,
    updated_at = now()
RETURNING (xmax = 0) AS inserted
"""


def _jsonb(value):
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def upsert_quality_result(session_id: str, *, overall_score, version, scenario_id,
                          employee, session_created_at, duration_seconds, skip_reason,
                          criteria, objections, talk_metrics, sentiment_counts,
                          risk_flags) -> bool:
    """Голый идемпотентный upsert. Исключения НЕ глотает — ловит вызывающий.

    Возвращает True при ПЕРВОЙ вставке строки (xmax = 0), False при UPDATE
    по ON CONFLICT (редоставка/reprocess) — нужно для однократного алерта."""
    conn = tenant_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(_UPSERT_SQL, {
                    "session_id": session_id,
                    "overall_score": overall_score,
                    "version": version,
                    "scenario_id": scenario_id,
                    "employee": employee,
                    "session_created_at": session_created_at,
                    "duration_seconds": duration_seconds,
                    "skip_reason": skip_reason,
                    "criteria": _jsonb(criteria),
                    "objections": _jsonb(objections),
                    "talk_metrics": _jsonb(talk_metrics),
                    "sentiment_counts": _jsonb(sentiment_counts),
                    "risk_flags": _jsonb(risk_flags or []),
                })
                inserted = bool(cur.fetchone()[0])
    finally:
        conn.close()
    return inserted


def _fetch_session_row(session_id: str):
    conn = tenant_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metadata, created_at, duration_seconds FROM sessions WHERE id = %s",
                (session_id,))
            return cur.fetchone()
    finally:
        conn.close()


def _send_risk_alert_safe(**kwargs) -> None:
    """Локальный импорт: alerts появляется в соседней таске; без него — молчим."""
    try:
        from tasks.alerts import send_risk_alert
        send_risk_alert(**kwargs)
    except ImportError:
        pass
    except Exception:
        logger.exception("risk alert failed (continuing)")


def record_quality_result(session_id: str, quality_report: dict | None, *,
                          card: dict | None = None, transcript: list | None = None,
                          sentiment_results: list | None = None,
                          company_config: dict | None = None,
                          skip_reason: str | None = None,
                          duration_seconds: float | None = None,
                          suppress_alert: bool = False) -> list[str]:
    """Единая best-effort точка записи аналитики (пайплайн зовёт только её).

    Алерт шлём только при ПЕРВОЙ вставке строки и при suppress_alert=False:
    редоставка/reprocess (UPDATE по конфликту) и бэкфилл истории не спамят."""
    try:
        row = _fetch_session_row(session_id)
        if row is None:
            logger.warning(f"[{session_id}] quality_results: сессия не найдена — скип")
            return []
        meta = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
        report = quality_report or {}

        objections = extract_objections(report, card)
        talk = compute_talk_metrics(transcript, report.get("speaker_roles"))
        counts = sentiment_counts_from(sentiment_results)
        thresholds = (company_config or {}).get("alerts")
        flags = [] if skip_reason else compute_risk_flags(
            report.get("overall_score"), counts, objections, thresholds)

        inserted = upsert_quality_result(
            session_id,
            overall_score=report.get("overall_score"),
            version=report.get("score_version") or report.get("version"),
            scenario_id=meta.get("scenario_id"),
            employee=meta.get("employee"),
            session_created_at=row[1] or datetime.now(timezone.utc),
            duration_seconds=duration_seconds if duration_seconds is not None else row[2],
            skip_reason=skip_reason,
            criteria=report.get("criteria"),
            objections=objections,
            talk_metrics=talk,
            sentiment_counts=counts,
            risk_flags=flags,
        )
        if flags and inserted and not suppress_alert:
            _send_risk_alert_safe(
                slug=get_tenant_slug(), session_id=session_id,
                employee=meta.get("employee"),
                score=report.get("overall_score"), flags=flags,
                company_config=company_config)
        return flags
    except Exception:
        logger.exception(f"[{session_id}] quality_results запись не удалась (пайплайн продолжает)")
        return []
