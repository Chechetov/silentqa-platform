# Multi-Tenant Auth (Phase 1 / Plan 2) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Включить пер-тенант авторизацию: email+пароль логин с Redis-сессиями (`/api/user-auth/*`), роли admin/viewer, enforcement пер-тенант API-ключей с двойной авторизацией ingestion-эндпоинтов, tenant-claim в брокерских JWT с grace-окном, матрица доступа 5.6 (закрытие `/api/companies`, удаление `/api/webhooks`, server-owned `company_id`/`scenario_id`, гейт AmoCRM-push по тенанту), снятие Basic Auth и `DELETE_PASSWORD`, SPA-логин, `X-API-Key` в recorder-клиентах.

**Architecture:** Spec: `docs/superpowers/specs/2026-06-10-multi-tenant-core-design.md` разделы 5, 5.5–5.6, 6.1, 6.4, 7, 9. Новые модули backend: `redis_client.py` (async Redis), `auth_sessions.py` (Redis-сессии `t:{slug}:sess:{sid}` + rate-limit логина), `auth_user.py` (dependencies: `get_current_user`/`require_viewer`/`require_admin`/`require_ingestion_auth`/`require_amocrm_tenant`), `routes/user_auth.py`. `TenantResolutionMiddleware` дополнительно кладёт строку тенанта в `scope["state"]["tenant"]` (доступна как `request.state.tenant`). Матрица доступа применяется decorator-level `dependencies=[...]` на каждом эндпоинте. Worker: гейт push по `AMOCRM_TENANT_SLUGS`, `company_config_id` из `shared.tenants`, пер-тенант `dashboard_base_url`.

**Tech Stack:** FastAPI 0.115/Starlette 0.41, redis-py 5.2 (`redis.asyncio`), argon2-cffi (юзеры; брокеры остаются на passlib/bcrypt), python-jose (JWT), pytest + Starlette TestClient (без context manager!), ванильный JS SPA, Electron desktop-app, MV3-расширения.

**Контекст выполнения:** рабочая копия — git worktree `/root/projects/meet-mt` (ветка `multi-tenant-core-phase1`), `.venv`/`.env` — симлинки. Прод-чекаут `/root/projects/meet` НЕ трогать. Тесты: `cd /root/projects/meet-mt/worker && set -a; source ../.env; set +a; ../.venv/bin/python -m pytest` (без `.env` — 4 пре-существующих error в `test_complex_match.py`, это норма) и `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests`.

**Жёсткие инварианты (выверены по коду ветки):**
1. **`TestClient(app)` использовать ТОЛЬКО без context manager** (`client = TestClient(app); client.get(...)`). `with TestClient(...)` запускает lifespan → `python -m app.migrate` → реальная БД. Существующие тесты следуют этому правилу.
2. **Auth-зависимости падают ДО `get_db`-запросов** — соединение SQLAlchemy ленивое, поэтому negative-тесты (401/403) против реального `app.main.app` БД не требуют. Positive-пути с БД тестируются в мини-приложениях с подменёнными зависимостями.
3. **`/api/auth/*` (брокерский контур) остаётся открытым** (спека 5.6) — не вешать на него `require_viewer`. На него добавляется только tenant-claim логика.
4. **CORS остаётся `allow_origins=["*"]` БЕЗ `allow_credentials=True`** — cookie защищена host-only-скоупом + SameSite=Lax + тем, что браузер не шлёт cookie cross-origin без credentials; extension-клиенты ходят с `X-API-Key` без cookies (спека 5.2).
5. **`AUTH_USERNAME`/`AUTH_PASSWORD` НЕ удалять** — они остаются как interim-защита `/api/companies` на платформенном контуре до Plan 3. Удаляется только `DELETE_PASSWORD` (+ `BasicAuthMiddleware`).
6. **Снятие Basic/X-Delete-Password (Task 15) идёт ПОСЛЕ применения матрицы (Tasks 7–12)**; ветка X-Delete-Password в middleware для PROTECTED_PREFIXES снимается раньше — в Task 9, сразу после того как роуты templates/complexes/extractions получают require_admin (иначе middleware отвечает 503/401 до роут-зависимостей и матричные тесты не позеленеют) — на промежуточных коммитах защита не опускается ниже текущей.
7. **Worker module-globals не убирать** (`DASHBOARD_BASE_URL`, `AUDIO_PATH`, `RESULTS_PATH` монкипатчатся тестами) — новые хелперы добавляются ПОВЕРХ, глобалы остаются.
8. **`portal.amocrm_tokens` и `amocrm_sync._read_token_from_portal_db` не трогать** (инвариант Plan 1).
9. **Брокерская таблица и таблица `users` — в схеме тенанта**; на платформенном контуре (`get_tenant_slug() is None`) login/claim эндпоинты обязаны отвечать 404 ДО любого SQL (иначе 500 на отсутствующих public-таблицах).
10. Пароли юзеров — **argon2** (`argon2-cffi`, как сеет `provision_tenant.py`); брокеры — **bcrypt** (passlib) — не смешивать.
11. Cookie: **host-only** (НЕ задавать `Domain=`), `HttpOnly`, `Secure` из `settings.SESSION_COOKIE_SECURE`, `SameSite=Lax`, `path=/`. В тестах `base_url="https://realestate.silentqa.com"` (https — иначе httpx не вернёт Secure-cookie).
12. **Backend-тесты гоняются БЕЗ .env**, а `app/auth_jwt.py` fail-loud падает на импорте при плейсхолдерном SECRET_KEY — поэтому `os.environ.setdefault("SECRET_KEY", ...)` обязан стоять в начале `backend/tests/conftest.py` (Task 1) ДО импорта любых `app.*`. НЕ сорсить прод-.env в backend-тестах (затянет прод DATABASE_URL/DELETE_PASSWORD).
13. **`asyncio.get_event_loop()` в тестах запрещён** (py3.12: после первого anyio-теста кидает RuntimeError) — только `asyncio.run(...)`. Per-request `cookies=` в httpx 0.28 — deprecated, но работает; при будущем апгрейде заменить на `client.cookies.set(...)`.
14. В матричных тестах реальное приложение получает фейковый реестр через `monkeypatch.setattr(TenantRegistry, "all_tenants", ...)` (class-level) — инстанс реестра, переданный в middleware при импорте `app.main`, иначе недостижим.

---

### Task 1: Настройки + async Redis-клиент

**Files:**
- Modify: `backend/app/config.py`
- Create: `backend/app/redis_client.py`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/test_redis_client.py`

- [ ] **Step 1: Failing-тест**

`backend/tests/test_redis_client.py`:

```python
"""redis_client: lazy singleton + test seam."""
from app.redis_client import get_redis, set_redis_for_tests


class _Stub:
    pass


def test_set_redis_for_tests_overrides_singleton():
    stub = _Stub()
    set_redis_for_tests(stub)
    assert get_redis() is stub
    set_redis_for_tests(None)


def test_get_redis_returns_client_with_decoded_responses():
    set_redis_for_tests(None)
    client = get_redis()
    # redis.asyncio.Redis: соединение ленивое, сам объект создаётся без I/O
    assert client.connection_pool.connection_kwargs.get("decode_responses") is True
    set_redis_for_tests(None)
```

- [ ] **Step 2: Прогнать — падает**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/test_redis_client.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app.redis_client'`

- [ ] **Step 3: Реализация**

`backend/app/redis_client.py`:

```python
"""Lazy async Redis client shared by dashboard sessions and rate limiting.

Connection is created on first use (redis.asyncio connects lazily), so
importing this module never performs I/O. Tests inject a fake via
set_redis_for_tests().
"""
from __future__ import annotations

import redis.asyncio as redis

from .config import settings

_client = None


def get_redis():
    global _client
    if _client is None:
        _client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


def set_redis_for_tests(client) -> None:
    """Test seam: replace (or with None — reset) the singleton."""
    global _client
    _client = client
```

В `backend/app/config.py` после блока `DELETE_PASSWORD` (его пока НЕ удаляем — Task 15) и перед `# Multi-tenancy` добавить:

```python
    # Dashboard auth (Plan 2)
    SESSION_TTL_SECONDS: int = 7 * 24 * 3600  # 7 days
    SESSION_COOKIE_SECURE: bool = True  # False only for local http:// dev
    LOGIN_RATE_MAX_ATTEMPTS: int = 10
    LOGIN_RATE_WINDOW_SECONDS: int = 300
    LOGIN_RATE_IP_MULTIPLIER: int = 10  # IP-backstop: max_attempts × множитель
    # ISO-дата (UTC) конца grace-окна для брокерских JWT без tenant-claim
    # (спека 5.4: 30 дней от деплоя). Пусто = grace выключен, claim обязателен.
    BROKER_JWT_TENANT_GRACE_UNTIL: str = ""
```

- [ ] **Step 4: Фикстура fake Redis в conftest + тестовый SECRET_KEY**

В САМОЕ НАЧАЛО `backend/tests/conftest.py` (сразу после docstring, ДО sys.path-блока — инвариант 12) добавить:

```python
import os

# app/auth_jwt.py fail-loud отбивает плейсхолдерный SECRET_KEY на импорте;
# тесты не сорсят .env — даём тестовый секрет до любых импортов app.*
os.environ.setdefault("SECRET_KEY", "unit-test-secret-key")
```

и дописать в конец:

```python
import pytest


class FakeRedis:
    """Минимальный async-стаб под команды, используемые auth_sessions."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def expire(self, key, ttl):
        self.ttls[key] = ttl


@pytest.fixture
def fake_redis():
    from app.redis_client import set_redis_for_tests

    client = FakeRedis()
    set_redis_for_tests(client)
    yield client
    set_redis_for_tests(None)
```

- [ ] **Step 5: Прогнать — зелёный**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests -v`
Expected: новые 2 теста PASS, остальные без регрессий.

- [ ] **Step 6: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/config.py backend/app/redis_client.py backend/tests/conftest.py backend/tests/test_redis_client.py
git commit -m "feat(auth): settings + lazy async redis client with test seam"
```

---

### Task 2: `auth_sessions.py` — Redis-сессии и rate-limit логина

**Files:**
- Create: `backend/app/auth_sessions.py`
- Test: `backend/tests/test_auth_sessions.py`

- [ ] **Step 1: Failing-тест**

`backend/tests/test_auth_sessions.py`:

