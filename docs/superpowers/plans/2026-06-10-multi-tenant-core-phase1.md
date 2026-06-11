# Multi-Tenant Core (Phase 1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Превратить одно-тенантную систему в мульти-тенантную: реестр тенантов в схеме `shared`, схема `t_<slug>` на тенанта, резолв по домену, единая фабрика sync-соединений с пер-тенант `search_path`, проброс тенанта в Celery, изоляция файлового хранилища, провижининг-CLI.

**Architecture:** Spec: `docs/superpowers/specs/2026-06-10-multi-tenant-core-design.md` (v2). Новый top-level пакет `tenancy/` (contextvars + фабрика соединений + пути) импортируется и backend'ом, и worker'ом. Alembic делится на два трека: shared (`backend/alembic_shared/`, ревизии S001–S002) и tenant (существующий `backend/alembic/`, 001–012, version table внутри каждой тенант-схемы). Cutover переносит таблицы `public` → `t_realestate` через `SET SCHEMA` и переносит stamp.

**Tech Stack:** FastAPI/Starlette (pure-ASGI middleware), SQLAlchemy 2 (async + событие `begin`), psycopg2 (`options=-c search_path=...`), Alembic 1.14 (два трека, `version_table_schema`), argon2-cffi (хеш пароля admin-юзера), Celery 5.4.

**Контекст выполнения:** проект НЕ деплоится из этого чекаута автоматически; прод — systemd `realestate-backend.service`/`realestate-worker.service` из `/root/projects/realestate`. Все тесты гоняются локально: worker-тесты `cd worker && ../.venv/bin/python -m pytest`, новые backend-тесты `cd backend && ../.venv/bin/python -m pytest tests`. Интерпретатор: `/root/projects/meet/.venv/bin/python`.

**Жёсткие инварианты (из адверсариального ревью спека):**
1. `SET LOCAL` в начале таски НЕ работает — воркер открывает ~27 независимых psycopg2-соединений. Только фабрика с `options=`.
2. `public.alembic_version` сейчас держит литеральную строку `'010'` (ревизии именованы `"001"`…`"010"`, не хеши). Stamp переносится копированием в `t_realestate.alembic_version`.
3. Beat-таски (`poll_amocrm_calls`, `sweep_stuck_sessions`, `reconcile_amocrm_calls`) аргументов не получают — они итерируют тенантов сами.
4. `amocrm_sync._read_token_from_portal_db` (worker/tasks/amocrm_sync.py:41) подключается к той же БД, но читает schema-qualified `portal.amocrm_tokens` — **НЕ переводить на фабрику, не трогать**.
5. `amocrm_reconcile.py:43-48` импортирует `_get_sync_db_url` из `amocrm_poll` — при замене хелпера оставить алиас.
6. Тесты `worker/tests/test_amocrm_recording_resolution.py:59,83` и `test_amocrm_inbound_and_push.py:62` монкипатчат `ap.AUDIO_PATH` — пути строим функциями, принимающими root параметром, module-globals не убираем.

---

### Task 1: Пакет `tenancy` — идентификаторы (slug/schema)

**Files:**
- Create: `tenancy/__init__.py` (пустой)
- Create: `tenancy/identifiers.py`
- Test: `worker/tests/test_tenancy_identifiers.py`

`worker/tests/conftest.py` уже вставляет `worker/` в `sys.path`; добавим и корень репо (нужно для `import tenancy`).

- [x] **Step 1: Расширить conftest**

В `worker/tests/conftest.py` после существующего `sys.path.insert(0, str(Path(__file__).parent.parent))` добавить:

```python
# Make top-level `tenancy` package importable during tests
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
```

- [x] **Step 2: Написать failing-тест**

```python
"""Tests for tenancy.identifiers — slug/schema validation and derivation."""
import pytest

from tenancy.identifiers import (
    RESERVED_SLUGS,
    schema_for_slug,
    slug_from_schema,
    validate_schema_name,
    validate_slug,
)


def test_valid_slug_roundtrip():
    assert validate_slug("realestate") == "realestate"
    assert schema_for_slug("realestate") == "t_realestate"
    assert slug_from_schema("t_realestate") == "realestate"


def test_slug_rejects_bad_charset():
    for bad in ("My-Client", "my-client", "1abc", "a", "x" * 32, "", "acme.evil", "т_кир"):
        with pytest.raises(ValueError):
            validate_slug(bad)


def test_slug_rejects_reserved():
    assert "admin" in RESERVED_SLUGS
    for r in ("admin", "www", "api", "shared", "public"):
        with pytest.raises(ValueError):
            validate_slug(r)


def test_schema_name_validation_blocks_injection():
    assert validate_schema_name("t_acme") == "t_acme"
    for bad in ("public", "t_acme; DROP TABLE x", "t_", "t_Acme", "acme"):
        with pytest.raises(ValueError):
            validate_schema_name(bad)


def test_slug_from_schema_rejects_foreign_schema():
    with pytest.raises(ValueError):
        slug_from_schema("public")
```

- [x] **Step 3: Прогнать — убедиться, что падает**

Run: `cd /root/projects/meet/worker && ../.venv/bin/python -m pytest tests/test_tenancy_identifiers.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'tenancy'`

- [x] **Step 4: Реализация `tenancy/identifiers.py`**

```python
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
    if not SLUG_RE.match(slug or ""):
        raise ValueError(
            f"invalid tenant slug {slug!r}: must match {SLUG_RE.pattern}"
        )
    if slug in RESERVED_SLUGS:
        raise ValueError(f"tenant slug {slug!r} is reserved")
    return slug


def schema_for_slug(slug: str) -> str:
    return f"t_{validate_slug(slug)}"


def validate_schema_name(schema: str) -> str:
    if not SCHEMA_RE.match(schema or ""):
        raise ValueError(f"invalid tenant schema name {schema!r}")
    return schema


def slug_from_schema(schema: str) -> str:
    return validate_schema_name(schema)[2:]
```

- [x] **Step 5: Прогнать — зелёный**

Run: `cd /root/projects/meet/worker && ../.venv/bin/python -m pytest tests/test_tenancy_identifiers.py -v`
Expected: 5 passed

- [x] **Step 6: Commit**

```bash
cd /root/projects/meet && git add tenancy worker/tests/test_tenancy_identifiers.py worker/tests/conftest.py
git commit -m "feat(tenancy): slug/schema identifier validation"
```

---

### Task 2: `tenancy/context.py` — contextvar тенанта

**Files:**
- Create: `tenancy/context.py`
- Test: `worker/tests/test_tenancy_context.py`

- [ ] **Step 1: Failing-тест**

```python
"""Tests for tenancy.context — request/task-scoped tenant state."""
import pytest

from tenancy.context import (
    TenantContextError,
    get_tenant_schema,
    get_tenant_slug,
    require_tenant_schema,
    require_tenant_slug,
    reset_tenant_schema,
    set_tenant_schema,
)


def test_default_is_unset():
    assert get_tenant_schema() is None
    assert get_tenant_slug() is None
    with pytest.raises(TenantContextError):
        require_tenant_schema()
    with pytest.raises(TenantContextError):
        require_tenant_slug()


def test_set_get_reset():
    token = set_tenant_schema("t_acme")
    try:
        assert get_tenant_schema() == "t_acme"
        assert require_tenant_schema() == "t_acme"
        assert get_tenant_slug() == "acme"
        assert require_tenant_slug() == "acme"
    finally:
        reset_tenant_schema(token)
    assert get_tenant_schema() is None


def test_set_validates_schema_name():
    with pytest.raises(ValueError):
        set_tenant_schema("public")
    with pytest.raises(ValueError):
        set_tenant_schema("t_acme; DROP SCHEMA shared")


def test_set_none_is_platform_context():
    token = set_tenant_schema(None)
    try:
        assert get_tenant_schema() is None
    finally:
        reset_tenant_schema(token)
```

- [ ] **Step 2: Прогнать — FAIL (`ModuleNotFoundError`)**

Run: `cd /root/projects/meet/worker && ../.venv/bin/python -m pytest tests/test_tenancy_context.py -v`

- [ ] **Step 3: Реализация `tenancy/context.py`**

```python
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
```

- [ ] **Step 4: Прогнать — зелёный; Commit**

```bash
cd /root/projects/meet/worker && ../.venv/bin/python -m pytest tests/test_tenancy_context.py -v
cd /root/projects/meet && git add tenancy/context.py worker/tests/test_tenancy_context.py
git commit -m "feat(tenancy): tenant contextvar with validation"
```

---

### Task 3: `tenancy/db.py` — фабрика sync-соединений

**Files:**
- Create: `tenancy/db.py`
- Test: `worker/tests/test_tenancy_db.py`

- [ ] **Step 1: Failing-тест** (psycopg2.connect монкипатчится — реальная БД не нужна)

