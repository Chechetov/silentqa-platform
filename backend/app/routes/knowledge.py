"""Knowledge Base CRUD + import + mentions. Reads=viewer, mutations=admin.
All routes gated by the knowledge_base module."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_user import employee_scope, require_admin, require_viewer
from app.database import get_db
from app.modules import require_module
from app.schemas_knowledge import (
    CategoryCreate, CategoryUpdate, EntryCreate, EntryUpdate, ImportRequest,
)

router = APIRouter(
    prefix="/api/knowledge", tags=["knowledge"],
    dependencies=[Depends(require_module("knowledge_base"))],
)

IMPORT_MAX_BYTES = 1_048_576  # 1 MiB hard cap on the import body


@router.get("/categories", dependencies=[Depends(require_viewer)])
async def list_categories(include_entries: bool = False, db: AsyncSession = Depends(get_db)):
    cats = (await db.execute(text(
        "SELECT id, name, slug, description, feeds_asr, feeds_llm, is_taxonomy, updated_at "
        "FROM kb_categories ORDER BY created_at"
    ))).all()
    out = [dict(id=str(c[0]), name=c[1], slug=c[2], description=c[3], feeds_asr=c[4],
                feeds_llm=c[5], is_taxonomy=c[6], updated_at=c[7], entries=[]) for c in cats]
    if include_entries:
        rows = (await db.execute(text(
            "SELECT id, category_id, term, aliases, description FROM kb_entries ORDER BY created_at"
        ))).all()
        by_id = {c["id"]: c for c in out}
        for r in rows:
            cat = by_id.get(str(r[1]))
            if cat is not None:
                cat["entries"].append(dict(id=str(r[0]), term=r[2], aliases=r[3], description=r[4]))
    return out


@router.post("/categories", status_code=201, dependencies=[Depends(require_admin)])
async def create_category(body: CategoryCreate, db: AsyncSession = Depends(get_db)):
    new_id = uuid.uuid4()
    try:
        await db.execute(text(
            "INSERT INTO kb_categories (id, name, slug, description, feeds_asr, feeds_llm, is_taxonomy) "
            "VALUES (:id,:name,:slug,:desc,:fa,:fl,:tax)"
        ), {"id": new_id, "name": body.name, "slug": body.slug, "desc": body.description,
            "fa": body.feeds_asr, "fl": body.feeds_llm, "tax": body.is_taxonomy})
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Cannot create category: {e}")
    return {"id": str(new_id)}


@router.patch("/categories/{category_id}", dependencies=[Depends(require_admin)])
async def update_category(category_id: uuid.UUID, body: CategoryUpdate, db: AsyncSession = Depends(get_db)):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return {"id": str(category_id)}
    sets = ", ".join(f"{k} = :{k}" for k in fields) + ", updated_at = now()"
    res = await db.execute(text(f"UPDATE kb_categories SET {sets} WHERE id=:id RETURNING id"),
                           {**fields, "id": category_id})
    if not res.first():
        raise HTTPException(status_code=404, detail="Category not found")
    await db.commit()
    return {"id": str(category_id)}


@router.delete("/categories/{category_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_category(category_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    res = await db.execute(text("DELETE FROM kb_categories WHERE id=:id RETURNING id"), {"id": category_id})
    if not res.first():
        raise HTTPException(status_code=404, detail="Category not found")
    await db.commit()


@router.get("/categories/{category_id}/entries", dependencies=[Depends(require_viewer)])
async def list_entries(category_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(text(
        "SELECT id, term, aliases, description, metadata FROM kb_entries "
        "WHERE category_id=:cid ORDER BY created_at"
    ), {"cid": category_id})).all()
    return [dict(id=str(r[0]), term=r[1], aliases=r[2], description=r[3], metadata=r[4]) for r in rows]


@router.post("/entries", status_code=201, dependencies=[Depends(require_admin)])
async def create_entry(body: EntryCreate, db: AsyncSession = Depends(get_db)):
    new_id = uuid.uuid4()
    try:
        await db.execute(text(
            "INSERT INTO kb_entries (id, category_id, term, aliases, description, metadata) "
            "VALUES (:id,:cid,:term,CAST(:al AS jsonb),:desc,CAST(:meta AS jsonb))"
        ), {"id": new_id, "cid": body.category_id, "term": body.term,
            "al": json.dumps(body.aliases, ensure_ascii=False), "desc": body.description,
            "meta": json.dumps(body.metadata, ensure_ascii=False)})
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Cannot create entry: {e}")
    return {"id": str(new_id)}


@router.patch("/entries/{entry_id}", dependencies=[Depends(require_admin)])
async def update_entry(entry_id: uuid.UUID, body: EntryUpdate, db: AsyncSession = Depends(get_db)):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return {"id": str(entry_id)}
    sets, params = [], {"id": entry_id}
    for k, v in fields.items():
        if k in ("aliases", "metadata"):
            sets.append(f"{k} = CAST(:{k} AS jsonb)")
            params[k] = json.dumps(v, ensure_ascii=False)
        else:
            sets.append(f"{k} = :{k}")
            params[k] = v
    sets.append("updated_at = now()")
    res = await db.execute(text(f"UPDATE kb_entries SET {', '.join(sets)} WHERE id=:id RETURNING id"), params)
    if not res.first():
        raise HTTPException(status_code=404, detail="Entry not found")
    await db.commit()
    return {"id": str(entry_id)}


@router.delete("/entries/{entry_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_entry(entry_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    res = await db.execute(text("DELETE FROM kb_entries WHERE id=:id RETURNING id"), {"id": entry_id})
    if not res.first():
        raise HTTPException(status_code=404, detail="Entry not found")
    await db.commit()


@router.post("/import", dependencies=[Depends(require_admin)])
async def import_entries(body: ImportRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Bulk insert; dedup on (category_id, term) via ON CONFLICT DO NOTHING."""
    cl = request.headers.get("content-length")
    if cl and int(cl) > IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="import body too large (max 1 MiB)")
    inserted = 0
    for row in body.rows:
        res = await db.execute(text(
            "INSERT INTO kb_entries (id, category_id, term, aliases, description) "
            "VALUES (gen_random_uuid(), :cid, :term, CAST(:al AS jsonb), :desc) "
            "ON CONFLICT (category_id, term) DO NOTHING RETURNING id"
        ), {"cid": body.category_id, "term": row.term,
            "al": json.dumps(row.aliases, ensure_ascii=False), "desc": row.description})
        if res.first():
            inserted += 1
    await db.commit()
    return {"inserted": inserted, "received": len(body.rows)}


@router.get("/entries/{entry_id}/mentions", dependencies=[Depends(require_viewer)])
async def entry_mentions(entry_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                         scope: str | None = Depends(employee_scope)):
    q = ("SELECT s.id, s.created_at, m.count FROM kb_entry_mentions m "
         "JOIN sessions s ON s.id = m.session_id WHERE m.entry_id = :eid")
    params = {"eid": entry_id}
    if scope is not None:  # manager: only own calls
        q += " AND s.metadata->>'employee' = :emp"
        params["emp"] = scope
    rows = (await db.execute(text(q + " ORDER BY s.created_at"), params)).all()
    return [{"session_id": str(r[0]), "created_at": r[1], "count": r[2]} for r in rows]
