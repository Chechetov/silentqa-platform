"""Broker authentication routes.

Three-step UX:
  1. /api/auth/claim/start    — broker enters email; we confirm they exist in
                                AmoCRM and have not yet set a password.
  2. /api/auth/claim/complete — broker sets their password and gets a JWT.
  3. /api/auth/login          — subsequent logins.

`/api/auth/me` validates a JWT and returns broker info.

These endpoints are still gated by the global HTTP Basic admin middleware in
`app.main` (everything under `/api/*` is open to the extension/desktop, and
the admin gate sits on UI routes — Basic Auth is not removed).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tenancy.context import get_tenant_slug, require_tenant_slug

from app.auth_jwt import (
    create_token,
    get_current_broker,
    hash_password,
    verify_password,
)
from app.database import get_db
from app.schemas import (
    BrokerClaimCompleteRequest,
    BrokerClaimStartRequest,
    BrokerClaimStartResponse,
    BrokerInfo,
    BrokerLoginRequest,
    BrokerTokenResponse,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Computed once at import. Used to keep /login response time roughly constant
# whether the email exists or not — without it, "no such email" returns
# instantly while "wrong password" takes ~100ms (a timing oracle for
# enumerating valid emails).
_DUMMY_HASH = hash_password("dummy-not-a-real-password")


def _mask_email(email: str) -> str:
    """`Tropicana1907@gmail.com` → `T***@gmail.com`."""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    if not local:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


@router.post("/claim/start", response_model=BrokerClaimStartResponse)
async def claim_start(body: BrokerClaimStartRequest, db: AsyncSession = Depends(get_db)):
    if get_tenant_slug() is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    row = (
        await db.execute(
            text(
                "SELECT email, name, password_hash, is_active "
                "FROM brokers "
                "WHERE LOWER(email) = LOWER(:email)"
            ),
            {"email": body.email},
        )
    ).first()
    if not row or not row[3]:
        raise HTTPException(status_code=404, detail="Email not found")
    if row[2] is not None:
        raise HTTPException(status_code=409, detail="Already claimed")
    return BrokerClaimStartResponse(name=row[1], masked_email=_mask_email(row[0]))


@router.post("/claim/complete", response_model=BrokerTokenResponse)
async def claim_complete(body: BrokerClaimCompleteRequest, db: AsyncSession = Depends(get_db)):
    if get_tenant_slug() is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    row = (
        await db.execute(
            text(
                "SELECT id, amocrm_user_id, email, name, password_hash, is_active "
                "FROM brokers "
                "WHERE LOWER(email) = LOWER(:email)"
            ),
            {"email": body.email},
        )
    ).first()
    if not row or not row[5]:
        raise HTTPException(status_code=404, detail="Email not found")
    if row[4] is not None:
        raise HTTPException(status_code=409, detail="Already claimed")

    pw_hash = hash_password(body.password)
    await db.execute(
        text(
            "UPDATE brokers "
            "SET password_hash = :h, claimed_at = NOW(), updated_at = NOW() "
            "WHERE id = :id"
        ),
        {"h": pw_hash, "id": row[0]},
    )
    await db.commit()

    token = create_token(
        broker_id=row[0],
        amocrm_user_id=int(row[1]),
        email=row[2],
        name=row[3],
        tenant=require_tenant_slug(),
    )
    return BrokerTokenResponse(
        token=token,
        broker=BrokerInfo(
            id=row[0],
            amocrm_user_id=int(row[1]),
            email=row[2],
            name=row[3],
        ),
    )


@router.post("/login", response_model=BrokerTokenResponse)
async def login(body: BrokerLoginRequest, db: AsyncSession = Depends(get_db)):
    if get_tenant_slug() is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    row = (
        await db.execute(
            text(
                "SELECT id, amocrm_user_id, email, name, password_hash, is_active "
                "FROM brokers "
                "WHERE LOWER(email) = LOWER(:email)"
            ),
            {"email": body.email},
        )
    ).first()
    # Single generic 401 — don't leak whether the email exists. Run a dummy
    # bcrypt verify on the not-found branch so response time doesn't reveal
    # which path was taken.
    if not row or not row[5] or not row[4]:
        verify_password(body.password, _DUMMY_HASH)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_password(body.password, row[4]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_token(
        broker_id=row[0],
        amocrm_user_id=int(row[1]),
        email=row[2],
        name=row[3],
        tenant=require_tenant_slug(),
    )
    return BrokerTokenResponse(
        token=token,
        broker=BrokerInfo(
            id=row[0],
            amocrm_user_id=int(row[1]),
            email=row[2],
            name=row[3],
        ),
    )


@router.get("/me", response_model=BrokerInfo)
async def me(broker: BrokerInfo | None = Depends(get_current_broker)):
    if broker is None:
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    return broker
