import asyncio
import logging
import shutil
import uuid
from pathlib import Path

from celery import Celery
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select, func, text, table, column
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from tenancy.context import get_tenant_slug
from tenancy.registry import AMOCRM_TENANT_SLUGS

from app.auth_jwt import get_current_broker
from app.auth_user import (
    UserCtx,
    _NO_EMPLOYEE,
    employee_scope,
    get_current_user,
    require_admin,
    require_ingestion_auth,
    require_session_access,
    require_viewer,
)
from app.config import settings
from app.database import get_db
from app.models import Chunk, Session, SessionStatus
from app.modules import require_module
from app.schemas import BrokerInfo, SessionCreate, SessionResponse, SpeakerMapUpdate
from tenancy.context import require_tenant_slug
from tenancy.paths import tenant_audio_sessions_dir, tenant_results_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

celery_app = Celery("voiceqa", broker=settings.REDIS_URL)

# Lightweight table handle for the kb_tag filter subquery (no ORM model needed).
_kb_mentions = table(
    "kb_entry_mentions",
    column("session_id", PGUUID(as_uuid=True)),
    column("entry_id", PGUUID(as_uuid=True)),
)


@router.get("", dependencies=[Depends(require_viewer)])
async def list_sessions(
    limit: int = 50,
    offset: int = 0,
    source: str | None = None,
    phone: str | None = None,
    template_id: str | None = None,
    kb_tag: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    """List all sessions with pagination, sorted by created_at DESC.

    Optional filters:
    - source: exact match on metadata.source (e.g. 'amocrm', 'desktop-app')
    - phone: substring match on metadata.phone (case-insensitive, digits-friendly)
    - template_id: 'none' = sessions without any template (regular broker calls);
                   UUID = sessions with that exact template
    """
    filters = []
    if source:
        filters.append(Session.metadata_["source"].astext == source)
    if phone:
        filters.append(Session.metadata_["phone"].astext.ilike(f"%{phone.strip()}%"))
    if template_id:
        if template_id == "none":
            filters.append(Session.metadata_["template_id"].astext.is_(None))
        else:
            filters.append(Session.metadata_["template_id"].astext == template_id)

    if scope is not None:
        filters.append(Session.metadata_["employee"].astext == scope)

    if kb_tag:
        filters.append(Session.id.in_(
            select(_kb_mentions.c.session_id).where(_kb_mentions.c.entry_id == kb_tag)
        ))

    count_q = select(func.count()).select_from(Session)
    list_q = select(Session).order_by(Session.created_at.desc())
    for f in filters:
        count_q = count_q.where(f)
        list_q = list_q.where(f)

    total = await db.scalar(count_q)

    result = await db.execute(list_q.limit(limit).offset(offset))
    sessions = result.scalars().all()

    # Build template_id -> name map for the visible page (one SQL query, cheap)
    tpl_ids = {(s.metadata_ or {}).get("template_id") for s in sessions}
    tpl_ids.discard(None)
    tpl_map: dict[str, str] = {}
    if tpl_ids:
        tpl_rows = (await db.execute(text(
            "SELECT id::text, name FROM extraction_templates WHERE id::text = ANY(:ids)"
        ), {"ids": list(tpl_ids)})).all()
        tpl_map = {r[0]: r[1] for r in tpl_rows}

    items = []
    for session in sessions:
        chunks_count = await db.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.session_id == session.id)
        )
        resp = _to_response(session, chunks_count or 0)
        # Inject template_name into the response metadata so the dashboard can render it.
        meta_in = (session.metadata_ or {})
        tid = meta_in.get("template_id")
        if tid and tid in tpl_map:
            resp = resp.model_copy(update={"metadata": {**(resp.metadata or {}), "template_name": tpl_map[tid]}})
        items.append(resp)

    return {"items": items, "total": total or 0}


# Metadata keys the server controls. A client may NOT set these directly —
# they're either populated from the authenticated broker JWT or left absent.
# This prevents an unauthenticated (or differently-authenticated) client from
# spoofing attribution by stuffing values into request metadata.
_SERVER_OWNED_METADATA = (
    "broker_id", "amocrm_user_id", "broker_name", "responsible_user_id",
    # company_id/scenario_id — выбор конфига оценки принадлежит серверу
    # (берётся из shared.tenants.company_config_id), клиент подменить не может.
    "company_id", "scenario_id",
)

