# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**SilentQA** — a multi-tenant SaaS platform that records sales/support calls and meetings (Chrome/Yandex extension, Electron desktop app, dashboard file-upload, or AmoCRM polling), runs them through a transcription → diarization → sentiment → LLM-quality-scoring pipeline, and shows results in a per-tenant dashboard. It is the **same QA engine as the older single-tenant `realestate` app, plus multi-tenancy** (schema-per-tenant, platform admin, user auth + roles, tenant provisioning). Active branch: `multi-tenant-core-phase1`. The clean GitHub remote is `Chechetov/silentqa-platform`; production runs from a sibling checkout (`/root/projects/silentqa`, port 8007).

Most docs, comments, commit messages, and dashboard UI are in **Russian** — match that when editing existing files.

> `INSTRUCTIONS.md` and `docs/architecture/system-overview.md` describe the engine/pipeline accurately but are **realestate-era**: branding (`rogov.automate-it.fun`), Basic-auth creds, and the AmoCRM-centric framing predate the SilentQA multi-tenant work. Treat them as product/pipeline reference, not as current deployment truth.

## Components

| Path | What | Run as |
|---|---|---|
| `backend/` | FastAPI API + serves the SPAs | `app.main:app` on `:8002` |
| `worker/` | Celery worker + Beat (the whole pipeline) | `tasks.celery_app` |
| `tenancy/` | **Shared** tenant-core package, imported by *both* backend and worker | — |
| `backend/static/` | Vanilla-JS SPAs (dashboard, platform admin, download page) — **no build step** | served by FastAPI |
| `extension/`, `extension-yandex/` | Chrome MV3 tab+mic recorders (yandex is byte-identical + a README) | loaded unpacked |
| `desktop-app/` | Electron recorder (system audio + mic) | npm scripts |
| `companies/` | Per-company JSON config (ASR/word-boost/criteria/prompt) | file config |
| `docs/` | Specs (`docs/superpowers/`), architecture, repo landscape map |

## Commands

Prereqs (per `run.sh`): PostgreSQL, Redis, ffmpeg, Python 3.12+, a venv. Local defaults: Postgres `localhost:5434`, Redis `localhost:6381` (non-standard ports). Copy `.env.example` → `.env`.

```bash
# Install (separate dependency sets)
pip install -r backend/requirements.txt
pip install -r worker/requirements.txt

# Run everything locally (sets DATABASE_URL/REDIS_URL/storage defaults, creates data dirs)
./run.sh all          # backend :8002 + worker, backgrounded
./run.sh backend      # API only
./run.sh worker       # Celery worker only
./run.sh stop

# Run pieces by hand
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8002
cd worker  && celery -A tasks.celery_app worker --loglevel=info --concurrency=1 --max-tasks-per-child=10 -Q default,transcription,analysis
cd worker  && celery -A tasks.celery_app beat     # periodic AmoCRM polling / sweeps
```

### Tests

Tests are **pure unit tests** (Starlette `TestClient` + a `FakeRedis` fixture + monkeypatched tenant registry / `get_db` override) — **no Postgres or Redis required**. **The working directory matters** (imports rely on it):

```bash
cd backend && python -m pytest tests/                                  # all backend tests
cd backend && python -m pytest tests/test_impersonation.py::test_token_single_use   # single

cd worker  && python -m pytest                                         # all worker tests (pytest.ini sets pythonpath=.)
cd worker  && python -m pytest tests/test_quality_schemas.py::<name>   # single
```

### Migrations & tenant provisioning (run from `backend/`)

There are **two independent Alembic trees** — this is the single most important thing to understand before touching the DB:

```bash
# SHARED / platform schema (shared.tenants, shared.platform_admins) — raw-SQL migrations, sync engine
alembic -c alembic_shared.ini revision -m "msg"
alembic -c alembic_shared.ini upgrade head

# PER-TENANT schema template (sessions, chunks, complexes, users, …) — async engine, applied per schema
alembic -c alembic.ini revision -m "msg"
alembic -c alembic.ini -x tenant_schema=t_acme upgrade head

# Canonical runner: shared first, then EVERY active tenant. Idempotent. A deploy step (run.sh / deploy_prod.sh / staging ExecStartPre) — backend startup only runs --check (see below).
python -m app.migrate
python -m app.migrate --check   # read-only: какие схемы отстают от head (exit 1 при дрейфе)

# Provision a new tenant: CREATE SCHEMA t_<slug>, INSERT shared.tenants, migrate it, seed admin (argon2), mint sqa_ API key
python -m app.provision_tenant <slug> --name "Acme" --admin-email a@b.com
python -m app.provision_tenant realestate --seed-only      # for the pre-existing tenant

python -m app.platform_admin   # bootstrap a platform (cross-tenant) admin
```

