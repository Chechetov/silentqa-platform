"""Управление клиентами платформы: /api/platform/tenants* (спека §3)."""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Literal

import anyio
from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tenancy.identifiers import SCHEMA_RE

from ..auth_platform import require_platform_admin
from ..database import get_db
from ..provision_tenant import ProvisionError, generate_api_key, generate_password, provision
from ..tenancy_http import registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/platform/tenants", tags=["platform-tenants"],
                   dependencies=[Depends(require_platform_admin)])


class CreateTenantBody(BaseModel):
    slug: str
    display_name: str = ""
    admin_email: EmailStr
    admin_password: str = ""


def _month_start() -> dt.datetime:
    now = dt.datetime.now(dt.timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _tenant_rows(db: AsyncSession) -> list[dict]:
    res = await db.execute(text(
        "SELECT slug, schema_name, display_name, status, created_at "
        "FROM shared.tenants ORDER BY created_at"
    ))
    return [dict(r._mapping) for r in res]


async def _tenant_stats(db: AsyncSession, schema: str,
                        dt_from: dt.datetime, dt_to: dt.datetime | None) -> dict:
    if not SCHEMA_RE.fullmatch(schema):  # schema из БД, но пояс не лишний
        raise HTTPException(status_code=500, detail="bad schema name")
    until = "AND created_at < :dt_to" if dt_to else ""
    row = (await db.execute(text(
        f"SELECT count(*) AS sessions_count, "
        f"coalesce(sum(duration_seconds) FILTER (WHERE status = 'completed'), 0) "
        f"  AS total_seconds, "
        f"(SELECT max(created_at) FROM {schema}.sessions) AS last_activity "
        f"FROM {schema}.sessions WHERE created_at >= :dt_from {until}"),
        {"dt_from": dt_from, **({"dt_to": dt_to} if dt_to else {})},
    )).first()
    return {
        "sessions_count": row.sessions_count,
        "minutes": int(row.total_seconds // 60),
        "last_activity": row.last_activity.isoformat() if row.last_activity else None,
    }


@router.get("")
async def list_tenants(dt_from: dt.datetime | None = None,
                       dt_to: dt.datetime | None = None,
                       db: AsyncSession = Depends(get_db)):
    start = dt_from or _month_start()
    out = []
    for t in await _tenant_rows(db):
        stats = await _tenant_stats(db, t["schema_name"], start, dt_to)
        created = t["created_at"]
        out.append({
            "slug": t["slug"], "display_name": t["display_name"],
            "status": t["status"],
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
            **stats,
        })
    return out


@router.post("", status_code=201)
async def create_tenant(body: CreateTenantBody):
    try:
        res = await anyio.to_thread.run_sync(
            lambda: provision(body.slug, body.display_name,
                              str(body.admin_email), body.admin_password)
        )
    except ProvisionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    registry.invalidate()
    logger.info("tenant provisioned via API: %s", res.slug)
    # Секреты в ответе ОДИН раз — нигде больше не доступны
    return {"slug": res.slug, "admin_email": res.admin_email,
            "admin_password": res.admin_password, "api_key": res.api_key}


class PatchTenantBody(BaseModel):
    status: Literal["active", "suspended"]


async def _set_status(db: AsyncSession, slug: str, status: str) -> bool:
    res = await db.execute(text(
        "UPDATE shared.tenants SET status = :status WHERE slug = :slug"),
        {"status": status, "slug": slug})
    await db.commit()
    return res.rowcount > 0


async def _rotate_key(db: AsyncSession, slug: str) -> str | None:
    key, digest = generate_api_key()
    res = await db.execute(text(
        "UPDATE shared.tenants SET api_key_hash = :h WHERE slug = :slug"),
        {"h": digest, "slug": slug})
    await db.commit()
    return key if res.rowcount > 0 else None


@router.patch("/{slug}")
async def patch_tenant(slug: str, body: PatchTenantBody,
                       db: AsyncSession = Depends(get_db)):
    if not await _set_status(db, slug, body.status):
        raise HTTPException(status_code=404, detail="tenant not found")
    registry.invalidate()
    logger.info("tenant %s status -> %s", slug, body.status)
    return {"slug": slug, "status": body.status}


@router.post("/{slug}/rotate-key")
async def rotate_key(slug: str, db: AsyncSession = Depends(get_db)):
    key = await _rotate_key(db, slug)
    if key is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    registry.invalidate()
    logger.info("tenant %s api key rotated", slug)
    return {"slug": slug, "api_key": key}


# ── Task 11: управление юзерами клиента ──────────────────────────────────────

_ph = PasswordHasher()


class CreateUserBody(BaseModel):
    email: EmailStr
    role: Literal["admin", "viewer", "manager"] = "viewer"
    employee_name: str | None = None


class PatchUserBody(BaseModel):
    role: Literal["admin", "viewer", "manager"] | None = None
    employee_name: str | None = None


async def _schema_for(db: AsyncSession, slug: str) -> str | None:
    row = (await db.execute(text(
        "SELECT schema_name FROM shared.tenants WHERE slug = :slug"),
        {"slug": slug})).first()
    return row.schema_name if row else None


async def _require_schema(db: AsyncSession, slug: str) -> str:
    schema = await _schema_for(db, slug)
    if schema is None or not SCHEMA_RE.fullmatch(schema):
        raise HTTPException(status_code=404, detail="tenant not found")
    return schema


async def _list_users(db: AsyncSession, schema: str) -> list[dict]:
    res = await db.execute(text(
        f"SELECT id, email, role, employee_name, created_at, last_login "
        f"FROM {schema}.users ORDER BY created_at"))
    return [
        {"id": str(r.id), "email": r.email, "role": r.role,
         "employee_name": r.employee_name,
         "created_at": r.created_at.isoformat() if r.created_at else None,
         "last_login": r.last_login.isoformat() if r.last_login else None}
        for r in res
    ]


async def _insert_user(db, schema, email, role, employee_name, pw_hash) -> str | None:
    try:
        row = (await db.execute(text(
            f"INSERT INTO {schema}.users (email, password_hash, role, employee_name) "
            f"VALUES (:email, :pw, :role, :emp) RETURNING id"),
            {"email": email, "pw": pw_hash, "role": role, "emp": employee_name},
        )).first()
        await db.commit()
        return str(row.id)
    except Exception:  # uq_users_email_lower
        await db.rollback()
        return None


async def _reset_password(db, schema, user_id, pw_hash) -> bool:
    res = await db.execute(text(
        f"UPDATE {schema}.users SET password_hash = :pw WHERE id = :id"),
        {"pw": pw_hash, "id": user_id})
    await db.commit()
    return res.rowcount > 0


async def _delete_user(db, schema, user_id) -> bool:
    res = await db.execute(text(
        f"DELETE FROM {schema}.users WHERE id = :id"), {"id": user_id})
    await db.commit()
    return res.rowcount > 0


@router.get("/{slug}/users")
async def tenant_users(slug: str, db: AsyncSession = Depends(get_db)):
    schema = await _require_schema(db, slug)
    return await _list_users(db, schema)


@router.post("/{slug}/users", status_code=201)
async def tenant_user_create(slug: str, body: CreateUserBody,
                             db: AsyncSession = Depends(get_db)):
    schema = await _require_schema(db, slug)
    password = generate_password()
    uid = await _insert_user(db, schema, str(body.email), body.role,
                             body.employee_name, _ph.hash(password))
    if uid is None:
        raise HTTPException(status_code=409, detail="email already exists")
    return {"id": uid, "email": str(body.email), "role": body.role,
            "password": password}


@router.patch("/{slug}/users/{user_id}")
async def tenant_user_patch(slug: str, user_id: uuid.UUID, body: PatchUserBody,
                            db: AsyncSession = Depends(get_db)):
    schema = await _require_schema(db, slug)
    sets, params = [], {"id": str(user_id)}
    if body.role is not None:
        sets.append("role = :role"); params["role"] = body.role
    if body.employee_name is not None:
        sets.append("employee_name = :emp"); params["emp"] = body.employee_name or None
    if not sets:
        raise HTTPException(status_code=422, detail="nothing to change")
    res = await db.execute(text(
        f"UPDATE {schema}.users SET {', '.join(sets)} WHERE id = :id"), params)
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True}


@router.delete("/{slug}/users/{user_id}", status_code=204)
async def tenant_user_delete(slug: str, user_id: uuid.UUID,
                             db: AsyncSession = Depends(get_db)):
    schema = await _require_schema(db, slug)
    if not await _delete_user(db, schema, str(user_id)):
        raise HTTPException(status_code=404, detail="user not found")


@router.post("/{slug}/users/{user_id}/reset-password")
async def tenant_user_reset_password(slug: str, user_id: uuid.UUID,
                                     db: AsyncSession = Depends(get_db)):
    schema = await _require_schema(db, slug)
    password = generate_password()
    if not await _reset_password(db, schema, str(user_id), _ph.hash(password)):
        raise HTTPException(status_code=404, detail="user not found")
    return {"password": password}