```python
"""Tests for tenancy.db — the single sync-connection factory."""
import pytest

import tenancy.db as tdb
from tenancy.context import reset_tenant_schema, set_tenant_schema, TenantContextError


@pytest.fixture()
def fake_connect(monkeypatch):
    calls = []

    def _connect(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(tdb.psycopg2, "connect", _connect)
    monkeypatch.setenv("DATABASE_URL_SYNC", "postgresql+psycopg2://u:p@h:5/db")
    return calls


def test_get_sync_db_url_strips_dialect(monkeypatch):
    monkeypatch.setenv("DATABASE_URL_SYNC", "postgresql+psycopg2://u:p@h:5/db")
    assert tdb.get_sync_db_url() == "postgresql://u:p@h:5/db"
    monkeypatch.delenv("DATABASE_URL_SYNC")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5/db")
    assert tdb.get_sync_db_url() == "postgresql://u:p@h:5/db"
    monkeypatch.delenv("DATABASE_URL")
    assert tdb.get_sync_db_url() == ""


def test_tenant_connect_injects_search_path(fake_connect):
    token = set_tenant_schema("t_acme")
    try:
        tdb.tenant_connect()
    finally:
        reset_tenant_schema(token)
    url, kwargs = fake_connect[0]
    assert url == "postgresql://u:p@h:5/db"
    assert kwargs["options"] == "-c search_path=t_acme,shared,public"


def test_tenant_connect_requires_context(fake_connect):
    with pytest.raises(TenantContextError):
        tdb.tenant_connect()


def test_tenant_connect_passes_kwargs(fake_connect):
    token = set_tenant_schema("t_acme")
    try:
        tdb.tenant_connect(connect_timeout=5)
    finally:
        reset_tenant_schema(token)
    assert fake_connect[0][1]["connect_timeout"] == 5


def test_shared_connect(fake_connect):
    tdb.shared_connect()
    assert fake_connect[0][1]["options"] == "-c search_path=shared,public"


def test_tenant_engine_options(monkeypatch):
    monkeypatch.setenv("DATABASE_URL_SYNC", "postgresql+psycopg2://u:p@h:5/db")
    captured = {}

    def _fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(tdb, "_create_engine", _fake_create_engine)
    token = set_tenant_schema("t_acme")
    try:
        tdb.tenant_engine()
    finally:
        reset_tenant_schema(token)
    assert captured["url"] == "postgresql+psycopg2://u:p@h:5/db"
    assert captured["kwargs"]["connect_args"] == {
        "options": "-c search_path=t_acme,shared,public"
    }
```

- [ ] **Step 2: Прогнать — FAIL. Step 3: Реализация `tenancy/db.py`**

```python
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
```

- [ ] **Step 4: Прогнать — зелёный; Commit**

```bash
cd /root/projects/meet/worker && ../.venv/bin/python -m pytest tests/test_tenancy_db.py -v
cd /root/projects/meet && git add tenancy/db.py worker/tests/test_tenancy_db.py
git commit -m "feat(tenancy): sync connection factory with per-connection search_path"
```

---

### Task 4: `tenancy/paths.py` + `tenancy/registry.py`

**Files:**
- Create: `tenancy/paths.py`, `tenancy/registry.py`
- Test: `worker/tests/test_tenancy_paths.py`

- [ ] **Step 1: Failing-тест**

```python
"""Tests for tenancy.paths and tenancy.registry."""
from pathlib import Path

import tenancy.registry as treg
from tenancy.paths import (
    tenant_audio_amocrm_dir,
    tenant_audio_sessions_dir,
    tenant_results_dir,
)


def test_path_layouts():
    assert tenant_audio_sessions_dir("./data/audio", "acme", "sid-1") == Path(
        "./data/audio/acme/sessions/sid-1"
    )
    assert tenant_audio_amocrm_dir("./data/audio", "acme", 123) == Path(
        "./data/audio/acme/amocrm/123"
    )
    assert tenant_results_dir("./data/results", "acme", "sid-1") == Path(
        "./data/results/acme/sid-1"
    )


def test_iter_active_tenants(monkeypatch):
    rows = [("realestate", "t_realestate"), ("acme", "t_acme")]

    class _Cur:
        def execute(self, sql, params=None):
            self._sql = sql
        def fetchall(self):
            return rows
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(treg, "shared_connect", lambda: _Conn())
    tenants = treg.iter_active_tenants()
    assert [t["slug"] for t in tenants] == ["realestate", "acme"]
    assert tenants[0]["schema_name"] == "t_realestate"
    # Phase 1: AmoCRM integration is hardcoded to realestate
    amo = treg.iter_amocrm_tenants()
    assert [t["slug"] for t in amo] == ["realestate"]
```

- [ ] **Step 2: FAIL → Step 3: Реализация**

`tenancy/paths.py`:

```python
"""Tenant-aware storage layout: <root>/<slug>/...

Roots stay as the callers' module globals (worker tests monkeypatch
``AUDIO_PATH``/``RESULTS_PATH``), so every helper takes the root explicitly.
"""
from __future__ import annotations

from pathlib import Path


def tenant_audio_sessions_dir(root: str | Path, slug: str, session_id) -> Path:
    return Path(root) / slug / "sessions" / str(session_id)


def tenant_audio_amocrm_dir(root: str | Path, slug: str, note_id) -> Path:
    return Path(root) / slug / "amocrm" / str(note_id)


def tenant_results_dir(root: str | Path, slug: str, session_id) -> Path:
    return Path(root) / slug / str(session_id)
```

`tenancy/registry.py`:

```python
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
                "SELECT slug, schema_name FROM shared.tenants "
                "WHERE status = 'active' ORDER BY slug"
            )
            return [
                {"slug": r[0], "schema_name": r[1]} for r in cur.fetchall()
            ]
    finally:
        conn.close()


def iter_amocrm_tenants() -> list[dict]:
    return [
        t for t in iter_active_tenants() if t["slug"] in AMOCRM_TENANT_SLUGS
    ]
```

- [ ] **Step 4: Прогнать — зелёный; Commit**

```bash
cd /root/projects/meet/worker && ../.venv/bin/python -m pytest tests/test_tenancy_paths.py -v
cd /root/projects/meet && git add tenancy/paths.py tenancy/registry.py worker/tests/test_tenancy_paths.py
git commit -m "feat(tenancy): storage path layout and tenant registry iteration"
```

---

### Task 5: Tenant-трек Alembic — env.py + ревизии 011, 012

**Files:**
- Modify: `backend/alembic/env.py` (полная замена)
- Create: `backend/alembic/versions/011_amocrm_calls_drift.py`
- Create: `backend/alembic/versions/012_users.py`

Ревизии 001–010 остаются нетронутыми. env.py учится принимать `tenant_schema` (через `config.attributes` из раннера или `-x tenant_schema=...` из CLI), выставлять `search_path` и `version_table_schema`.

- [ ] **Step 1: Заменить `backend/alembic/env.py` целиком**

```python
import asyncio
import os
import re
import sys
from logging.config import fileConfig
from pathlib import Path

# Ensure the app package is importable when alembic runs from /app directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Override sqlalchemy.url from environment
db_url = os.getenv("DATABASE_URL", "")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

# Tenant schema: programmatic (app.migrate sets config.attributes) or CLI
# (alembic -x tenant_schema=t_foo upgrade head). None = legacy single-tenant
# mode against public — kept only so old dev databases can still be inspected;
# production runs go through app.migrate which always passes a schema.
_TENANT_SCHEMA = config.attributes.get(
    "tenant_schema"
) or context.get_x_argument(as_dictionary=True).get("tenant_schema")
if _TENANT_SCHEMA and not re.match(r"^t_[a-z][a-z0-9_]{1,30}$", _TENANT_SCHEMA):
    raise ValueError(f"invalid tenant_schema {_TENANT_SCHEMA!r}")


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=_TENANT_SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    if _TENANT_SCHEMA:
        # Unqualified DDL in revisions 001-012 lands in the first schema of
        # the search_path — i.e. the tenant schema.
        connection.exec_driver_sql(
            f"SET search_path TO {_TENANT_SCHEMA}, shared, public"
        )
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=_TENANT_SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 2: Ревизия 011 — `backend/alembic/versions/011_amocrm_calls_drift.py`**

```python
"""Capture schema drift: amocrm_calls.responsible_user_id was added on prod
by hand, outside migrations. IF NOT EXISTS makes this idempotent both for the
realestate schema (column already there) and for fresh tenant schemas.

Revision ID: 011
Revises: 010
"""
from typing import Sequence, Union

from alembic import op

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE amocrm_calls ADD COLUMN IF NOT EXISTS responsible_user_id BIGINT"
    )


def downgrade() -> None:
    # The column predates the migration history on prod — never drop it.
    pass
```

- [ ] **Step 3: Ревизия 012 — `backend/alembic/versions/012_users.py`**

```python
"""Tenant dashboard users (email+password accounts with roles).

Login endpoints arrive in Phase-1 Plan 2; the table and the seed CLI need the
schema now. Password hashes are argon2 (argon2-cffi), NOT the bcrypt/passlib
stack used by brokers.

Revision ID: 012
Revises: 011
"""
from typing import Sequence, Union

