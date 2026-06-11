"""JWT + password helpers for broker authentication.

Fails loud at import time if SECRET_KEY is missing/empty so we don't quietly
sign tokens with an empty key.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tenancy.context import get_tenant_slug

from app.config import settings
from app.database import get_db
from app.schemas import BrokerInfo


logger = logging.getLogger(__name__)


# Fail loud at import time if SECRET_KEY is not configured OR is the
# placeholder default — we won't sign tokens with a public key.
_PLACEHOLDER_SECRETS = {"", "change-this-in-production", "changeme", "secret"}
if (
    not settings.SECRET_KEY
    or not str(settings.SECRET_KEY).strip()
    or str(settings.SECRET_KEY).strip().lower() in _PLACEHOLDER_SECRETS
):
    raise RuntimeError(
        "SECRET_KEY is not configured (or is the placeholder default); "
        "refusing to start broker auth module"
    )

_SECRET_KEY: str = settings.SECRET_KEY
_ALGORITHM: str = "HS256"
_TOKEN_TTL = timedelta(days=30)

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return _pwd_context.verify(plain, hashed)
    except (ValueError, TypeError):
        return False


def create_token(
    broker_id: uuid.UUID, amocrm_user_id: int, email: str, name: str, tenant: str
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(broker_id),
        "amocrm_user_id": int(amocrm_user_id),
        "email": email,
        "name": name,
        "tenant": tenant,
        "iat": int(now.timestamp()),
        "exp": int((now + _TOKEN_TTL).timestamp()),
    }
    return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)


def decode_token(token: str) -> dict | None:
    if not token:
        return None
    try:
        return jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
    except JWTError:
        return None
    except Exception:
        # Defensive: jose can raise unexpected errors on weird input.
        logger.exception("Unexpected error decoding broker token")
        return None


def _grace_active() -> bool:
    """Grace-окно (спека 5.4): legacy-JWT без tenant-claim живут 30 дней."""
    raw = settings.BROKER_JWT_TENANT_GRACE_UNTIL
    if not raw:
        return False
    try:
        deadline = date.fromisoformat(raw)
    except ValueError:
        return False
    return datetime.now(timezone.utc).date() < deadline


def check_tenant_claim(payload: dict) -> None:
    """401, если tenant-claim токена не совпадает с текущим тенантом.

    Токен без claim (выдан до Plan 2) трактуется как realestate до конца
    grace-окна; после — отклоняется.
    """
    tok_tenant = payload.get("tenant")
    slug = get_tenant_slug()
    if tok_tenant is not None:
        if tok_tenant != slug:
            raise HTTPException(status_code=401, detail="Invalid token")
        return
    if not (_grace_active() and slug == "realestate"):
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_broker(
    x_broker_token: str | None = Header(default=None, alias="X-Broker-Token"),
    db: AsyncSession = Depends(get_db),
) -> BrokerInfo | None:
    """Optional broker auth dependency.

    - Returns None if no token is supplied (broker attribution is optional).
    - Returns 401 if a token IS supplied but is invalid/expired/refers to a
      missing or inactive broker.
    """
    if not x_broker_token:
        return None

    payload = decode_token(x_broker_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid token")

    check_tenant_claim(payload)

    try:
        broker_id = uuid.UUID(str(sub))
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid token") from None

    row = (
        await db.execute(
            text(
                "SELECT id, amocrm_user_id, email, name, is_active "
                "FROM brokers WHERE id = :id"
            ),
            {"id": broker_id},
        )
    ).first()
    if not row or not row[4]:
        raise HTTPException(status_code=401, detail="Broker not found or inactive")

    return BrokerInfo(
        id=row[0],
        amocrm_user_id=int(row[1]),
        email=row[2],
        name=row[3],
    )
