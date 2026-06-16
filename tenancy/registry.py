"""Sync access to the shared.tenants registry (worker side).

Beat tasks receive no arguments by design — they iterate tenants themselves.
"""
from __future__ import annotations

from tenancy.db import shared_connect

# Phase 1: the AmoCRM integration exists only for realestate. Phase 2 replaces
# this constant with per-tenant integration config.
AMOCRM_TENANT_SLUGS = ("realestate",)


def iter_active_tenants() -> list[dict]:
    conn = shared_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT slug, schema_name, modules FROM shared.tenants "
                "WHERE status = 'active' ORDER BY slug"
            )
            return [
                {"slug": r[0], "schema_name": r[1], "modules": dict(r[2] or {})}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def iter_amocrm_tenants() -> list[dict]:
    return [
        t for t in iter_active_tenants() if t["slug"] in AMOCRM_TENANT_SLUGS
    ]