from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role VARCHAR(10) NOT NULL CHECK (role IN ('admin', 'viewer')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_login TIMESTAMPTZ
        )"""
    )
    op.execute("CREATE UNIQUE INDEX uq_users_email_lower ON users (LOWER(email))")


def downgrade() -> None:
    op.execute("DROP TABLE users")
```

- [ ] **Step 4: Smoke-проверка синтаксиса (без БД)**

Run: `cd /root/projects/meet/backend && ../.venv/bin/python -c "import ast; [ast.parse(open(f).read()) for f in ['alembic/env.py','alembic/versions/011_amocrm_calls_drift.py','alembic/versions/012_users.py']]; print('ok')"`
Expected: `ok`. Реальный прогон миграций — в Task 14 (интеграционная верификация).

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet && git add backend/alembic
git commit -m "feat(alembic): tenant-track env with version_table_schema; revisions 011 drift + 012 users"
```

---

### Task 6: Shared-трек Alembic — S001 реестр, S002 cutover

**Files:**
- Create: `backend/alembic_shared.ini`
- Create: `backend/alembic_shared/env.py`, `backend/alembic_shared/script.py.mako` (копия из `backend/alembic/script.py.mako`)
- Create: `backend/alembic_shared/versions/s001_shared_registry.py`
- Create: `backend/alembic_shared/versions/s002_realestate_cutover.py`

- [ ] **Step 1: `backend/alembic_shared.ini`**

```ini
[alembic]
script_location = alembic_shared
sqlalchemy.url = driver://user:pass@localhost/dbname

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 2: `backend/alembic_shared/env.py`** (sync-движок — проще и достаточно)

```python
import os
import sys
from logging.config import fileConfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import context
from sqlalchemy import create_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Shared track owns no ORM metadata — registry tables are raw-SQL revisions.
target_metadata = None


def _db_url() -> str:
    url = os.getenv("DATABASE_URL_SYNC", "") or os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def run_migrations_online() -> None:
    engine = create_engine(_db_url(), future=True)
    with engine.connect() as connection:
        # The version table lives in `shared`, which S001 itself creates —
        # bootstrap the schema before alembic touches the version table.
        connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS shared")
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema="shared",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline mode is not supported for the shared track")
run_migrations_online()
```

- [ ] **Step 3: `backend/alembic_shared/versions/s001_shared_registry.py`**

```python
"""Shared registry: tenants + platform_admins.