```python
"""Redis session store: tenant-namespaced keys, TTL, rate limit."""
import pytest

from tenancy.context import reset_tenant_schema, set_tenant_schema

from app import auth_sessions
from app.config import settings


@pytest.fixture
def tenant_ctx():
    token = set_tenant_schema("t_realestate")
    yield
    reset_tenant_schema(token)


@pytest.mark.anyio
async def test_create_load_destroy_roundtrip(fake_redis, tenant_ctx):
    sid = await auth_sessions.create_session("u1", "a@b.c", "admin")
    assert sid and len(sid) >= 32
    key = f"t:realestate:sess:{sid}"
    assert key in fake_redis.store
    assert fake_redis.ttls[key] == settings.SESSION_TTL_SECONDS
    data = await auth_sessions.load_session(sid)
    assert data == {"user_id": "u1", "email": "a@b.c", "role": "admin"}
    await auth_sessions.destroy_session(sid)
    assert await auth_sessions.load_session(sid) is None


@pytest.mark.anyio
async def test_sessions_do_not_cross_tenants(fake_redis):
    token = set_tenant_schema("t_realestate")
    sid = await auth_sessions.create_session("u1", "a@b.c", "viewer")
    reset_tenant_schema(token)

    token = set_tenant_schema("t_acme")
    assert await auth_sessions.load_session(sid) is None
    reset_tenant_schema(token)


@pytest.mark.anyio
async def test_login_rate_limit_per_email(fake_redis, tenant_ctx):
    # за relay/Caddy client.host вырождается в один IP (см. комментарий в
    # реализации) — основной лимит по email
    for _ in range(settings.LOGIN_RATE_MAX_ATTEMPTS):
        assert await auth_sessions.register_login_attempt("1.2.3.4", "a@b.c") is True
    assert await auth_sessions.register_login_attempt("1.2.3.4", "a@b.c") is False
    # другой email с того же IP — независимый счётчик (IP-лимит выше)
    assert await auth_sessions.register_login_attempt("1.2.3.4", "x@y.z") is True


@pytest.mark.anyio
async def test_login_rate_limit_ip_backstop(fake_redis, tenant_ctx):
    limit = settings.LOGIN_RATE_MAX_ATTEMPTS * settings.LOGIN_RATE_IP_MULTIPLIER
    for i in range(limit):
        assert await auth_sessions.register_login_attempt("9.9.9.9", f"u{i}@t.io") is True
    assert await auth_sessions.register_login_attempt("9.9.9.9", "uX@t.io") is False
```

Примечание: `pytest.mark.anyio` требует фикстуру `anyio_backend`; добавить в начало файла:

```python
@pytest.fixture
def anyio_backend():
    return "asyncio"
```

(anyio 4.x в venv несёт собственный pytest-плагин — маркер работает без pytest-asyncio, проверено. Фоллбек, если что-то пойдёт не так: синхронные тесты с `asyncio.run(...)` — НЕ `get_event_loop` (инвариант 13).)

- [ ] **Step 2: Прогнать — падает**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/test_auth_sessions.py -v`
Expected: FAIL `ModuleNotFoundError`/`ImportError` на `app.auth_sessions`

- [ ] **Step 3: Реализация**

`backend/app/auth_sessions.py`:

```python
"""Server-side dashboard sessions in Redis (spec 5.2) + login rate limit.

Key layout (spec 4.4): sessions  t:{slug}:sess:{sid}
                       ratelimit t:{slug}:rl:login:{ip}
Both are namespaced by the *current* tenant slug, so a session id stolen
from one tenant is useless on another (load_session reads through the
tenant context of the resolving request, not of the cookie).
"""
from __future__ import annotations

import json
import secrets

from tenancy.context import require_tenant_slug

from .config import settings
from .redis_client import get_redis

SESSION_COOKIE = "sqa_session"


def _sess_key(slug: str, sid: str) -> str:
    return f"t:{slug}:sess:{sid}"


async def create_session(user_id: str, email: str, role: str) -> str:
    sid = secrets.token_urlsafe(32)
    slug = require_tenant_slug()
    payload = json.dumps({"user_id": user_id, "email": email, "role": role})
    await get_redis().set(
        _sess_key(slug, sid), payload, ex=settings.SESSION_TTL_SECONDS
    )
    return sid


async def load_session(sid: str) -> dict | None:
    slug = require_tenant_slug()
    raw = await get_redis().get(_sess_key(slug, sid))
    return json.loads(raw) if raw else None


async def destroy_session(sid: str) -> None:
    slug = require_tenant_slug()
    await get_redis().delete(_sess_key(slug, sid))


async def register_login_attempt(ip: str, email: str) -> bool:
    """True — попытка разрешена; False — лимит исчерпан (отвечать 429).

    Два счётчика: основной — по email (за KZ-relay/Caddy client.host
    вырождается в IP релея для всего трафика, лимит чисто по IP лочил бы
    весь тенант одним атакующим); по IP — только backstop с множителем
    против тупого перебора множества email.
    """
    slug = require_tenant_slug()
    r = get_redis()
    email_key = f"t:{slug}:rl:login:email:{email.lower()}"
    ip_key = f"t:{slug}:rl:login:ip:{ip}"
    n_email = await r.incr(email_key)
    if n_email == 1:
        await r.expire(email_key, settings.LOGIN_RATE_WINDOW_SECONDS)
    n_ip = await r.incr(ip_key)
    if n_ip == 1:
        await r.expire(ip_key, settings.LOGIN_RATE_WINDOW_SECONDS)
    return (
        n_email <= settings.LOGIN_RATE_MAX_ATTEMPTS
        and n_ip <= settings.LOGIN_RATE_MAX_ATTEMPTS * settings.LOGIN_RATE_IP_MULTIPLIER
    )
```

- [ ] **Step 4: Прогнать — зелёный**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/test_auth_sessions.py -v`
Expected: 3 passed (+ возможно anyio параметризация ×2 — это ок)

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/auth_sessions.py backend/tests/test_auth_sessions.py
git commit -m "feat(auth): tenant-namespaced redis sessions + login rate limit"
```

---

### Task 3: Реестр отдаёт api-key поля; middleware кладёт тенанта в request.state

**Files:**
- Modify: `backend/app/tenancy_http.py`
- Modify: `backend/tests/test_tenant_middleware.py` (StubRegistry + новый тест)

- [ ] **Step 1: Failing-тест**

В `backend/tests/test_tenant_middleware.py` НЕТ фикстуры `client` — тесты строятся через модульный хелпер `_app(default_tenant="")`, возвращающий TestClient. Правки:

1. В rows StubRegistry добавить КАЖДОЙ записи два поля:

```python
                "api_key_hash": None,
                "api_key_required": False,
```

2. Handler `/whoami` внутри `_app` расширить полем из scope-state (посмотреть текущую реализацию и сохранить существующие поля/assertions):

```python
    tenant = request.scope.get("state", {}).get("tenant")
    # ...в возвращаемый JSON добавить:
    #   "api_key_required": (tenant or {}).get("api_key_required"),
```

3. Добавить тесты в стиле файла:

```python
def test_tenant_row_lands_in_request_state():
    c = _app()
    r = c.get("/whoami", headers={"host": "realestate.silentqa.com"})
    assert r.status_code == 200
    assert r.json()["api_key_required"] is False


def test_platform_contour_state_tenant_is_none():
    c = _app()
    r = c.get("/whoami", headers={"host": "silentqa.com"})
    assert r.status_code == 200
    assert r.json()["api_key_required"] is None
```

- [ ] **Step 2: Прогнать — падает**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/test_tenant_middleware.py -v`
Expected: новые тесты FAIL (state.tenant нет), старые PASS

- [ ] **Step 3: Реализация**

В `backend/app/tenancy_http.py`:

1. В `TenantRegistry.all_tenants()` SELECT (строка ~36) расширить:

```python
                        "SELECT slug, schema_name, status, custom_domains, "
                        "api_key_hash, api_key_required "
                        "FROM shared.tenants"
```

и в построении dict добавить `"api_key_hash": r[4], "api_key_required": r[5]`.

2. В `TenantResolutionMiddleware.__call__` после `kind, row = await self._resolve(host)` и обработки notfound, ПЕРЕД `set_tenant_schema(...)` добавить:

```python
        scope.setdefault("state", {})["tenant"] = dict(row) if row else None
```

- [ ] **Step 4: Прогнать — зелёный**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/test_tenant_middleware.py -v`
Expected: все PASS

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/tenancy_http.py backend/tests/test_tenant_middleware.py
git commit -m "feat(tenancy): registry exposes api-key fields; tenant row in request.state"
```

---

### Task 4: `auth_user.py` — dependencies (viewer/admin/dual/amocrm-gate)

**Files:**
- Create: `backend/app/auth_user.py`
- Test: `backend/tests/test_auth_deps.py`

- [ ] **Step 1: Failing-тест**

`backend/tests/test_auth_deps.py` — мини-приложение с теми же middleware-паттернами, что в `test_tenant_middleware.py`:

```python
"""Auth dependencies: cookie session, roles, dual ingestion auth."""
import hashlib

import pytest
from fastapi import Depends, FastAPI
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE
from app.auth_user import (
    UserCtx,
    get_current_user,
    require_admin,
    require_ingestion_auth,
    require_viewer,
)
from app.tenancy_http import TenantResolutionMiddleware

API_KEY = "sqa_test_key"
API_KEY_HASH = hashlib.sha256(API_KEY.encode()).hexdigest()


class StubRegistry:
    def __init__(self, api_key_required: bool):
        self.rows = [{
            "slug": "realestate",
            "schema_name": "t_realestate",
            "status": "active",
            "custom_domains": [],
            "api_key_hash": API_KEY_HASH,
            "api_key_required": api_key_required,
        }]

    async def all_tenants(self):
        return self.rows


def make_client(api_key_required: bool) -> TestClient:
    app = FastAPI()

    @app.get("/viewer", dependencies=[Depends(require_viewer)])
    async def viewer_ep():
        return {"ok": True}

    @app.get("/admin", dependencies=[Depends(require_admin)])
    async def admin_ep():
        return {"ok": True}

    @app.post("/ingest", dependencies=[Depends(require_ingestion_auth)])
    async def ingest_ep():
        return {"ok": True}

    @app.get("/me")
    async def me_ep(user: UserCtx | None = Depends(get_current_user)):
        return {"user": user.email if user else None}

    app.add_middleware(
        TenantResolutionMiddleware,
        registry=StubRegistry(api_key_required),
        base_domain="silentqa.com",
        default_tenant="",
    )
    return TestClient(app, base_url="https://realestate.silentqa.com")


async def _seed_session(role: str) -> str:
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    token = set_tenant_schema("t_realestate")
    try:
        return await auth_sessions.create_session("u1", "u@t.io", role)
    finally:
        reset_tenant_schema(token)


def _sid(fake_redis, role: str) -> str:
    import asyncio
    return asyncio.run(_seed_session(role))  # НЕ get_event_loop (инвариант 13)


def test_viewer_requires_session(fake_redis):
    c = make_client(api_key_required=True)
    assert c.get("/viewer").status_code == 401
    assert c.get("/viewer").json()["detail"] == "auth_required"
    sid = _sid(fake_redis, "viewer")
    assert c.get("/viewer", cookies={SESSION_COOKIE: sid}).status_code == 200


def test_admin_requires_admin_role(fake_redis):
    c = make_client(api_key_required=True)
    sid_v = _sid(fake_redis, "viewer")
    sid_a = _sid(fake_redis, "admin")
    assert c.get("/admin", cookies={SESSION_COOKIE: sid_v}).status_code == 403
    assert c.get("/admin", cookies={SESSION_COOKIE: sid_a}).status_code == 200


def test_ingest_dual_auth_matrix(fake_redis):
    c = make_client(api_key_required=True)
    # без ничего → 401
    assert c.post("/ingest").status_code == 401
    # валидный ключ → 200
    assert c.post("/ingest", headers={"X-API-Key": API_KEY}).status_code == 200
    # невалидный ключ → 403 (даже не 401: ключ предъявлен, но чужой/битый)
    assert c.post("/ingest", headers={"X-API-Key": "sqa_wrong"}).status_code == 403
    # cookie-сессия (viewer достаточно) → 200
    sid = _sid(fake_redis, "viewer")
    assert c.post("/ingest", cookies={SESSION_COOKIE: sid}).status_code == 200


def test_ingest_open_while_key_not_required(fake_redis):
    c = make_client(api_key_required=False)
    # rollout-окно (спека 6.4): без ключа пускаем
    assert c.post("/ingest").status_code == 200
    # но предъявленный НЕВЕРНЫЙ ключ всё равно отбивается
    assert c.post("/ingest", headers={"X-API-Key": "sqa_wrong"}).status_code == 403


def test_stale_cookie_is_anonymous(fake_redis):
    c = make_client(api_key_required=True)
    assert c.get("/me", cookies={SESSION_COOKIE: "ghost"}).json()["user"] is None
```

