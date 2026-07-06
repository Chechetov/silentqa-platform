"""Агрегаты дашборда руководителя поверх quality_results (миграция 018).

Идиома модуля: _*_rows-хелперы (text()-SQL, monkeypatch в тестах) + тонкие
хендлеры. avg_score везде взвешенный (AVG по строкам с overall_score),
calls — все строки (включая skip-гейты, чтобы биться со списком звонков).
Менеджерский скоуп (employee_scope) сужает ВСЕ запросы до своего employee."""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_user import employee_scope, require_viewer
from app.database import get_db

router = APIRouter(prefix="/api/stats", tags=["stats"],
                   dependencies=[Depends(require_viewer)])

_SCOPE_SQL = "AND (CAST(:scope AS TEXT) IS NULL OR employee = :scope)"
_SCENARIO_SQL = "AND (CAST(:scenario AS TEXT) IS NULL OR scenario_id = :scenario)"


async def _kpi_row(db: AsyncSession, scope, start, end, scenario=None) -> dict:
    row = (await db.execute(text(f"""
        SELECT COUNT(*) AS calls,
               COUNT(overall_score) AS scored_calls,
               ROUND(AVG(overall_score)::numeric, 2) AS avg_score,
               COUNT(*) FILTER (WHERE jsonb_array_length(risk_flags) > 0) AS risk_calls,
               ROUND(AVG((talk_metrics->>'talk_ratio')::numeric), 3) AS avg_talk_ratio
        FROM quality_results
        WHERE session_created_at >= :start AND session_created_at < :end {_SCOPE_SQL} {_SCENARIO_SQL}
    """), {"scope": scope, "scenario": scenario, "start": start, "end": end})).mappings().one()
    return {k: (float(v) if k in ("avg_score", "avg_talk_ratio") and v is not None else v)
            for k, v in dict(row).items()}


async def _series_rows(db: AsyncSession, scope, start, end, granularity, scenario=None) -> list[dict]:
    rows = (await db.execute(text(f"""
        SELECT to_char(date_trunc(:g, session_created_at), 'YYYY-MM-DD') AS bucket,
               COUNT(*) AS calls,
               ROUND(AVG(overall_score)::numeric, 2) AS avg_score
        FROM quality_results
        WHERE session_created_at >= :start AND session_created_at < :end {_SCOPE_SQL} {_SCENARIO_SQL}
        GROUP BY 1 ORDER BY 1
    """), {"g": granularity, "scope": scope, "scenario": scenario, "start": start, "end": end})).mappings().all()
    return [{"bucket": r["bucket"], "calls": r["calls"],
             "avg_score": float(r["avg_score"]) if r["avg_score"] is not None else None}
            for r in rows]


async def _manager_rows(db: AsyncSession, scope, start, scenario=None) -> list[dict]:
    rows = (await db.execute(text(f"""
        SELECT employee AS name, COUNT(*) AS calls,
               ROUND(AVG(overall_score)::numeric, 2) AS avg_score,
               COUNT(*) FILTER (WHERE jsonb_array_length(risk_flags) > 0) AS risk_calls,
               ROUND(AVG((talk_metrics->>'talk_ratio')::numeric), 3) AS avg_talk_ratio,
               MAX(session_created_at) AS last_call_date
        FROM quality_results
        WHERE session_created_at >= :start AND employee IS NOT NULL {_SCOPE_SQL} {_SCENARIO_SQL}
        GROUP BY employee ORDER BY calls DESC
    """), {"scope": scope, "scenario": scenario, "start": start})).mappings().all()
    return [{**dict(r),
             "avg_score": float(r["avg_score"]) if r["avg_score"] is not None else None,
             "avg_talk_ratio": float(r["avg_talk_ratio"]) if r["avg_talk_ratio"] is not None else None,
             "last_call_date": r["last_call_date"].isoformat() if r["last_call_date"] else None}
            for r in rows]