Revision ID: s001
Revises:
"""
from typing import Sequence, Union

from alembic import op

revision: str = "s001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS shared")
    op.execute(
        """CREATE TABLE shared.tenants (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug TEXT NOT NULL UNIQUE,
            schema_name TEXT NOT NULL UNIQUE,
            display_name TEXT,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended')),
            api_key_hash TEXT,
            api_key_required BOOLEAN NOT NULL DEFAULT TRUE,
            company_config_id TEXT,
            custom_domains TEXT[] NOT NULL DEFAULT '{}',
            dashboard_base_url TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        """CREATE TABLE shared.platform_admins (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_login TIMESTAMPTZ
        )"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE shared.platform_admins")
    op.execute("DROP TABLE shared.tenants")
```

- [ ] **Step 4: `backend/alembic_shared/versions/s002_realestate_cutover.py`**

```python
"""Cutover: move legacy public tables into t_realestate, transfer the alembic
stamp, register the realestate tenant.

Conditional: a fresh database (no public.sessions) skips the move entirely —
new installs get tenants only via the provisioning CLI.

Revision ID: s002
Revises: s001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "s002"
down_revision: Union[str, None] = "s001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Order matters only for readability; SET SCHEMA carries indexes, constraints
# and sequences with each table. The session_status ENUM type intentionally
# stays in public (search_path includes public last).
TABLES = (
    "sessions",
    "chunks",
    "extraction_templates",
    "complexes",
    "complex_extractions",
    "brokers",
    "amocrm_calls",
    "amocrm_deal_summaries",
)

LEGACY_DASHBOARD = "https://rogov.automate-it.fun"


def upgrade() -> None:
    conn = op.get_bind()
    if not conn.execute(sa.text("SELECT to_regclass('public.sessions')")).scalar():
        return  # fresh install — nothing to cut over

    op.execute("CREATE SCHEMA IF NOT EXISTS t_realestate")
    for t in TABLES:
        op.execute(f"ALTER TABLE public.{t} SET SCHEMA t_realestate")

    # Transfer the single-track stamp ('010') into the tenant schema so the
    # tenant track resumes from 010 and applies 011+ only.
    if conn.execute(
        sa.text("SELECT to_regclass('public.alembic_version')")
    ).scalar():
        op.execute(
            "CREATE TABLE t_realestate.alembic_version ("
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
        op.execute(
            "INSERT INTO t_realestate.alembic_version "
            "SELECT version_num FROM public.alembic_version"
        )
        op.execute("DROP TABLE public.alembic_version")

    # api_key_required=false: already-deployed recorder clients send no key
    # until the Phase-1 rollout (spec 6.4) finishes.
    op.execute(
        sa.text(
            "INSERT INTO shared.tenants (slug, schema_name, display_name, "
            "status, api_key_required, company_config_id, custom_domains, "
            "dashboard_base_url) VALUES ('realestate', 't_realestate', "
            "'Агентство недвижимости', 'active', FALSE, 'realestate', "
            "ARRAY['rogov.automate-it.fun'], :dash) "
            "ON CONFLICT (slug) DO NOTHING"
        ).bindparams(dash=LEGACY_DASHBOARD)
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not conn.execute(
        sa.text("SELECT to_regclass('t_realestate.sessions')")
    ).scalar():
        return
    if conn.execute(
        sa.text("SELECT to_regclass('t_realestate.alembic_version')")
    ).scalar():
        op.execute(
            "CREATE TABLE public.alembic_version ("
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
        op.execute(
            "INSERT INTO public.alembic_version "
            "SELECT version_num FROM t_realestate.alembic_version"
        )
        op.execute("DROP TABLE t_realestate.alembic_version")
    for t in reversed(TABLES):
        op.execute(f"ALTER TABLE t_realestate.{t} SET SCHEMA public")
    op.execute("DELETE FROM shared.tenants WHERE slug = 'realestate'")
    op.execute("DROP SCHEMA t_realestate")
```

**Внимание:** downgrade вернёт `public.alembic_version` со stamp'ом головы tenant-трека (может быть `012`, не `010`) — после даунгрейда надо вручную `UPDATE public.alembic_version SET version_num='010'`, если откатываемся к до-тенантному коду. Зафиксировано в ранбуке (Task 15).

- [ ] **Step 5: Syntax smoke + Commit**

```bash
cd /root/projects/meet/backend && ../.venv/bin/python -c "import ast; [ast.parse(open(f).read()) for f in ['alembic_shared/env.py','alembic_shared/versions/s001_shared_registry.py','alembic_shared/versions/s002_realestate_cutover.py']]; print('ok')"
cd /root/projects/meet && git add backend/alembic_shared.ini backend/alembic_shared
git commit -m "feat(alembic): shared track — registry S001, realestate cutover S002"
```

---

### Task 7: Раннер миграций `app/migrate.py` + замена стартового хука

**Files:**
- Create: `backend/app/migrate.py`
- Modify: `backend/app/main.py:77-88` (lifespan)

- [ ] **Step 1: `backend/app/migrate.py`**

```python
"""Two-track migration runner.

Replaces the bare ``alembic upgrade head`` startup hook: the shared track
(registry + cutover) runs first, then the tenant track is applied to every
active tenant schema. Idempotent — safe to run on every backend start.

Usage: python -m app.migrate   (cwd-independent; paths derived from __file__)
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR.parent))  # repo root → import tenancy

from tenancy.db import shared_connect  # noqa: E402
from tenancy.identifiers import validate_schema_name  # noqa: E402


def _config(ini_name: str, script_dir: str) -> Config:
    cfg = Config(str(BACKEND_DIR / ini_name))
    cfg.set_main_option("script_location", str(BACKEND_DIR / script_dir))
    return cfg


def run_shared() -> None:
    command.upgrade(_config("alembic_shared.ini", "alembic_shared"), "head")


def run_tenant(schema_name: str) -> None:
    validate_schema_name(schema_name)
    cfg = _config("alembic.ini", "alembic")
    cfg.attributes["tenant_schema"] = schema_name
    command.upgrade(cfg, "head")


def _active_schemas() -> list[str]:
    conn = shared_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT schema_name FROM shared.tenants "
                "WHERE status = 'active' ORDER BY slug"
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def main() -> None:
    run_shared()
    for schema in _active_schemas():
        print(f"[migrate] tenant track → {schema}", flush=True)
        run_tenant(schema)
    print("[migrate] done", flush=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Заменить lifespan в `backend/app/main.py`**

Old (строки 76-88):

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run alembic migrations on startup
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        capture_output=True, text=True,
    )
```

New:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run the two-track migration runner (shared registry first, then the
    # tenant track per active schema). Fresh process: env.py's asyncio.run
    # would clash with the already-running loop here, hence subprocess.
    result = subprocess.run(
        [sys.executable, "-m", "app.migrate"],
        capture_output=True, text=True,
    )
```

И добавить `import sys` к импортам main.py (после `import subprocess`). Остальное тело lifespan (проверка returncode, RuntimeError) не меняется.

- [ ] **Step 3: Smoke: `cd /root/projects/meet/backend && ../.venv/bin/python -c "import app.migrate; print('ok')"` → `ok`. Step 4: Commit**

```bash
cd /root/projects/meet && git add backend/app/migrate.py backend/app/main.py
git commit -m "feat(migrate): two-track runner replaces bare alembic startup hook"
```

---

### Task 8: Backend — резолв тенанта (registry, middleware, search_path-листенер)

**Files:**
- Modify: `backend/app/config.py` (две настройки)
- Create: `backend/app/tenancy_http.py`
- Modify: `backend/app/database.py` (листенер `begin`)
- Modify: `backend/app/main.py` (подключение middleware + sys.path для `tenancy`)
- Create: `backend/tests/__init__.py` (пустой), `backend/tests/conftest.py`
- Test: `backend/tests/test_tenant_middleware.py`

- [ ] **Step 1: config.py — добавить в класс Settings**

```python
    # Multi-tenancy
    BASE_DOMAIN: str = "silentqa.com"
    # Fallback tenant slug for hosts that match neither custom_domains nor
    # *.BASE_DOMAIN (local dev: localhost/IP). Empty → platform contour.
    DEFAULT_TENANT: str = ""
```

- [ ] **Step 2: `backend/tests/conftest.py`**

```python
"""Backend test fixtures: import paths for app/ and the tenancy package."""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))          # import app.*
sys.path.insert(0, str(BACKEND.parent))   # import tenancy.*
```

- [ ] **Step 3: Failing-тест `backend/tests/test_tenant_middleware.py`**

```python
"""Tenant resolution middleware: host → tenant schema contextvar."""
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.tenancy_http import TenantResolutionMiddleware
from tenancy.context import get_tenant_schema


class StubRegistry:
    """In-memory stand-in for the shared.tenants lookup."""

    def __init__(self):
        self.rows = [
            {
                "slug": "realestate",
                "schema_name": "t_realestate",
                "status": "active",
                "custom_domains": ["rogov.automate-it.fun"],
            },
            {
                "slug": "frozen",
                "schema_name": "t_frozen",
                "status": "suspended",
                "custom_domains": [],
            },
        ]

    async def all_tenants(self):
        return self.rows


def _app(default_tenant=""):
    async def whoami(request):
        return JSONResponse({"schema": get_tenant_schema()})

    app = Starlette(routes=[Route("/whoami", whoami)])
    return TestClient(
        app=TenantResolutionMiddleware(
            app,
            registry=StubRegistry(),
            base_domain="silentqa.com",
            default_tenant=default_tenant,
        ),
        base_url="http://testserver",
    )


def test_subdomain_resolves_tenant():
    c = _app()
    r = c.get("/whoami", headers={"host": "realestate.silentqa.com"})
    assert r.json() == {"schema": "t_realestate"}


def test_custom_domain_resolves_tenant():
    c = _app()
    r = c.get("/whoami", headers={"host": "rogov.automate-it.fun"})
    assert r.json() == {"schema": "t_realestate"}


def test_unknown_subdomain_404():
    c = _app()
    r = c.get("/whoami", headers={"host": "ghost.silentqa.com"})
    assert r.status_code == 404


def test_suspended_tenant_404():
    c = _app()
    r = c.get("/whoami", headers={"host": "frozen.silentqa.com"})
    assert r.status_code == 404


def test_apex_and_admin_are_platform():
    c = _app()
    for host in ("silentqa.com", "admin.silentqa.com"):
        r = c.get("/whoami", headers={"host": host})
        assert r.json() == {"schema": None}, host


def test_foreign_host_uses_default_tenant():
    c = _app(default_tenant="realestate")
    r = c.get("/whoami", headers={"host": "localhost:8002"})
    assert r.json() == {"schema": "t_realestate"}


def test_foreign_host_without_default_is_platform():
    c = _app()
    r = c.get("/whoami", headers={"host": "localhost:8002"})
    assert r.json() == {"schema": None}


def test_context_reset_after_request():
    c = _app()
    c.get("/whoami", headers={"host": "realestate.silentqa.com"})
    assert get_tenant_schema() is None
```

- [ ] **Step 4: FAIL → Step 5: Реализация `backend/app/tenancy_http.py`**

```python
"""HTTP-plane tenant resolution: Host header → shared.tenants → contextvar.

Pure ASGI middleware (not BaseHTTPMiddleware): cheaper and contextvar
semantics are explicit. Resolution order (spec 3.2):
  1. exact match against custom_domains  → tenant  (legacy prod domain)
  2. <slug>.BASE_DOMAIN                  → tenant by slug
  3. apex / admin.BASE_DOMAIN            → platform (no tenant)
  4. anything else → DEFAULT_TENANT if set, else platform
Unknown slug / suspended tenant → 404.
"""
from __future__ import annotations

import time

from sqlalchemy import text
from starlette.responses import JSONResponse

from tenancy.context import reset_tenant_schema, set_tenant_schema


class TenantRegistry:
    """TTL-cached snapshot of shared.tenants (async engine)."""

    def __init__(self, ttl_seconds: float = 30.0):
        self._ttl = ttl_seconds
        self._rows: list[dict] = []
        self._loaded_at = 0.0

    async def all_tenants(self) -> list[dict]:
        if time.monotonic() - self._loaded_at > self._ttl:
            from app.database import async_session

            async with async_session() as db:
                res = await db.execute(
                    text(
                        "SELECT slug, schema_name, status, custom_domains "
                        "FROM shared.tenants"
                    )
                )
                self._rows = [
                    {
                        "slug": r.slug,
                        "schema_name": r.schema_name,
                        "status": r.status,
                        "custom_domains": list(r.custom_domains or []),
                    }
                    for r in res
                ]
            self._loaded_at = time.monotonic()
        return self._rows


class TenantResolutionMiddleware:
    def __init__(self, app, registry, base_domain: str, default_tenant: str = ""):
        self.app = app
        self.registry = registry
        self.base_domain = base_domain.lower()
        self.default_tenant = default_tenant

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        host = ""
        for name, value in scope.get("headers", []):
            if name == b"host":
                host = value.decode("latin-1").split(":")[0].lower()
                break

        kind, row = await self._resolve(host)
        if kind == "notfound":
            resp = JSONResponse({"detail": "Unknown tenant"}, status_code=404)
            return await resp(scope, receive, send)

        token = set_tenant_schema(row["schema_name"] if row else None)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_tenant_schema(token)

    async def _resolve(self, host: str):
        rows = await self.registry.all_tenants()

        for r in rows:
            if host in r["custom_domains"]:
                return self._gate(r)

        if host == self.base_domain or host == f"admin.{self.base_domain}":
            return "platform", None

        suffix = f".{self.base_domain}"
        if host.endswith(suffix):
            slug = host[: -len(suffix)]
            if "." in slug:  # only single-label subdomains are tenants
                return "notfound", None
            for r in rows:
                if r["slug"] == slug:
                    return self._gate(r)
            return "notfound", None

        if self.default_tenant:
            for r in rows:
                if r["slug"] == self.default_tenant:
                    return self._gate(r)
            return "notfound", None
        return "platform", None

    @staticmethod
    def _gate(row):
        if row["status"] != "active":
            return "notfound", None
        return "tenant", row
```

- [ ] **Step 6: Листенер в `backend/app/database.py`** — добавить после строки 8 (`async_session = ...`):

```python
import re

from sqlalchemy import event

from tenancy.context import get_tenant_schema

_SCHEMA_RE = re.compile(r"^t_[a-z][a-z0-9_]{1,30}$")


@event.listens_for(engine.sync_engine, "begin")
def _set_tenant_search_path(conn):
    """SET LOCAL per transaction — the pooled connection comes back clean."""
    schema = get_tenant_schema()
    if schema:
        if not _SCHEMA_RE.match(schema):
            raise ValueError(f"invalid tenant schema {schema!r}")
        conn.exec_driver_sql(
            f"SET LOCAL search_path TO {schema}, shared, public"
        )
```

- [ ] **Step 7: Подключение в `backend/app/main.py`**

В начало файла (до импортов `app.*`), после stdlib-импортов:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root → tenancy
```

После `app.add_middleware(BasicAuthMiddleware)` добавить (TenantResolution добавляется последним = внешний слой, резолвит до auth):

```python
# Tenant resolution — outermost: unknown hosts 404 before anything else runs
from app.tenancy_http import TenantRegistry, TenantResolutionMiddleware

app.add_middleware(
    TenantResolutionMiddleware,
    registry=TenantRegistry(),
    base_domain=settings.BASE_DOMAIN,
    default_tenant=settings.DEFAULT_TENANT,
)
```

- [ ] **Step 8: Прогнать backend-тесты — зелёные**

Run: `cd /root/projects/meet/backend && ../.venv/bin/python -m pytest tests -v`
Expected: 8 passed (httpx уже в venv как зависимость fastapi-тулчейна; если нет — `../.venv/bin/pip install httpx`)

- [ ] **Step 9: Worker-тесты не сломаны: `cd /root/projects/meet/worker && ../.venv/bin/python -m pytest -q`. Step 10: Commit**

```bash
cd /root/projects/meet && git add backend/app/config.py backend/app/tenancy_http.py backend/app/database.py backend/app/main.py backend/tests
git commit -m "feat(backend): tenant resolution middleware + per-transaction search_path"
```

---

### Task 9: Перевод raw-соединений воркера на фабрику

**Files:**
- Modify: `worker/tasks/pipeline.py`, `worker/tasks/amocrm_poll.py`, `worker/tasks/amocrm_reconcile.py`, `worker/tasks/session_watchdog.py`, `worker/tasks/deal_summary.py`, `worker/tasks/prior_context.py`
- Test: `worker/tests/test_no_raw_connects.py` (новый guard-тест)

**Рецепт (одинаковый для всех файлов):**
1. В импорты модуля добавить `from tenancy.db import get_sync_db_url, tenant_connect` (а где есть ad-hoc `create_engine` — ещё `tenant_engine, get_sync_dialect_url`).
2. Тело локального хелпера `_get_sync_db_url` заменить алиасом (def удалить): `_get_sync_db_url = get_sync_db_url`. Это сохраняет импорт `from tasks.amocrm_poll import _get_sync_db_url` в `amocrm_reconcile.py:43` и все локальные guard'ы `db_url = _get_sync_db_url(); if not db_url: ...` (тесты без БД полагаются на пустой URL → early-return).
3. Каждый `psycopg2.connect(db_url)` → `tenant_connect()`. Сигнатуры/транзакционные паттерны (`with conn:`, try/finally close) НЕ менять.
4. Ad-hoc `create_engine`-сайты: блок `db_url = os.environ.get("DATABASE_URL_SYNC") or ...` + `eng = create_engine(db_url, future=True)` → guard `if not get_sync_dialect_url(): return <прежний дефолт>` + `eng = tenant_engine()`. `eng.dispose()` в finally остаётся.

**Полная таблица сайтов** (из инвентаризации; строки до правок):

| Файл | Функция | Строки connect | Замена |
|---|---|---|---|
| pipeline.py | `update_session_status` (146) | 164 | `tenant_connect()` |
| pipeline.py | `_get_session_metadata` (190) | 196 | `tenant_connect()` |
| pipeline.py | `_load_template_kind` (209) | 217-221 | `tenant_engine()` |
| pipeline.py | `_load_evaluation_template_prompt` (236) | 238-242 | `tenant_engine()` |
| pipeline.py | `_load_evaluation_template_criteria` (254) | 256-260 | `tenant_engine()` |
| pipeline.py | `_get_session_created_at` (274) | 280 | `tenant_connect()` |
| pipeline.py | `_save_speaker_roles` (390) | 396 | `tenant_connect()` |
| pipeline.py | `_update_session_metadata` (412) | 418 | `tenant_connect()` |
| pipeline.py | extraction-ветка `_run_pipeline_inner` (620-634) | 626-627 | `tenant_engine()` |
| amocrm_poll.py | `_get_last_poll_timestamp` (55) | 71 | `tenant_connect()` |
| amocrm_poll.py | `_insert_call` (87) | 102 | `tenant_connect()` |
| amocrm_poll.py | `_update_call_status` (127) | 143 | `tenant_connect()` |
| amocrm_poll.py | `reset_call_for_reprocess` (152) | 158 | `tenant_connect()` |
| amocrm_poll.py | `_get_retryable_calls` (177) | 189 | `tenant_connect()` |
| amocrm_poll.py | `_create_session` (215) | 224 | `tenant_connect()` |
| amocrm_poll.py | `process_amocrm_call` (339) | 349 | `tenant_connect()` |
| amocrm_reconcile.py | `_existing_note_ids` (63) | 71 | `tenant_connect()` |
| amocrm_reconcile.py | `_stuck_counts` (83) | 89 | `tenant_connect()` |
| session_watchdog.py | `sweep_stuck_sessions` (36) | 48 | `tenant_connect()` |
| deal_summary.py | `get_existing_summary_note_id` (136) | 141 | `tenant_connect()` |
| deal_summary.py | `upsert_summary_record` (154) | 159 | `tenant_connect()` |
| prior_context.py | `load_past_sessions_for_lead` (214) | 224 | `tenant_connect()` |

**НЕ трогать:** `amocrm_sync.py:41 _read_token_from_portal_db` (портальная таблица `portal.amocrm_tokens`, schema-qualified — работает при любом search_path; глобальный ресурс, не тенантский).

Пример полной трансформации (`update_session_status`, pipeline.py):

```python
# Было:
    db_url = _get_sync_db_url()
    if not db_url:
        logger.warning("DATABASE_URL not set, skipping status update")
        return
    ...
    try:
        conn = psycopg2.connect(db_url)

# Стало (guard остаётся, db_url больше не передаётся):
    if not get_sync_db_url():
        logger.warning("DATABASE_URL not set, skipping status update")
        return
    ...
    try:
        conn = tenant_connect()
```

- [ ] **Step 1: Guard-тест `worker/tests/test_no_raw_connects.py`** (написать ДО правок — он зафиксирует завершённость замены)

```python
"""No application code may call psycopg2.connect directly — only tenancy.db.

The tenant search_path is injected per-connection by the factory; a stray
direct connect silently reads/writes the wrong schema after the cutover.
"""
import re
from pathlib import Path

WORKER_TASKS = Path(__file__).resolve().parents[1] / "tasks"
BACKEND_APP = Path(__file__).resolve().parents[2] / "backend" / "app"

# Exception: amocrm_sync.py's portal-token reader keeps its own connection —
# it queries the schema-qualified portal.amocrm_tokens table (global resource).
CONNECT_RE = re.compile(r"psycopg2\.connect\(")


def _offending(root: Path) -> list[str]:
    hits = []
    for f in root.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if CONNECT_RE.search(line):
                if f.name == "amocrm_sync.py" and "portal" in text:
                    continue
                hits.append(f"{f.name}:{i}: {line.strip()}")
    return hits


def test_no_direct_psycopg2_connect_in_worker_tasks():
    assert _offending(WORKER_TASKS) == []


def test_no_direct_psycopg2_connect_in_backend_app():
    assert _offending(BACKEND_APP) == []
```

- [ ] **Step 2: Прогнать — FAIL (десятки находок). Step 3: Применить рецепт ко всем 22 сайтам таблицы.**

- [ ] **Step 4: Прогнать guard-тест — `test_no_direct_psycopg2_connect_in_worker_tasks` зелёный (backend-тест ещё красный — Task 10). Прогнать весь worker-suite:**

Run: `cd /root/projects/meet/worker && ../.venv/bin/python -m pytest -q --deselect tests/test_no_raw_connects.py::test_no_direct_psycopg2_connect_in_backend_app`
Expected: всё зелёное. Существующие тесты идут без `DATABASE_URL_SYNC` → guard'ы early-return до `tenant_connect()`, контекст не нужен. Если какой-то тест ставит URL и доходит до connect — добавить в него фикстуру:

```python
@pytest.fixture(autouse=True)
def _tenant_ctx():
    from tenancy.context import set_tenant_schema, reset_tenant_schema
    token = set_tenant_schema("t_realestate")
    yield
    reset_tenant_schema(token)
```

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet && git add worker/tasks worker/tests/test_no_raw_connects.py
git commit -m "refactor(worker): route all raw DB connections through tenancy.db factory"
```

---

### Task 10: Backend raw-сайты + операционные скрипты

**Files:**
- Modify: `backend/app/routes/amocrm.py:109-123`, `backend/app/routes/complexes.py:28-46`
- Modify: `worker/scripts/reassess_quality.py`, `worker/scripts/compare_reassess.py`, `worker/scripts/sync_brokers.py`, `worker/scripts/backfill_amocrm_push.py`

- [ ] **Step 1: `routes/amocrm.py` force-ветка (109-123)** — заменить inline psycopg2 на фабрику:

```python
# Было:
                    from tasks.amocrm_poll import _get_sync_db_url
                    import psycopg2
                    conn = psycopg2.connect(_get_sync_db_url())
# Стало:
                    from tenancy.db import tenant_connect
                    conn = tenant_connect()
```

(Тенант-контекст выставлен middleware'ом запроса; `tenancy` импортируется — main.py уже добавил repo root в sys.path.)

- [ ] **Step 2: `routes/complexes.py` `_recompute_aggregate_sync` (32-46)**:

```python
# Было:
    eng = create_engine(_sync_db_url(), future=True)
# Стало:
    from tenancy.db import tenant_engine
    eng = tenant_engine()
```

Удалить хелпер `_sync_db_url` (28-29), если на него больше нет ссылок в файле (проверить grep'ом).

- [ ] **Step 3: Скрипты — обязательный `--tenant <slug>`.** В каждый из четырёх скриптов добавить в начало `main()` (или перед первым обращением к БД):

```python
import argparse
from tenancy.context import set_tenant_schema
from tenancy.identifiers import schema_for_slug

parser = argparse.ArgumentParser()
parser.add_argument("--tenant", required=True, help="tenant slug, e.g. realestate")
# ... существующие аргументы скрипта переносятся в этот же parser ...
args = parser.parse_args()
set_tenant_schema(schema_for_slug(args.tenant))
RESULTS_PATH = str(Path(RESULTS_ROOT) / args.tenant)  # для скриптов с путями
```

Конкретно:
- `reassess_quality.py:50` и `compare_reassess.py:31`: `RESULTS_PATH = os.getenv("RESULTS_STORAGE_PATH", "/data/realestate/results")` → корень `RESULTS_ROOT = os.getenv("RESULTS_STORAGE_PATH", "./data/results")`, путь = `RESULTS_ROOT/<slug>` (см. сниппет выше).
- `sync_brokers.py`, `backfill_amocrm_push.py`: только `--tenant` + контекст (психопг-сайты уже ходят через свои `_get_sync_db_url`-алиасы → заменить connect на `tenant_connect()` по рецепту Task 9; `backfill_amocrm_push.py:39` импортирует `RESULTS_PATH` из pipeline — заменить на построение через `tenant_results_dir`).
- Скрипты без `--tenant` должны падать сразу (argparse `required=True` это гарантирует).

- [ ] **Step 4: Guard-тест целиком зелёный + worker-suite:**

Run: `cd /root/projects/meet/worker && ../.venv/bin/python -m pytest -q`
Expected: всё зелёное, включая оба теста `test_no_raw_connects.py`.

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet && git add backend/app/routes worker/scripts
git commit -m "refactor: backend raw DB sites and ops scripts go through tenancy factory (--tenant)"
```

---

### Task 11: Проброс тенанта в Celery-таски

**Files:**
- Modify: `worker/tasks/pipeline.py` (сигнатуры 763-764, 814-815), `worker/tasks/amocrm_poll.py` (339-340, 312-314, 330-333, beat-тело 317+), `worker/tasks/amocrm_reconcile.py` (127+), `worker/tasks/session_watchdog.py` (36+, 105-110)
- Modify: `backend/app/routes/sessions.py` (3 enqueue-сайта: 231-235, 330-334, 376-380), `backend/app/routes/amocrm.py` (`_enqueue_process_call`, 47-58)
- Test: `worker/tests/test_tenant_propagation.py`

- [ ] **Step 1: Failing-тест**

```python
"""Tenant propagation: enqueue tasks require tenant_schema; beat iterates."""
import pytest

import tasks.pipeline as pl
from tenancy.context import get_tenant_schema


def test_process_session_requires_tenant_schema():
    with pytest.raises(ValueError, match="tenant_schema"):
        pl.process_session.run("sid-1", None)  # tenant_schema omitted


def test_process_session_sets_and_resets_context(monkeypatch):
    seen = {}

    def _fake_body(task, session_id, config):
        seen["schema"] = get_tenant_schema()
        return {"ok": True}

    monkeypatch.setattr(pl, "_process_session_body", _fake_body)
    pl.process_session.run("sid-1", None, tenant_schema="t_acme")
    assert seen["schema"] == "t_acme"
    assert get_tenant_schema() is None  # reset after task


def test_process_amocrm_call_requires_tenant_schema():
    import tasks.amocrm_poll as ap

    with pytest.raises(ValueError, match="tenant_schema"):
        ap.process_amocrm_call.run(1)
```

- [ ] **Step 2: FAIL → Step 3: Сигнатуры enqueue-тасок.** Паттерн (на примере `process_session`; тело таски выносится в `_process_session_body(task, session_id, config)` без изменений логики):

```python
@app.task(bind=True, queue="transcription", name="pipeline.process_session")
def process_session(self, session_id: str, config: dict | None = None,
                    tenant_schema: str | None = None):
    if not tenant_schema:
        raise ValueError("tenant_schema is required (fail fast: a task without "
                         "tenant context would read/write the wrong schema)")
    token = set_tenant_schema(tenant_schema)
    try:
        return _process_session_body(self, session_id, config)
    finally:
        reset_tenant_schema(token)
```

То же для `process_session_from_file(self, session_id, audio_path, config=None, tenant_schema=None)` и `amocrm_poll.process_amocrm_call(self, call_id, tenant_schema=None)`. Импорты: `from tenancy.context import set_tenant_schema, reset_tenant_schema`.

- [ ] **Step 4: Beat-таски — итерация тенантов.** Существующее тело каждой beat-таски выносится в `_<name>_for_current_tenant()`, новое тело:

```python
@app.task(name="amocrm_poll.poll_amocrm_calls")
def poll_amocrm_calls():
    from tenancy.registry import iter_amocrm_tenants
    for t in iter_amocrm_tenants():
        token = set_tenant_schema(t["schema_name"])
        try:
            _poll_for_current_tenant()
        except Exception:
            logger.exception(f"poll failed for tenant {t['slug']}")
        finally:
            reset_tenant_schema(token)
```

- `session_watchdog.sweep_stuck_sessions` — то же, но `iter_active_tenants()` (все тенанты), и его enqueue-сайт (105-110) внутри per-tenant цикла дополняется: `app.send_task("pipeline.process_session", args=[sid], kwargs={"tenant_schema": get_tenant_schema()}, queue="transcription")`.
- `amocrm_reconcile.reconcile_amocrm_calls` — `iter_amocrm_tenants()`.
- Внутренние `.delay(call_id)` в `amocrm_poll.py:313` и `:333` → `.delay(call_id, tenant_schema=get_tenant_schema())` (контекст в этих точках уже выставлен beat-циклом).
- per-tenant исключение ловится и логируется — упавший тенант не прерывает остальных.

- [ ] **Step 5: Backend enqueue-сайты.** Все три блока в `sessions.py` (231-235, 330-334, 376-380):

```python
    from tenancy.context import require_tenant_schema
    tenant_schema = require_tenant_schema()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: celery_app.send_task(
            "pipeline.process_session",
            args=[str(session_id)],
            kwargs={"tenant_schema": tenant_schema},
            queue="transcription",
        ),
    )
```

(`require_tenant_schema()` вызывается ДО lambda — захват значения, не контекста: executor-поток contextvars не наследует.) Три блока байт-идентичны — при Edit включать в old_string уникальный контекст роута выше блока.

`amocrm.py _enqueue_process_call` (47-58): сигнатура `def _enqueue_process_call(call_id: int, tenant_schema: str) -> str`, в send_task добавить `kwargs={"tenant_schema": tenant_schema}`; оба вызова (124, 131) передают `require_tenant_schema()`.

- [ ] **Step 6: Прогнать: `cd /root/projects/meet/worker && ../.venv/bin/python -m pytest -q` — всё зелёное (существующие тесты, зовущие таски напрямую, обновить: передавать `tenant_schema="t_realestate"`).**

- [ ] **Step 7: Commit**

```bash
cd /root/projects/meet && git add worker/tasks backend/app/routes worker/tests/test_tenant_propagation.py
git commit -m "feat(celery): explicit tenant_schema on enqueue tasks; beat tasks iterate tenants"
```

---

### Task 12: Тенант-компонент в файловых путях + скрипт миграции хранилища

**Files:**
- Modify: `backend/app/routes/chunks.py:80-82,209-211`, `backend/app/routes/analysis.py:19-24,27-36`, `backend/app/routes/transcripts.py:12-17`, `backend/app/routes/sessions.py:189-197`
- Modify: `worker/tasks/pipeline.py` (`merge_chunks`:306, `save_results`:382, reprocess-чтение:583), `worker/tasks/extract.py:60-64,94-97`, `worker/tasks/prior_context.py:190-211`, `worker/tasks/amocrm_poll.py:415-434`
- Create: `scripts/migrate_storage_layout.sh`
- Test: `worker/tests/test_tenant_storage_paths.py`

Слой: `<root>/<slug>/sessions/<id>` (аудио), `<root>/<slug>/amocrm/<note_id>` (аудио AmoCRM), `<root>/<slug>/<id>` (результаты). Slug везде берётся `require_tenant_slug()` (контекст уже выставлен: в backend — middleware, в worker — входом таски).

- [ ] **Step 1: Failing-тест**

```python
"""Storage paths gain the tenant slug component."""
from pathlib import Path

import pytest

import tasks.pipeline as pl
from tenancy.context import reset_tenant_schema, set_tenant_schema


@pytest.fixture()
def acme_ctx():
    token = set_tenant_schema("t_acme")
    yield
    reset_tenant_schema(token)


def test_save_results_under_tenant_slug(tmp_path, monkeypatch, acme_ctx):
    monkeypatch.setattr(pl, "RESULTS_PATH", str(tmp_path))
    pl.save_results("sid-1", "quality", {"x": 1})
    assert (tmp_path / "acme" / "sid-1" / "quality.json").exists()


def test_merge_chunks_reads_tenant_dir(tmp_path, monkeypatch, acme_ctx):
    monkeypatch.setattr(pl, "AUDIO_PATH", str(tmp_path))
    sdir = tmp_path / "acme" / "sessions" / "sid-1"
    sdir.mkdir(parents=True)
    with pytest.raises(Exception, match="[Nn]o chunks"):
        pl.merge_chunks("sid-1")  # пустая директория → прежняя ошибка "no chunks"
```

- [ ] **Step 2: FAIL → Step 3: Правки.** Образцы:

`pipeline.py save_results` (382): `results_dir = Path(RESULTS_PATH) / session_id` → `results_dir = tenant_results_dir(RESULTS_PATH, require_tenant_slug(), session_id)`; `merge_chunks` (306): `session_dir = Path(AUDIO_PATH) / "sessions" / session_id` → `session_dir = tenant_audio_sessions_dir(AUDIO_PATH, require_tenant_slug(), session_id)`; reprocess-чтение (583): `transcript_path = Path(RESULTS_PATH) / session_id / "transcript.json"` → через `tenant_results_dir(...)`. Импорт в шапку: `from tenancy.context import require_tenant_slug` + `from tenancy.paths import tenant_audio_sessions_dir, tenant_results_dir`.

`amocrm_poll.py` (416): `call_dir = Path(AUDIO_PATH) / "amocrm" / str(amo_note_id)` → `call_dir = tenant_audio_amocrm_dir(AUDIO_PATH, require_tenant_slug(), amo_note_id)`.

`extract.py` и `prior_context.py`: те же замены вокруг своих module-level `RESULTS_PATH` (24 и 20).

Backend (slug из request-контекста): `chunks.py:80` → `session_dir = tenant_audio_sessions_dir(settings.AUDIO_STORAGE_PATH, require_tenant_slug(), session_id)` (и 209 так же); `analysis.py:20` и `transcripts.py:13` → `result_path = tenant_results_dir(settings.RESULTS_STORAGE_PATH, require_tenant_slug(), session_id) / filename`; `analysis.py:29` → `base = tenant_audio_sessions_dir(settings.AUDIO_STORAGE_PATH, require_tenant_slug(), session_id)`.

`sessions.py delete_session` (189-197) — новые кандидаты + легаси для обратной совместимости:

```python
    sid = str(session_id)
    slug = require_tenant_slug()
    candidates = [
        tenant_audio_sessions_dir(settings.AUDIO_STORAGE_PATH, slug, sid),
        tenant_results_dir(settings.RESULTS_STORAGE_PATH, slug, sid),
        # legacy pre-tenant layout (files written before the cutover)
        Path(settings.AUDIO_STORAGE_PATH) / "sessions" / sid,
        Path(settings.RESULTS_STORAGE_PATH) / sid,
    ]
    for candidate in candidates:
        if candidate.exists():
            ...
```

Примечание: `chunks.file_path` в БД хранит старые абсолютные пути — их никто не читает для обработки (merge_chunks глобит директорию), оставляем как есть.

- [ ] **Step 4: `scripts/migrate_storage_layout.sh`**

```bash
#!/usr/bin/env bash
# One-shot storage cutover: nest existing audio/results under realestate/.
# Run with the worker STOPPED, right after the DB cutover migration.
set -euo pipefail
AUDIO="${AUDIO_STORAGE_PATH:-./data/audio}"
RESULTS="${RESULTS_STORAGE_PATH:-./data/results}"
SLUG="realestate"

if [ -d "$AUDIO/$SLUG" ]; then
    echo "$AUDIO/$SLUG already exists — storage already migrated?" >&2
    exit 1
fi
mkdir -p "$AUDIO/$SLUG" "$RESULTS/$SLUG"
for d in sessions amocrm; do
    [ -d "$AUDIO/$d" ] && mv "$AUDIO/$d" "$AUDIO/$SLUG/$d"
done
shopt -s nullglob
for p in "$RESULTS"/*; do
    base="$(basename "$p")"
    # webhooks.json stays at the root until Plan 2 removes the route
    [ "$base" = "$SLUG" ] && continue
    [ "$base" = "webhooks.json" ] && continue
    mv "$p" "$RESULTS/$SLUG/$base"
done
echo "storage migrated under $AUDIO/$SLUG and $RESULTS/$SLUG"
```

`chmod +x scripts/migrate_storage_layout.sh`.

- [ ] **Step 5: Полный прогон worker-suite + backend-suite.** Существующие тесты, упавшие из-за нового слоя путей, обновить: добавить фикстуру `acme_ctx`/`t_realestate`-контекст и ожидаемые пути с `<slug>/`. (Тесты, монкипатчащие `ap.AUDIO_PATH`, продолжают работать — root остался параметром.)

- [ ] **Step 6: Commit**

```bash
cd /root/projects/meet && git add backend/app/routes worker/tasks scripts/migrate_storage_layout.sh worker/tests/test_tenant_storage_paths.py
git commit -m "feat(storage): tenant slug in audio/results layout + cutover script"
```

---

### Task 13: `lead_lock` — тенантный неймспейс ключа

**Files:**
- Modify: `worker/tasks/lead_lock.py:21-22` и построение ключа
- Modify: `worker/tests/test_lead_lock.py`

- [ ] **Step 1: Обновить тесты** — в `test_acquires_on_first_try` ожидать ключ `lock:acme:42`; добавить фикстуру контекста и тест изоляции:

```python
@pytest.fixture(autouse=True)
def _tenant_ctx():
    from tenancy.context import set_tenant_schema, reset_tenant_schema
    token = set_tenant_schema("t_acme")
    yield
    reset_tenant_schema(token)


def test_lock_key_namespaced_by_tenant():
    from tenancy.context import set_tenant_schema, reset_tenant_schema
    client = MagicMock()
    client.set.return_value = True
    with lead_lock(42, client=client, poll_interval=0.01):
        pass
    assert client.set.call_args_list[0][0][0] == "lock:acme:42"
    token = set_tenant_schema("t_other")
    try:
        with lead_lock(42, client=client, poll_interval=0.01):
            pass
    finally:
        reset_tenant_schema(token)
    assert client.set.call_args_list[1][0][0] == "lock:other:42"
```

- [ ] **Step 2: FAIL → Step 3: Правка `lead_lock.py`:** `LOCK_KEY_PREFIX = "lead_lock:"` удалить; в `lead_lock()` вместо `key = f"{LOCK_KEY_PREFIX}{lead_id}"`:

```python
    from tenancy.context import require_tenant_slug
    key = f"lock:{require_tenant_slug()}:{lead_id}"
```

(Контекст в продакшене гарантирован входом таски — Task 11.) Импорт `LOCK_KEY_PREFIX` в тестах заменить на литералы.

- [ ] **Step 4: `cd /root/projects/meet/worker && ../.venv/bin/python -m pytest tests/test_lead_lock.py -v` — зелёный. Step 5: Commit**

```bash
cd /root/projects/meet && git add worker/tasks/lead_lock.py worker/tests/test_lead_lock.py
git commit -m "feat(lead_lock): per-tenant lock key namespace"
```

---

### Task 14: Провижининг-CLI + сид realestate

**Files:**
- Create: `backend/app/provision_tenant.py`
- Modify: `backend/requirements.txt` (добавить строку `argon2-cffi==23.1.0`)

- [ ] **Step 1: `backend/app/provision_tenant.py`**

```python
"""Tenant provisioning CLI.

  python -m app.provision_tenant acme --name "ACME Corp" \
      --admin-email admin@acme.ru [--admin-password ...]

  python -m app.provision_tenant realestate --seed-only \
      --admin-email alex.chechetov@gmail.com

--seed-only: tenant already exists (the cutover created realestate) — only
create the first admin user and the API key. The API-key plaintext is printed
ONCE to stdout; only its sha256 lands in shared.tenants.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

from argon2 import PasswordHasher

from tenancy.db import shared_connect, get_sync_db_url
from tenancy.identifiers import schema_for_slug, validate_slug


def _seed_admin(conn, schema: str, email: str, password: str) -> None:
    ph = PasswordHasher()
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema}.users (email, password_hash, role) "
            "VALUES (%s, %s, 'admin')",
            (email, ph.hash(password)),
        )


def _set_api_key(conn, slug: str) -> str:
    key = f"sqa_{secrets.token_urlsafe(32)}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE shared.tenants SET api_key_hash = %s WHERE slug = %s",
            (digest, slug),
        )
    return key


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("slug")
    p.add_argument("--name", default="")
    p.add_argument("--admin-email", required=True)
    p.add_argument("--admin-password", default="")
    p.add_argument("--seed-only", action="store_true",
                   help="tenant exists; only seed admin user + API key")
    args = p.parse_args()

    slug = validate_slug(args.slug)
    schema = schema_for_slug(slug)
    if not get_sync_db_url():
        sys.exit("DATABASE_URL_SYNC / DATABASE_URL is not set")
    password = args.admin_password or getpass.getpass(
        f"Password for {args.admin_email}: "
    )

    conn = shared_connect()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM shared.tenants WHERE slug = %s", (slug,)
            )
            exists = cur.fetchone() is not None

        if args.seed_only:
            if not exists:
                sys.exit(f"tenant {slug!r} not found (run without --seed-only)")
        else:
            if exists:
                sys.exit(f"tenant {slug!r} already exists")
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA {schema}")
                cur.execute(
                    "INSERT INTO shared.tenants "
                    "(slug, schema_name, display_name, status) "
                    "VALUES (%s, %s, %s, 'active')",
                    (slug, schema, args.name or slug),
                )
            conn.commit()
            # tenant track 001→head on the fresh schema
            from app.migrate import run_tenant

            run_tenant(schema)

        _seed_admin(conn, schema, args.admin_email, password)
        key = _set_api_key(conn, slug)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"tenant: {slug}  schema: {schema}")
    print(f"admin:  {args.admin_email}")
    print(f"API key (shown once, store it now): {key}")