Backend startup no longer migrates: the lifespan only runs a read-only
`python -m app.migrate --check` head-comparison and logs CRITICAL on drift
(it does not crash). Migrations are a deploy step: `run.sh` (local),
`scripts/deploy_prod.sh` (prod), staging unit's `ExecStartPre`.

### Desktop app (`cd desktop-app`)

```bash
npm install
npm start                 # dev (electron .)
npm run build:win         # NSIS .exe   | build:mac (.dmg) | build:linux (AppImage+deb)
npm run build             # win + linux  | build:all = win+mac+linux
```

## Architecture: the multi-tenant core

**Schema-per-tenant Postgres.** Three schema kinds: `public` (legacy ENUM types), `shared` (platform registry: `tenants`, `platform_admins`), and one `t_<slug>` per tenant (all business tables). Tenant context flows through a **contextvar** that scopes both the DB `search_path` and storage paths.

**Request flow (HTTP):** `TenantResolutionMiddleware` (`backend/app/tenancy_http.py:60`, outermost middleware) reads the `Host` header and resolves it (`_resolve`, `tenancy_http.py:106`):

1. exact match in a tenant's `custom_domains` → that tenant
2. `<slug>.BASE_DOMAIN` → tenant by slug
3. apex or `admin.BASE_DOMAIN` → **platform contour** (no tenant)
4. otherwise → `DEFAULT_TENANT` if set, else platform

Unknown slug → 404; suspended tenant → 403. The registry is a 30s TTL cache of `shared.tenants` (`.invalidate()` after platform mutations). The middleware calls `set_tenant_schema(schema)` (contextvar) for the duration of the request. The async engine then issues `SET LOCAL search_path TO <schema>, shared, public` per transaction (`backend/app/database.py`). `GET /` serves `admin.html` on the platform contour and `index.html` on a tenant contour; `StaticFiles` is mounted **last** so it never shadows `/api/*`.

Стек middleware: Tenant (внешний) → CORS → `BodySizeLimitMiddleware` (413 по Content-Length > `MAX_REQUEST_BODY_MB`, дефолт 600 МБ; пер-роутные капы ингеста — `MAX_CHUNK_UPLOAD_MB`/`MAX_AUDIO_UPLOAD_MB` в `routes/chunks.py`; внешний слой — `request_body 600MB` в Caddy). Swagger (`/docs`,`/redoc`,`/openapi.json`) **выключен по умолчанию** — включается `ENABLE_API_DOCS=1` (run.sh ставит его для локали; на проде не задан).

**Contour gating** (`tenancy_http.py:96-105`): `PLATFORM_PREFIXES = /api/platform, /api/companies, /api/tenancy` are platform-only; tenant API paths 404 on the platform contour and vice-versa, *before* auth runs. **Exception:** `/api/tenancy` (the tenant-check endpoint, `routes/tenancy_check.py`) is explicitly exempted from the gate (`tenancy_http.py:102`) and is reachable on **both** contours. `DEFAULT_TENANT=` (empty) means localhost/IP requests hit the platform contour — set it to a slug for tenant-side local dev.

**The `tenancy/` package** (repo root, on `sys.path` in both services) is the only sanctioned DB-access layer:
- `context.py` — the contextvar (`set/reset/require_tenant_schema`, `get_tenant_slug`). `None` = platform.
- `db.py` — sync connections (`tenant_connect`/`shared_connect`/`tenant_engine`) that bake `search_path` into libpq `options`. **Raw `psycopg2.connect` is forbidden and enforced by a test** (`worker/tests/test_no_raw_connects.py`).
- `identifiers.py` — slug rules `^[a-z][a-z0-9_]{1,30}$`, `schema = t_<slug>`, reserved-slug blocking, `validate_schema_name` (last guard before interpolation). Schema names are **never** built from the Host header.
- `paths.py` — per-tenant storage layout `<root>/<slug>/sessions|amocrm/...`.
- `registry.py` — worker-side sync reader of `shared.tenants`.