async def _spark_rows(db: AsyncSession, scope, start, scenario=None) -> list[dict]:
    rows = (await db.execute(text(f"""
        SELECT employee AS name,
               to_char(date_trunc('week', session_created_at), 'YYYY-MM-DD') AS bucket,
               ROUND(AVG(overall_score)::numeric, 2) AS avg_score
        FROM quality_results
        WHERE session_created_at >= :start AND employee IS NOT NULL {_SCOPE_SQL} {_SCENARIO_SQL}
        GROUP BY 1, 2 ORDER BY 1, 2
    """), {"scope": scope, "scenario": scenario, "start": start})).mappings().all()
    return [{"name": r["name"], "bucket": r["bucket"],
             "avg_score": float(r["avg_score"]) if r["avg_score"] is not None else None}
            for r in rows]


async def _objection_rows(db: AsyncSession, scope, start, scenario=None) -> list[dict]:
    rows = (await db.execute(text(f"""
        SELECT o->>'category' AS category, COUNT(*) AS count,
               ROUND(AVG(CASE WHEN (o->>'resolved')::boolean THEN 1.0
                              WHEN (o->>'resolved') IS NOT NULL THEN 0.0 END)::numeric, 2)
                   AS resolved_rate,
               ROUND(AVG((o->>'handling_quality')::numeric), 1) AS avg_handling_quality,
               (ARRAY_AGG(o->>'text'))[1:3] AS examples
        FROM quality_results qr
        CROSS JOIN LATERAL jsonb_array_elements(qr.objections) AS o
        WHERE qr.session_created_at >= :start {_SCOPE_SQL.replace('employee', 'qr.employee')} {_SCENARIO_SQL}
        GROUP BY 1 ORDER BY count DESC
    """), {"scope": scope, "scenario": scenario, "start": start})).mappings().all()
    return [{**dict(r),
             "resolved_rate": float(r["resolved_rate"]) if r["resolved_rate"] is not None else None,
             "avg_handling_quality": float(r["avg_handling_quality"])
             if r["avg_handling_quality"] is not None else None,
             "examples": list(r["examples"] or [])}
            for r in rows]


async def _risk_rows(db: AsyncSession, scope, start, limit, scenario=None) -> list[dict]:
    rows = (await db.execute(text(f"""
        SELECT session_id::text, employee, overall_score AS score,
               risk_flags, session_created_at AS created_at
        FROM quality_results
        WHERE jsonb_array_length(risk_flags) > 0
          AND session_created_at >= :start {_SCOPE_SQL} {_SCENARIO_SQL}
        ORDER BY session_created_at DESC LIMIT :limit
    """), {"scope": scope, "scenario": scenario, "start": start, "limit": limit})).mappings().all()
    return [{"session_id": r["session_id"], "employee": r["employee"],
             "score": r["score"], "risk_flags": list(r["risk_flags"] or []),
             "created_at": r["created_at"].isoformat()}
            for r in rows]


def _window(days: int):
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days), now


@router.get("/overview")
async def stats_overview(
    days: int = Query(30, ge=1, le=365),
    granularity: Literal["day", "week"] = "day",
    scenario_id: str | None = Query(None, max_length=64),
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    start, end = _window(days)
    prev_start = start - timedelta(days=days)
    return {
        "kpi": await _kpi_row(db, scope, start, end, scenario=scenario_id),
        "prev_kpi": await _kpi_row(db, scope, prev_start, start, scenario=scenario_id),
        "series": await _series_rows(db, scope, start, end, granularity, scenario=scenario_id),
    }


@router.get("/managers")
async def stats_managers(
    days: int = Query(30, ge=1, le=365),
    scenario_id: str | None = Query(None, max_length=64),
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    start, _ = _window(days)
    managers = await _manager_rows(db, scope, start, scenario=scenario_id)
    sparks: dict[str, list] = {}
    for r in await _spark_rows(db, scope, start, scenario=scenario_id):
        sparks.setdefault(r["name"], []).append(r["avg_score"])
    return [{**m, "spark": sparks.get(m["name"], [])} for m in managers]


@router.get("/objections")
async def stats_objections(
    days: int = Query(30, ge=1, le=365),
    scenario_id: str | None = Query(None, max_length=64),
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    start, _ = _window(days)
    return await _objection_rows(db, scope, start, scenario=scenario_id)


@router.get("/risk-calls")
async def stats_risk_calls(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
    scenario_id: str | None = Query(None, max_length=64),
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    start, _ = _window(days)
    return await _risk_rows(db, scope, start, limit, scenario=scenario_id)