if __name__ == "__main__":
    main()
```

Примечание: `run_tenant()` коммитит свои DDL сам (alembic), поэтому строка тенанта коммитится до него — если миграция упадёт, строку и схему надо удалить вручную (см. вывод ошибки); для Phase 1 этого достаточно.

- [ ] **Step 2: `../.venv/bin/pip install argon2-cffi==23.1.0` + добавить в `backend/requirements.txt`. Smoke: `cd backend && ../.venv/bin/python -c "import app.provision_tenant; print('ok')"`. Step 3: Commit**

```bash
cd /root/projects/meet && git add backend/app/provision_tenant.py backend/requirements.txt
git commit -m "feat(provision): tenant provisioning CLI with admin seed and one-shot API key"
```

---

### Task 15: Интеграционная верификация на чистой БД + ранбук деплоя

**Files:**
- Create: `docs/superpowers/specs/2026-06-10-phase1-deploy-runbook.md`

- [ ] **Step 1: Скретч-БД и прогон cutover-сценария** (порт/креды взять из `.env`; ниже — дефолты `run.sh`)

```bash
cd /root/projects/meet
export PGPASSWORD=changeme
createdb -h localhost -p 5434 -U realestate mt_scratch
export DATABASE_URL="postgresql+asyncpg://realestate:changeme@localhost:5434/mt_scratch"
export DATABASE_URL_SYNC="postgresql+psycopg2://realestate:changeme@localhost:5434/mt_scratch"