### Auth layers (several — know which applies where)

| Layer | Where | Mechanism |
|---|---|---|
| Tenant **dashboard** users | `auth_user.py` (`get_current_user`/`require_viewer`/`require_admin`), login `routes/user_auth.py` | cookie `sqa_session`, Redis `t:{slug}:sess:{sid}`, argon2 |
| **Platform** admins | `auth_platform.py` (`require_platform_admin`), login `routes/platform_auth.py` | cookie `sqa_admin`, Redis `platform:sess:*` |
| **Recorder/extension brokers** | `auth_jwt.py` (`get_current_broker`) | HS256 JWT, 30-day, header `X-Broker-Token`; `check_tenant_claim` rejects cross-tenant tokens (legacy tokenless tolerated as `realestate` only until `BROKER_JWT_TENANT_GRACE_UNTIL`) |
| **Ingestion** (chunk upload) | `auth_user.py:require_ingestion_auth` | valid session **or** tenant `X-API-Key` (sha256, constant-time); tokenless allowed only while `tenants.api_key_required=false` |
| **AmoCRM module** (`/api/amocrm/*`) | `auth_user.py:require_amocrm_tenant` | `require_admin` **and** tenant `modules.amocrm` enabled (else 403 `module_disabled`) |
| **Impersonation** | platform → tenant | one-time `secrets` token in Redis `platform:imp:{token}` (TTL 60s, single-use `getdel`), exchanged for a short admin session |

RBAC roles are `admin` / `viewer` / `manager`; `manager` sees only its own calls (matched on `session.metadata.employee`, see `auth_user.py` `employee_scope` / `require_session_access`). Legacy HTTP Basic exists only in a docstring (historically terminated at Caddy) — there is no Basic dependency in current backend code.

**Module gating.** Each tenant row carries a JSON `modules` dict (seeded `{"knowledge_base": true, "complexes": false, "amocrm": false}` in `provision_tenant.py`). The registry stashes it on `request.state.tenant`; `app.modules.module_enabled` / `require_amocrm_tenant` enforce it server-side, `data-module` nav attributes gate it in the dashboard, and `tenancy/registry.py:tenant_amocrm_enabled` gates AmoCRM Beat polling. This JSON flag — **not** a hardcoded slug list — is how AmoCRM (and complexes/KB) stay per-tenant opt-in.

### DB engines

- **Async** (`postgresql+asyncpg`, `DATABASE_URL`) — the FastAPI request path; one engine + `async_sessionmaker` in `backend/app/database.py`, `get_db` dependency.
- **Sync** (`postgresql+psycopg2`, `DATABASE_URL_SYNC`) — worker, migrations, registry reads, all via `tenancy/db.py`.

ORM models (`backend/app/models.py`: `Session, Chunk, ExtractionTemplate, Complex, ComplexExtraction, Broker`) are **tenant-schema** tables. Shared/registry tables are managed by raw SQL + the `alembic_shared` tree, not the ORM. Quality reports are stored as JSON files on disk, **not** in the DB.

## Architecture: the processing pipeline (`worker/`)

Celery app `tasks.celery_app` (`Celery("voiceqa")`), Redis broker+backend, **three queues** (`default`, `transcription`, `analysis`). Tasks are registered via an explicit `include=[...]` list (no autodiscovery) with explicit names. Beat schedules `poll_amocrm_calls` (5m), `sweep_stuck_sessions` (5m), `reconcile_amocrm_calls` (30m, `AMOCRM_RECONCILE_INTERVAL_SEC`). In prod the two heavy queues run in **separate worker units** (`silentqa-worker` for `transcription`, `silentqa-worker-io` for `analysis`).

The pipeline is a **two-stage Celery handoff** (not a single monolith, and not a `chain`): a CPU stage on the `transcription` queue hands off to an IO/LLM stage on the `analysis` queue via an explicit `send_task`. Backend enqueues `celery_app.send_task("pipeline.process_session", queue="transcription", kwargs={"tenant_schema": ...})` (`backend/app/routes/sessions.py`). Entry points `process_session` (browser/WebM chunks) and `process_session_from_file` (uploaded) run **stage 1** (`_run_stage1`, `worker/tasks/pipeline.py`), which ends by enqueuing `pipeline.analyze_session` on the `analysis` queue; `analyze_session` runs **stage 2** (`_analyze_inner`, `acks_late=True` for at-least-once). The AmoCRM-poll path (`process_amocrm_call`) still runs both stages synchronously in-process via `_run_pipeline` (no handoff).

