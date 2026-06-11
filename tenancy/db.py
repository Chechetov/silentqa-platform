"""The single factory for raw sync DB connections.

Every psycopg2 / ad-hoc SQLAlchemy sync connection in the codebase MUST come
from here: the tenant search_path is injected per-connection via libpq
``options`` (a per-task ``SET LOCAL`` cannot cover the dozens of short-lived
connections the worker opens). Direct ``psycopg2.connect`` in application
code is forbidden — enforced by worker/tests/test_no_raw_connects.py.
"""
from __future__ import annotations

import os

import psycopg2
from sqlalchemy import create_engine as _create_engine

from tenancy.context import require_tenant_schema


def get_sync_db_url() -> str:
    """Plain postgresql:// URL for psycopg2 (dialect prefixes stripped)."""
    url = os.getenv("DATABASE_URL_SYNC", "") or os.getenv("DATABASE_URL", "")
    url = url.replace("postgresql+psycopg2://", "postgresql://")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    return url


def get_sync_dialect_url() -> str:
    """postgresql+psycopg2:// URL for SQLAlchemy sync engines."""
    url = os.getenv("DATABASE_URL_SYNC", "") or os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def _options(search_path: str) -> str:
    return f"-c search_path={search_path}"


def tenant_connect(**kwargs):
    """psycopg2 connection with the current tenant's search_path baked in."""
    schema = require_tenant_schema()
    return psycopg2.connect(
        get_sync_db_url(), options=_options(f"{schema},shared,public"), **kwargs
    )


def shared_connect(**kwargs):
    """psycopg2 connection scoped to the shared registry (no tenant)."""
    return psycopg2.connect(
        get_sync_db_url(), options=_options("shared,public"), **kwargs
    )


def tenant_engine():
    """Throwaway SQLAlchemy sync engine bound to the current tenant.

    Caller owns the lifecycle (``.dispose()`` in finally), matching the
    existing ad-hoc create_engine call sites it replaces.
    """
    schema = require_tenant_schema()
    return _create_engine(
        get_sync_dialect_url(),
        future=True,
        connect_args={"options": _options(f"{schema},shared,public")},
    )