# 1. Эмуляция «старого прода»: единый трек до головы 010 в public
cd backend && ../.venv/bin/alembic upgrade head
psql -h localhost -p 5434 -U realestate mt_scratch -c "SELECT version_num FROM public.alembic_version"
# Expected: 010

# 2. Двухтрековый раннер: shared S001+S002 (cutover) + tenant 011-012
../.venv/bin/python -m app.migrate
# Expected: "[migrate] tenant track → t_realestate" ... "[migrate] done"

# 3. Проверки
psql -h localhost -p 5434 -U realestate mt_scratch -c "
  SELECT slug, schema_name, api_key_required FROM shared.tenants;
  SELECT version_num FROM t_realestate.alembic_version;
  SELECT to_regclass('public.sessions'), to_regclass('t_realestate.sessions'),
         to_regclass('t_realestate.users');
  SELECT to_regclass('public.alembic_version');"
# Expected: realestate|t_realestate|f ; 012 ; NULL + t_realestate.sessions +
#           t_realestate.users ; NULL

# 4. Идемпотентность: повторный прогон ничего не ломает
../.venv/bin/python -m app.migrate && echo IDEMPOTENT-OK
```

- [ ] **Step 2: Провижининг второго тенанта на той же БД**

```bash
cd /root/projects/meet/backend
../.venv/bin/python -m app.provision_tenant acme --name "ACME" \
    --admin-email admin@acme.test --admin-password secret123