```
STAGE 1  (transcription queue, CPU):
  merge chunks (ffmpeg → 16k mono WAV) → [short-call <20s gate] → [broken-recording silence gate]
  → transcribe → diarize (skipped if ASR already returned speakers) → merge transcript+speakers
  → save transcript.json → send_task("pipeline.analyze_session", queue="analysis")
STAGE 2  (analysis queue, IO/LLM):
  knowledge-base normalize → sentiment → quality (LLM) → card/plan extraction
  → save results + mark completed   [+ AmoCRM writeback for realestate]
```

Стадия 2 и оба гейта пишут денормализованную строку в тенант-таблицу `quality_results` (`worker/tasks/results_db.py`, best-effort — сбой записи не фейлит пайплайн); рисковые звонки шлют Telegram-алерт (`worker/tasks/alerts.py`, env `TELEGRAM_BOT_TOKEN` + `RISK_ALERT_CHAT_ID`/company-config `alerts.telegram_chat_id`). Формы данных: `speaker_roles` в отчёте — `{id: {role, name}}`, версия отчёта — ключ `score_version`.

Pluggable engines:
- **ASR** — `transcribe.py:transcribe_audio(engine_override=...)`. **Three** pluggable engines behind an ordered fallback chain: `transcribe_audio` builds `chain = [ASR_ENGINE] + [elevenlabs, assemblyai, whisper]` (dupes removed) and tries each until one succeeds. **ElevenLabs Scribe v2** (`ELEVENLABS_API_KEY`, built-in diarization) and **AssemblyAI** (`ASSEMBLYAI_API_KEY`, `word_boost`) are the cloud engines actually used — set `ASR_ENGINE` to one of them to make it primary (`.env.example` uses `assemblyai`). ⚠️ **Local faster-whisper (CPU) is a last-resort fallback only** — it sits at the tail of the chain, is effectively *never exercised in practice*, produces noticeably worse quality, and CPU-level ASR is being phased out; treat any run that actually falls through to whisper as a red flag worth surfacing. The code default `ASR_ENGINE=whisper` is misleading — real deployments set a cloud engine. `fallback=False` (ASR-tuning/comparison, `transcribe_compare.py`) forces exactly the requested engine with no fallthrough.
- **Diarization** — pyannote `speaker-diarization-3.1` (`HF_TOKEN`).
- **Sentiment** — `seara/rubert-tiny2-russian-sentiment`.
- **Quality** — OpenAI **GPT-5.4** `client.responses.create(...)` structured output (`quality.py`, `OPENAI_API_KEY`). v2 (5 base criteria) vs v4 (extended, with sales-playbook meeting-argumentation) is chosen by whether the company scenario has a custom `prompt` (`use_extended = bool(scenario and scenario.get("prompt"))`). Все structured-вызовы (quality/plan/card/extract + backend `eval_prompt_rewrite`) несут `max_output_tokens` и `prompt_cache_key` (стоимость, D-1.1); требуется `openai>=2.44`. Инвариант фронта: пользовательский текст никогда не интерполируется в inline-`onclick` — только `data-*`-атрибуты + `this` (quote-safe `escapeHtml` не спасает JS-контекст).

**Every task requires a `tenant_schema` kwarg**; it sets the contextvar on entry and resets it in `finally` (missing schema → `ValueError`, fail-fast). Per-tenant storage roots come from `AUDIO_STORAGE_PATH` / `RESULTS_STORAGE_PATH`. Per-tenant **fairness slots** (`tasks/tenant_slots.py`, `TENANT_MAX_CONCURRENT`) throttle heavy tasks per tenant (a task self-retries when no slot is free). `worker/scripts/` holds ops tools (`reassess_quality.py`, `compare_reassess.py`, `backfill_amocrm_push.py`, `backfill_quality_results.py`, `sync_brokers.py`, `seed_realestate_kb.py`, `cleanup_zoom_template.py`) — not Celery tasks.

## Per-company config (`companies/`)

