"""Request/task-scoped tenant context.

The web middleware and every Celery task entry set the current tenant schema
here; the DB factory (tenancy.db) and path helpers read it. ``None`` is the
platform contour (no tenant): shared-only queries.
"""
from __future__ import annotations

import contextvars

from tenancy.identifiers import slug_from_schema, validate_schema_name


class TenantContextError(RuntimeError):
    """Raised when tenant context is required but not set."""


_tenant_schema: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_schema", default=None
)


def set_tenant_schema(schema: str | None) -> contextvars.Token:
    if schema is not None:
        validate_schema_name(schema)
    return _tenant_schema.set(schema)


def reset_tenant_schema(token: contextvars.Token) -> None:
    _tenant_schema.reset(token)


def get_tenant_schema() -> str | None:
    return _tenant_schema.get()


def require_tenant_schema() -> str:
    schema = _tenant_schema.get()
    if not schema:
        raise TenantContextError(
            "tenant context is not set; pass tenant_schema to the task or "
            "ensure the request went through TenantResolutionMiddleware"
        )
    return schema


def get_tenant_slug() -> str | None:
    schema = _tenant_schema.get()
    return slug_from_schema(schema) if schema else None


def require_tenant_slug() -> str:
    return slug_from_schema(require_tenant_schema())