# Expected: печать API-ключа sqa_...
psql -h localhost -p 5434 -U realestate mt_scratch -c "
  SELECT version_num FROM t_acme.alembic_version;
  SELECT email, role FROM t_acme.users;
  SELECT count(*) FROM t_acme.extraction_templates;"
# Expected: 012 ; admin@acme.test|admin ; 2  (сиды 005/008 легли в новый тенант)
```

- [ ] **Step 3: Изоляция на уровне SQL**

```bash
psql -h localhost -p 5434 -U realestate mt_scratch -c "
  SET search_path TO t_acme, shared, public;
  INSERT INTO sessions (id, status) VALUES (gen_random_uuid(), 'created');
  SET search_path TO t_realestate, shared, public;
  SELECT count(*) FROM sessions;"
# Expected: 0  (сессия acme не видна из t_realestate)
```

- [ ] **Step 4: Полные сьюты + downgrade-репетиция**

```bash
cd /root/projects/meet/worker && ../.venv/bin/python -m pytest -q
cd /root/projects/meet/backend && ../.venv/bin/python -m pytest tests -q
# Downgrade-репетиция (на скретч-БД):
cd /root/projects/meet/backend
../.venv/bin/alembic -c alembic_shared.ini downgrade s001   # откат cutover
psql -h localhost -p 5434 -U realestate mt_scratch -c "SELECT to_regclass('public.sessions')"
# Expected: public.sessions (таблицы вернулись)
dropdb -h localhost -p 5434 -U realestate mt_scratch
```

- [ ] **Step 5: Ранбук `docs/superpowers/specs/2026-06-10-phase1-deploy-runbook.md`** — зафиксировать порядок прод-деплоя:

```markdown
# Phase 1 cutover — прод-ранбук

