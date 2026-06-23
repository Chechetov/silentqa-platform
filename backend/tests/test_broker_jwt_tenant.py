"""Брокерский JWT: tenant-claim (спека 5.4) — выпуск и верификация."""
import uuid

import pytest
from jose import jwt as jose_jwt

from tenancy.context import reset_tenant_schema, set_tenant_schema

from app import auth_jwt
from app.config import settings


@pytest.fixture
def realestate_ctx():
    token = set_tenant_schema("t_realestate")
    yield
    reset_tenant_schema(token)


@pytest.fixture
def acme_ctx():
    token = set_tenant_schema("t_acme")
    yield
    reset_tenant_schema(token)


def _make_legacy_token() -> str:
    """Токен БЕЗ tenant-claim — как все выданные до Plan 2."""
    return jose_jwt.encode(
        {"sub": str(uuid.uuid4()), "amocrm_user_id": 1,
         "email": "b@t.io", "name": "B", "iat": 0, "exp": 99999999999},
        settings.SECRET_KEY, algorithm="HS256",
    )


def test_create_token_embeds_current_tenant(realestate_ctx):
    tok = auth_jwt.create_token(
        broker_id=uuid.uuid4(), amocrm_user_id=1, email="b@t.io", name="B",
        tenant="realestate",
    )
    payload = jose_jwt.decode(tok, settings.SECRET_KEY, algorithms=["HS256"])
    assert payload["tenant"] == "realestate"


def test_check_tenant_claim_matching_ok(realestate_ctx):
    auth_jwt.check_tenant_claim({"tenant": "realestate"})  # no raise


def test_check_tenant_claim_foreign_401(acme_ctx):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        auth_jwt.check_tenant_claim({"tenant": "realestate"})
    assert e.value.status_code == 401


def test_legacy_token_rejected_when_grace_off(realestate_ctx, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(settings, "BROKER_JWT_TENANT_GRACE_UNTIL", "")
    with pytest.raises(HTTPException):
        auth_jwt.check_tenant_claim({})


def test_legacy_token_ok_for_realestate_in_grace(realestate_ctx, monkeypatch):
    monkeypatch.setattr(settings, "BROKER_JWT_TENANT_GRACE_UNTIL", "2099-01-01")
    auth_jwt.check_tenant_claim({})  # no raise


def test_legacy_token_rejected_for_other_tenant_even_in_grace(acme_ctx, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(settings, "BROKER_JWT_TENANT_GRACE_UNTIL", "2099-01-01")
    with pytest.raises(HTTPException):
        auth_jwt.check_tenant_claim({})
