# Universal Knowledge Base + per-tenant module visibility — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every tenant a configurable, generic **Knowledge Base** (terms/brands/diseases/etc.) that corrects transcripts (engine-agnostic), feeds the LLM glossary, and tags calls — and hide real-estate-specific UI/endpoints behind per-tenant **module flags**, without renaming or dropping the existing ЖК (`complexes`) stack.

**Architecture:** Six sequential phases, each shippable and green on its own. (0) Module-flag infrastructure in `shared.tenants` + a `require_module` gate. (1) Three KB tables per tenant schema. (2) A `worker/tasks/knowledge_base.py` module with **pure** matcher/normalize/glossary logic (unit-tested, no DB) + thin DB wrappers (try/except → degrade, never fail transcription), wired into the pipeline. (3) A `/api/knowledge` router mirroring `routes/templates.py`. (4) A one-shot ops script seeding realestate's existing `word_boost`. (5) Dashboard: a `#knowledge` page + KB-tag section + RE-leak gating. Tenant isolation rides the existing contextvar/`search_path` machinery; the worker steps are data-driven (empty KB = no-op).

**Tech Stack:** Python 3.12, FastAPI (async, raw SQL via `sqlalchemy.text`), Alembic two-track (tenant `alembic.ini` + shared `alembic_shared.ini`), psycopg2 sync (worker), Celery, Postgres schema-per-tenant, vanilla-JS SPA (no bundler), pytest. New runtime deps: **none** (matcher uses stdlib `re`).

---

## Plan-time decisions (resolving spec §11)