Прод: /root/projects/realestate, systemd realestate-{backend,worker}.service.

1. Бэкап: pg_dump + tar data/. Прогнать весь Task-15 сценарий на копии прод-БД.
2. `systemctl stop realestate-worker` (beat переживёт паузу: POLL_SAFETY_WINDOW).
3. git pull; `.venv/bin/pip install -r backend/requirements.txt` (argon2-cffi).
4. `bash scripts/migrate_storage_layout.sh` (с env из .env!).
5. В .env добавить: `BASE_DOMAIN=silentqa.com`, `DEFAULT_TENANT=` (пусто —
   прод резолвится через custom_domains=rogov.automate-it.fun из S002).
6. `systemctl restart realestate-backend` — lifespan гонит app.migrate
   (S001→S002 cutover→tenant 011-012). Смотреть journalctl: "[migrate] done".
7. Сид: `cd backend && ../.venv/bin/python -m app.provision_tenant realestate
   --seed-only --admin-email <email>` → записать API-ключ.
8. `systemctl start realestate-worker`.
9. Smoke: дашборд по https://rogov.automate-it.fun открывается (Basic Auth
   пока жив — Plan 2 заменит); POST тестовой сессии; AmoCRM-поллер в логах
   работает по t_realestate.
10. Откат: stop both → `alembic -c alembic_shared.ini downgrade s001` →
    `UPDATE public.alembic_version SET version_num='010'` (вернуть старый
    stamp!) → обратный mv каталогов → git checkout прежнего коммита → start.
```

- [ ] **Step 6: Final commit**

```bash
cd /root/projects/meet && git add docs/superpowers/specs/2026-06-10-phase1-deploy-runbook.md
git commit -m "docs: phase 1 deploy runbook + integration verification recipe"
```

---

## Вне этого плана (следующие планы Фазы 1)

- **Plan 2 (auth):** `/api/user-auth/*` логин, Redis-сессии, enforcement API-ключей (`api_key_required`), grace для брокерских JWT, матрица доступа 5.6 (закрытие `/api/companies`, удаление `/api/webhooks`, server-owned `company_id`), SPA-логин, снятие Basic Auth, `X-API-Key` в recorder-клиентах.
- **Plan 3 (платформа):** admin-контур, платформенные админы, Cloudflare DNS/TLS wildcard, nginx/RU-relay для silentqa.com.

## Self-review checklist (выполнен автором плана)

- Spec-покрытие: разделы 3.1–3.4, 4.1–4.4, 6.3 спека → Tasks 1–14; 4.2-сид и 9 (миграционные тесты) → Tasks 14–15. Разделы 5 (auth), 5.5–5.6, 6.4, 7 — осознанно в Plan 2 (см. выше).
- Все 22 connect-сайта из инвентаризации перечислены в таблице Task 9; портальный сайт исключён осознанно.
- Типы согласованы: `set_tenant_schema`/`require_tenant_slug` (Task 2) используются в Tasks 8–13 с теми же сигнатурами; `tenant_connect()` без аргументов везде.
