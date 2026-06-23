"""Sync access to the shared.tenants registry (worker side).

Beat tasks receive no arguments by design — they iterate tenants themselves.
"""
from __future__ import annotations

from tenancy.db import shared_connect


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
    # Single source of truth = tenants.modules.amocrm (inline default OFF; keeps
    # tenancy/ app-independent so the worker doesn't import app.modules).
    return [
        t for t in iter_active_tenants()
        if bool((t.get("modules") or {}).get("amocrm", False))
    ]


def tenant_amocrm_enabled(slug: str) -> bool:
    """True если у тенанта (по slug) включён модуль amocrm — одна истина с
    iter_amocrm_tenants / app.modules (tenants.modules.amocrm, дефолт OFF).
    Заменяет легаси-константу AMOCRM_TENANT_SLUGS для внутренних RE-путей."""
    conn = shared_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT modules FROM shared.tenants WHERE slug = %s AND status = 'active'",
                (slug,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    modules = dict(row[0] or {}) if row else {}
    return bool(modules.get("amocrm", False))
