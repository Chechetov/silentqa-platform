"""Аналитическая проекция отчётов качества → тенант-таблица quality_results.

Файл quality.json остаётся источником истины; здесь — денормализованная
строка для SQL-агрегаций дашборда (/api/stats). Все внешние вызовы
best-effort: сбой записи НИКОГДА не валит пайплайн.
"""
from __future__ import annotations

import logging

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

    roles = {str(k): str(v) for k, v in (speaker_roles or {}).items()}
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