- [ ] **Step 2: Прогнать — падает**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/test_auth_deps.py -v`
Expected: FAIL `ModuleNotFoundError: app.auth_user`

- [ ] **Step 3: Реализация**

`backend/app/auth_user.py`:

```python
"""Auth dependencies for the access matrix (spec 5.6).

Three planes:
- cookie session (tenant users)      → get_current_user / require_viewer / require_admin
- X-API-Key (recorder ingestion)     → require_ingestion_auth (cookie OR key)
- broker JWT                         → auth_jwt.get_current_broker (отдельный модуль)
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from tenancy.context import get_tenant_slug

from .auth_sessions import SESSION_COOKIE, load_session


@dataclass
class UserCtx:
    user_id: str
    email: str
    role: str


async def get_current_user(request: Request) -> UserCtx | None:
    """Cookie session → UserCtx; None на платформенном контуре/без сессии."""
    if get_tenant_slug() is None:
        return None
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    data = await load_session(sid)
    if not data:
        return None
    return UserCtx(user_id=data["user_id"], email=data["email"], role=data["role"])


async def require_viewer(
    user: UserCtx | None = Depends(get_current_user),
) -> UserCtx:
    if user is None:
        # detail-маркер: SPA редиректит на логин ТОЛЬКО по нему (спека 5.2)
        raise HTTPException(status_code=401, detail="auth_required")
    return user


async def require_admin(user: UserCtx = Depends(require_viewer)) -> UserCtx:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin_required")
    return user


def _api_key_matches(provided: str, expected_hash: str | None) -> bool:
    if not expected_hash:
        return False
    digest = hashlib.sha256(provided.encode()).hexdigest()
    return secrets.compare_digest(digest, expected_hash)


async def require_ingestion_auth(
    request: Request,
    user: UserCtx | None = Depends(get_current_user),
) -> None:
    """Двойная авторизация ingestion-эндпоинтов (спека 5.3/5.6).

    Порядок: валидная cookie-сессия → ок; предъявленный X-API-Key обязан
    совпасть с ключом ИМЕННО этого тенанта (двойное совпадение), иначе 403;
    без креденшелов — пускаем только пока api_key_required=false (rollout
    6.4), иначе 401.
    """
    if user is not None:
        return
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    provided = request.headers.get("X-API-Key")
    if provided:
        if _api_key_matches(provided, tenant.get("api_key_hash")):
            return
        raise HTTPException(status_code=403, detail="api_key_mismatch")
    if not tenant.get("api_key_required", True):
        return
    raise HTTPException(status_code=401, detail="api_key_required")


async def require_amocrm_tenant(
    user: UserCtx = Depends(require_admin),
) -> UserCtx:
    """/api/amocrm/*: admin + только тенант с AmoCRM-интеграцией (спека 5.6)."""
    from tenancy.registry import AMOCRM_TENANT_SLUGS

    if get_tenant_slug() not in AMOCRM_TENANT_SLUGS:
        raise HTTPException(status_code=403, detail="amocrm_not_enabled")
    return user
```

- [ ] **Step 4: Прогнать — зелёный**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/test_auth_deps.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/auth_user.py backend/tests/test_auth_deps.py
git commit -m "feat(auth): viewer/admin/dual-ingestion/amocrm dependencies"
```

---

### Task 5: `/api/user-auth/login|logout|me`

**Files:**
- Create: `backend/app/routes/user_auth.py`
- Modify: `backend/app/main.py` (import + include_router)
- Test: `backend/tests/test_user_auth_routes.py`

- [ ] **Step 1: Failing-тест**

`backend/tests/test_user_auth_routes.py`. БД подменяется через `app.dependency_overrides[get_db]` на StubDB; argon2-хеш считается в тесте:

```python
"""/api/user-auth/*: login sets cookie, logout invalidates, me reports role."""
import hashlib

from argon2 import PasswordHasher
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE
from app.database import get_db
from app.routes import user_auth
from app.tenancy_http import TenantResolutionMiddleware

_ph = PasswordHasher()
GOOD_HASH = _ph.hash("correct-horse")


class _Row:
    def __init__(self):
        self.id = "11111111-1111-1111-1111-111111111111"
        self.email = "admin@t.io"
        self.password_hash = GOOD_HASH
        self.role = "admin"


class StubDB:
    """Возвращает заданный row на SELECT, проглатывает UPDATE/commit."""

    def __init__(self, row):
        self._row = row
        self.committed = False

    async def execute(self, *a, **kw):
        class _Res:
            def __init__(self, row):
                self._row = row

            def first(self):
                return self._row

        return _Res(self._row)

    async def commit(self):
        self.committed = True


class StubRegistry:
    def __init__(self):
        self.rows = [{
            "slug": "realestate", "schema_name": "t_realestate",
            "status": "active", "custom_domains": [],
            "api_key_hash": None, "api_key_required": False,
        }]

    async def all_tenants(self):
        return self.rows


def make_client(row) -> TestClient:
    app = FastAPI()
    app.include_router(user_auth.router)
    app.dependency_overrides[get_db] = lambda: StubDB(row)
    app.add_middleware(
        TenantResolutionMiddleware,
        registry=StubRegistry(),
        base_domain="silentqa.com",
        default_tenant="",
    )
    return TestClient(app, base_url="https://realestate.silentqa.com")


def test_login_success_sets_cookie_and_me_works(fake_redis):
    c = make_client(_Row())
    r = c.post("/api/user-auth/login",
               json={"email": "admin@t.io", "password": "correct-horse"})
    assert r.status_code == 200
    assert r.json() == {"email": "admin@t.io", "role": "admin"}
    cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE in cookie and "HttpOnly" in cookie
    assert "SameSite=lax" in cookie or "samesite=lax" in cookie
    assert "Domain=" not in cookie  # host-only (инвариант 11)
    me = c.get("/api/user-auth/me")
    assert me.status_code == 200 and me.json()["role"] == "admin"


def test_login_wrong_password_401_generic(fake_redis):
    c = make_client(_Row())
    r = c.post("/api/user-auth/login",
               json={"email": "admin@t.io", "password": "nope"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


def test_login_unknown_email_401_same_detail(fake_redis):
    c = make_client(None)
    r = c.post("/api/user-auth/login",
               json={"email": "ghost@t.io", "password": "whatever"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


def test_logout_invalidates_session(fake_redis):
    c = make_client(_Row())
    c.post("/api/user-auth/login",
           json={"email": "admin@t.io", "password": "correct-horse"})
    assert c.get("/api/user-auth/me").status_code == 200
    c.post("/api/user-auth/logout")
    assert c.get("/api/user-auth/me").status_code == 401


def test_login_rate_limited_429(fake_redis):
    from app.config import settings

    c = make_client(_Row())
    for _ in range(settings.LOGIN_RATE_MAX_ATTEMPTS):
        c.post("/api/user-auth/login",
               json={"email": "admin@t.io", "password": "nope"})
    r = c.post("/api/user-auth/login",
               json={"email": "admin@t.io", "password": "correct-horse"})
    assert r.status_code == 429


def test_login_on_platform_contour_404(fake_redis):
    c_platform = TestClient(_platform_app(), base_url="https://silentqa.com")
    r = c_platform.post("/api/user-auth/login",
                        json={"email": "a@b.c", "password": "x"})
    assert r.status_code == 404


def _platform_app() -> FastAPI:
    app = FastAPI()
    app.include_router(user_auth.router)
    app.add_middleware(
        TenantResolutionMiddleware,
        registry=StubRegistry(),
        base_domain="silentqa.com",
        default_tenant="",
    )
    return app
```

- [ ] **Step 2: Прогнать — падает**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/test_user_auth_routes.py -v`
Expected: FAIL `ImportError: cannot import name 'user_auth'`

- [ ] **Step 3: Реализация**

`backend/app/routes/user_auth.py`:

```python
"""Дашборд-авторизация тенант-юзеров: /api/user-auth/* (спека 5.2).

Отдельный неймспейс: /api/auth/* занят брокерским (recorder) контуром.
Пароли — argon2 (как сеет provision_tenant.py). Сессии — Redis.
"""
from __future__ import annotations

import logging

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tenancy.context import get_tenant_slug

from .. import auth_sessions
from ..auth_user import UserCtx, get_current_user
from ..config import settings
from ..database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/user-auth", tags=["user-auth"])

_ph = PasswordHasher()
# Статический argon2-хеш заведомо неверного секрета: verify против него на
# ветке "email не найден" выравнивает тайминг (не раскрываем существование
# аккаунта). Тот же приём, что в брокерском /api/auth/login.
_DUMMY_HASH = _ph.hash("timing-equalizer-not-a-real-password")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


def _verify(stored_hash: str, password: str) -> bool:
    try:
        _ph.verify(stored_hash, password)
        return True
    except Exception:
        return False


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    # Инвариант 9: на платформенном контуре таблицы users нет — 404 до SQL.
    if get_tenant_slug() is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")

    ip = request.client.host if request.client else "unknown"
    if not await auth_sessions.register_login_attempt(ip, body.email):
        raise HTTPException(status_code=429, detail="too_many_attempts")

    row = (
        await db.execute(
            text(
                "SELECT id, email, password_hash, role FROM users "
                "WHERE LOWER(email) = LOWER(:email)"
            ),
            {"email": body.email},
        )
    ).first()

    stored = row.password_hash if row is not None else _DUMMY_HASH
    if not _verify(stored, body.password) or row is None:
        raise HTTPException(status_code=401, detail="invalid_credentials")

    await db.execute(
        text("UPDATE users SET last_login = now() WHERE id = :id"),
        {"id": row.id},
    )
    await db.commit()

    sid = await auth_sessions.create_session(str(row.id), row.email, row.role)
    response.set_cookie(
        auth_sessions.SESSION_COOKIE,
        sid,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite="lax",
        max_age=settings.SESSION_TTL_SECONDS,
        path="/",
        # Domain НЕ задаём — host-only cookie скоупится на домен тенанта.
    )
    return {"email": row.email, "role": row.role}


@router.post("/logout")
async def logout(request: Request, response: Response):
    if get_tenant_slug() is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    sid = request.cookies.get(auth_sessions.SESSION_COOKIE)
    if sid:
        await auth_sessions.destroy_session(sid)
    response.delete_cookie(auth_sessions.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: UserCtx | None = Depends(get_current_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="auth_required")
    return {"email": user.email, "role": user.role}
```

В `backend/app/main.py`: добавить `user_auth` в импорт роутов (строка с `from app.routes import ...`) и после `app.include_router(auth.router)` добавить:

```python
app.include_router(user_auth.router)
```

- [ ] **Step 4: Прогнать — зелёный + полный suite**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests -v`
Expected: новые 6 PASS, без регрессий.

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/routes/user_auth.py backend/app/main.py backend/tests/test_user_auth_routes.py
git commit -m "feat(auth): /api/user-auth login/logout/me with argon2 + rate limit"
```

---

### Task 6: Tenant-claim в брокерских JWT + grace-окно

**Files:**
- Modify: `backend/app/auth_jwt.py` (create_token, get_current_broker, _grace_active)
- Modify: `backend/app/routes/auth.py` (2 сайта выпуска токена)
- Test: `backend/tests/test_broker_jwt_tenant.py`

- [ ] **Step 1: Failing-тест**

`backend/tests/test_broker_jwt_tenant.py`:

```python
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
```

- [ ] **Step 2: Прогнать — падает**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/test_broker_jwt_tenant.py -v`
Expected: FAIL (`create_token` не принимает tenant; `check_tenant_claim` нет)

- [ ] **Step 3: Реализация**

В `backend/app/auth_jwt.py`:

1. Импорты дополнить:

```python
from datetime import date, datetime, timedelta, timezone

from tenancy.context import get_tenant_slug
```

2. `create_token` — добавить обязательный параметр и claim:

```python
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
```

3. Новые функции (над `get_current_broker`):

```python
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
```

4. В `get_current_broker` сразу после блока `sub`-валидации (перед `uuid.UUID(...)` допустимо после — главное ДО запроса в БД) добавить:

```python
    check_tenant_claim(payload)
```

5. В `backend/app/routes/auth.py` оба вызова `create_token(...)` (claim/complete и login) дополнить аргументом:

```python
        tenant=require_tenant_slug(),
```

с импортом `from tenancy.context import require_tenant_slug` и guard'ом в начале обоих хендлеров `claim/start`, `claim/complete`, `login` (инвариант 9):

```python
    if get_tenant_slug() is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
```

(импортировать и `get_tenant_slug`).

- [ ] **Step 4: Прогнать — зелёный + оба suite**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests -v`
Expected: новые 6 PASS; существующие тесты без регрессий.

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/auth_jwt.py backend/app/routes/auth.py backend/tests/test_broker_jwt_tenant.py
git commit -m "feat(auth): tenant claim in broker JWT with 30-day grace window"
```

---

### Task 7: Матрица доступа — sessions.py и chunks.py

**Files:**
- Modify: `backend/app/routes/sessions.py`
- Modify: `backend/app/routes/chunks.py`
- Test: `backend/tests/test_access_matrix.py` (новый, общий для Tasks 7–9, 11–12)

**Раскладка по матрице 5.6:**

| Эндпоинт | Auth |
|---|---|
| `GET /api/sessions` (list) | `require_viewer` |
| `POST /api/sessions` (create) | `require_ingestion_auth` |
| `GET /api/sessions/{id}` | `require_ingestion_auth` (поллинг recorder-клиентами) |
| `DELETE /api/sessions/{id}` | `require_admin` (X-Delete-Password удаляется) |
| `POST .../reprocess` | `require_admin` |
| `POST .../link-lead` | `require_ingestion_auth` |
| `GET .../extraction` | `require_viewer` |
| `POST .../finish` | `require_ingestion_auth` |
| `PATCH .../speaker-map` | `require_admin` (мутация данных) |
| `POST .../chunks` | `require_ingestion_auth` |
| `GET .../missing-chunks` | `require_ingestion_auth` |
| `POST .../upload-audio` | `require_ingestion_auth` (дашборд-аплоад идёт по cookie-ветке) |

- [ ] **Step 1: Failing-тест**

`backend/tests/test_access_matrix.py` — против РЕАЛЬНОГО приложения, инварианты 1, 2, 12:

```python
"""Матрица доступа 5.6: negative-кейсы против реального app (без БД).

Auth-зависимости отрабатывают до первого SQL (ленивое соединение) — поэтому
401/403 проверяемы без Postgres. Positive-пути покрыты в test_auth_deps.py.
"""
import asyncio

import pytest
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE


ROWS = [{
    "slug": "realestate", "schema_name": "t_realestate", "status": "active",
    "custom_domains": [], "api_key_hash": None, "api_key_required": True,
}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app  # noqa: WPS433 — import после патча безопасен

    return TestClient(app, base_url="https://realestate.silentqa.com")


def _viewer_cookie(fake_redis) -> dict:
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_realestate")
        try:
            return await auth_sessions.create_session("u1", "v@t.io", "viewer")
        finally:
            reset_tenant_schema(token)

    sid = asyncio.run(seed())  # инвариант 13
    return {SESSION_COOKIE: sid}


VIEWER_GETS = [
    "/api/sessions",
    "/api/sessions/00000000-0000-0000-0000-000000000001/extraction",
    "/api/sessions/00000000-0000-0000-0000-000000000001/transcript",
    "/api/sessions/00000000-0000-0000-0000-000000000001/analysis",
    "/api/sessions/00000000-0000-0000-0000-000000000001/audio",
    "/api/sessions/00000000-0000-0000-0000-000000000001/sentiment",
    "/api/sessions/00000000-0000-0000-0000-000000000001/quality",
    "/api/sessions/00000000-0000-0000-0000-000000000001/full",
    "/api/managers",
    "/api/templates",
    "/api/complexes",
]

INGESTION = [
    ("post", "/api/sessions"),
    ("get", "/api/sessions/00000000-0000-0000-0000-000000000001"),
    ("post", "/api/sessions/00000000-0000-0000-0000-000000000001/finish"),
    ("post", "/api/sessions/00000000-0000-0000-0000-000000000001/link-lead"),
    ("post", "/api/sessions/00000000-0000-0000-0000-000000000001/chunks"),
    ("get", "/api/sessions/00000000-0000-0000-0000-000000000001/missing-chunks"),
    ("post", "/api/sessions/00000000-0000-0000-0000-000000000001/upload-audio"),
]

ADMIN_MUTATIONS = [
    ("delete", "/api/sessions/00000000-0000-0000-0000-000000000001"),
    ("post", "/api/sessions/00000000-0000-0000-0000-000000000001/reprocess"),
    ("patch", "/api/sessions/00000000-0000-0000-0000-000000000001/speaker-map"),
    ("patch", "/api/sessions/00000000-0000-0000-0000-000000000001/reassign-speaker"),
    ("post", "/api/templates"),
    ("patch", "/api/templates/00000000-0000-0000-0000-000000000001"),
    ("delete", "/api/templates/00000000-0000-0000-0000-000000000001"),
    ("patch", "/api/complexes/00000000-0000-0000-0000-000000000001"),
    ("delete", "/api/complexes/00000000-0000-0000-0000-000000000001"),
    ("post", "/api/complexes/00000000-0000-0000-0000-000000000001/merge"),
    ("post", "/api/extractions/00000000-0000-0000-0000-000000000001/relink"),
    # ВНИМАНИЕ: /api/amocrm/* сюда НЕ добавлять до Task 12 — до навешивания
    # auth-зависимостей search-leads реально исполнит handler и дёрнет живой
    # AmoCRM (portal-токен). Пути добавляются в Task 12 вместе с гейтом.
]


@pytest.mark.parametrize("path", VIEWER_GETS)
def test_viewer_endpoints_401_without_session(client, path):
    r = client.get(path)
    assert r.status_code == 401, path
    assert r.json()["detail"] == "auth_required"


@pytest.mark.parametrize("method,path", INGESTION)
def test_ingestion_endpoints_401_without_creds(client, method, path):
    r = getattr(client, method)(path)
    assert r.status_code == 401, (method, path)
    assert r.json()["detail"] == "api_key_required"


@pytest.mark.parametrize("method,path", ADMIN_MUTATIONS)
def test_admin_mutations_403_for_viewer(client, fake_redis, method, path):
    cookies = _viewer_cookie(fake_redis)
    r = getattr(client, method)(path, cookies=cookies)
    assert r.status_code == 403, (method, path)


def test_broker_auth_contour_stays_open(client):
    # /api/auth/* не за cookie-гейтом (спека 5.6): claim/start валидирует
    # body (422 без него), а не требует сессию (не 401 auth_required)
    r = client.post("/api/auth/claim/start", json={})
    assert r.status_code == 422


def test_health_open(client):
    assert client.get("/health").status_code == 200
```

Примечания: тест включает пути Tasks 8–9, 11–12 — на этом таске прогонять ТОЛЬКО подмножество sessions/chunks (`-k "sessions or chunks or ingestion"` не сработает из-за параметризации — поэтому: пока Tasks 8–12 не сделаны, остальные параметры будут падать; это ожидаемо — здесь и в Tasks 8–12 прогонять файл целиком и фиксировать в отчёте, какие параметры уже зелёные; ПОЛНОСТЬЮ зелёным файл обязан стать после Task 12).

- [ ] **Step 2: Прогнать — соответствующие параметры падают**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/test_access_matrix.py -v`
Expected: sessions/chunks-параметры FAIL (сейчас эндпоинты открыты → не 401)

- [ ] **Step 3: Реализация sessions.py**

1. Импорт: `from ..auth_user import require_admin, require_ingestion_auth, require_viewer`
2. Декораторы (functions: `list_sessions`, `create_session`, `get_session`, `delete_session`, `reprocess_session`, `link_lead`, `get_extraction`, `finish_session`, `update_speaker_map` — сверить имена по файлу):

```python
@router.get("", response_model=..., dependencies=[Depends(require_viewer)])          # list
@router.post("", response_model=SessionResponse, status_code=201,
             dependencies=[Depends(require_ingestion_auth)])                          # create
@router.get("/{session_id}", response_model=SessionResponse,
            dependencies=[Depends(require_ingestion_auth)])                           # get
@router.delete("/{session_id}", status_code=204, dependencies=[Depends(require_admin)])
@router.post("/{session_id}/reprocess", response_model=SessionResponse,
             dependencies=[Depends(require_admin)])
@router.post("/{session_id}/link-lead", response_model=SessionResponse,
             dependencies=[Depends(require_ingestion_auth)])
@router.get("/{session_id}/extraction", dependencies=[Depends(require_viewer)])
@router.post("/{session_id}/finish", response_model=SessionResponse,
             dependencies=[Depends(require_ingestion_auth)])
@router.patch("/{session_id}/speaker-map", response_model=SessionResponse,
              dependencies=[Depends(require_admin)])
```

(существующие аргументы декораторов сохранить, добавляется только `dependencies=[...]`).

3. Удалить из `delete_session`: параметр `x_delete_password`, вызов `_require_delete_password(...)` (строка ~181) и саму функцию `_require_delete_password` (строки ~162–168). Импорт `Header` убрать, если больше не используется.

- [ ] **Step 4: Реализация chunks.py**

Импорт `from ..auth_user import require_ingestion_auth`; на все три эндпоинта (`upload_chunk`, `missing_chunks`, `upload_audio_file`) добавить `dependencies=[Depends(require_ingestion_auth)]`.

- [ ] **Step 5: Прогнать**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/test_access_matrix.py -v 2>&1 | tail -20`
Expected: все sessions/chunks-параметры PASS (transcripts/analysis/managers/templates/complexes/amocrm — ещё FAIL, это план Tasks 8–12); `tests -k "not access_matrix"` — без регрессий.

- [ ] **Step 6: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/routes/sessions.py backend/app/routes/chunks.py backend/tests/test_access_matrix.py
git commit -m "feat(auth): access matrix on sessions + chunks; drop X-Delete-Password from delete"
```

---

### Task 8: Матрица — transcripts.py, analysis.py, managers.py

**Files:**
- Modify: `backend/app/routes/transcripts.py` (2 GET → `require_viewer`)
- Modify: `backend/app/routes/analysis.py` (4 GET → `require_viewer`; PATCH reassign-speaker → `require_admin`)
- Modify: `backend/app/routes/managers.py` (2 GET → `require_viewer`)

- [ ] **Step 1: Соответствующие параметры test_access_matrix уже красные** — прогнать и зафиксировать:

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/test_access_matrix.py -v 2>&1 | grep -E "transcript|analysis|audio|sentiment|quality|full|managers|reassign"`

- [ ] **Step 2: Реализация** — в каждый файл импорт нужных deps и `dependencies=[Depends(require_viewer)]` на GET-эндпоинты; `dependencies=[Depends(require_admin)]` на `PATCH /{session_id}/reassign-speaker`. ВНИМАНИЕ: transcripts.py и analysis.py не импортируют `Depends` — расширить их fastapi-импорт (`from fastapi import APIRouter, Depends, ...`).

- [ ] **Step 3: Прогнать — эти параметры зелёные, полный suite без регрессий**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests -v 2>&1 | tail -5`

- [ ] **Step 4: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/routes/transcripts.py backend/app/routes/analysis.py backend/app/routes/managers.py
git commit -m "feat(auth): access matrix on transcripts/analysis/managers"
```

---

### Task 9: Матрица — templates.py, complexes.py

**Files:**
- Modify: `backend/app/routes/templates.py` (GET×2 → viewer; POST/PATCH/DELETE → admin)
- Modify: `backend/app/routes/complexes.py` (GET×2 → viewer; PATCH/DELETE/merge/relink → admin)
- Modify: `backend/app/main.py` (удалить ветку X-Delete-Password из BasicAuthMiddleware)

- [ ] **Step 1: Зафиксировать красные параметры** (как в Task 8). Сейчас POST/PATCH/DELETE на /api/templates|complexes|extractions отвечают 503/401 ИЗ MIDDLEWARE (ветка PROTECTED_PREFIXES перехватывает до роутинга) — не 403.

- [ ] **Step 2: Реализация роутов** — добавить `dependencies=[...]` по матрице.

- [ ] **Step 3: Снять X-Delete-Password-ветку из middleware**

В `backend/app/main.py` из `BasicAuthMiddleware.dispatch` удалить блок PROTECTED_PREFIXES/PROTECTED_METHODS/X-Delete-Password (строки ~29–50: константы и if-ветка) — роуты этих префиксов с этого коммита под `require_admin`, защита не опускается (инвариант 6). Остальной Basic-механизм для UI-роутов пока остаётся (до Task 15).

- [ ] **Step 4: Прогнать** — параметры templates/complexes/extractions в test_access_matrix теперь зелёные (403 от require_admin); полный suite без регрессий.

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/routes/templates.py backend/app/routes/complexes.py backend/app/main.py
git commit -m "feat(auth): access matrix on templates/complexes; retire delete-password middleware branch"
```

---

### Task 10: `/api/companies` — только платформенный контур (interim Basic)

**Files:**
- Modify: `backend/app/auth_user.py` (новая dependency)
- Modify: `backend/app/routes/companies.py` (router-level dependency)
- Test: `backend/tests/test_companies_platform.py`

- [ ] **Step 1: Failing-тест**

`backend/tests/test_companies_platform.py`:

```python
"""/api/companies: глобальные конфиги — только платформенный контур (5.6).

До Plan 3 (platform-admin сессии) платформенный контур interim-гейтится
HTTP Basic с существующими AUTH_USERNAME/AUTH_PASSWORD.
"""
import base64

import pytest
from starlette.testclient import TestClient

from app.config import settings

ROWS = [{
    "slug": "realestate", "schema_name": "t_realestate", "status": "active",
    "custom_domains": [], "api_key_hash": None, "api_key_required": False,
}]


@pytest.fixture
def clients(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app

    tenant = TestClient(app, base_url="https://realestate.silentqa.com")
    platform = TestClient(app, base_url="https://silentqa.com")
    return tenant, platform


def _basic(user, pwd) -> dict:
    cred = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return {"Authorization": f"Basic {cred}"}


def test_companies_404_on_tenant_contour(clients):
    tenant, _ = clients
    # даже с валидным Basic — на тенант-контуре раздел не существует
    r = tenant.get("/api/companies",
                   headers=_basic(settings.AUTH_USERNAME, settings.AUTH_PASSWORD))
    assert r.status_code == 404


def test_companies_401_on_platform_without_basic(clients):
    _, platform = clients
    r = platform.get("/api/companies")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Basic"


def test_companies_ok_on_platform_with_basic(clients):
    _, platform = clients
    r = platform.get("/api/companies",
                     headers=_basic(settings.AUTH_USERNAME, settings.AUTH_PASSWORD))
    # 200 — список (читает companies/*.json с диска, БД не нужна)
    assert r.status_code == 200
```

- [ ] **Step 2: Прогнать — падает** (сейчас companies открыт на тенант-контуре → не 404).

- [ ] **Step 3: Реализация**

В `backend/app/auth_user.py` добавить (импортировать `base64`, `settings`):

```python
def require_platform_admin_basic(request: Request) -> None:
    """Interim-гейт /api/companies до Plan 3 (спека 5.6).

    На тенант-контуре раздел не существует (404). На платформенном контуре —
    HTTP Basic с AUTH_USERNAME/AUTH_PASSWORD (заменится platform-admin
    сессиями в Plan 3).
    """
    if get_tenant_slug() is not None:
        raise HTTPException(status_code=404, detail="Not found")
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode()
            username, _, password = decoded.partition(":")
        except Exception:
            username, password = "", ""
        if secrets.compare_digest(username, settings.AUTH_USERNAME) and \
                secrets.compare_digest(password, settings.AUTH_PASSWORD):
            return
    raise HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )
```

(`from .config import settings` добавить в импорты auth_user.py.)

В `backend/app/routes/companies.py` объявление роутера дополнить (ВНИМАНИЕ: файл импортирует из fastapi только APIRouter/HTTPException — добавить `Depends` в импорт):

```python
from fastapi import APIRouter, Depends, HTTPException

from ..auth_user import require_platform_admin_basic

router = APIRouter(prefix="/api/companies", tags=["companies"],
                   dependencies=[Depends(require_platform_admin_basic)])
```

(сохранить существующие prefix/tags как есть; `POST /api/companies` и прочие мутации тем самым тоже закрыты.)

- [ ] **Step 4: Прогнать — зелёный; полный suite.**

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/auth_user.py backend/app/routes/companies.py backend/tests/test_companies_platform.py
git commit -m "feat(auth): /api/companies platform-contour-only with interim basic gate"
```

---

### Task 11: Удалить `/api/webhooks` (мёртвая поверхность)

**Files:**
- Delete: `backend/app/routes/webhooks.py`
- Modify: `backend/app/main.py` (импорт + include_router)
- Test: дополнение в `backend/tests/test_access_matrix.py`

Обоснование (спека 5.6 + инвентаризация): глобальный `webhooks.json` без auth и без тенант-скоупа; события никто не отправляет — ни worker, ни pipeline файл не читают. SPA `/api/webhooks` не вызывает.

- [ ] **Step 1: Failing-тест** — в `test_access_matrix.py` добавить:

```python
def test_webhooks_surface_removed(client):
    assert client.get("/api/webhooks").status_code == 404
    # POST падает в StaticFiles-маунт "/" → 405 Method Not Allowed (не 404!)
    assert client.post("/api/webhooks", json={}).status_code in (404, 405)
```

(главное — не 200: поверхность недоступна.)

- [ ] **Step 2: Прогнать — падает** (сейчас GET отдаёт 200).

- [ ] **Step 3: Реализация** — `git rm backend/app/routes/webhooks.py`; в `main.py` убрать `webhooks` из импорта роутов и строку `app.include_router(webhooks.router)`.

- [ ] **Step 4: Прогнать — зелёный; полный suite.**

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet-mt && git add -A backend/app/routes backend/app/main.py backend/tests/test_access_matrix.py
git commit -m "feat(auth): remove dead unauthenticated /api/webhooks surface"
```

---

### Task 12: Матрица — amocrm.py (admin + только AmoCRM-тенант)

**Files:**
- Modify: `backend/app/routes/amocrm.py`
- Test: дополнение в `backend/tests/test_access_matrix.py`

- [ ] **Step 1: Failing-тест** — в `test_access_matrix.py` добавить (admin-сессия для тенанта БЕЗ AmoCRM → 403; viewer уже покрыт в ADMIN_MUTATIONS):

```python
def test_amocrm_admin_of_non_amocrm_tenant_403(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    rows = [{
        "slug": "acme", "schema_name": "t_acme", "status": "active",
        "custom_domains": [], "api_key_hash": None, "api_key_required": True,
    }]

    async def fake_all(self):
        return rows

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u1", "a@t.io", "admin")
        finally:
            reset_tenant_schema(token)

    sid = asyncio.run(seed())  # инвариант 13
    c = TestClient(app, base_url="https://acme.silentqa.com")
    r = c.post("/api/amocrm/reprocess", cookies={SESSION_COOKIE: sid},
               json={"lead_id": 1})
    assert r.status_code == 403
    assert r.json()["detail"] == "amocrm_not_enabled"
```

- [ ] **Step 2: Прогнать — падает.**

- [ ] **Step 2b: Вернуть amocrm-пути в матрицу** — в `test_access_matrix.py` в ADMIN_MUTATIONS добавить (теперь безопасно — гейт отработает до handler'а):

```python
    ("post", "/api/amocrm/reprocess"),
    ("get", "/api/amocrm/search-leads?q=ab"),
```

- [ ] **Step 3: Реализация** — в `backend/app/routes/amocrm.py` импорт `from ..auth_user import require_amocrm_tenant`; обоим эндпоинтам (`reprocess`, `amocrm_search_leads`) добавить `dependencies=[Depends(require_amocrm_tenant)]`. ВНИМАНИЕ: добавить `Depends` в fastapi-импорт файла.

- [ ] **Step 4: Прогнать — ВЕСЬ `test_access_matrix.py` теперь зелёный целиком + полный suite.**

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests -v 2>&1 | tail -5`

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/routes/amocrm.py backend/tests/test_access_matrix.py
git commit -m "feat(auth): amocrm routes admin-only and gated to amocrm tenants"
```

---

### Task 13: Server-owned `company_id`/`scenario_id`; worker берёт company из строки тенанта

**Files:**
- Modify: `backend/app/routes/sessions.py` (`_SERVER_OWNED_METADATA`)
- Modify: `worker/tasks/company_config.py` (новый `tenant_company_config_id()`)
- Modify: `worker/tasks/pipeline.py` (резолв company_id/scenario_id, ~строка 763)
- Test: `backend/tests/test_access_matrix.py` НЕ трогать; новый `worker/tests/test_company_from_tenant.py`; backend-тест в `backend/tests/test_server_owned_metadata.py`

Закрывает маршрут из матрицы 5.6: «чужой тенант шлёт `company_id=realestate` + phone → его звонок уезжает в AmoCRM realestate».

- [ ] **Step 1: Failing-тест backend**

`backend/tests/test_server_owned_metadata.py`:

```python
"""company_id/scenario_id — server-owned: клиентский ввод вычищается."""
from app.routes.sessions import _SERVER_OWNED_METADATA


def test_company_and_scenario_are_server_owned():
    assert "company_id" in _SERVER_OWNED_METADATA
    assert "scenario_id" in _SERVER_OWNED_METADATA
    # старый набор не потерян
    for k in ("broker_id", "amocrm_user_id", "broker_name", "responsible_user_id"):
        assert k in _SERVER_OWNED_METADATA
```

- [ ] **Step 2: Failing-тест worker**

`worker/tests/test_company_from_tenant.py`:

```python
"""Воркер берёт company_config_id из shared.tenants, не из метаданных клиента."""
from unittest import mock

import pytest

from tenancy.context import reset_tenant_schema, set_tenant_schema

import tasks.company_config as cc


@pytest.fixture(autouse=True)
def clear_cache():
    cc._TENANT_COMPANY_CACHE.clear()
    yield
    cc._TENANT_COMPANY_CACHE.clear()


def _fake_conn(value):
    conn = mock.MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (value,)
    return conn


def test_reads_company_config_id_for_current_tenant(monkeypatch):
    monkeypatch.setattr(cc, "shared_connect", lambda: _fake_conn("realestate"))
    token = set_tenant_schema("t_realestate")
    try:
        assert cc.tenant_company_config_id() == "realestate"
        # второй вызов — из кеша, без нового соединения
        monkeypatch.setattr(cc, "shared_connect", mock.MagicMock(side_effect=AssertionError))
        assert cc.tenant_company_config_id() == "realestate"
    finally:
        reset_tenant_schema(token)


def test_requires_tenant_context():
    from tenancy.context import TenantContextError
    with pytest.raises(TenantContextError):
        cc.tenant_company_config_id()
```

- [ ] **Step 3: Прогнать — оба падают.**

- [ ] **Step 4: Реализация**

1. `backend/app/routes/sessions.py`, строка ~98:

```python
_SERVER_OWNED_METADATA = (
    "broker_id", "amocrm_user_id", "broker_name", "responsible_user_id",
    # company_id/scenario_id — выбор конфига оценки принадлежит серверу
    # (берётся из shared.tenants.company_config_id), клиент подменить не может.
    "company_id", "scenario_id",
)
```

2. `worker/tasks/company_config.py` — добавить (импорт `shared_connect` сделать module-level: `from tenancy.db import shared_connect`):

```python
_TENANT_COMPANY_CACHE: dict[str, str | None] = {}


def tenant_company_config_id() -> str | None:
    """company_config_id текущего тенанта из shared.tenants (кеш на процесс).

    Worker перезапускается при деплоях/ротациях (см. CLAUDE.md), поэтому
    простой module-level кеш безопасен — как и остальные module-globals тут.
    """
    from tenancy.context import require_tenant_slug

    slug = require_tenant_slug()
    if slug in _TENANT_COMPANY_CACHE:
        return _TENANT_COMPANY_CACHE[slug]
    conn = shared_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT company_config_id FROM shared.tenants WHERE slug = %s",
                (slug,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    _TENANT_COMPANY_CACHE[slug] = row[0] if row else None
    return _TENANT_COMPANY_CACHE[slug]
```

3. `worker/tasks/pipeline.py` (~строки 763–767) — резолв меняется так:

```python
    session_meta = _get_session_metadata(session_id)
    # company/scenario — server-owned (спека 5.6): из клиентских метаданных
    # сессии НЕ читаются. company — из shared.tenants, scenario — только из
    # явного config (ops-скрипты/reprocess).
    company_id = config.get("company_id") or tenant_company_config_id()
    scenario_id = config.get("scenario_id")
    company_config = load_company_config(company_id)
    scenario = get_scenario(company_config, scenario_id)
```

(импортировать `tenant_company_config_id` из `tasks.company_config`; ВНИМАНИЕ: проверить фактический текущий блок в файле — условие `if not config.get("company_id") else {}` у `session_meta` убрать нельзя бездумно: `session_meta` используется ниже (lead_id/phone для push) — сохранить его безусловную загрузку, как показано.)

4. `worker/tasks/pipeline.py`, ВТОРОЙ сайт резолва — `_process_session_from_file_body` (~строки 825–830, путь upload-audio/файловых сессий): тот же фикс —

```python
    company_id = config.get("company_id") or tenant_company_config_id()
    scenario_id = config.get("scenario_id")
```

(клиентские фоллбеки `session_meta.get("company_id")`/`session_meta.get("scenario_id")` убрать; сам `session_meta` оставить — нужен ниже.)

- [ ] **Step 5: Прогнать оба suite целиком**

Run: worker + backend полные сьюты. Существующие тесты pipeline, монкипатчащие company-резолв, могут потребовать обновления — фиксируй в отчёте, обновляй только их assertions на новый источник (`tenant_company_config_id`), не ослабляя их.

- [ ] **Step 6: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/routes/sessions.py backend/tests/test_server_owned_metadata.py worker/tasks/company_config.py worker/tasks/pipeline.py worker/tests/test_company_from_tenant.py
git commit -m "feat(tenancy): company_id/scenario_id are server-owned; worker resolves company from tenant row"
```

---

### Task 14: Worker — гейт AmoCRM-push по тенанту + пер-тенант dashboard_base_url

**Files:**
- Modify: `worker/tasks/pipeline.py` (гейт push, ~строка 738)
- Modify: `worker/tasks/amocrm_sync.py` (`_dashboard_base_url()`, сайт ~строка 743)
- Test: `worker/tests/test_push_tenant_gate.py`, `worker/tests/test_dashboard_url.py`

- [ ] **Step 1: Failing-тесты**

`worker/tests/test_push_tenant_gate.py`:

```python
"""AmoCRM push гейтится принадлежностью тенанта к AMOCRM_TENANT_SLUGS."""
from unittest import mock

from tenancy.registry import AMOCRM_TENANT_SLUGS

import tasks.pipeline as pl


def test_amocrm_tenant_slugs_is_realestate_only():
    assert AMOCRM_TENANT_SLUGS == ("realestate",)


def test_push_to_amocrm_noops_for_foreign_tenant(monkeypatch):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    # любой AmoCRM-вызов внутри тела должен быть недостижим
    monkeypatch.setattr(pl, "find_lead_by_phone", mock.MagicMock(side_effect=AssertionError),
                        raising=False)
    token = set_tenant_schema("t_acme")
    try:
        # ранний return до любых обращений к CRM/БД
        pl._push_to_amocrm("sid", {}, {}, "/tmp/x.wav")
    finally:
        reset_tenant_schema(token)


def test_pipeline_gates_lead_resolution():
    import inspect
    src = inspect.getsource(pl)
    assert "amocrm_enabled" in src  # сторожевой: lead-резолв гейтится флагом
```

(Поведенческий тест push-сайта требует исполнения всего `_run_pipeline_inner` — несоразмерно; сторожевой source-тест + ревью достаточны, по образцу существующего `test_no_raw_connects.py`.)

`worker/tests/test_dashboard_url.py`:

```python
"""Пер-тенант dashboard_base_url для ссылок в AmoCRM-нотах (спека 5.5)."""
from unittest import mock

import pytest

from tenancy.context import reset_tenant_schema, set_tenant_schema

import tasks.amocrm_sync as ams


@pytest.fixture(autouse=True)
def clear_cache():
    ams._DASHBOARD_URL_CACHE.clear()
    yield
    ams._DASHBOARD_URL_CACHE.clear()


def _fake_conn(value):
    conn = mock.MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (value,)
    return conn


def test_uses_tenant_row_value(monkeypatch):
    monkeypatch.setattr(ams, "shared_connect", lambda: _fake_conn("https://rogov.automate-it.fun"))
    token = set_tenant_schema("t_realestate")
    try:
        assert ams._dashboard_base_url() == "https://rogov.automate-it.fun"
    finally:
        reset_tenant_schema(token)


def test_falls_back_to_subdomain(monkeypatch):
    monkeypatch.setattr(ams, "shared_connect", lambda: _fake_conn(None))
    monkeypatch.setenv("BASE_DOMAIN", "silentqa.com")
    token = set_tenant_schema("t_acme")
    try:
        assert ams._dashboard_base_url() == "https://acme.silentqa.com"
    finally:
        reset_tenant_schema(token)


def test_no_tenant_context_uses_env_global():
    assert ams._dashboard_base_url() == ams.DASHBOARD_BASE_URL
```

- [ ] **Step 2: Прогнать — падают.**

- [ ] **Step 3: Реализация pipeline.py**

Module-top импорт: `from tenancy.registry import AMOCRM_TENANT_SLUGS` (рядом с другими tenancy-импортами; `require_tenant_slug` там уже импортирован — проверить). КРИТИЧНО (находка ревью): `_push_to_amocrm` зовётся из ТРЁХ сайтов (~536 short-call, ~560 broken-recording, ~739 основной), а ДО push в `_run_pipeline_inner` есть негейченные AmoCRM-вызовы (~676–689: `find_lead_by_phone`, `get_lead_stage`, `fetch_lead_events`) — без гейта чужой тенант дёргает CRM realestate и тащит данные его лидов в свой LLM-промпт.

1. Членство — внутрь `_push_to_amocrm` (ранний return закрывает все 3 сайта и будущие):

```python
def _push_to_amocrm(session_id, quality_report, session_meta, audio_path, **kwargs):
    slug = require_tenant_slug()
    if slug not in AMOCRM_TENANT_SLUGS:
        logger.info(
            f"[{session_id}] AmoCRM push skipped: tenant '{slug}' has no AmoCRM integration"
        )
        return
    ...существующее тело...
```

2. В `_run_pipeline_inner` перед блоком резолва лида (~676) завести флаг и гейтить им lead-резолв/стейдж/события:

```python
        amocrm_enabled = require_tenant_slug() in AMOCRM_TENANT_SLUGS
        if not lead_id and phone and amocrm_enabled:
            ...find_lead_by_phone как сейчас...
        deal_stage = get_lead_stage(lead_id) if (lead_id and amocrm_enabled) else None
        # аналогично fetch_lead_events / prior_context-ветку — свериться с
        # фактическим кодом ~676–689 и гейтить каждый AmoCRM-вызов
```

- [ ] **Step 4: Реализация amocrm_sync.py**

Импорты дополнить: `from tenancy.context import get_tenant_slug` и `from tenancy.db import shared_connect`. Глобал `DASHBOARD_BASE_URL` (строка ~25) ОСТАВИТЬ (инвариант 7). Добавить под ним:

```python
_DASHBOARD_URL_CACHE: dict[str, str] = {}


def _dashboard_base_url() -> str:
    """База ссылок на дашборд для текущего тенанта (спека 5.5).

    Приоритет: shared.tenants.dashboard_base_url → https://{slug}.{BASE_DOMAIN}
    → env DASHBOARD_BASE_URL (dev-фоллбек без тенант-контекста).
    """
    slug = get_tenant_slug()
    if slug is None:
        return DASHBOARD_BASE_URL
    if slug in _DASHBOARD_URL_CACHE:
        return _DASHBOARD_URL_CACHE[slug]
    url = None
    try:
        conn = shared_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT dashboard_base_url FROM shared.tenants WHERE slug = %s",
                    (slug,),
                )
                row = cur.fetchone()
                url = row[0] if row and row[0] else None
        finally:
            conn.close()
    except Exception:
        logger.exception("dashboard_base_url lookup failed; using fallback")
    if not url:
        url = f"https://{slug}.{os.getenv('BASE_DOMAIN', 'silentqa.com')}"
    _DASHBOARD_URL_CACHE[slug] = url
    return url
```

Сайт ссылки (строка ~743): `parts.append(f"Подробнее: {_dashboard_base_url()}/#call/{session_id}")`.
Затем `grep -n "DASHBOARD_BASE_URL" worker/tasks/*.py` — если ссылки строятся ещё где-то (например, deal_summary.py), перевести те сайты на `_dashboard_base_url()` тоже (импортом из amocrm_sync); прочие использования глобала не трогать.

- [ ] **Step 5: Прогнать полный worker-suite — зелёный.**

- [ ] **Step 6: Commit**

```bash
cd /root/projects/meet-mt && git add worker/tasks/pipeline.py worker/tasks/amocrm_sync.py worker/tests/test_push_tenant_gate.py worker/tests/test_dashboard_url.py
git commit -m "feat(tenancy): gate amocrm push by tenant; per-tenant dashboard links"
```

---

### Task 15: Снять BasicAuthMiddleware, X-Delete-Password, /recorder

**Files:**
- Modify: `backend/app/main.py` (удалить класс BasicAuthMiddleware целиком — X-Delete-Password-ветка снята ещё в Task 9, здесь уходят остатки Basic для UI-роутов; `app.add_middleware(BasicAuthMiddleware)`; роут `/recorder` ~строки 140–142; неиспользуемые импорты `FileResponse`/`base64`/`secrets` — проверить)
- Modify: `backend/app/config.py` (удалить `DELETE_PASSWORD` с комментарием; `AUTH_USERNAME`/`AUTH_PASSWORD` ОСТАЮТСЯ — инвариант 5, дополнить их комментарием `# interim gate for /api/companies until Plan 3`)
- Modify: `.env.example` (удалить DELETE_PASSWORD; добавить новые настройки)
- Test: дополнение в `backend/tests/test_access_matrix.py`

Предусловие: Tasks 7–12 завершены (инвариант 6).

- [ ] **Step 1: Failing-тест** — в `test_access_matrix.py`:

```python
def test_ui_routes_public_no_basic(client):
    # SPA-статика публична by design (спека 5.2): / отдаёт index.html без Basic
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_recorder_route_removed(client):
    # роут указывал на отсутствующий static/recorder.html — удалён
    r = client.get("/recorder")
    assert r.status_code in (404, 200)  # 404 или index.html от SPA-fallback
    # главное: это больше не FileResponse несуществующего файла (раньше — 500)
```

- [ ] **Step 2: Прогнать** — `test_ui_routes_public_no_basic` FAIL (сейчас Basic → 401).

- [ ] **Step 3: Реализация** — удаления по списку Files. В `.env.example` удалить строки DELETE_PASSWORD (с комментарием) и добавить в конец:

```
# Dashboard auth (Plan 2)
# SESSION_COOKIE_SECURE=false  # только для локальной http://-разработки
# ISO-дата конца grace-окна брокерских JWT без tenant-claim (деплой + 30 дней)
BROKER_JWT_TENANT_GRACE_UNTIL=
```

- [ ] **Step 4: Прогнать оба suite — зелёные. Дополнительно глазами:** `grep -rn "DELETE_PASSWORD\|X-Delete-Password" backend/ --include=*.py` → пусто.

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/main.py backend/app/config.py .env.example backend/tests/test_access_matrix.py
git commit -m "feat(auth): retire BasicAuthMiddleware, X-Delete-Password and /recorder"
```

---

### Task 16: SPA — экран логина, роли, чистка delete-password

**Files:**
- Modify: `backend/static/app.js`
- Modify: `backend/static/index.html` (кнопка выхода в навигации)
- Modify: `backend/static/styles.css` (стили логин-экрана)

JS-тестов в проекте нет — приёмка: ручной чек-лист Step 6 + матричные тесты backend уже гарантируют контракт API.

- [ ] **Step 1: Состояние и 401-обработка в `api()`**

В `backend/static/app.js` рядом с существующим состоянием (строки ~12–14) добавить:

```javascript
let currentUser = null; // {email, role} после логина
const isAdmin = () => currentUser && currentUser.role === 'admin';
```

Обёртку `api()` (строки ~77–88) дополнить: ПЕРЕД существующим `if (!res.ok) throw ...`:

```javascript
    if (res.status === 401) {
      let detail = '';
      try { detail = (await res.clone().json()).detail || ''; } catch (e) {}
      // Редирект на логин ТОЛЬКО для cookie-гейченных ответов (спека 5.2):
      // ошибки API-ключа/брокерского JWT сюда не относятся.
      if (detail === 'auth_required') {
        showLogin();
        throw new Error('auth required');
      }
    }
```

- [ ] **Step 2: Экран логина + boot-последовательность**

Добавить в `app.js` (рядом с router()):

```javascript
async function bootAuth() {
  try {
    currentUser = await api('/api/user-auth/me');
    return true;
  } catch (e) {
    return false;
  }
}

function showLogin() {
  currentUser = null;
  const el = document.getElementById('app') || document.body;
  el.innerHTML = `
    <div class="login-screen">
      <form id="login-form" class="login-card">
        <h2>Вход в дашборд</h2>
        <input type="email" id="login-email" placeholder="Email" required autocomplete="username">
        <input type="password" id="login-password" placeholder="Пароль" required autocomplete="current-password">
        <button type="submit">Войти</button>
        <div id="login-error" class="login-error"></div>
      </form>
    </div>`;
  document.getElementById('login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const errEl = document.getElementById('login-error');
    errEl.textContent = '';
    try {
      const res = await fetch('/api/user-auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: document.getElementById('login-email').value.trim(),
          password: document.getElementById('login-password').value,
        }),
      });
      if (!res.ok) {
        errEl.textContent = res.status === 429
          ? 'Слишком много попыток — подождите 5 минут'
          : 'Неверный email или пароль';
        return;
      }
      currentUser = await res.json();
      window.location.reload();
    } catch (e) {
      errEl.textContent = 'Сервер недоступен';
    }
  });
}

async function logout() {
  try { await fetch('/api/user-auth/logout', { method: 'POST' }); } catch (e) {}
  showLogin();
}
```

Точка входа сейчас (строки ~70–74): `window.addEventListener('hashchange', router); window.addEventListener('load', () => { checkHealth(); router(); });` — заменить, СОХРАНИВ load-обёртку и checkHealth():

```javascript
window.addEventListener('hashchange', router);
window.addEventListener('load', () => {
  checkHealth();
  bootAuth().then((ok) => { if (ok) { router(); } else { showLogin(); } });
});
```

(проверить фактический контейнер: если в `index.html` корневой элемент не `#app` — использовать его id; посмотреть разметку перед правкой).

- [ ] **Step 3: Кнопка выхода** — в `index.html` в навигацию добавить:

```html
<button class="nav-logout" onclick="logout()" title="Выйти">Выйти</button>
```

и в `styles.css` минимальные стили `.login-screen/.login-card/.login-error/.nav-logout` (центрированная карточка, в духе существующих стилей — посмотреть переменные/классы рядом).

- [ ] **Step 4: Чистка X-Delete-Password + сокрытие admin-действий**

1. Удалить `ensureDeletePassword()` (строки ~90–98) и ВСЕ места, где собирается заголовок `X-Delete-Password` (grep по файлу; фактические сайты: deleteSession ~1545, saveTemplate PATCH/POST ~1922/1928, deleteTemplate ~1960, relinkExtraction ~2491, renameComplex ~2600, deleteComplex ~2620; merge-кнопки в SPA нет) — вызовы остаются, заголовок и prompt убрать.
2. Сокрытие admin-действий для viewer (`isAdmin()`): обернуть рендер кнопок:
   - renderCallDetail: кнопки «Удалить» и «Переоценить» (~строки 431–432) и reassign-speaker UI;
   - renderTemplates: «+ Новый шаблон» (~1815) и иконки удаления (~1823); renderTemplateEdit — открывать только admin (в router добавить guard: `#template/...` → если !isAdmin() → navigate('#templates'));
   - renderComplexDetail: «Переименовать»/«Удалить»/merge (~2576–2577) и relink;
   - паттерн: `${isAdmin() ? `<button ...>...</button>` : ''}`.
3. `renderCompanies()`: обернуть загрузку в try/catch — при 404 показать `«Раздел доступен только платформенному администратору»` (после Task 10 на тенант-контуре /api/companies отдаёт 404).
4. Форма ручной загрузки (renderUpload, селекторы `#uploadCompany`/`#uploadScenario` ~строки 1350/1357, запись в metadata ~1458–1470): УДАЛИТЬ селекторы company/scenario и их запись в metadata — после Task 13 сервер вычищает эти ключи, UI стал бы мёртвым и вводящим в заблуждение. Выбор шаблона (template_id) остаётся — он server-owned не является.

- [ ] **Step 5: Синтаксис-смоук**

Run: `node --check /root/projects/meet-mt/backend/static/app.js`
Expected: без ошибок.

- [ ] **Step 6: Ручной чек-лист (записать результаты в отчёт)**

Поднять backend локально НЕЛЬЗЯ против прод-БД — для смоука достаточно статической проверки: `grep -c "X-Delete-Password" app.js` → 0; `grep -c "ensureDeletePassword" app.js` → 0; login-форма присутствует; `isAdmin()` используется во всех перечисленных render-функциях (grep).

- [ ] **Step 7: Commit**

```bash
cd /root/projects/meet-mt && git add backend/static/app.js backend/static/index.html backend/static/styles.css
git commit -m "feat(spa): login screen, role-aware UI, drop delete-password prompts"
```

---

### Task 17: Recorder-клиенты — X-API-Key (desktop + расширения)

**Files:**
- Modify: `desktop-app/src/renderer/recorder.js` (~строки 19–21, 62–66, 667)
- Modify: `desktop-app/src/renderer/index.html` (разметка setup-формы, input #setupServerUrl ~строка 40)
- Modify: `desktop-app/src/renderer/renderer.js` (чтение поля + persistCredentials ~249–258, init ~726–785)
- Modify: `extension/background.js`, `extension/offscreen.js`
- Modify: `extension-yandex/background.js`, `extension-yandex/offscreen.js`

Сервер уже принимает `X-API-Key` (Task 4); у realestate `api_key_required=false`, поэтому клиенты без ключа продолжают работать — поле опциональное (спека 6.4).

- [ ] **Step 1: desktop recorder.js**

Рядом с `let brokerToken` (~строка 21): `let apiKey = '';`
`buildHeaders()` (~62–66):

```javascript
function buildHeaders(extra = {}, auth = authHeader) {
  const h = { Authorization: auth };
  if (brokerToken) h['X-Broker-Token'] = brokerToken;
  if (apiKey) h['X-API-Key'] = apiKey;
  return Object.assign(h, extra);
}
```

Рядом с `setBrokerToken` (~667) добавить и ЭКСПОРТИРОВАТЬ тем же способом (посмотреть, как экспортирован setBrokerToken — window.Recorder):

```javascript
function setApiKey(key) { apiKey = key || ''; }
```

- [ ] **Step 2: desktop renderer.js**

1. Разметка живёт в `desktop-app/src/renderer/index.html` (НЕ в renderer.js): рядом с `#setupServerUrl` (~строка 40) добавить input `id="setupApiKey"`, placeholder `API-ключ (опционально, sqa_...)`, type=password.
2. В renderer.js: в обработчике сохранения настроек (~220–235, где читается `setupServerUrl.value`) читать `setupApiKey.value`; `persistCredentials()` (~249–258): включить `apiKey` в сохраняемый объект (значение из поля либо из текущих creds).
3. `init()` (~726–785): после загрузки creds — `if (window.Recorder.setApiKey) window.Recorder.setApiKey(creds.apiKey);`
4. Проверить `src/main/index.js` save-credentials (~152–165): если он сохраняет объект целиком — изменений не нужно; если перечисляет поля — добавить `apiKey`.

- [ ] **Step 3: расширения (обе копии: extension/ и extension-yandex/)**

1. `background.js` `startRecording()` (~20–43): в `chrome.storage.local.get([...])` добавить `'apiKey'`; включить `apiKey` в сообщение offscreen-документу рядом с serverUrl/authUsername/authPassword.
2. `offscreen.js`: принять `apiKey` из сообщения (рядом с историей authHeader ~строка 91), и во всех ТРЁХ местах заголовков (~29, 53, 74) добавить:

```javascript
...(apiKey ? { "X-API-Key": apiKey } : {}),
```

(значение задаётся через `chrome.storage.local.set({apiKey: 'sqa_...'})` в консоли расширения; options-страница — Фаза 2.)

- [ ] **Step 4: Смоук**

Run: `node --check` на все 6 изменённых js-файлов.
Expected: без ошибок.

- [ ] **Step 5: Commit**

```bash
cd /root/projects/meet-mt && git add desktop-app/src extension extension-yandex
git commit -m "feat(recorder): optional X-API-Key in desktop app and extensions"
```

---

### Task 18: Ранбук Plan 2 + отметки выкатки

**Files:**
- Modify: `docs/superpowers/specs/2026-06-10-phase1-deploy-runbook.md`

- [ ] **Step 1: Дописать в конец ранбука раздел**

```markdown
## Plan 2 (auth) — дополнение к деплою

Деплой Plan 2 идёт ТЕМ ЖЕ релизом, что и ядро (одна ветка). Дополнительно к
шагам выше:

1. Перед рестартом backend в .env добавить:
   `BROKER_JWT_TENANT_GRACE_UNTIL=<дата деплоя + 30 дней, YYYY-MM-DD>`
   (без неё все ранее выданные брокерские JWT умрут сразу — спека 5.4).
2. Убедиться, что admin-юзер посеян (сид-CLI шаг 7 выше; иначе после снятия
   Basic в дашборд никто не войдёт — риск «локаут» из спеки §10).
   Проверка: `POST /api/user-auth/login` с сид-кредами → 200 + Set-Cookie.
3. DELETE_PASSWORD из .env можно удалить (больше не читается).
   AUTH_USERNAME/AUTH_PASSWORD ОСТАВИТЬ — interim-гейт /api/companies
   (доступен только с платформенного контура: Host=silentqa.com или IP
   при пустом DEFAULT_TENANT).
4. Прод-статика realestate: из каталога Caddy (/home/dev/projects/...)
   удалить посторонние zip-артефакты расширений, если лежат (спека 5.2);
   в этом репо их нет.
5. Smoke после рестарта:
   - GET / → index.html БЕЗ Basic-промпта; логин-форма в SPA;
   - login viewer-юзером → кнопки удаления/переоценки скрыты;
   - POST /api/sessions без ключа → 201 (api_key_required=false);
   - POST /api/sessions с X-API-Key: sqa_wrong → 403;
   - GET /api/companies на тенант-домене → 404; на платформенном — 401/Basic;
   - GET /api/webhooks → 404;
   - звонок из AmoCRM-поллера: нота содержит ссылку на
     https://rogov.automate-it.fun/#call/... (dashboard_base_url из S002).
6. Завершение rollout (через ≤30 дней, по факту обновления клиентов):
   `UPDATE shared.tenants SET api_key_required = TRUE WHERE slug='realestate';`
   и удалить BROKER_JWT_TENANT_GRACE_UNTIL из .env (конец grace).
   Ключ генерится вручную кодом _set_api_key (флага ротации в CLI нет):
   `cd backend && ../.venv/bin/python -c "
   from app.provision_tenant import _set_api_key
   from tenancy.db import shared_connect
   conn = shared_connect(); print(_set_api_key(conn, 'realestate')); conn.commit(); conn.close()"`
   — вывод показывается один раз, раздаётся клиентам.

### Откат Plan 2
Код-откат = git revert ветки; данных Plan 2 не создаёт (users уже была в 012,
сессии в Redis истекают сами). После отката вернуть DELETE_PASSWORD в .env.
```

- [ ] **Step 2: Commit**

```bash
cd /root/projects/meet-mt && git add docs/superpowers/specs/2026-06-10-phase1-deploy-runbook.md
git commit -m "docs(runbook): plan 2 auth rollout, smoke and rollback"
```

---

## Вне этого плана (Plan 3 — платформа)

- Platform-admin сессии (`padmin:sess:{sid}`), логин на admin.silentqa.com, замена interim-Basic на /api/companies.
- Cloudflare/Hetzner DNS + wildcard TLS, nginx/KZ-relay для `*.silentqa.com`, перевод realestate-дашборда с легаси-домена.
- Админ-UI провижининга; options-страница API-ключа в расширениях; ротация ключей.

## Self-review checklist (выполнен автором плана)

- Spec-покрытие: 5.1 (interim: Task 10; полноценно — Plan 3), 5.2 (Tasks 1–5, 15, 16), 5.3 (Task 4), 5.4 (Task 6), 5.5 (Task 14), 5.6 (Tasks 7–13, 15), 6.1 (Task 17), 6.4 (флаг уже в БД; enforcement Task 4; процедура Task 18), 7 (Task 16), 9-auth (тесты Tasks 2, 4, 5, 6, 7–12).
- Типы согласованы: `UserCtx(user_id, email, role)` единый (Tasks 4, 5); `SESSION_COOKIE = "sqa_session"` (Tasks 2, 4, 5, 7); `require_*`-имена единые во всех matrix-тасках; `check_tenant_claim(payload)` (Task 6); `tenant_company_config_id()` (Task 13); `_dashboard_base_url()` (Task 14).
- Безопасность: timing-equalized login (dummy argon2), compare_digest для ключей/Basic, host-only Secure cookie, SameSite=Lax, rate-limit, двойное совпадение API-ключа, server-owned metadata.
- Порядок задач: матрица (7–12) ДО снятия Basic (15); SPA (16) ПОСЛЕ снятия (иначе UI ломался бы об Basic).