`companies/<id>.json` is **file config, not DB data** — it parameterizes a domain (ASR engine + `word_boost`, evaluation `protocol`/`criteria`, custom `prompt`, call `scenarios`). A tenant points at one via `shared.tenants.company_config_id` → filename. Worker resolves it through `worker/tasks/company_config.py` (`tenant_company_config_id()` → `load_company_config()`, falling back to `default.json`). The backend edits these JSON files directly via `routes/companies.py` (platform-admin only; `default.json` is undeletable). Loaded from `COMPANIES_PATH` (default `/companies`). Present configs: `default.json` (intentionally minimal → v2 scoring), `realestate.json` (rich domain example → v4 scoring), plus `chechetov.json` and `dental.json` (generic `card_extraction` / `default_scenario_id` blocks — the config-driven "карта приёма" that replaces hardcoded real-estate extraction).

## Client surfaces (`backend/static/` + apps)

No bundler anywhere — raw HTML/JS/CSS served statically. Dashboard `index.html`+`app.js` is a hash-router SPA (`#dashboard #calls #managers #upload #companies #templates #complexes #knowledge #reprocess #evaluation #team #profile`; nav items are feature-gated by `data-module` / `data-admin-only` — e.g. `#knowledge` "База знаний" and `#complexes` "База ЖК" appear per `tenants.modules`); **`#upload` is file-upload only — there is no live recorder in the dashboard**, live capture is the extension/desktop app. `#dashboard` — дашборд руководителя (`/api/stats/*` поверх `quality_results`); графики — `static/charts.js` (рукописный SVG, node-тестируется). Platform admin is `admin.html`+`admin.js` → `/api/platform/*`. All recorders use the same chunk protocol: `POST /api/sessions` → repeated `POST /api/sessions/{id}/chunks` (10-second WebM/Opus) → `POST /api/sessions/{id}/finish`. Built recorder artifacts are dropped into `static/download/dl/` (not committed) and surfaced by the `/download` page.

## Gotchas & known drift

- **The extension is already on the SilentQA model** (this is a *corrected* gotcha — it no longer hardcodes Rogov): `manifest.json` host is `*.silentqa.com`, the server URL is user-entered, auth is `X-API-Key` (`sqa_`) only, and it actively purges legacy Basic creds (`extension/popup.js`). The **desktop app** still supports **both** `Authorization: Basic` (realestate broker mode) and `X-API-Key` (silentqa/fulldent mode). Stale Rogov branding remnants linger in `extension/mic-permission.html`, `extension-yandex/README.md` (still says v1.3.0 / `rogov.automate-it.fun`), and desktop `package.json` `author`. The ingestion endpoint itself accepts both `Authorization: Basic` and `X-API-Key`.
- **AmoCRM integration is realestate-only** in Phase 1 (Beat polling, CRM writeback) — gated by the tenant `modules.amocrm` flag (default off), enforced at `require_amocrm_tenant` and `tenancy/registry.py:tenant_amocrm_enabled`, **not** a hardcoded slug list. Don't assume it applies to new tenants.
- When adding a table, write the migration in the **tenant** tree (`alembic.ini`) unless it's genuinely platform-wide (then `alembic_shared.ini`), and apply it with `python -m app.migrate` so all tenants get it.

## Where to read more

- `docs/2026-06-16-repo-landscape-map.md` — map of *all* related repos/checkouts on this box (realestate, meet, silentqa, dental, fillers, PBN…) and where work-in-progress risk lives. Read this if confused about which checkout is which.
- `docs/superpowers/specs/` & `docs/superpowers/plans/` — design specs and execution plans. **Their status headers are stale:** **unify-recorder-identity** (`2026-06-14`, universal user/manager identity + `employee_name` attribution) and **universal-knowledge-base** (`2026-06-16`, generic per-tenant knowledge base replacing hardcoded "База ЖК"/complexes) have **substantially landed** in this branch (migrations `012_users`, `013_manager_role_employee_name`, `014_knowledge_base`; `routes/knowledge.py`; `app/modules.py`; the `#knowledge` page). Remaining work is residual real-estate-string cleanup behind module flags and retiring the legacy `Broker` / `get_current_broker` path — not the features themselves. The newer `docs/superpowers/plans/2026-07-02-techdebt-*.md` accurately describe the current **two-stage pipeline**; `docs/loop-progress-universal-kb.md` is the live tracker. Phase 2 = per-tenant integration config (telephony/CRM beyond AmoCRM).
- `docs/architecture/ru-relay.md` — production serving for Russian users routes through external VPS + nginx + gost + residential SOCKS5 (Hetzner traffic is shaped from RU). That's deployment infra outside this repo, not code edited here.