- **Migration numbers are locked** against current heads (verified `2026-06-16`): tenant-track head = `013` → KB tables = **`014`** (`down_revision='013'`); shared-track head = `s003` → modules = **`s004`** (`down_revision='s003'`). The `unify-recorder-identity` spec also pencils in `014` but has **no migration file yet** — when it lands it must use `015` (`down_revision='014'`). Note this in that spec; do not leave two `014`s (Alembic `MultipleHeads` → backend won't start).
- **Matcher = stdlib compiled alternation**, not Aho-Corasick: `re.compile(r'(?<!\w)(' + '|'.join(map(re.escape, aliases)) + r')(?!\w)', re.IGNORECASE)`. Avoids a new dependency; fine for the alias cap below. Build **once per pipeline run**. (Aho-Corasick / `pyahocorasick` is a future swap if a bench on a real 40-min transcript × full KB shows need — §9.)
- **AssemblyAI layer-2 hint is best-effort; correctness comes from layer-1 normalization** (engine-agnostic). SDK pinned at `assemblyai==0.58.0`, current model is `["universal-3-pro","universal-2"]` (no `keyterms_prompt` in code). Rule: feed `word_boost = dedup(config.word_boost + kb_keyterms())`; do **not** add `keyterms_prompt` (that is a Slam-1 path, out of scope). Live-API verification of `keyterms_prompt×model` is deferred — it does not affect correctness.
- **Concrete limits:** glossary ≤ **2000** approx-tokens; keyterms ≤ **1000** terms; import body ≤ **1 MiB**; ≤ **5000** entries per import; alias cap per tenant feeding the matcher ≤ **10000** (keyterms still capped at 1000). All caps `log()` what they drop.

---

## File Structure

**New files:**
- `backend/alembic_shared/versions/s004_tenant_modules.py` — `shared.tenants.modules jsonb` + backfill.
- `backend/alembic/versions/014_knowledge_base.py` — `kb_categories`, `kb_entries`, `kb_entry_mentions`.
- `backend/app/modules.py` — module-flag defaults resolver + `require_module(name)` dependency factory.
- `backend/app/routes/knowledge.py` — `/api/knowledge/*` router (mirror of `routes/templates.py`).
- `backend/app/schemas_knowledge.py` — Pydantic request/response models.
- `worker/tasks/knowledge_base.py` — pure matcher/normalize/glossary/keyterms + DB wrappers.
- `worker/scripts/seed_realestate_kb.py` — one-shot idempotent realestate `word_boost` → KB seed.
- Tests: `backend/tests/test_modules.py`, `backend/tests/test_tenancy_features.py`, `backend/tests/test_knowledge_routes.py`, `backend/tests/test_session_kb_tags.py`, `backend/tests/test_migration_s004_modules.py`; `worker/tests/test_kb_normalize.py`, `worker/tests/test_kb_glossary.py`, `worker/tests/test_kb_db.py`, `worker/tests/test_kb_pipeline_wiring.py`.

**Modified files:**
- `backend/app/tenancy_http.py` — add `modules` to the registry SELECT + cached row.
- `tenancy/registry.py` — add `modules` to `iter_active_tenants`; replace `iter_amocrm_tenants` slug-list with module filter.
- `backend/app/auth_user.py` — `require_amocrm_tenant` → delegate to `require_module('amocrm')`.
- `backend/app/routes/sessions.py` — gate `/extraction` behind `require_module('complexes')`; add `/{id}/tags` + `kb_tag` filter; gate `_DEFAULT_DESKTOP_TEMPLATE_NAME` auto-apply behind complexes.
- `backend/app/provision_tenant.py` — write explicit `modules` in the INSERT; find-or-create "Термины" KB category after `run_tenant`.
- `backend/app/main.py` — `include_router(knowledge.router)`.
- `worker/tasks/pipeline.py` — KB normalize + record_mentions around `:612`; keyterms into `word_boost` at `:590`; glossary into `assess_quality` (`:704`) and `run_extraction` (`:626`).
- `worker/tasks/quality.py` — `assess_quality` + both sub-paths accept `glossary`.
- `worker/tasks/extract.py` — `run_extraction` accepts `glossary`, injects into user prompt.
- `backend/static/index.html`, `backend/static/app.js` — `#knowledge` page, features fetch, nav + RE-leak gating.
- `backend/tests/test_access_matrix.py` — add `modules` to fixture `ROWS` (gating makes the bare rows 403 otherwise).

---

# PHASE 0 — Module-flag infrastructure

*Independently shippable: adds `modules` to the registry and a working `require_module` gate + `/api/tenancy/features`, with realestate unchanged.*

### Task 1: Shared migration `s004` — `shared.tenants.modules`

**Files:**
- Create: `backend/alembic_shared/versions/s004_tenant_modules.py`
- Test: `backend/tests/test_migration_s004_modules.py`

- [ ] **Step 1: Write the failing test** (asserts the migration module's SQL shape — pure, no DB)

```python
# backend/tests/test_migration_s004_modules.py
import importlib.util
from pathlib import Path

MIG = Path(__file__).resolve().parents[1] / "alembic_shared" / "versions" / "s004_tenant_modules.py"


def _load():
    spec = importlib.util.spec_from_file_location("s004", MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_chain():
    m = _load()
    assert m.revision == "s004"
    assert m.down_revision == "s003"


def test_upgrade_adds_column_and_backfills_by_rule():
    m = _load()
    sql = "\n".join(c.args[0] for c in _calls(m))
    assert "ADD COLUMN IF NOT EXISTS modules jsonb" in sql
    # realestate gets complexes+amocrm on; everyone keeps knowledge_base implicit-on
    assert "slug = 'realestate'" in sql
    assert '"complexes": true' in sql and '"amocrm": true' in sql


def _calls(m):
    import unittest.mock as mock
    with mock.patch.object(m.op, "execute") as ex:
        m.upgrade()
    return ex.call_args_list
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_migration_s004_modules.py -v`
Expected: FAIL — file does not exist (ModuleNotFoundError/ImportError).

- [ ] **Step 3: Write the migration**

```python
# backend/alembic_shared/versions/s004_tenant_modules.py
"""shared.tenants.modules jsonb — per-tenant module flags (universal dashboard).

Revision ID: s004
Revises: s003
"""
from typing import Sequence, Union

from alembic import op

revision: str = "s004"
down_revision: Union[str, None] = "s003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE shared.tenants "
        "ADD COLUMN IF NOT EXISTS modules jsonb NOT NULL DEFAULT '{}'::jsonb"
    )
    # Backfill by EXPLICIT rule for ALL existing tenants (not by slug list):
    #   knowledge_base — left implicit (resolver defaults it ON);
    #   complexes/amocrm — ON only for realestate, OFF (implicit) for the rest.
    op.execute(
        "UPDATE shared.tenants "
        "SET modules = modules || '{\"complexes\": true, \"amocrm\": true}'::jsonb "
        "WHERE slug = 'realestate'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE shared.tenants DROP COLUMN IF EXISTS modules")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_migration_s004_modules.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Apply on the dev DB and confirm**

Run: `cd backend && /root/projects/silentqa/.venv/bin/alembic -c alembic_shared.ini upgrade head`
Then: `cd backend && /root/projects/silentqa/.venv/bin/alembic -c alembic_shared.ini heads`
Expected: head shows `s004`. (If no DB is configured in this environment, skip the apply — the unit test is the gate.)

- [ ] **Step 6: Commit**

```bash
git add backend/alembic_shared/versions/s004_tenant_modules.py backend/tests/test_migration_s004_modules.py
git commit -m "feat(modules): shared s004 — tenants.modules jsonb + realestate backfill"
```

---

### Task 2: Registry SELECTs carry `modules`

**Files:**
- Modify: `backend/app/tenancy_http.py:34-51` (async `TenantRegistry.all_tenants`)
- Modify: `tenancy/registry.py:14-26` (sync `iter_active_tenants`)
- Test: `backend/tests/test_modules.py` (created here, extended in Task 3)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_modules.py
def test_registry_row_exposes_modules(monkeypatch):
    # async registry maps the modules column into the cached row dict
    import asyncio
    from app.tenancy_http import TenantRegistry

    class FakeRow:
        slug = "acme"; schema_name = "t_acme"; status = "active"
        custom_domains = []; api_key_hash = None; api_key_required = True
        modules = {"knowledge_base": True, "complexes": False}

    class FakeRes:
        def __iter__(self): return iter([FakeRow()])

    class FakeDB:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *a, **k): return FakeRes()

    monkeypatch.setattr("app.database.async_session", lambda: FakeDB())
    reg = TenantRegistry(ttl_seconds=0)
    rows = asyncio.run(reg.all_tenants())
    assert rows[0]["modules"] == {"knowledge_base": True, "complexes": False}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_modules.py -v`
Expected: FAIL — `KeyError: 'modules'`.

- [ ] **Step 3: Add `modules` to both SELECTs**

In `backend/app/tenancy_http.py`, extend the SELECT and the row dict (currently lines 34-51):

```python
                res = await db.execute(
                    text(
                        "SELECT slug, schema_name, status, custom_domains, "
                        "api_key_hash, api_key_required, modules "
                        "FROM shared.tenants"
                    )
                )
                self._rows = [
                    {
                        "slug": r.slug,
                        "schema_name": r.schema_name,
                        "status": r.status,
                        "custom_domains": list(r.custom_domains or []),
                        "api_key_hash": r.api_key_hash,
                        "api_key_required": r.api_key_required,
                        "modules": dict(r.modules or {}),
                    }
                    for r in res
                ]
```

In `tenancy/registry.py`, extend `iter_active_tenants` (lines 14-26):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_modules.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/tenancy_http.py tenancy/registry.py backend/tests/test_modules.py
git commit -m "feat(modules): registry SELECTs expose tenants.modules"
```

---

### Task 3: `module_enabled` resolver + `require_module` dependency

**Files:**
- Create: `backend/app/modules.py`
- Test: `backend/tests/test_modules.py` (extend)

- [ ] **Step 1: Write the failing tests**

```python
# append to backend/tests/test_modules.py
import pytest
from app.modules import module_enabled, MODULE_DEFAULTS


def test_default_semantics():
    # knowledge_base absent → ON; complexes/amocrm absent → OFF
    assert module_enabled({}, "knowledge_base") is True
    assert module_enabled({}, "complexes") is False
    assert module_enabled({}, "amocrm") is False


def test_explicit_overrides_default():
    assert module_enabled({"knowledge_base": False}, "knowledge_base") is False
    assert module_enabled({"complexes": True}, "complexes") is True


def test_unknown_module_defaults_off():
    assert module_enabled({}, "nonexistent") is False
    assert "knowledge_base" in MODULE_DEFAULTS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_modules.py -v`
Expected: FAIL — `ModuleNotFoundError: app.modules`.

- [ ] **Step 3: Write `app/modules.py`**

```python
# backend/app/modules.py
"""Per-tenant module flags (universal dashboard).

Default semantics (spec §5.1): a flag missing from tenants.modules resolves to
its DEFAULT here — NOT to "off" and NOT to "all on". knowledge_base is generic
(ON for everyone); RE-specific modules are strict opt-in (OFF).
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

MODULE_DEFAULTS: dict[str, bool] = {
    "knowledge_base": True,
    "complexes": False,
    "amocrm": False,
}


def module_enabled(modules: dict | None, name: str) -> bool:
    m = modules or {}
    if name in m:
        return bool(m[name])
    return MODULE_DEFAULTS.get(name, False)


def _tenant_modules(request: Request) -> dict:
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        # platform contour — no tenant modules
        raise HTTPException(status_code=404, detail="Unknown tenant")
    return tenant.get("modules") or {}


def require_module(name: str):
    """Dependency factory: 403 module_disabled if `name` is off for this tenant."""

    async def _dep(request: Request) -> None:
        if not module_enabled(_tenant_modules(request), name):
            raise HTTPException(status_code=403, detail="module_disabled")

    return _dep
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_modules.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules.py backend/tests/test_modules.py
git commit -m "feat(modules): module_enabled resolver + require_module gate"
```

---

### Task 4: `GET /api/tenancy/features`

**Files:**
- Modify: `backend/app/routes/tenancy_check.py` (the `/api/tenancy` router; add the endpoint)
- Test: `backend/tests/test_tenancy_features.py`

- [ ] **Step 1: Write the failing test** (mirror the `test_access_matrix.py` client fixture)

```python
# backend/tests/test_tenancy_features.py
import pytest
from starlette.testclient import TestClient

ROWS = [{
    "slug": "acme", "schema_name": "t_acme", "status": "active",
    "custom_domains": [], "api_key_hash": None, "api_key_required": True,
    "modules": {"complexes": False},
}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def test_features_resolves_defaults(client):
    r = client.get("/api/tenancy/features")
    assert r.status_code == 200
    body = r.json()
    assert body["knowledge_base"] is True   # default ON
    assert body["complexes"] is False        # explicit OFF
    assert body["amocrm"] is False           # default OFF
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_tenancy_features.py -v`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Add the endpoint** to `backend/app/routes/tenancy_check.py`

```python
# add near the other routes in tenancy_check.py
from fastapi import Request
from app.modules import MODULE_DEFAULTS, module_enabled

@router.get("/features")
async def get_features(request: Request):
    """Resolved module flags for the current tenant (used by the SPA to gate UI).
    Reads request.state.tenant — correct under impersonation (target tenant)."""
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    modules = tenant.get("modules") or {}
    return {name: module_enabled(modules, name) for name in MODULE_DEFAULTS}
```

(If `router` in that file has a prefix other than `/api/tenancy`, place the path so the full route is `/api/tenancy/features`. Confirm `HTTPException`/`Request` are imported.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_tenancy_features.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/tenancy_check.py backend/tests/test_tenancy_features.py
git commit -m "feat(modules): GET /api/tenancy/features resolves tenant flags"
```

---

### Task 5: Switch AmoCRM gating to `require_module('amocrm')`

**Files:**
- Modify: `backend/app/auth_user.py:129-137` (`require_amocrm_tenant`)
- Modify: `tenancy/registry.py:29-32` (`iter_amocrm_tenants`)
- Modify: `backend/tests/test_access_matrix.py:14-17` (add `modules` to `ROWS`)
- Test: `backend/tests/test_modules.py` (extend)

- [ ] **Step 1: Write the failing test** (module-gate via the access-matrix pattern)

```python
# append to backend/tests/test_modules.py
import asyncio
from starlette.testclient import TestClient
from app.auth_sessions import SESSION_COOKIE


def _admin_cookie(schema):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema(schema)
        try:
            return await auth_sessions.create_session("u1", "a@t.io", "admin")
        finally:
            reset_tenant_schema(token)
    return {SESSION_COOKIE: asyncio.run(seed())}


def test_amocrm_blocked_when_module_off(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": {"amocrm": False}}]
    monkeypatch.setattr(TenantRegistry, "all_tenants", lambda self: _coro(rows))
    from app.main import app
    c = TestClient(app, base_url="https://acme.silentqa.com")
    r = c.post("/api/amocrm/reprocess", cookies=_admin_cookie("t_acme"), json={"lead_id": 1})
    assert r.status_code == 403
    assert r.json()["detail"] == "module_disabled"


async def _coro(v):
    return v
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_modules.py::test_amocrm_blocked_when_module_off -v`
Expected: FAIL — current code returns 403 `amocrm_not_enabled` (wrong detail), or 200/other.

- [ ] **Step 3: Delegate `require_amocrm_tenant` to the module gate**

In `backend/app/auth_user.py`, replace the body (lines 129-137):

```python
async def require_amocrm_tenant(
    request: Request,
    user: UserCtx = Depends(require_admin),
) -> UserCtx:
    """/api/amocrm/*: admin + tenant with the amocrm module enabled (spec §5.3)."""
    from app.modules import module_enabled

    modules = (getattr(request.state, "tenant", None) or {}).get("modules")
    if not module_enabled(modules, "amocrm"):
        raise HTTPException(status_code=403, detail="module_disabled")
    return user
```

In `tenancy/registry.py`, make `iter_amocrm_tenants` module-driven (replace lines 29-32; keep `AMOCRM_TENANT_SLUGS` only if other code still imports it — otherwise delete it and its import sites):

```python
def iter_amocrm_tenants() -> list[dict]:
    from app.modules import module_enabled  # local import: avoids worker→app cycle at import time
    return [t for t in iter_active_tenants() if module_enabled(t.get("modules"), "amocrm")]
```

> ⚠️ If `worker/` cannot import `app.modules` (separate dependency set), instead inline the same default rule in `tenancy/registry.py` (a 4-line `_module_enabled` helper duplicating `MODULE_DEFAULTS`) to avoid a worker→backend import. Decide at implementation time by checking whether `app` is importable from the worker venv; prefer the inline helper to keep `tenancy/` app-independent.

- [ ] **Step 4: Update the access-matrix fixture** so existing gated rows still 401 (not 403)

In `backend/tests/test_access_matrix.py`, add `modules` to `ROWS` (line 14-17) so realestate keeps complexes+amocrm on:

```python
ROWS = [{
    "slug": "realestate", "schema_name": "t_realestate", "status": "active",
    "custom_domains": [], "api_key_hash": None, "api_key_required": True,
    "modules": {"complexes": True, "amocrm": True, "knowledge_base": True},
}]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_modules.py tests/test_access_matrix.py -v`
Expected: PASS (including the existing access-matrix amocrm test, now asserting `module_disabled` — update its expected detail string at `test_access_matrix.py:262` from `amocrm_not_enabled` to `module_disabled`).

- [ ] **Step 6: Commit**

```bash
git add backend/app/auth_user.py tenancy/registry.py backend/tests/test_modules.py backend/tests/test_access_matrix.py
git commit -m "feat(modules): amocrm gated by require_module('amocrm'); one source of truth"
```

---

### Task 6: Provisioning writes explicit `modules`

**Files:**
- Modify: `backend/app/provision_tenant.py:101-108` (the INSERT)
- Test: `backend/tests/test_provision_modules.py`

- [ ] **Step 1: Write the failing test** (assert the INSERT SQL includes modules with the explicit default)

```python
# backend/tests/test_provision_modules.py
import unittest.mock as mock
from app import provision_tenant


def test_provision_insert_includes_modules():
    captured = {}

    class FakeCur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            if "INSERT INTO shared.tenants" in sql:
                captured["sql"] = sql; captured["params"] = params
        def fetchone(self): return None

    class FakeConn:
        autocommit = False
        def cursor(self): return FakeCur()
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass

    with mock.patch.object(provision_tenant, "shared_connect", return_value=FakeConn()), \
         mock.patch.object(provision_tenant, "run_tenant", create=True), \
         mock.patch("app.migrate.run_tenant"), \
         mock.patch.object(provision_tenant, "_seed_admin"), \
         mock.patch.object(provision_tenant, "_set_api_key", return_value="sqa_x"), \
         mock.patch.object(provision_tenant, "_seed_kb_category", create=True):
        provision_tenant.provision("acme", "ACME", "a@b.io", "pw")

    assert "modules" in captured["sql"]
    assert '"knowledge_base": true' in captured["params"][-1]
    assert '"complexes": false' in captured["params"][-1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_provision_modules.py -v`
Expected: FAIL — INSERT has no modules column.

- [ ] **Step 3: Add `modules` to the INSERT** (`provision_tenant.py`, lines 101-108)

```python
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(
                "INSERT INTO shared.tenants "
                "(slug, schema_name, display_name, status, modules) "
                "VALUES (%s, %s, %s, 'active', %s::jsonb)",
                (slug, schema, name or slug,
                 '{"knowledge_base": true, "complexes": false, "amocrm": false}'),
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_provision_modules.py -v`
Expected: PASS. (The `_seed_kb_category` mock is a forward-declared hook filled in Task 15; `create=True` tolerates its absence now.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/provision_tenant.py backend/tests/test_provision_modules.py
git commit -m "feat(modules): provisioning writes explicit per-tenant modules"
```

**Phase 0 gate:** `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/ -q` → all green.

---

# PHASE 1 — KB data model (tenant migration `014`)

### Task 7: Tenant migration `014` — KB tables

**Files:**
- Create: `backend/alembic/versions/014_knowledge_base.py`
- Test: `backend/tests/test_migration_014_kb.py`

- [ ] **Step 1: Write the failing test** (pure SQL-shape assertions, mirror Task 1)

```python
# backend/tests/test_migration_014_kb.py
import importlib.util, unittest.mock as mock
from pathlib import Path

MIG = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "014_knowledge_base.py"


def _load():
    spec = importlib.util.spec_from_file_location("m014", MIG)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


def test_chain_and_tables():
    m = _load()
    assert m.revision == "014" and m.down_revision == "013"
    with mock.patch.object(m.op, "execute") as ex:
        m.upgrade()
    sql = "\n".join(c.args[0] for c in ex.call_args_list)
    assert "CREATE TABLE kb_categories" in sql
    assert "CREATE TABLE kb_entries" in sql
    assert "CREATE TABLE kb_entry_mentions" in sql
    assert "UNIQUE (slug)" in sql or "unique(slug)" in sql.lower()
    assert "UNIQUE (category_id, term)" in sql or "unique(category_id, term)" in sql.lower()
    assert "UNIQUE (entry_id, session_id)" in sql or "unique(entry_id, session_id)" in sql.lower()
    assert "REFERENCES sessions(id) ON DELETE CASCADE" in sql
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_migration_014_kb.py -v`
Expected: FAIL — file missing.

- [ ] **Step 3: Write the migration** (tenant track — unqualified names resolve via `search_path`, per `013`'s style; `gen_random_uuid()` needs pgcrypto/PG13+ — use it as in existing tenant tables, else `uuid_generate_v4()` if that's the repo convention; verify against `001_initial.py` before writing and match it)

```python
# backend/alembic/versions/014_knowledge_base.py
"""Knowledge Base: kb_categories, kb_entries, kb_entry_mentions (spec §1).

Revision ID: 014
Revises: 013
"""
from typing import Sequence, Union

from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE kb_categories (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name        text NOT NULL,
            slug        text NOT NULL,
            description text,
            feeds_asr   boolean NOT NULL DEFAULT false,
            feeds_llm   boolean NOT NULL DEFAULT false,
            is_taxonomy boolean NOT NULL DEFAULT false,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            UNIQUE (slug)
        )
    """)
    op.execute("""
        CREATE TABLE kb_entries (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            category_id uuid NOT NULL REFERENCES kb_categories(id) ON DELETE CASCADE,
            term        text NOT NULL,
            aliases     jsonb NOT NULL DEFAULT '[]'::jsonb,
            description text,
            metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            UNIQUE (category_id, term)
        )
    """)
    op.execute("CREATE INDEX ix_kb_entries_category ON kb_entries (category_id)")
    op.execute("""
        CREATE TABLE kb_entry_mentions (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            entry_id   uuid NOT NULL REFERENCES kb_entries(id) ON DELETE CASCADE,
            session_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            count      integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (entry_id, session_id)
        )
    """)
    op.execute("CREATE INDEX ix_kb_mentions_session ON kb_entry_mentions (session_id, entry_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS kb_entry_mentions")
    op.execute("DROP TABLE IF EXISTS kb_entries")
    op.execute("DROP TABLE IF EXISTS kb_categories")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_migration_014_kb.py -v`
Expected: PASS.

- [ ] **Step 5: Apply to all tenant schemas and confirm**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m app.migrate`
Then verify head: `cd backend && /root/projects/silentqa/.venv/bin/alembic -c alembic.ini -x tenant_schema=t_realestate heads` → `014`. (Skip if no DB in this env; unit test is the gate.)

- [ ] **Step 6: Commit**

```bash
git add backend/alembic/versions/014_knowledge_base.py backend/tests/test_migration_014_kb.py
git commit -m "feat(kb): tenant 014 — kb_categories/kb_entries/kb_entry_mentions"
```

---

# PHASE 2 — Worker KB module + pipeline wiring

### Task 8: Pure matcher / normalize / glossary / keyterms logic

**Files:**
- Create: `worker/tasks/knowledge_base.py` (pure functions only in this task)
- Test: `worker/tests/test_kb_normalize.py`, `worker/tests/test_kb_glossary.py`

- [ ] **Step 1: Write the failing tests**

```python
# worker/tests/test_kb_normalize.py
from tasks.knowledge_base import build_matcher, normalize_transcript, cap_keyterms

ENTRIES = [
    {"entry_id": "e1", "term": "Эталон", "aliases": ["эталон", "etalon"]},
    {"entry_id": "e2", "term": "ЖК Шагал", "aliases": ["шагал", "жк шагал"]},
]


def test_normalize_rewrites_segment_text_case_insensitive():
    m = build_matcher(ENTRIES)
    t = [{"speaker": "A", "text": "был у etalon вчера"}]
    out, hits = normalize_transcript(t, m)
    assert out[0]["text"] == "был у Эталон вчера"
    assert out[0]["orig_text"] == "был у etalon вчера"
    assert ("e1", 1) in hits


def test_normalize_word_boundaries_and_multiword():
    m = build_matcher(ENTRIES)
    t = [{"speaker": "A", "text": "смотрел жк шагал и эталоновый дом"}]
    out, hits = normalize_transcript(t, m)
    # multiword alias replaced; 'эталоновый' NOT matched (boundary)
    assert "ЖК Шагал" in out[0]["text"]
    assert "эталоновый" in out[0]["text"]
    assert dict(hits).get("e1") is None


def test_normalize_is_idempotent():
    m = build_matcher(ENTRIES)
    t = [{"speaker": "A", "text": "у etalon"}]
    once, _ = normalize_transcript(t, m)
    twice, _ = normalize_transcript([dict(s) for s in once], m)
    assert twice[0]["text"] == once[0]["text"] == "у Эталон"


def test_cap_keyterms_dedup_and_limit():
    terms = ["a", "a", "b"] + [f"t{i}" for i in range(2000)]
    out = cap_keyterms(terms, limit=1000)
    assert len(out) == 1000 and len(set(out)) == 1000
```

```python
# worker/tests/test_kb_glossary.py
from tasks.knowledge_base import format_glossary


def test_glossary_empty_returns_empty():
    assert format_glossary([]) == ""


def test_glossary_renders_terms_and_aliases():
    cats = [{"name": "Бренды", "entries": [
        {"term": "Ювидерм", "aliases": ["juvederm"], "description": "филлер"}]}]
    g = format_glossary(cats)
    assert "Ювидерм" in g and "juvederm" in g and "филлер" in g


def test_glossary_token_cap_truncates_with_marker():
    cats = [{"name": "C", "entries": [
        {"term": f"термин-{i}", "aliases": [], "description": "x" * 50} for i in range(500)]}]
    g = format_glossary(cats, max_tokens=200)
    assert "опущено" in g
    assert len(g) // 4 <= 260  # ~max_tokens + marker slack
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_kb_normalize.py tests/test_kb_glossary.py -v`
Expected: FAIL — `ModuleNotFoundError: tasks.knowledge_base`.

- [ ] **Step 3: Write the pure logic**

```python
# worker/tasks/knowledge_base.py  (pure-logic section; DB wrappers added in Task 9)
"""Knowledge Base: transcript normalization, LLM glossary, ASR keyterms, tagging.

Pure functions operate on plain inputs (unit-tested, no DB). The kb_* DB wrappers
(Task 9) open their own tenant connection and degrade to empty on ANY error so they
NEVER fail transcription (pipeline marks the session failed + re-raises otherwise).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

KEYTERMS_LIMIT = 1000
ALIAS_LIMIT = 10000
GLOSSARY_MAX_TOKENS = 2000


class Matcher:
    """Compiled alternation over (alias → (term, entry_id)), longest-alias-first."""

    def __init__(self, pattern, lookup):
        self._pattern = pattern
        self._lookup = lookup  # casefolded alias -> (term, entry_id)

    def find_and_replace(self, text_str: str):
        hits: dict[str, int] = {}
        if self._pattern is None:
            return text_str, hits

        def _sub(mobj):
            alias = mobj.group(0)
            term, entry_id = self._lookup[alias.casefold()]
            hits[entry_id] = hits.get(entry_id, 0) + 1
            return term

        return self._pattern.sub(_sub, text_str), hits


def build_matcher(entries: list[dict]) -> Matcher:
    """entries: [{entry_id, term, aliases:[...]}]. Match term + aliases, case-insensitive,
    on word boundaries. Idempotent: the canonical `term` is itself an alias, so a second
    pass is a no-op. Longest alias first so multiword wins over a contained single word."""
    lookup: dict[str, tuple[str, str]] = {}
    for e in entries:
        forms = [e["term"], *(e.get("aliases") or [])]
        for f in forms:
            f = (f or "").strip()
            if f:
                lookup.setdefault(f.casefold(), (e["term"], e["entry_id"]))
    if not lookup:
        return Matcher(None, {})
    alts = sorted(lookup.keys(), key=len, reverse=True)[:ALIAS_LIMIT]
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(a) for a in alts) + r")(?!\w)",
        re.IGNORECASE,
    )
    return Matcher(pattern, lookup)


def normalize_transcript(transcript: list[dict], matcher: Matcher):
    """Rewrite each segment['text'] to canonical terms (LLM + saved transcript read it).
    Preserve the original in segment['orig_text']. Returns (transcript, hits=[(entry_id,count)])."""
    total: dict[str, int] = {}
    for seg in transcript:
        original = seg.get("text") or ""
        if not original:
            continue
        new_text, hits = matcher.find_and_replace(original)
        if new_text != original:
            seg.setdefault("orig_text", original)
            seg["text"] = new_text
        for entry_id, c in hits.items():
            total[entry_id] = total.get(entry_id, 0) + c
    return transcript, list(total.items())


def cap_keyterms(terms: list[str], limit: int = KEYTERMS_LIMIT) -> list[str]:
    seen, out = set(), []
    for t in terms:
        t = (t or "").strip()
        if t and t.casefold() not in seen:
            seen.add(t.casefold()); out.append(t)
        if len(out) >= limit:
            logger.info("kb keyterms capped at %d (dropped extras)", limit)
            break
    return out


def _approx_tokens(s: str) -> int:
    return len(s) // 4


def format_glossary(categories: list[dict], max_tokens: int = GLOSSARY_MAX_TOKENS) -> str:
    """categories: [{name, entries:[{term, aliases, description}]}] (feeds_llm only)."""
    if not categories:
        return ""
    lines = ["## Глоссарий (правильные написания и значения терминов)"]
    omitted = 0
    for cat in categories:
        for e in cat.get("entries") or []:
            alias_s = ", ".join(e.get("aliases") or [])
            desc = e.get("description") or ""
            line = f"- {e['term']}" + (f" (={alias_s})" if alias_s else "") + (f" — {desc}" if desc else "")
            candidate = "\n".join(lines + [line])
            if _approx_tokens(candidate) > max_tokens:
                omitted += 1
                continue
            lines.append(line)
    if omitted:
        lines.append(f"… ещё {omitted} записей опущено")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_kb_normalize.py tests/test_kb_glossary.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/knowledge_base.py worker/tests/test_kb_normalize.py worker/tests/test_kb_glossary.py
git commit -m "feat(kb): pure matcher/normalize/glossary/keyterms logic"
```

---

### Task 9: KB DB wrappers (fallback-safe)

**Files:**
- Modify: `worker/tasks/knowledge_base.py` (append DB wrappers)
- Test: `worker/tests/test_kb_db.py` (integration, `DATABASE_URL_SYNC`), plus a fallback unit test

- [ ] **Step 1: Write the failing tests**

```python
# worker/tests/test_kb_db.py
import os
import uuid
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session as DbSession

from tasks import knowledge_base as kb

requires_db = pytest.mark.skipif(
    "DATABASE_URL_SYNC" not in os.environ, reason="integration test needs DATABASE_URL_SYNC"
)


def test_kb_record_mentions_degrades_without_tables(monkeypatch):
    # tenant_connect raises (no schema/ctx) → wrapper must swallow and not raise
    def boom():
        raise RuntimeError("no tenant ctx")
    monkeypatch.setattr(kb, "tenant_connect", boom, raising=False)
    kb.kb_record_mentions(uuid.uuid4(), [("e1", 2)])  # must NOT raise


@requires_db
def test_record_mentions_upsert_replaces_count():
    # uses search_path of DATABASE_URL_SYNC; assumes alembic head (014) applied.
    url = os.environ["DATABASE_URL_SYNC"]
    eng = create_engine(url, future=True)
    with eng.connect() as conn:
        with conn.begin() as trans:
            with DbSession(bind=conn, expire_on_commit=False) as s:
                cat = uuid.uuid4(); ent = uuid.uuid4(); sid = uuid.uuid4()
                s.execute(text("INSERT INTO kb_categories (id,name,slug,is_taxonomy) VALUES (:i,'C','c-%s',true)" % cat.hex[:6]), {"i": cat})
                s.execute(text("INSERT INTO kb_entries (id,category_id,term) VALUES (:i,:c,'T')"), {"i": ent, "c": cat})
                s.execute(text("INSERT INTO sessions (id,status) VALUES (:i,'completed')"), {"i": sid})
                s.execute(text("""INSERT INTO kb_entry_mentions (entry_id,session_id,count) VALUES (:e,:s,5)
                                  ON CONFLICT (entry_id,session_id) DO UPDATE SET count=EXCLUDED.count"""), {"e": ent, "s": sid})
                s.execute(text("""INSERT INTO kb_entry_mentions (entry_id,session_id,count) VALUES (:e,:s,2)
                                  ON CONFLICT (entry_id,session_id) DO UPDATE SET count=EXCLUDED.count"""), {"e": ent, "s": sid})
                row = s.execute(text("SELECT count FROM kb_entry_mentions WHERE entry_id=:e AND session_id=:s"), {"e": ent, "s": sid}).first()
                assert row[0] == 2  # replace, not +=
            trans.rollback()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_kb_db.py -v`
Expected: FAIL — `kb_record_mentions` attribute does not exist.

- [ ] **Step 3: Append the DB wrappers** to `worker/tasks/knowledge_base.py`

```python
# --- DB wrappers: own their connection via tenant ctx; degrade to empty on error ---
from tenancy.db import tenant_connect  # noqa: E402


def _fetch_entries(feeds_col: str) -> list[dict]:
    """Rows for matcher/keyterms/glossary from categories where the given flag is true."""
    conn = tenant_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT e.id::text, e.term, e.aliases, e.description, c.name "
                f"FROM kb_entries e JOIN kb_categories c ON c.id = e.category_id "
                f"WHERE c.{feeds_col} = true ORDER BY c.created_at, e.created_at"
            )
            return [{"entry_id": r[0], "term": r[1], "aliases": r[2] or [],
                     "description": r[3], "category": r[4]} for r in cur.fetchall()]
    finally:
        conn.close()


def kb_build_matcher() -> Matcher:
    """Matcher from feeds_asr + is_taxonomy entries (correction + tagging)."""
    try:
        conn = tenant_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT e.id::text, e.term, e.aliases FROM kb_entries e "
                    "JOIN kb_categories c ON c.id = e.category_id "
                    "WHERE c.feeds_asr = true OR c.is_taxonomy = true"
                )
                rows = [{"entry_id": r[0], "term": r[1], "aliases": r[2] or []} for r in cur.fetchall()]
        finally:
            conn.close()
        return build_matcher(rows)
    except Exception:
        logger.exception("kb_build_matcher failed; using empty matcher")
        return build_matcher([])


def kb_keyterms() -> list[str]:
    try:
        rows = _fetch_entries("feeds_asr")
        terms = [r["term"] for r in rows] + [a for r in rows for a in r["aliases"]]
        return cap_keyterms(terms)
    except Exception:
        logger.exception("kb_keyterms failed; returning []")
        return []


def kb_glossary() -> str:
    try:
        rows = _fetch_entries("feeds_llm")
        by_cat: dict[str, dict] = {}
        for r in rows:
            by_cat.setdefault(r["category"], {"name": r["category"], "entries": []})
            by_cat[r["category"]]["entries"].append(r)
        return format_glossary(list(by_cat.values()))
    except Exception:
        logger.exception("kb_glossary failed; returning ''")
        return ""


def kb_record_mentions(session_id, hits: list[tuple[str, int]]) -> None:
    if not hits:
        return
    try:
        conn = tenant_connect()
        try:
            with conn.cursor() as cur:
                for entry_id, count in hits:
                    cur.execute(
                        "INSERT INTO kb_entry_mentions (entry_id, session_id, count) "
                        "VALUES (%s, %s, %s) "
                        "ON CONFLICT (entry_id, session_id) "
                        "DO UPDATE SET count = EXCLUDED.count, updated_at = now()",
                        (entry_id, str(session_id), count),
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("kb_record_mentions failed; skipping tags for %s", session_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_kb_db.py -v`
Expected: PASS (the integration test is **skipped** unless `DATABASE_URL_SYNC` is set; the fallback test passes always).

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/knowledge_base.py worker/tests/test_kb_db.py
git commit -m "feat(kb): fallback-safe DB wrappers (matcher/keyterms/glossary/mentions)"
```

---

### Task 10: Thread `glossary` through `assess_quality` and `run_extraction`

**Files:**
- Modify: `worker/tasks/quality.py:1106-1115` (sig), `:1178-1218` (structured), `:1248-1257` (legacy)
- Modify: `worker/tasks/extract.py:51`, `:80-94` (sig + user prompt)
- Test: `worker/tests/test_kb_glossary.py` (extend with injection tests)

- [ ] **Step 1: Write the failing tests**

```python
# append to worker/tests/test_kb_glossary.py
def test_structured_path_injects_glossary(monkeypatch):
    import tasks.quality as q
    captured = {}

    class FakeResp:
        output_text = '{"score_version": 2}'
    class FakeClient:
        class responses:
            @staticmethod
            def create(**kw): captured.update(kw); return FakeResp()

    q._assess_with_structured_output(FakeClient(), "TRANSCRIPT", "[]", None, "",
                                     glossary="## Глоссарий\n- X")
    user_msg = [m for m in captured["input"] if m["role"] == "user"][0]["content"]
    assert "## Глоссарий" in user_msg


def test_legacy_path_injects_glossary(monkeypatch):
    import tasks.quality as q
    captured = {}

    class FakeResp:
        class choices:
            class message: content = "{}"
        choices = [type("C", (), {"message": type("M", (), {"content": "{}"})})()]
    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw): captured.update(kw); return FakeResp()

    q._assess_with_legacy_prompt(FakeClient(), "T", "[]", None, "", glossary="## Глоссарий\n- Y")
    assert "## Глоссарий" in captured["messages"][0]["content"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_kb_glossary.py -v`
Expected: FAIL — sub-paths don't accept `glossary`.

- [ ] **Step 3: Add the `glossary` parameter and inject it**

In `quality.py`, `assess_quality` signature (after `template_driven`):

```python
    template_driven: bool = False,
    glossary: str | None = None,
) -> dict:
```

Find where `assess_quality` calls the two sub-paths and pass `glossary=glossary` to both.

In `_assess_with_structured_output` (sig + the `user_parts` block, lines 1178-1218):

```python
def _assess_with_structured_output(
    client, transcript_text, sentiment_json, protocol, custom_instructions,
    criteria_instructions="", use_extended_schema=False,
    prior_context=None, template_driven=False, glossary=None,
):
    ...
    user_parts = []
    if glossary:
        user_parts.append(glossary + "\n")
    user_parts.append(
        USER_PROMPT.format(
            protocol=protocol or DEFAULT_PROTOCOL,
            transcript=transcript_text,
            sentiment_summary=sentiment_json,
        )
    )
    if prior_context is not None:
        user_parts.append("\n## Prior context\n" + json.dumps(prior_context, ensure_ascii=False, indent=2))
    user = "\n".join(user_parts)
```

In `_assess_with_legacy_prompt` (sig + prompt, lines 1248-1257):

```python
def _assess_with_legacy_prompt(
    client, transcript_text, sentiment_json, protocol, custom_instructions, glossary=None
):
    prompt = ASSESSMENT_PROMPT.format(
        protocol=protocol or DEFAULT_PROTOCOL,
        transcript=transcript_text,
        sentiment_summary=sentiment_json,
        custom_instructions=custom_instructions,
    )
    if glossary:
        prompt = glossary + "\n\n" + prompt
```

In `extract.py`, `run_extraction` (line 51) gains `glossary`, and the user message (line 84) is prefixed:

```python
def run_extraction(db: DbSession, session_id, template_id, glossary: str | None = None) -> dict:
    ...
    user_content = (glossary + "\n\n" if glossary else "") + f"Транскрипт:\n\n{flat}"
    resp = client.responses.create(
        model="gpt-5.4",
        input=[
            {"role": "system", "content": template.prompt},
            {"role": "user", "content": user_content},
        ],
        ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_kb_glossary.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/quality.py worker/tasks/extract.py worker/tests/test_kb_glossary.py
git commit -m "feat(kb): thread glossary into quality (structured+legacy) and extraction"
```

---

### Task 11: Wire KB into the pipeline

**Files:**
- Modify: `worker/tasks/pipeline.py:590-593` (keyterms→word_boost), `:611-612` (normalize+record), `:626` (extraction glossary), `:704-713` (quality glossary)
- Test: `worker/tests/test_kb_pipeline_wiring.py`

- [ ] **Step 1: Write the failing test** (unit: KB calls are invoked, and failures don't propagate)

```python
# worker/tests/test_kb_pipeline_wiring.py
import tasks.knowledge_base as kb


def test_merge_keyterms_into_word_boost(monkeypatch):
    from tasks.pipeline import _merge_kb_keyterms
    monkeypatch.setattr(kb, "kb_keyterms", lambda: ["Эталон", "квартира"])
    out = _merge_kb_keyterms(["квартира", "ипотека"])
    assert "Эталон" in out and out.count("квартира") == 1  # union + dedup


def test_merge_keyterms_survives_kb_error(monkeypatch):
    from tasks.pipeline import _merge_kb_keyterms
    def boom(): raise RuntimeError("db down")
    monkeypatch.setattr(kb, "kb_keyterms", boom)
    assert _merge_kb_keyterms(["квартира"]) == ["квартира"]  # degrades to original
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_kb_pipeline_wiring.py -v`
Expected: FAIL — `_merge_kb_keyterms` not defined.

- [ ] **Step 3: Wire the pipeline**

Add the helper near the top of `pipeline.py` (after imports):

```python
def _merge_kb_keyterms(word_boost: list[str]) -> list[str]:
    """Union config word_boost with KB feeds_asr keyterms; dedup; survive KB errors."""
    try:
        from tasks.knowledge_base import kb_keyterms, cap_keyterms
        return cap_keyterms(list(word_boost or []) + kb_keyterms())
    except Exception:
        logger.exception("kb keyterms merge failed; using config word_boost only")
        return list(word_boost or [])
```

At `pipeline.py:590-593` (the transcription branch), merge before transcribing:

```python
        word_boost = get_word_boost(company_config)
        word_boost = _merge_kb_keyterms(word_boost)        # KB layer-2 (best-effort)
        engine_override = get_asr_engine(company_config)
        logger.info(f"[{session_id}] Step 2: Transcribing (word_boost: {len(word_boost)} terms)...")
        transcript = transcribe_audio(audio_path, word_boost=word_boost, engine_override=engine_override)
```

At `pipeline.py:611-612` — normalize (layer-1, authoritative) BEFORE persisting, then record mentions. Replace the single `save_results(... "transcript" ...)` line with:

```python
    # KB layer-1: engine-agnostic correction + tagging. Idempotent → safe for reprocess.
    try:
        from tasks.knowledge_base import kb_build_matcher, normalize_transcript, kb_record_mentions
        matcher = kb_build_matcher()
        transcript_with_speakers, _kb_hits = normalize_transcript(transcript_with_speakers, matcher)
    except Exception:
        logger.exception(f"[{session_id}] KB normalize failed; transcript unmodified")
        _kb_hits = []
    save_results(session_id, "transcript", transcript_with_speakers)  # normalized on disk
    try:
        from tasks.knowledge_base import kb_record_mentions
        kb_record_mentions(session_id, _kb_hits)
    except Exception:
        logger.exception(f"[{session_id}] KB record_mentions failed")
```

At `pipeline.py:626`, pass glossary into extraction:

```python
                    from tasks.knowledge_base import kb_glossary
                    run_extraction(db, session_id, template_id, glossary=kb_glossary())
```

At `pipeline.py:704-713`, pass glossary into quality:

```python
    quality_report = assess_quality(
        transcript_with_speakers,
        sentiment_results,
        protocol=quality_protocol,
        custom_prompt=quality_prompt,
        criteria_config=quality_criteria,
        use_extended_schema=use_extended,
        prior_context=prior_context,
        template_driven=template_driven,
        glossary=_kb_glossary_safe(),
    )
```

…where `_kb_glossary_safe` is a tiny helper added near `_merge_kb_keyterms`:

```python
def _kb_glossary_safe() -> str:
    try:
        from tasks.knowledge_base import kb_glossary
        return kb_glossary()
    except Exception:
        logger.exception("kb glossary failed; using empty")
        return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_kb_pipeline_wiring.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full worker suite (regression)**

Run: `cd worker && /root/projects/silentqa/.venv/bin/python -m pytest -q`
Expected: prior pass count + new KB tests pass; the 4 `test_complex_match` errors remain only if `DATABASE_URL_SYNC` is unset (unchanged).

- [ ] **Step 6: Commit**

```bash
git add worker/tasks/pipeline.py worker/tests/test_kb_pipeline_wiring.py
git commit -m "feat(kb): wire normalize/keyterms/glossary/mentions into pipeline (fallback-safe)"
```

---

# PHASE 3 — KB backend API

### Task 12: Pydantic schemas

**Files:**
- Create: `backend/app/schemas_knowledge.py`
- Test: covered via route tests (Task 13)

- [ ] **Step 1: Write the schemas**

```python
# backend/app/schemas_knowledge.py
from datetime import datetime
import uuid
from pydantic import BaseModel, Field


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_-]+$")
    description: str | None = None
    feeds_asr: bool = False
    feeds_llm: bool = False
    is_taxonomy: bool = False


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    feeds_asr: bool | None = None
    feeds_llm: bool | None = None
    is_taxonomy: bool | None = None


class EntryCreate(BaseModel):
    category_id: uuid.UUID
    term: str = Field(min_length=1, max_length=300)
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    metadata: dict = Field(default_factory=dict)


class EntryUpdate(BaseModel):
    term: str | None = Field(default=None, min_length=1, max_length=300)
    aliases: list[str] | None = None
    description: str | None = None
    metadata: dict | None = None


class ImportRow(BaseModel):
    term: str = Field(min_length=1, max_length=300)
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None


class ImportRequest(BaseModel):
    category_id: uuid.UUID
    rows: list[ImportRow] = Field(max_length=5000)
```

- [ ] **Step 2: Commit** (schemas alone don't run; they're imported by Task 13)

```bash
git add backend/app/schemas_knowledge.py
git commit -m "feat(kb): Pydantic schemas for knowledge routes"
```

---

### Task 13: `/api/knowledge` router

**Files:**
- Create: `backend/app/routes/knowledge.py`
- Modify: `backend/app/main.py` (register router after the other `include_router` calls, ~line 59-73)
- Test: `backend/tests/test_knowledge_routes.py`

- [ ] **Step 1: Write the failing tests** (RBAC + module-gate, pure-unit like `test_access_matrix.py`)

```python
# backend/tests/test_knowledge_routes.py
import asyncio
import pytest
from starlette.testclient import TestClient
from app.auth_sessions import SESSION_COOKIE


def _rows(modules):
    return [{"slug": "acme", "schema_name": "t_acme", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": modules}]


def _client(monkeypatch, modules):
    from app.tenancy_http import TenantRegistry
    async def fake_all(self): return _rows(modules)
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def _cookie(role):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions
    async def seed():
        token = set_tenant_schema("t_acme")
        try: return await auth_sessions.create_session("u", "x@t.io", role)
        finally: reset_tenant_schema(token)
    return {SESSION_COOKIE: asyncio.run(seed())}


def test_knowledge_gated_off_403(monkeypatch, fake_redis):
    c = _client(monkeypatch, {"knowledge_base": False})
    r = c.get("/api/knowledge/categories", cookies=_cookie("viewer"))
    assert r.status_code == 403 and r.json()["detail"] == "module_disabled"


def test_categories_require_session(monkeypatch, fake_redis):
    c = _client(monkeypatch, {"knowledge_base": True})
    assert c.get("/api/knowledge/categories").status_code == 401


def test_create_category_admin_only(monkeypatch, fake_redis):
    c = _client(monkeypatch, {"knowledge_base": True})
    r = c.post("/api/knowledge/categories", cookies=_cookie("viewer"),
               json={"name": "C", "slug": "c"})
    assert r.status_code == 403 and r.json()["detail"] == "admin_required"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_knowledge_routes.py -v`
Expected: FAIL — routes 404 (not registered).

- [ ] **Step 3: Write the router** (mirror `routes/templates.py`; gate ALL routes with `require_module('knowledge_base')`)

```python
# backend/app/routes/knowledge.py
"""Knowledge Base CRUD + import + mentions. Reads=viewer, mutations=admin.
All routes gated by the knowledge_base module."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_user import employee_scope, require_admin, require_viewer
from app.database import get_db
from app.modules import require_module
from app.schemas_knowledge import (
    CategoryCreate, CategoryUpdate, EntryCreate, EntryUpdate, ImportRequest,
)

router = APIRouter(
    prefix="/api/knowledge", tags=["knowledge"],
    dependencies=[Depends(require_module("knowledge_base"))],
)


@router.get("/categories", dependencies=[Depends(require_viewer)])
async def list_categories(include_entries: bool = False, db: AsyncSession = Depends(get_db)):
    cats = (await db.execute(text(
        "SELECT id, name, slug, description, feeds_asr, feeds_llm, is_taxonomy, updated_at "
        "FROM kb_categories ORDER BY created_at"
    ))).all()
    out = [dict(id=str(c[0]), name=c[1], slug=c[2], description=c[3], feeds_asr=c[4],
                feeds_llm=c[5], is_taxonomy=c[6], updated_at=c[7], entries=[]) for c in cats]
    if include_entries:
        rows = (await db.execute(text(
            "SELECT id, category_id, term, aliases, description FROM kb_entries ORDER BY created_at"
        ))).all()
        by_id = {c["id"]: c for c in out}
        for r in rows:
            cat = by_id.get(str(r[1]))
            if cat is not None:
                cat["entries"].append(dict(id=str(r[0]), term=r[2], aliases=r[3], description=r[4]))
    return out


@router.post("/categories", status_code=201, dependencies=[Depends(require_admin)])
async def create_category(body: CategoryCreate, db: AsyncSession = Depends(get_db)):
    new_id = uuid.uuid4()
    try:
        await db.execute(text(
            "INSERT INTO kb_categories (id, name, slug, description, feeds_asr, feeds_llm, is_taxonomy) "
            "VALUES (:id,:name,:slug,:desc,:fa,:fl,:tax)"
        ), {"id": new_id, "name": body.name, "slug": body.slug, "desc": body.description,
            "fa": body.feeds_asr, "fl": body.feeds_llm, "tax": body.is_taxonomy})
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Cannot create category: {e}")
    return {"id": str(new_id)}


@router.patch("/categories/{category_id}", dependencies=[Depends(require_admin)])
async def update_category(category_id: uuid.UUID, body: CategoryUpdate, db: AsyncSession = Depends(get_db)):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return {"id": str(category_id)}
    sets = ", ".join(f"{k} = :{k}" for k in fields) + ", updated_at = now()"
    res = await db.execute(text(f"UPDATE kb_categories SET {sets} WHERE id=:id RETURNING id"),
                           {**fields, "id": category_id})
    if not res.first():
        raise HTTPException(status_code=404, detail="Category not found")
    await db.commit()
    return {"id": str(category_id)}


@router.delete("/categories/{category_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_category(category_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    res = await db.execute(text("DELETE FROM kb_categories WHERE id=:id RETURNING id"), {"id": category_id})
    if not res.first():
        raise HTTPException(status_code=404, detail="Category not found")
    await db.commit()


@router.post("/entries", status_code=201, dependencies=[Depends(require_admin)])
async def create_entry(body: EntryCreate, db: AsyncSession = Depends(get_db)):
    new_id = uuid.uuid4()
    try:
        await db.execute(text(
            "INSERT INTO kb_entries (id, category_id, term, aliases, description, metadata) "
            "VALUES (:id,:cid,:term,CAST(:al AS jsonb),:desc,CAST(:meta AS jsonb))"
        ), {"id": new_id, "cid": body.category_id, "term": body.term,
            "al": json.dumps(body.aliases, ensure_ascii=False), "desc": body.description,
            "meta": json.dumps(body.metadata, ensure_ascii=False)})
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Cannot create entry: {e}")
    return {"id": str(new_id)}


@router.patch("/entries/{entry_id}", dependencies=[Depends(require_admin)])
async def update_entry(entry_id: uuid.UUID, body: EntryUpdate, db: AsyncSession = Depends(get_db)):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return {"id": str(entry_id)}
    sets, params = [], {"id": entry_id}
    for k, v in fields.items():
        if k in ("aliases", "metadata"):
            sets.append(f"{k} = CAST(:{k} AS jsonb)"); params[k] = json.dumps(v, ensure_ascii=False)
        else:
            sets.append(f"{k} = :{k}"); params[k] = v
    sets.append("updated_at = now()")
    res = await db.execute(text(f"UPDATE kb_entries SET {', '.join(sets)} WHERE id=:id RETURNING id"), params)
    if not res.first():
        raise HTTPException(status_code=404, detail="Entry not found")
    await db.commit()
    return {"id": str(entry_id)}


@router.delete("/entries/{entry_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_entry(entry_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    res = await db.execute(text("DELETE FROM kb_entries WHERE id=:id RETURNING id"), {"id": entry_id})
    if not res.first():
        raise HTTPException(status_code=404, detail="Entry not found")
    await db.commit()


@router.post("/import", dependencies=[Depends(require_admin)])
async def import_entries(body: ImportRequest, db: AsyncSession = Depends(get_db)):
    """Bulk insert; dedup on (category_id, term) via ON CONFLICT DO NOTHING."""
    inserted = 0
    for row in body.rows:
        res = await db.execute(text(
            "INSERT INTO kb_entries (id, category_id, term, aliases, description) "
            "VALUES (gen_random_uuid(), :cid, :term, CAST(:al AS jsonb), :desc) "
            "ON CONFLICT (category_id, term) DO NOTHING RETURNING id"
        ), {"cid": body.category_id, "term": row.term,
            "al": json.dumps(row.aliases, ensure_ascii=False), "desc": row.description})
        if res.first():
            inserted += 1
    await db.commit()
    return {"inserted": inserted, "received": len(body.rows)}


@router.get("/entries/{entry_id}/mentions", dependencies=[Depends(require_viewer)])
async def entry_mentions(entry_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                         scope: str | None = Depends(employee_scope)):
    q = ("SELECT s.id, s.created_at, m.count FROM kb_entry_mentions m "
         "JOIN sessions s ON s.id = m.session_id WHERE m.entry_id = :eid")
    params = {"eid": entry_id}
    if scope is not None:  # manager: only own calls
        q += " AND s.metadata->>'employee' = :emp"; params["emp"] = scope
    rows = (await db.execute(text(q + " ORDER BY s.created_at"), params)).all()
    return [{"session_id": str(r[0]), "created_at": r[1], "count": r[2]} for r in rows]
```

Register in `main.py` (with the other routers):

```python
from app.routes import knowledge
app.include_router(knowledge.router)
```

> **Body-size cap (spec §3):** the ≤1 MiB import limit is enforced at the ASGI layer. If a global limit isn't already configured, add a small middleware in `main.py` that 413s when `Content-Length` on `/api/knowledge/import` exceeds `1_048_576`. (The `max_length=5000` on `ImportRequest.rows` already bounds row count.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_knowledge_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/knowledge.py backend/app/main.py backend/tests/test_knowledge_routes.py
git commit -m "feat(kb): /api/knowledge router (CRUD+import+mentions, RBAC+module-gate)"
```

---

### Task 14: Session KB tags + `kb_tag` filter + gate `/extraction`

**Files:**
- Modify: `backend/app/routes/sessions.py` — add `/{session_id}/tags`; add `kb_tag` param to `list_sessions`; gate `/extraction` (line 389) with `require_module('complexes')`
- Test: `backend/tests/test_session_kb_tags.py`, and extend `test_access_matrix.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_session_kb_tags.py
import asyncio
import pytest
from starlette.testclient import TestClient
from app.auth_sessions import SESSION_COOKIE


def _client(monkeypatch, fake_redis, modules):
    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True, "modules": modules}]
    async def fake_all(self): return rows
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def _viewer():
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions
    async def seed():
        t = set_tenant_schema("t_acme")
        try: return await auth_sessions.create_session("u", "v@t.io", "viewer")
        finally: reset_tenant_schema(t)
    return {SESSION_COOKIE: asyncio.run(seed())}


def test_extraction_403_when_complexes_off(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis, {"complexes": False})
    r = c.get("/api/sessions/00000000-0000-0000-0000-000000000001/extraction", cookies=_viewer())
    assert r.status_code == 403 and r.json()["detail"] == "module_disabled"


def test_tags_endpoint_requires_session(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis, {"knowledge_base": True})
    assert c.get("/api/sessions/00000000-0000-0000-0000-000000000001/tags").status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_session_kb_tags.py -v`
Expected: FAIL — `/extraction` returns 401 (not yet gated) and `/tags` is 404.

- [ ] **Step 3: Implement**

Gate `/extraction` (sessions.py:389) — add the module dep:

```python
@router.get("/{session_id}/extraction",
            dependencies=[Depends(require_session_access), Depends(require_module("complexes"))])
async def get_session_extraction(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
```

Add the tags endpoint (near the extraction one):

```python
@router.get("/{session_id}/tags", dependencies=[Depends(require_session_access)])
async def get_session_tags(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(text("""
        SELECT e.id, e.term, c.name, m.count
        FROM kb_entry_mentions m
        JOIN kb_entries e ON e.id = m.entry_id
        JOIN kb_categories c ON c.id = e.category_id
        WHERE m.session_id = :sid ORDER BY m.count DESC
    """), {"sid": session_id})).all()
    return [{"entry_id": str(r[0]), "term": r[1], "category": r[2], "count": r[3]} for r in rows]
```

Add `kb_tag` to `list_sessions` (sessions.py:42-71). Add the param and a subquery filter:

```python
    template_id: str | None = None,
    kb_tag: str | None = None,
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    ...
    if kb_tag:
        filters.append(Session.id.in_(
            select(text("session_id")).select_from(text("kb_entry_mentions"))
            .where(text("entry_id = :kbt")).params(kbt=kb_tag)
        ))
```

(`require_module` must be imported in `sessions.py`: `from app.modules import require_module`.)

Update `test_access_matrix.py`: the `VIEWER_GETS` entry `/api/sessions/.../extraction` now needs complexes ON in `ROWS` (already set in Task 5 Step 4 — realestate has complexes:true), so it stays 401 without a session. Confirm that test still passes.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_session_kb_tags.py tests/test_access_matrix.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/sessions.py backend/tests/test_session_kb_tags.py
git commit -m "feat(kb): session /tags + kb_tag filter; gate /extraction behind complexes"
```

**Phase 3 gate:** `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/ -q` → green.

---

# PHASE 4 — realestate seed (+ provisioning KB category)

### Task 15: Find-or-create "Термины" + one-shot realestate seed script

**Files:**
- Modify: `backend/app/provision_tenant.py` — add `_seed_kb_category(conn, schema)` called after `run_tenant` (line ~113)
- Create: `worker/scripts/seed_realestate_kb.py`
- Test: `backend/tests/test_provision_kb_category.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_provision_kb_category.py
import unittest.mock as mock
from app import provision_tenant


def test_seed_kb_category_find_or_create():
    executed = []

    class FakeCur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): executed.append(sql)
        def fetchone(self): return None  # not present → insert path

    class FakeConn:
        def cursor(self): return FakeCur()

    provision_tenant._seed_kb_category(FakeConn(), "t_acme")
    joined = "\n".join(executed)
    assert "kb_categories" in joined
    assert "ON CONFLICT (slug) DO NOTHING" in joined
    assert "Термины" in str(executed) or "termins" in joined.lower() or "terms" in joined.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_provision_kb_category.py -v`
Expected: FAIL — `_seed_kb_category` does not exist.

- [ ] **Step 3: Add `_seed_kb_category` and call it**

In `provision_tenant.py`:

```python
def _seed_kb_category(conn, schema: str) -> None:
    """Find-or-create the default 'Термины' KB category (feeds_asr + feeds_llm)."""
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {schema}.kb_categories (name, slug, feeds_asr, feeds_llm) "
            "VALUES ('Термины', 'terms', true, true) ON CONFLICT (slug) DO NOTHING"
        )
```

Call it in `provision()` right after `run_tenant(schema)` (line ~112):

```python
        run_tenant(schema)
        _seed_kb_category(conn, schema)
        _seed_admin(conn, schema, admin_email, password)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_provision_kb_category.py tests/test_provision_modules.py -v`
Expected: PASS.

- [ ] **Step 5: Write the one-shot seed script** (NOT a migration — would fan out to every schema)

```python
# worker/scripts/seed_realestate_kb.py
"""One-shot, idempotent: seed realestate's config word_boost (53 terms) into the
realestate tenant's KB 'Термины' category. Run once, post-deploy. Skips if the
category already has entries. The config word_boost remains a fallback.

  python -m scripts.seed_realestate_kb   (cwd: worker/)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root → tenancy

from tenancy.db import tenant_connect          # noqa: E402
from tenancy.context import set_tenant_schema  # noqa: E402

COMPANIES = Path(os.getenv("COMPANIES_PATH", Path(__file__).resolve().parents[2] / "companies"))


def main() -> None:
    cfg = json.loads((COMPANIES / "realestate.json").read_text())
    word_boost = (cfg.get("asr") or {}).get("word_boost") or []
    if not word_boost:
        print("no word_boost in realestate.json; nothing to seed"); return

    token = set_tenant_schema("t_realestate")
    try:
        conn = tenant_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO kb_categories (name, slug, feeds_asr, feeds_llm) "
                            "VALUES ('Термины','terms',true,true) ON CONFLICT (slug) DO NOTHING")
                cur.execute("SELECT id FROM kb_categories WHERE slug='terms'")
                cat_id = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM kb_entries WHERE category_id=%s", (cat_id,))
                if cur.fetchone()[0] > 0:
                    print("'Термины' already populated; skipping (idempotent)"); return
                for term in word_boost:
                    cur.execute(
                        "INSERT INTO kb_entries (category_id, term) VALUES (%s, %s) "
                        "ON CONFLICT (category_id, term) DO NOTHING", (cat_id, term))
            conn.commit()
            print(f"seeded {len(word_boost)} realestate terms into KB 'Термины'")
        finally:
            conn.close()
    finally:
        from tenancy.context import reset_tenant_schema
        reset_tenant_schema(token)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/provision_tenant.py worker/scripts/seed_realestate_kb.py backend/tests/test_provision_kb_category.py
git commit -m "feat(kb): provision seeds 'Термины'; one-shot realestate word_boost seed script"
```

---

# PHASE 5 — Dashboard (edit + manual/curl verify; no JS test runner)

> There is no JS test harness in this repo (vanilla JS, no bundler). Verify each task by serving the SPA and checking behavior; the underlying APIs are already covered by Python tests. After each task: `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/ -q` must still be green (no Python regressions).

### Task 16: Fetch features + gate nav

**Files:**
- Modify: `backend/static/index.html` (nav items: add `#knowledge`, tag RE items with `data-module`)
- Modify: `backend/static/app.js` (`bootAuth` fetches features; hide off-module nav)

- [ ] **Step 1:** In `index.html`, add `data-module="complexes"` to the «База ЖК» `<li>` (line 42) and add a Knowledge nav item before it:

```html
      <li><a href="#knowledge" data-route="knowledge" class="nav-link" data-module="knowledge_base">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 3h6a4 4 0 014 4v14a3 3 0 00-3-3H2z"/><path d="M22 3h-6a4 4 0 00-4 4v14a3 3 0 013-3h7z"/></svg>
        <span>База знаний</span>
      </a></li>
      <li><a href="#complexes" data-route="complexes" class="nav-link" data-module="complexes">
```

- [ ] **Step 2:** In `app.js`, add a `features` global and load it in `bootAuth` (after `currentUser = await api('/api/user-auth/me')`, line 88):

```javascript
let features = {};
// inside bootAuth(), after currentUser is set:
    try { features = await api('/api/tenancy/features'); } catch (e) { features = {}; }
    document.querySelectorAll('[data-module]').forEach(el => {
      const li = el.closest('li') || el;
      if (features[el.dataset.module] === false) li.style.display = 'none';
    });
```

Add a `moduleOn` helper near `isAdmin` (line 16):

```javascript
const moduleOn = (name) => features[name] !== false;  // default-on unless explicitly false
```

- [ ] **Step 3: Manual verify**

Serve the app (`cd backend && /root/projects/silentqa/.venv/bin/uvicorn app.main:app --port 8002` against a tenant whose `complexes` is off) and confirm in the browser: «База знаний» appears; «База ЖК» is hidden. For realestate (complexes on) both show.

- [ ] **Step 4: Commit**

```bash
git add backend/static/index.html backend/static/app.js
git commit -m "feat(kb-ui): fetch /api/tenancy/features; gate nav by module"
```

---

### Task 17: `#knowledge` page

**Files:**
- Modify: `backend/static/app.js` (`router()` route + `renderKnowledge()`)

- [ ] **Step 1:** Add the route in `router()` (after the `complexes` branch, ~line 66):

```javascript
  } else if (route === 'knowledge') {
    await renderKnowledge();
```

- [ ] **Step 2:** Add `renderKnowledge()` modeled on `renderTemplates()` (line 1833): fetch `GET /api/knowledge/categories?include_entries=true`, render categories with their `feeds_asr/feeds_llm/is_taxonomy` toggles and entries (`term`, `aliases`, `description`); admin-only CRUD forms (guard with `isAdmin()`); an "Импорт списком" textarea that POSTs `/api/knowledge/import`. On an entry card, fetch `/api/knowledge/entries/{id}/mentions` and render the count + a simple trend (group `created_at` by day). Use the existing `api()` wrapper and `escapeHtml`. Keep markup consistent with `renderTemplates`.

- [ ] **Step 3: Manual verify**: navigate to `#knowledge` as admin → create a category + entry + import a list; as viewer → forms hidden, list visible.

- [ ] **Step 4: Commit**

```bash
git add backend/static/app.js
git commit -m "feat(kb-ui): #knowledge page (categories/entries CRUD + import + mentions)"
```

---

### Task 18: Call-card KB tags + RE-leak guards

**Files:**
- Modify: `backend/static/app.js` (`renderCallDetail`, line 450-558; upload hint line 1423)

- [ ] **Step 1:** Guard the extraction fetch (line 463) so non-complexes tenants don't even call the now-403 endpoint:

```javascript
    let extraction = null;
    if (moduleOn('complexes')) { try { extraction = await api(`/api/sessions/${id}/extraction`); } catch {} }
```

- [ ] **Step 2:** Add a KB-tags section. After the extraction section block (line 558), fetch and render tags when `knowledge_base` is on:

```javascript
    let kbTags = [];
    if (moduleOn('knowledge_base')) { try { kbTags = await api(`/api/sessions/${id}/tags`); } catch {} }
    if (kbTags.length) {
      html += `<div class="kb-tags-section"><h2>Теги базы знаний</h2>` +
        kbTags.map(t => `<span class="badge">${escapeHtml(t.term)} · ${escapeHtml(t.category)} (${t.count})</span>`).join(' ') +
        `</div>`;
    }
```

- [ ] **Step 3:** Gate the upload hint «Презентация ЖК» (line 1423) behind `moduleOn('complexes')` (wrap the `<p>` so it's only emitted when complexes is on).

- [ ] **Step 4: Manual verify**: for a KB-tagged session, tags render; for a complexes-off tenant, no extraction call/section and no «Презентация ЖК» hint.

- [ ] **Step 5: Commit**

```bash
git add backend/static/app.js
git commit -m "feat(kb-ui): call-card KB tags; gate complexes-only UI"
```

---

### Task 19: Tenant-configurable AmoCRM link

**Files:**
- Modify: `backend/static/app.js` (lines 2275, 2322 — hardcoded `rogovestate.amocrm.ru`)

- [ ] **Step 1:** Gate the AmoCRM deal links behind `moduleOn('amocrm')` and stop hardcoding the subdomain. Minimal correct change: render the AmoCRM link only when `moduleOn('amocrm')`, and source the base from a tenant-provided value. If `/api/tenancy/features` is extended later to include an `amocrm_subdomain`, read it; for now, when `amocrm` is off, omit the link entirely (realestate keeps it because its module is on — but to fully de-hardcode, replace the literal with a `currentAmoBase` constant defaulting to `rogovestate.amocrm.ru` and only used when `moduleOn('amocrm')`).

```javascript
const amoBase = (features.amocrm_subdomain) || 'rogovestate.amocrm.ru';
// line 2275 / 2322: only emit when moduleOn('amocrm'):
${moduleOn('amocrm') ? `<a href="https://${amoBase}/leads/detail/${leadId}" ...>` : ''}
```

> Spec §4.3 wants the base tenant-configurable. Full configurability (adding `amocrm_subdomain` to the tenant row + `/features`) is a small follow-up; this task at minimum **gates** the link by module so non-amocrm tenants never see the Rogov link. If you add the config field, do it as a shared-track column + extend the `/features` payload.

- [ ] **Step 2: Manual verify**: amocrm-off tenant shows no AmoCRM link; realestate unchanged.

- [ ] **Step 3: Commit**

```bash
git add backend/static/app.js
git commit -m "feat(kb-ui): gate AmoCRM deal links by module; de-hardcode base"
```

---

## Final verification

- [ ] `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/ -q` → all green (expect ~172 + new tests).
- [ ] `cd worker && /root/projects/silentqa/.venv/bin/python -m pytest -q` → green (the 4 `test_complex_match` errors persist only if `DATABASE_URL_SYNC` is unset; new KB unit tests pass).
- [ ] `cd backend && /root/projects/silentqa/.venv/bin/alembic -c alembic_shared.ini heads` → `s004`; `... -c alembic.ini -x tenant_schema=t_realestate heads` → `014` (where a DB is available).
- [ ] **Deploy order (spec §10):** restart **backend first** (runs `python -m app.migrate` → applies `s004` + `014`), confirm `alembic heads`, **then** restart the worker. Run `python -m scripts.seed_realestate_kb` once afterward. Fallbacks in `knowledge_base.py` cover the brief window before the worker sees the new tables.

---

## Self-Review (completed against spec)

**Spec coverage:** §1 data model → Task 7. §2 pipeline (normalize/keyterms/glossary/mentions, fallbacks, reprocess-safe, extraction reads normalized disk transcript) → Tasks 8–11. §3 API → Tasks 12–14. §4 dashboard (KB page, tags, RE-leak gating) → Tasks 16–19. §5 module flags (storage, /features, backend gate, defaults, provisioning) → Tasks 1–6. §6 migrations/seed → Tasks 1, 7, 15. §7 testing → tests in every task. §8 no-deletion → only ADD/gating, downgrades drop only new objects. §10 deploy order → Final verification.

**Known follow-ups (spec §9, deliberately deferred):** `_DEFAULT_DESKTOP_TEMPLATE_NAME` auto-apply gating (Task 18 covers the upload hint; the desktop auto-template seed gating in `sessions.py:159-162` + migration 008 is a small add — gate `_resolve_default_desktop_template_id` behind `module_enabled(...,'complexes')` if not done in Task 14); full `amocrm_subdomain` config (Task 19 gates but doesn't yet add the config column); Aho-Corasick swap; LLM tagging; retro mention backfill.

**Placeholder scan:** no TBD/TODO/"handle errors"; every code step carries real code; commands are concrete with the repo's venv interpreter.

**Type/name consistency:** `module_enabled`/`MODULE_DEFAULTS`/`require_module` consistent across Tasks 3–6, 13–14; `build_matcher`/`normalize_transcript`/`cap_keyterms`/`format_glossary` + wrappers `kb_build_matcher`/`kb_keyterms`/`kb_glossary`/`kb_record_mentions` consistent across Tasks 8–11; `glossary` param name consistent across quality/extract/pipeline; migration ids `s004`/`014` consistent.
