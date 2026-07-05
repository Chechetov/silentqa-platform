"""Менеджеры: SQL-агрегаты вместо in-memory скана всех сессий.

avg_score — из quality_results (реальный AVG по overall_score); раньше
читалось метаполе metadata->>'score', которое никто не писал (мёртвый ноль).
total_calls — ВСЕ сессии менеджера (как раньше), не только оценённые."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_user import employee_scope, require_viewer
from app.database import get_db
from app.schemas import SessionResponse

router = APIRouter(prefix="/api/managers", tags=["managers"])

_SCOPE_SQL = "AND (CAST(:scope AS TEXT) IS NULL OR s.metadata->>'employee' = :scope)"


async def _manager_agg_rows(db: AsyncSession, scope) -> list[dict]:
    rows = (await db.execute(text(f"""
        SELECT s.metadata->>'employee' AS name,
               COUNT(*) AS total_calls,
               ROUND(AVG(qr.overall_score)::numeric, 2) AS avg_score,
               MAX(s.created_at) AS last_call_date
        FROM sessions s
        LEFT JOIN quality_results qr ON qr.session_id = s.id
        WHERE s.metadata->>'employee' IS NOT NULL {_SCOPE_SQL}
        GROUP BY 1 ORDER BY total_calls DESC
    """), {"scope": scope})).mappings().all()
    return [{"name": r["name"], "total_calls": r["total_calls"],
             "avg_score": float(r["avg_score"]) if r["avg_score"] is not None else 0.0,
             "last_call_date": r["last_call_date"].isoformat()
             if r["last_call_date"] else None}
            for r in rows]


async def _sessions_for(db: AsyncSession, name: str) -> list[dict]:
    rows = (await db.execute(text("""
        SELECT s.id, s.status, s.created_at, s.finished_at, s.duration_seconds,
               s.file_size_bytes, s.metadata, COALESCE(c.cnt, 0) AS chunks_count
        FROM sessions s
        LEFT JOIN (SELECT session_id, COUNT(*) AS cnt FROM chunks GROUP BY 1) c
               ON c.session_id = s.id
        WHERE s.metadata->>'employee' = :name
        ORDER BY s.created_at DESC
    """), {"name": name})).mappings().all()
    return [dict(r) for r in rows]


@router.get("", dependencies=[Depends(require_viewer)])
async def list_managers(
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    """Уникальные менеджеры с агрегатами (взвешенный avg_score из quality_results)."""
    return await _manager_agg_rows(db, scope)


@router.get("/{name}/sessions", response_model=list[SessionResponse],
            dependencies=[Depends(require_viewer)])
async def get_manager_sessions(
    name: str,
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    if scope is not None and name != scope:
        raise HTTPException(status_code=403, detail="foreign_manager")
    rows = await _sessions_for(db, name)
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"No sessions found for manager '{name}'")
    return [SessionResponse(
        id=r["id"] if isinstance(r["id"], uuid.UUID) else uuid.UUID(str(r["id"])),
        status=r["status"], created_at=r["created_at"], finished_at=r["finished_at"],
        duration_seconds=r["duration_seconds"], file_size_bytes=r["file_size_bytes"],
        metadata=r["metadata"], chunks_count=r["chunks_count"],
    ) for r in rows]
