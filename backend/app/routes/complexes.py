"""Complexes browse + detail + manual merge/relink."""
from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas_templates import (
    ComplexDetail, ComplexListItem, ComplexMerge, ComplexRename, ExtractionRelink,
)

# Make worker/tasks importable (shared codebase for complex helpers and DB ops)
WORKER_PATH = Path(__file__).resolve().parents[3] / "worker"
if str(WORKER_PATH) not in sys.path:
    sys.path.insert(0, str(WORKER_PATH))

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["complexes"])


def _recompute_aggregate_sync(complex_id: uuid.UUID) -> None:
    """Run worker's _recompute_aggregate synchronously inside a fresh sync session."""
    from sqlalchemy.orm import Session as DbSession

    from tasks.complex_match import _recompute_aggregate
    from tenancy.db import tenant_engine

    eng = tenant_engine()
    try:
        with eng.connect() as conn:
            with DbSession(bind=conn, expire_on_commit=False) as sdb:
                _recompute_aggregate(sdb, complex_id)
                sdb.commit()
    finally:
        eng.dispose()


@router.get("/complexes", response_model=list[ComplexListItem])
async def list_complexes(
    developer: str | None = None,
    class_: str | None = None,
    district: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    where = ["1=1"]
    params: dict = {"limit": limit, "offset": offset}
    if developer:
        where.append("c.developer ILIKE :dev")
        params["dev"] = f"%{developer}%"
    if class_:
        where.append("c.class = :cls")
        params["cls"] = class_
    if district:
        where.append("c.district ILIKE :dist")
        params["dist"] = f"%{district}%"
    if q:
        where.append("c.name ILIKE :q")
        params["q"] = f"%{q}%"
    rows = (await db.execute(text(f"""
        SELECT c.id, c.name, c.developer, c.class, c.district, c.updated_at,
               (SELECT COUNT(*) FROM complex_extractions ce WHERE ce.complex_id = c.id) AS sources
        FROM complexes c
        WHERE {' AND '.join(where)}
        ORDER BY c.updated_at DESC
        LIMIT :limit OFFSET :offset
    """), params)).all()
    return [
        ComplexListItem(
            id=r[0], name=r[1], developer=r[2], class_=r[3], district=r[4],
            updated_at=r[5], sources_count=r[6],
        ) for r in rows
    ]


@router.get("/complexes/{complex_id}", response_model=ComplexDetail)
async def get_complex(complex_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    c = (await db.execute(text("""
        SELECT id, name, developer, class, district, updated_at, aggregated_data
        FROM complexes WHERE id=:id
    """), {"id": complex_id})).first()
    if not c:
        raise HTTPException(status_code=404, detail="Complex not found")
    sources = (await db.execute(text("""
        SELECT ce.id, ce.session_id, ce.created_at
        FROM complex_extractions ce WHERE ce.complex_id=:id
        ORDER BY ce.created_at DESC
    """), {"id": complex_id})).all()
    sources_list = [
        {"extraction_id": str(s[0]), "session_id": str(s[1]), "created_at": s[2].isoformat()}
        for s in sources
    ]
    return ComplexDetail(
        id=c[0], name=c[1], developer=c[2], class_=c[3], district=c[4],
        updated_at=c[5], aggregated_data=c[6] or {},
        sources=sources_list,
        sources_count=len(sources_list),
    )


@router.patch("/complexes/{complex_id}", response_model=ComplexDetail)
async def rename_complex(complex_id: uuid.UUID, body: ComplexRename, db: AsyncSession = Depends(get_db)):
    from tasks._text_normalize import normalize_complex_name
    res = await db.execute(text("""
        UPDATE complexes SET name=:name, name_normalized=:nn, updated_at=now()
        WHERE id=:id RETURNING id
    """), {"id": complex_id, "name": body.name, "nn": normalize_complex_name(body.name)})
    if not res.first():
        raise HTTPException(status_code=404, detail="Complex not found")
    await db.commit()
    return await get_complex(complex_id, db)


@router.delete("/complexes/{complex_id}", status_code=204)
async def delete_complex(complex_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    res = await db.execute(text("DELETE FROM complexes WHERE id=:id RETURNING id"), {"id": complex_id})
    if not res.first():
        raise HTTPException(status_code=404, detail="Complex not found")
    await db.commit()


@router.post("/complexes/{complex_id}/merge", status_code=204)
async def merge_complex(complex_id: uuid.UUID, body: ComplexMerge, db: AsyncSession = Depends(get_db)):
    if complex_id == body.target_complex_id:
        raise HTTPException(status_code=400, detail="Cannot merge complex into itself")
    target = (await db.execute(text("SELECT id FROM complexes WHERE id=:id"),
                               {"id": body.target_complex_id})).first()
    if not target:
        raise HTTPException(status_code=404, detail="Target complex not found")
    src = (await db.execute(text("SELECT id FROM complexes WHERE id=:id"), {"id": complex_id})).first()
    if not src:
        raise HTTPException(status_code=404, detail="Source complex not found")

    await db.execute(text(
        "UPDATE complex_extractions SET complex_id=:tgt WHERE complex_id=:src"
    ), {"src": complex_id, "tgt": body.target_complex_id})
    await db.execute(text("DELETE FROM complexes WHERE id=:id"), {"id": complex_id})
    await db.commit()

    _recompute_aggregate_sync(body.target_complex_id)


@router.post("/extractions/{extraction_id}/relink", status_code=204)
async def relink_extraction(extraction_id: uuid.UUID, body: ExtractionRelink, db: AsyncSession = Depends(get_db)):
    if body.complex_id is not None:
        target = (await db.execute(text("SELECT id FROM complexes WHERE id=:id"),
                                   {"id": body.complex_id})).first()
        if not target:
            raise HTTPException(status_code=404, detail="Target complex not found")

    old_row = (await db.execute(text(
        "SELECT complex_id FROM complex_extractions WHERE id=:id"
    ), {"id": extraction_id})).first()
    if not old_row:
        raise HTTPException(status_code=404, detail="Extraction not found")
    old_cid = old_row[0]

    await db.execute(text(
        "UPDATE complex_extractions SET complex_id=:cid WHERE id=:id"
    ), {"cid": body.complex_id, "id": extraction_id})
    await db.commit()

    for cid in {old_cid, body.complex_id}:
        if cid:
            _recompute_aggregate_sync(cid)