# Seeded by migration 008 — applied by default to desktop-app recordings,
# which are Zoom meetings (not outbound calls).
_DEFAULT_DESKTOP_TEMPLATE_NAME = "Zoom-встреча брокера (презентация ЖК)"

_EMPLOYEE_MAX_LEN = 120


def build_session_metadata(raw_meta: dict | None, broker: "BrokerInfo | None") -> dict:
    """Собрать метаданные сессии (без БД): вычистить server-owned, санкционировать
    клиентский `employee` (атрибуция для recorder-only тенантов), проставить
    broker_* при наличии брокера. Чистая функция — юнит-тестируется без БД."""
    meta = dict(raw_meta or {})
    # Strip any server-owned keys the client tried to supply.
    for k in _SERVER_OWNED_METADATA:
        meta.pop(k, None)

    # Санкционированный `employee`: только непустая строка, обрезаем до лимита.
    # Намеренно НЕ в _SERVER_OWNED_METADATA — это легитимная клиентская атрибуция
    # (десктоп по API-ключу проставляет имя сотрудника; AmoCRM-потоки его не шлют).
    emp = meta.get("employee")
    if isinstance(emp, str):
        emp = emp.strip()[:_EMPLOYEE_MAX_LEN]
        if emp:
            meta["employee"] = emp
        else:
            meta.pop("employee", None)
    else:
        meta.pop("employee", None)

    if broker is not None:
        # Auto-attribute to the authenticated broker (authoritative).
        meta["broker_id"] = str(broker.id)
        meta["amocrm_user_id"] = broker.amocrm_user_id
        meta["broker_name"] = broker.name
        meta["responsible_user_id"] = broker.amocrm_user_id

    return meta


async def _resolve_default_desktop_template_id(db: AsyncSession) -> str | None:
    row = (await db.execute(
        text("SELECT id FROM extraction_templates WHERE name = :n AND kind = 'evaluation' LIMIT 1"),
        {"n": _DEFAULT_DESKTOP_TEMPLATE_NAME},
    )).first()
    return str(row[0]) if row else None


@router.post("", response_model=SessionResponse, status_code=201,
             dependencies=[Depends(require_ingestion_auth)])
async def create_session(
    body: SessionCreate,
    db: AsyncSession = Depends(get_db),
    broker: BrokerInfo | None = Depends(get_current_broker),
):
    meta = build_session_metadata(body.metadata, broker)

    # Desktop-app recordings on AmoCRM-tenants (realestate) are Zoom presentations —
    # авто-применяем Zoom-evaluation-шаблон, чтобы протокол LLM совпадал с жанром.
    # Для не-AmoCRM тенантов (fulldent и пр.) этого НЕ делаем: их desktop-приёмы
    # оцениваются сценарием company-config, а realestate-протокол «презентация ЖК»
    # перебил бы его (pipeline.py:685-707). Клиент может явно задать template_id.
    if (meta.get("source") == "desktop-app" and not meta.get("template_id")
            and get_tenant_slug() in AMOCRM_TENANT_SLUGS):
        tid = await _resolve_default_desktop_template_id(db)
        if tid:
            meta["template_id"] = tid

    session = Session(metadata_=meta or None)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return _to_response(session, 0)


@router.get("/{session_id}", response_model=SessionResponse,
            dependencies=[Depends(require_ingestion_auth)])
async def get_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserCtx | None = Depends(get_current_user),
):
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if user is not None and user.role == "manager":
        emp = (session.metadata_ or {}).get("employee")
        if emp != (user.employee_name or _NO_EMPLOYEE):
            raise HTTPException(status_code=404, detail="Session not found")

    chunks_count = await db.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.session_id == session_id)
    )
    return _to_response(session, chunks_count or 0)


