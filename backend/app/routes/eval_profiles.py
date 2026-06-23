"""Пер-тенантные, пер-сценарные профили оценки (критерии + eval-промпт) с версиями.

Каждое изменение — новая версия; ровно одна активна на сценарий; откат =
активировать старую версию. «Пожелания» → LLM-переработка промпта (новая версия).
Чтение — viewer, мутации — admin.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_user import UserCtx, require_admin, require_viewer
from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/eval-profiles", tags=["eval-profiles"])


# ── schemas ──────────────────────────────────────────────────────
class VersionCreate(BaseModel):
    criteria: list[dict] = Field(default_factory=list)
    prompt: str | None = None
    source: str = "manual"
    activate: bool = True


class RewriteBody(BaseModel):
    wishes: str = Field(min_length=1)


class VersionOut(BaseModel):
    id: uuid.UUID
    scenario_id: str
    version: int
    criteria: list
    prompt: str | None
    source: str
    wishes_input: str | None
    rewrite_meta: dict | None
    is_active: bool
    created_at: datetime


def _coerce(v):
    return json.loads(v) if isinstance(v, str) else v


def _row_to_out(r) -> VersionOut:
    return VersionOut(
        id=r[0], scenario_id=r[1], version=r[2], criteria=_coerce(r[3]) or [],
        prompt=r[4], source=r[5], wishes_input=r[6], rewrite_meta=_coerce(r[7]),
        is_active=r[8], created_at=r[9],
    )

_COLS = "id, scenario_id, version, criteria, prompt, source, wishes_input, rewrite_meta, is_active, created_at"


async def _get_version(db: AsyncSession, vid: uuid.UUID) -> VersionOut:
    r = (await db.execute(text(
        f"SELECT {_COLS} FROM evaluation_profile_versions WHERE id=:id"), {"id": vid})).first()
    if not r:
        raise HTTPException(status_code=404, detail="Version not found")
    return _row_to_out(r)


def _file_fallback(company_config_id: str | None, scenario_id: str) -> dict | None:
    """Критерии/промпт сценария из файл-конфига — что применится без DB-профиля."""
    if not company_config_id:
        return None
    try:
        from app.routes.companies import _read_config
        cfg = _read_config(company_config_id)
    except Exception:
        return None
    for sc in (cfg.get("scenarios") or []):
        if sc.get("id") == scenario_id:
            return {"criteria": sc.get("criteria") or [], "prompt": sc.get("prompt")}
    return {"criteria": [], "prompt": (cfg.get("quality") or {}).get("prompt")}


async def _insert_version(db: AsyncSession, scenario_id: str, *, criteria, prompt,
                          source, wishes_input, rewrite_meta, activate, created_by) -> uuid.UUID:
    next_v = (await db.execute(text(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM evaluation_profile_versions WHERE scenario_id=:sid"
    ), {"sid": scenario_id})).scalar()
    new_id = uuid.uuid4()
    await db.execute(text("""
        INSERT INTO evaluation_profile_versions
            (id, scenario_id, version, criteria, prompt, source, wishes_input, rewrite_meta, is_active, created_by)
        VALUES (:id, :sid, :ver, CAST(:criteria AS jsonb), :prompt, :source, :wishes,
                CAST(:meta AS jsonb), false, :by)
    """), {
        "id": new_id, "sid": scenario_id, "ver": next_v,
        "criteria": json.dumps(criteria or [], ensure_ascii=False),
        "prompt": prompt, "source": source, "wishes": wishes_input,
        "meta": json.dumps(rewrite_meta, ensure_ascii=False) if rewrite_meta is not None else None,
        "by": uuid.UUID(created_by) if _is_uuid(created_by) else None,
    })
    if activate:
        await _do_activate(db, scenario_id, new_id)
    return new_id


def _is_uuid(s) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except Exception:
        return False


async def _do_activate(db: AsyncSession, scenario_id: str, vid: uuid.UUID) -> None:
    # Снять активность с сиблингов до установки новой (partial unique index).
    await db.execute(text(
        "UPDATE evaluation_profile_versions SET is_active=false WHERE scenario_id=:sid AND is_active"
    ), {"sid": scenario_id})
    await db.execute(text(
        "UPDATE evaluation_profile_versions SET is_active=true WHERE id=:id"), {"id": vid})


# ── endpoints ────────────────────────────────────────────────────
@router.get("", dependencies=[Depends(require_viewer)])
async def get_profile(scenario_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Активная версия + история + справочный fallback из файл-конфига для сценария."""
    rows = (await db.execute(text(
        f"SELECT {_COLS} FROM evaluation_profile_versions WHERE scenario_id=:sid ORDER BY version DESC"
    ), {"sid": scenario_id})).all()
    history = [_row_to_out(r) for r in rows]
    active = next((v for v in history if v.is_active), None)
    tenant = getattr(request.state, "tenant", None) or {}
    return {
        "scenario_id": scenario_id,
        "active": active,
        "history": history,
        "file_fallback": _file_fallback(tenant.get("company_config_id"), scenario_id),
    }


