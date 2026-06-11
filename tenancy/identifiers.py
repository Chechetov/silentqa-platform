"""Slug and schema-name validation for tenants.

Slugs become Postgres schema identifiers (``t_<slug>``) and are interpolated
into ``search_path`` strings, so the charset is locked down hard: lowercase
ASCII letters, digits, underscore; 2-31 chars; must start with a letter.
Hyphens are forbidden (invalid unquoted identifier). Schema names are never
built from a raw Host header — only from values read out of shared.tenants —
but validate_schema_name() is the last line of defence before interpolation.
"""
from __future__ import annotations

import re

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
SCHEMA_RE = re.compile(r"^t_[a-z][a-z0-9_]{1,30}$")

RESERVED_SLUGS = frozenset(
    {"admin", "www", "api", "app", "mail", "apex", "shared", "public", "default"}
)


def validate_slug(slug: str) -> str:
    if not SLUG_RE.fullmatch(slug or ""):
        raise ValueError(
            f"invalid tenant slug {slug!r}: must match {SLUG_RE.pattern}"
        )
    if slug in RESERVED_SLUGS:
        raise ValueError(f"tenant slug {slug!r} is reserved")
    return slug


def schema_for_slug(slug: str) -> str:
    return f"t_{validate_slug(slug)}"


def validate_schema_name(schema: str) -> str:
    if not SCHEMA_RE.fullmatch(schema or ""):
        raise ValueError(f"invalid tenant schema name {schema!r}")
    return schema


def slug_from_schema(schema: str) -> str:
    return validate_schema_name(schema)[2:]