@router.delete("/{session_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Delete a session: DB row (cascades chunks) + audio and results directories.

    Requires an admin cookie session (access matrix 5.6).
    """
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    await db.delete(session)
    await db.commit()

    sid = str(session_id)
    slug = require_tenant_slug()
    candidates = [
        tenant_audio_sessions_dir(settings.AUDIO_STORAGE_PATH, slug, sid),
        tenant_results_dir(settings.RESULTS_STORAGE_PATH, slug, sid),
        # legacy pre-tenant layout (files written before the cutover)
        Path(settings.AUDIO_STORAGE_PATH) / "sessions" / sid,
        Path(settings.RESULTS_STORAGE_PATH) / sid,
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                shutil.rmtree(candidate)
            except OSError as e:
                logger.warning("Failed to remove %s: %s", candidate, e)

    return Response(status_code=204)


class ReprocessBody(BaseModel):
    template_id: uuid.UUID


@router.post("/{session_id}/reprocess", response_model=SessionResponse,
             dependencies=[Depends(require_admin)])
async def reprocess_session(
    session_id: uuid.UUID,
    body: ReprocessBody,
    db: AsyncSession = Depends(get_db),
):
    """Reprocess an existing session with a different template (skip transcription)."""
    sess = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one_or_none()
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    if sess.status in (SessionStatus.processing, SessionStatus.uploading):
        raise HTTPException(status_code=409, detail=f"Session is currently {sess.status.value}")

    tpl = (await db.execute(text("SELECT id FROM extraction_templates WHERE id=:id"),
                            {"id": body.template_id})).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    meta = dict(sess.metadata_ or {})
    meta["template_id"] = str(body.template_id)
    sess.metadata_ = meta
    sess.status = SessionStatus.processing
    await db.commit()
    await db.refresh(sess)

    from tenancy.context import require_tenant_schema
    tenant_schema = require_tenant_schema()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: celery_app.send_task(
            "pipeline.process_session",
            args=[str(session_id)],
            kwargs={"tenant_schema": tenant_schema},
            queue="transcription",
        ),
    )

    chunks_count = await db.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.session_id == session_id)
    )
    return _to_response(sess, chunks_count or 0)


class LinkLeadBody(BaseModel):
    lead_id: int | None = None  # None = unlink


@router.post("/{session_id}/link-lead", response_model=SessionResponse,
             dependencies=[Depends(require_ingestion_auth)])
async def link_lead(
    session_id: uuid.UUID,
    body: LinkLeadBody,
    db: AsyncSession = Depends(get_db),
):
    """Attach, change, or detach an AmoCRM lead on a session.

    Behavior depends on the transition:

    - **Same lead** (idempotent re-link to the current lead, including
      unlink-of-unlinked): no-op. No DB write, no Celery task.
    - **Unlink** (`lead_id=null`, was linked): delete the notes we created
      on the old lead in AmoCRM, drop metadata.lead_id, status stays
      `completed`. No reprocess — re-evaluation would burn ~9k tokens
      with no destination to publish to.
    - **New / changed lead**: write metadata.lead_id, status →
      `processing`, enqueue `pipeline.process_session`. Re-evaluation is
      worth it here — `prior_context` (previous calls / deal stage /
      events) flows into the LLM prompt and `_push_to_amocrm` publishes
      the enriched note, plan, and deal summary to the new lead.

    Transcription is cached either way; the rerun only re-does
    sentiment + LLM quality (+ optional next-call plan).
    """
    sess = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one_or_none()
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    if sess.status in (SessionStatus.processing, SessionStatus.uploading):
        raise HTTPException(status_code=409, detail=f"Session is currently {sess.status.value}")

    meta = dict(sess.metadata_ or {})
    old_lead_id = meta.get("lead_id")
    new_lead_id = body.lead_id

    # Idempotent: caller asked to set the same lead we already have (or
    # to unlink a session that isn't linked). Nothing has changed.
    if old_lead_id == new_lead_id:
        chunks_count = await db.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.session_id == session_id)
        )
        return _to_response(sess, chunks_count or 0)

    # Lead is changing or being removed: tear down any notes we created
    # on the old lead in AmoCRM so the deal doesn't keep a stale
    # evaluation pointing back to a session that no longer references it.
    old_note_ids = [nid for nid in (meta.get("amo_note_id"), meta.get("plan_amo_note_id")) if nid]
    # Note-cleanup дёргает AmoCRM realestate — гейтим членством тенанта
    # (находка финального ревью Plan 2): чужой тенант не должен достучаться
    # до CRM, даже подсунув lead_id/amo_note_id в metadata.
    if old_lead_id and old_note_ids and get_tenant_slug() in AMOCRM_TENANT_SLUGS:
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _wp = _Path(__file__).resolve().parents[3] / "worker"
            if str(_wp) not in _sys.path:
                _sys.path.insert(0, str(_wp))
            from tasks.amocrm_sync import delete_note as _delete_note
            for nid in old_note_ids:
                _delete_note(int(old_lead_id), int(nid))
        except Exception:
            logger.exception("Failed to clean up old AmoCRM notes during link-lead")
        meta.pop("amo_note_id", None)
        meta.pop("plan_amo_note_id", None)

    if new_lead_id is None:
        # Unlink: notes are gone, nothing to re-evaluate against. Skip
        # the Celery reprocess — it would just burn tokens on a quality
        # report with no AmoCRM destination.
        meta.pop("lead_id", None)
        sess.metadata_ = meta
        await db.commit()
        await db.refresh(sess)
        chunks_count = await db.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.session_id == session_id)
        )
        return _to_response(sess, chunks_count or 0)

    # New or changed lead: reprocess so prior_context flows in and the
    # pipeline publishes a fresh note/plan/summary to the new lead.
    meta["lead_id"] = new_lead_id
    sess.metadata_ = meta
    sess.status = SessionStatus.processing
    await db.commit()
    await db.refresh(sess)

    from tenancy.context import require_tenant_schema
    tenant_schema = require_tenant_schema()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: celery_app.send_task(
            "pipeline.process_session",
            args=[str(session_id)],
            kwargs={"tenant_schema": tenant_schema},
            queue="transcription",
        ),
    )

    chunks_count = await db.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.session_id == session_id)
    )
    return _to_response(sess, chunks_count or 0)


@router.get("/{session_id}/extraction",
            dependencies=[Depends(require_session_access), Depends(require_module("complexes"))])
async def get_session_extraction(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Return the latest extraction for the session, or 404 if none exists."""
    row = (await db.execute(text("""
        SELECT ce.id, ce.raw_data, ce.complex_id, t.id, t.name, t.json_schema
        FROM complex_extractions ce
        JOIN extraction_templates t ON t.id = ce.template_id
        WHERE ce.session_id = :sid
        ORDER BY ce.created_at DESC LIMIT 1
    """), {"sid": session_id})).first()
    if not row:
        raise HTTPException(status_code=404, detail="No extraction for this session")
    return {
        "extraction_id": str(row[0]),
        "raw_data": row[1],
        "complex_id": str(row[2]) if row[2] else None,
        "template": {"id": str(row[3]), "name": row[4], "json_schema": row[5]},
    }


@router.get("/{session_id}/tags", dependencies=[Depends(require_session_access)])
async def get_session_tags(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(text("""
        SELECT e.id, e.term, c.name, m.count
        FROM kb_entry_mentions m
        JOIN kb_entries e ON e.id = m.entry_id
        JOIN kb_categories c ON c.id = e.category_id
        WHERE m.session_id = :sid ORDER BY m.count DESC
    """), {"sid": session_id})).all()
    return [{"entry_id": str(r[0]), "term": r[1], "category": r[2], "count": r[3]} for r in rows]


@router.post("/{session_id}/finish", response_model=SessionResponse,
             dependencies=[Depends(require_ingestion_auth)])
async def finish_session(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status not in (SessionStatus.created, SessionStatus.uploading):
        raise HTTPException(status_code=400, detail=f"Cannot finish session in status '{session.status}'")

    session.status = SessionStatus.processing
    await db.commit()
    await db.refresh(session)

    from tenancy.context import require_tenant_schema
    tenant_schema = require_tenant_schema()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: celery_app.send_task(
            "pipeline.process_session",
            args=[str(session_id)],
            kwargs={"tenant_schema": tenant_schema},
            queue="transcription",
        ),
    )

    chunks_count = await db.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.session_id == session_id)
    )
    return _to_response(session, chunks_count or 0)


@router.patch("/{session_id}/speaker-map", dependencies=[Depends(require_admin)])
async def update_speaker_map(
    session_id: uuid.UUID,
    body: SpeakerMapUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update speaker name/role mapping in session metadata."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    meta = dict(session.metadata_ or {})
    meta["speaker_map"] = {k: v.model_dump() for k, v in body.speaker_map.items()}
    session.metadata_ = meta
    await db.commit()
    await db.refresh(session)

    chunks_count = await db.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.session_id == session_id)
    )
    return _to_response(session, chunks_count or 0)


def _to_response(session: Session, chunks_count: int) -> SessionResponse:
    return SessionResponse(
        id=session.id,
        status=session.status,
        created_at=session.created_at,
        finished_at=session.finished_at,
        duration_seconds=session.duration_seconds,
        file_size_bytes=session.file_size_bytes,
        metadata=session.metadata_,
        chunks_count=chunks_count,
    )
