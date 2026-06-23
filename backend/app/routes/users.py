"""Команда тенанта: /api/users (спека §4). Только admin."""
from __future__ import annotations

import uuid

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Literal

from ..auth_sessions import destroy_user_sessions
from ..auth_user import UserCtx, require_admin
from ..database import get_db
from ..provision_tenant import generate_password

router = APIRouter(prefix="/api/users", tags=["users"])

_ph = PasswordHasher()


class CreateUserBody(BaseModel):
    email: EmailStr
    role: Literal["admin", "viewer", "manager"] = "viewer"
    employee_name: str | None = None


class PatchUserBody(BaseModel):
    role: Literal["admin", "viewer", "manager"] | None = None
    employee_name: str | None = None


# Хендлеры объявляют `user_id: str`, а НЕ `uuid.UUID`, намеренно:
#  - типизированный param заставил бы FastAPI вернуть 422 на неканонический/
#    мусорный id, а нам нужны 403 (self-guard) и 404 (мусор), а не 422;
#  - self-guard сравнивает path-id со СТРОКОЙ `me.user_id` (в тестах это "u1",
#    не валидный UUID) — типизация на uuid.UUID сломала бы это сравнение.
# Поэтому валидируем вручную через _validate_uuid, но ТОЛЬКО ПОСЛЕ self-guard.
# НЕ меняй на типизированный param — это вновь откроет обход self-guard.
def _validate_uuid(user_id: str) -> None:
    """Мусорный id → 404 (а не 500 на UUID-колонке). Вызывать ПОСЛЕ
    self-guards (они сравнивают строки и должны срабатывать раньше)."""
    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="user not found")


def _is_self(user_id: str, my_id: str) -> bool:
    """True если path-id указывает на текущего юзера. Устойчиво к
    неканоническому UUID (верхний регистр и т.п.): прод-сессия хранит
    канонический str(uuid), а path может прийти в любой форме —
    сырое сравнение тут дырявое (обход self-guard)."""
    if user_id == my_id:           # точное совпадение (в т.ч. не-UUID id типа "u1" в тестах)
        return True
    try:
        return str(uuid.UUID(user_id)) == my_id  # канонизируем path, сверяем с канон. сессией
    except ValueError:
        return False


async def _is_last_admin(db: AsyncSession, user_id: str, target: dict | None) -> bool:
    """target — админ и других админов в тенанте нет."""
    return bool(target) and target["role"] == "admin" \
        and await _count_other_admins(db, user_id) == 0


async def _get_user(db: AsyncSession, user_id: str) -> dict | None:
    row = (await db.execute(text(
        "SELECT id, email, role FROM users WHERE id = :id"),
        {"id": user_id})).first()
    return {"id": str(row.id), "email": row.email, "role": row.role} if row else None


async def _count_other_admins(db: AsyncSession, user_id: str) -> int:
    return (await db.execute(text(
        "SELECT count(*) FROM users WHERE role = 'admin' AND id != :id"),
        {"id": user_id})).scalar_one()


async def _delete_user(db: AsyncSession, user_id: str) -> bool:
    res = await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    await db.commit()
    return res.rowcount > 0


@router.get("", dependencies=[Depends(require_admin)])
async def list_users(db: AsyncSession = Depends(get_db)):
    res = await db.execute(text(
        "SELECT id, email, role, employee_name, created_at, last_login "
        "FROM users ORDER BY created_at"))
    return [
        {"id": str(r.id), "email": r.email, "role": r.role,
         "employee_name": r.employee_name,
         "created_at": r.created_at.isoformat() if r.created_at else None,
         "last_login": r.last_login.isoformat() if r.last_login else None}
        for r in res
    ]


@router.post("", status_code=201)
async def create_user(body: CreateUserBody, db: AsyncSession = Depends(get_db),
                      _: UserCtx = Depends(require_admin)):
    password = generate_password()
    try:
        row = (await db.execute(text(
            "INSERT INTO users (email, password_hash, role, employee_name) "
            "VALUES (:email, :pw, :role, :emp) RETURNING id"),
            {"email": str(body.email), "pw": _ph.hash(password),
             "role": body.role, "emp": body.employee_name},
        )).first()
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=409, detail="email already exists")
    return {"id": str(row.id), "email": str(body.email), "role": body.role,
            "employee_name": body.employee_name, "password": password}


@router.patch("/{user_id}")
async def patch_user(user_id: str, body: PatchUserBody,
                     db: AsyncSession = Depends(get_db),
                     me: UserCtx = Depends(require_admin)):
    # 1. self-guard (устойчив к неканоническому UUID, до uuid-валидации)
    if body.role is not None and body.role != "admin" and _is_self(user_id, me.user_id):
        raise HTTPException(status_code=403, detail="cannot_demote_self")
    # 2. uuid-валидация (мусорный id → 404, до БД)
    _validate_uuid(user_id)
    # 3. last_admin-проверка (обращения к БД); target=None → 404 даст мутация ниже
    if body.role is not None and body.role != "admin":
        target = await _get_user(db, user_id)
        if await _is_last_admin(db, user_id, target):
            raise HTTPException(status_code=403, detail="last_admin")
    sets, params = [], {"id": user_id}
    if body.role is not None:
        sets.append("role = :role"); params["role"] = body.role
    if body.employee_name is not None:
        sets.append("employee_name = :emp"); params["emp"] = body.employee_name or None
    if not sets:
        raise HTTPException(status_code=422, detail="nothing to change")
    res = await db.execute(text(
        f"UPDATE users SET {', '.join(sets)} WHERE id = :id"), params)
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True}


@router.delete("/{user_id}", status_code=204)
async def delete_user(user_id: str, db: AsyncSession = Depends(get_db),
                      me: UserCtx = Depends(require_admin)):
    # 1. self-guard (устойчив к неканоническому UUID, до uuid-валидации)
    if _is_self(user_id, me.user_id):
        raise HTTPException(status_code=403, detail="cannot_delete_self")
    # 2. uuid-валидация (мусорный id → 404, до БД)
    _validate_uuid(user_id)
    # 3. обращения к БД
    target = await _get_user(db, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if await _is_last_admin(db, user_id, target):
        raise HTTPException(status_code=403, detail="last_admin")
    await _delete_user(db, user_id)
    await destroy_user_sessions(target["email"])


@router.post("/{user_id}/reset-password")
async def reset_password(user_id: str, db: AsyncSession = Depends(get_db),
                         _: UserCtx = Depends(require_admin)):
    # self-guard нет; uuid-валидация в самом начале (мусорный id → 404, до БД)
    _validate_uuid(user_id)
    target = await _get_user(db, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    password = generate_password()
    await db.execute(text("UPDATE users SET password_hash = :pw WHERE id = :id"),
                     {"pw": _ph.hash(password), "id": user_id})
    await db.commit()
    await destroy_user_sessions(target["email"])
    return {"password": password}
