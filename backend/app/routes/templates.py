"""CRUD for extraction templates. Reads require viewer, mutations require admin."""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from jsonschema import Draft202012Validator, SchemaError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_user import require_admin, require_viewer
from app.database import get_db
from app.schemas_templates import (
    TemplateCreate, TemplateDetail, TemplateListItem, TemplateUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/templates", tags=["templates"])


def _validate_schema(schema: dict) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        raise HTTPException(status_code=422, detail=f"json_schema is not a valid JSON Schema: {e.message}")


@router.get("", response_model=list[TemplateListItem], dependencies=[Depends(require_viewer)])
async def list_templates(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(text(
        "SELECT id, name, description, kind, updated_at FROM extraction_templates ORDER BY name"
    ))).all()
    return [TemplateListItem(id=r[0], name=r[1], description=r[2], kind=r[3], updated_at=r[4]) for r in rows]


@router.get("/{template_id}", response_model=TemplateDetail, dependencies=[Depends(require_viewer)])
async def get_template(template_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(text("""
        SELECT id, name, description, kind, prompt, json_schema, created_at, updated_at
        FROM extraction_templates WHERE id=:id
    """), {"id": template_id})).first()
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    return TemplateDetail(
        id=row[0], name=row[1], description=row[2], kind=row[3],
        prompt=row[4], json_schema=row[5], created_at=row[6], updated_at=row[7],
    )


@router.post("", response_model=TemplateDetail, status_code=201, dependencies=[Depends(require_admin)])
async def create_template(body: TemplateCreate, db: AsyncSession = Depends(get_db)):
    _validate_schema(body.json_schema)
    new_id = uuid.uuid4()
    try:
        await db.execute(text("""
            INSERT INTO extraction_templates (id, name, description, kind, prompt, json_schema)
            VALUES (:id, :name, :desc, :kind, :prompt, CAST(:schema AS jsonb))
        """), {
            "id": new_id, "name": body.name, "desc": body.description, "kind": body.kind,
            "prompt": body.prompt,
            "schema": json.dumps(body.json_schema, ensure_ascii=False),
        })
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Cannot create template: {e}")
    return await get_template(new_id, db)


@router.patch("/{template_id}", response_model=TemplateDetail, dependencies=[Depends(require_admin)])
async def update_template(template_id: uuid.UUID, body: TemplateUpdate, db: AsyncSession = Depends(get_db)):
    if body.json_schema is not None:
        _validate_schema(body.json_schema)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if not fields:
        return await get_template(template_id, db)
    sets = []
    params: dict = {"id": template_id}
    for k, v in fields.items():
        if k == "json_schema":
            sets.append("json_schema = CAST(:schema AS jsonb)")
            params["schema"] = json.dumps(v, ensure_ascii=False)
        else:
            sets.append(f"{k} = :{k}")
            params[k] = v
    sets.append("updated_at = now()")
    res = await db.execute(text(f"UPDATE extraction_templates SET {', '.join(sets)} WHERE id=:id RETURNING id"), params)
    if not res.first():
        raise HTTPException(status_code=404, detail="Template not found")
    await db.commit()
    return await get_template(template_id, db)


@router.delete("/{template_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_template(template_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    used = (await db.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM complex_extractions WHERE template_id=:id) AS exts,
          (SELECT COUNT(*) FROM sessions WHERE metadata->>'template_id' = :id_str) AS sess
    """), {"id": template_id, "id_str": str(template_id)})).first()
    if used and (used[0] > 0 or used[1] > 0):
        raise HTTPException(
            status_code=409,
            detail=f"Template is in use (extractions={used[0]}, sessions={used[1]}); cannot delete",
        )
    res = await db.execute(text("DELETE FROM extraction_templates WHERE id=:id RETURNING id"), {"id": template_id})
    if not res.first():
        raise HTTPException(status_code=404, detail="Template not found")
    await db.commit()
