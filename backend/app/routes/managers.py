import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_user import employee_scope, require_viewer
from app.database import get_db
from app.models import Chunk, Session
from app.schemas import SessionResponse

router = APIRouter(prefix="/api/managers", tags=["managers"])


async def _all_sessions(db: AsyncSession):
    result = await db.execute(select(Session))
    return result.scalars().all()


@router.get("", dependencies=[Depends(require_viewer)])
async def list_managers(
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    """List unique managers with aggregated stats."""
    sessions = await _all_sessions(db)

    managers: dict[str, dict] = {}
    for session in sessions:
        employee = (session.metadata_ or {}).get("employee")
        if not employee:
            continue
        if scope is not None and employee != scope:
            continue

        if employee not in managers:
            managers[employee] = {
                "name": employee,
                "total_calls": 0,
                "avg_score": 0.0,
                "last_call_date": None,
                "_scores": [],
            }

        entry = managers[employee]
        entry["total_calls"] += 1

        if session.created_at:
            if entry["last_call_date"] is None or session.created_at > entry["last_call_date"]:
                entry["last_call_date"] = session.created_at

        score = (session.metadata_ or {}).get("score")
        if score is not None:
            entry["_scores"].append(float(score))

    result_list = []
    for entry in managers.values():
        scores = entry.pop("_scores")
        if scores:
            entry["avg_score"] = round(sum(scores) / len(scores), 2)
        result_list.append(entry)

    result_list.sort(key=lambda m: m["total_calls"], reverse=True)
    return result_list


@router.get("/{name}/sessions", response_model=list[SessionResponse], dependencies=[Depends(require_viewer)])
async def get_manager_sessions(
    name: str,
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    """List sessions for a specific manager."""
    if scope is not None and name != scope:
        raise HTTPException(status_code=403, detail="foreign_manager")

    sessions = await _all_sessions(db)

    matched = []
    for session in sessions:
        employee = (session.metadata_ or {}).get("employee")
        if employee == name:
            matched.append(session)

    if not matched:
        raise HTTPException(status_code=404, detail=f"No sessions found for manager '{name}'")

    responses = []
    for session in matched:
        chunks_count = await db.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.session_id == session.id)
        )
        responses.append(
            SessionResponse(
                id=session.id,
                status=session.status,
                created_at=session.created_at,
                finished_at=session.finished_at,
                duration_seconds=session.duration_seconds,
                file_size_bytes=session.file_size_bytes,
                metadata=session.metadata_,
                chunks_count=chunks_count or 0,
            )
        )

    return responses