@router.post("/{scenario_id}/versions", response_model=VersionOut, status_code=201,
             dependencies=[Depends(require_admin)])
async def create_version(scenario_id: str, body: VersionCreate,
                         user: UserCtx = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)):
    try:
        new_id = await _insert_version(
            db, scenario_id, criteria=body.criteria, prompt=body.prompt,
            source=body.source or "manual", wishes_input=None, rewrite_meta=None,
            activate=body.activate, created_by=user.user_id,
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Cannot create version: {e}")
    return await _get_version(db, new_id)


@router.post("/{scenario_id}/rewrite", response_model=VersionOut, status_code=201,
             dependencies=[Depends(require_admin)])
async def rewrite_version(scenario_id: str, body: RewriteBody, request: Request,
                          user: UserCtx = Depends(require_admin),
                          db: AsyncSession = Depends(get_db)):
    """«Пожелания» → LLM переписывает промпт → новая НЕактивная версия (превью)."""
    # текущая база: активная версия, иначе файл-конфиг.
    active_row = (await db.execute(text(
        "SELECT criteria, prompt FROM evaluation_profile_versions "
        "WHERE scenario_id=:sid AND is_active LIMIT 1"), {"sid": scenario_id})).first()
    if active_row:
        cur_criteria, cur_prompt = _coerce(active_row[0]) or [], active_row[1]
    else:
        tenant = getattr(request.state, "tenant", None) or {}
        fb = _file_fallback(tenant.get("company_config_id"), scenario_id) or {}
        cur_criteria, cur_prompt = fb.get("criteria") or [], fb.get("prompt")

    from app.eval_prompt_rewrite import rewrite_eval_prompt
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: rewrite_eval_prompt(cur_prompt, cur_criteria, body.wishes))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM rewrite failed: {e}")

    suggested = result.get("suggested_criteria") or []
    try:
        new_id = await _insert_version(
            db, scenario_id,
            criteria=suggested if suggested else cur_criteria,
            prompt=result.get("rewritten_prompt"),
            source="llm_rewrite", wishes_input=body.wishes,
            rewrite_meta={"rationale": result.get("rationale"),
                          "suggested_criteria": suggested},
            activate=False, created_by=user.user_id,
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Cannot store rewritten version: {e}")
    return await _get_version(db, new_id)


@router.patch("/versions/{version_id}/activate", response_model=VersionOut,
              dependencies=[Depends(require_admin)])
async def activate_version(version_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    r = (await db.execute(text(
        "SELECT scenario_id FROM evaluation_profile_versions WHERE id=:id"), {"id": version_id})).first()
    if not r:
        raise HTTPException(status_code=404, detail="Version not found")
    await _do_activate(db, r[0], version_id)
    await db.commit()
    return await _get_version(db, version_id)


@router.delete("/versions/{version_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_version(version_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    r = (await db.execute(text(
        "SELECT is_active FROM evaluation_profile_versions WHERE id=:id"), {"id": version_id})).first()
    if not r:
        raise HTTPException(status_code=404, detail="Version not found")
    if r[0]:
        raise HTTPException(status_code=409, detail="Cannot delete the active version; activate another first")
    await db.execute(text("DELETE FROM evaluation_profile_versions WHERE id=:id"), {"id": version_id})
    await db.commit()
