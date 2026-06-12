# Plan 3b: платформенная админка + «Команда» + роль manager

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** веб-админка платформы на admin.silentqa.com (управление клиентами, статистика, impersonation), раздел «Команда» в клиентском дашборде и роль `manager` с видимостью «только своё».

**Architecture:** то же FastAPI-приложение, контур выбирается по Host (спека `docs/superpowers/specs/2026-06-12-platform-admin-and-team-dashboards-design.md`, секции 1–5, 8–10). Платформенные сессии — отдельный Redis-неймспейс `platform:*` и cookie `sqa_admin`; изоляция контуров — гейт по пути внутри `TenantResolutionMiddleware`. Провижининг переиспользует CLI-логику через рефактор `main()` → `provision()`.

**Tech Stack:** FastAPI, psycopg2 (провижининг), SQLAlchemy async (роуты), Redis (sessions/rate-limit/imp-токены), argon2-cffi, vanilla JS SPA.

**Рабочая папка:** `/root/projects/meet-mt` (worktree ветки `multi-tenant-core-phase1`). Тесты: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/ -q`. База перед стартом: **81 passed**.

**Конвенции (обязательны):**
- TDD: тест → fail → код → pass → commit. Негативные кейсы тестируются без БД (auth-зависимости отрабатывают до первого SQL) — стиль `tests/test_access_matrix.py`.
- Фикстуры: `fake_redis` из conftest; `client`-стиль — monkeypatch `TenantRegistry.all_tenants` + `TestClient(app, base_url="https://<host>")`. TestClient БЕЗ context manager (lifespan гонит реальные миграции!).
- `asyncio.run(...)` для сидинга в тестах (НЕ `get_event_loop`).
- Идентификаторы: только `SLUG_RE/SCHEMA_RE.fullmatch` (re.match — известный баг).
- Коммиты после каждой задачи, сообщения `feat(scope): ...` на русском, как в истории ветки.

---

## Файловая карта

| Файл | Роль |
|---|---|
| `backend/alembic_shared/versions/s003_platform_admin_totp.py` | создать: totp_secret |
| `backend/alembic/versions/013_manager_role_employee_name.py` | создать: CHECK+employee_name+индекс |
| `backend/app/tenancy_http.py` | изменить: suspended→403, singleton registry + invalidate, path-гейт контуров |
| `backend/app/platform_sessions.py` | создать: платформенные сессии + rate-limit |
| `backend/app/auth_sessions.py` | изменить: `_register_attempt` рефактор, kwargs у create_session, destroy_user_sessions |
| `backend/app/auth_platform.py` | создать: PlatformAdminCtx, require_platform_admin |
| `backend/app/routes/platform_auth.py` | создать: /api/platform/auth/* |
| `backend/app/routes/platform_tenants.py` | создать: /api/platform/tenants* |
| `backend/app/platform_admin.py` | создать: CLI бутстрапа |
| `backend/app/provision_tenant.py` | изменить: рефактор в provision() |
| `backend/app/routes/user_auth.py` | изменить: impersonate, change-password, employee_name в сессии |
| `backend/app/routes/users.py` | создать: /api/users (Команда) |
| `backend/app/auth_user.py` | изменить: UserCtx поля, employee_scope, require_session_access; удалить require_platform_admin_basic |
| `backend/app/routes/companies.py` | изменить: гейт → require_platform_admin |
| `backend/app/routes/sessions.py`, `managers.py`, `transcripts.py`, `analysis.py` | изменить: manager-scoping |
| `backend/app/main.py` | изменить: роутеры, GET / диспетч |
| `backend/app/config.py` | изменить: удалить AUTH_USERNAME/AUTH_PASSWORD |
| `backend/static/admin.html`, `admin.js` | создать: админ-SPA |
| `backend/static/app.js`, `index.html` | изменить: Команда/Профиль/бейдж |
| `backend/tests/...` | тесты на всё новое + расширение матрицы |

---

### Task 1: Миграции S003 и 013

**Files:**
- Create: `backend/alembic_shared/versions/s003_platform_admin_totp.py`
- Create: `backend/alembic/versions/013_manager_role_employee_name.py`

- [x] **Step 1: написать обе миграции**

`s003_platform_admin_totp.py`:
```python
"""platform_admins.totp_secret — задел под TOTP (спека §2).

Revision ID: s003
Revises: s002
"""
from typing import Sequence, Union

from alembic import op

revision: str = "s003"
down_revision: Union[str, None] = "s002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE shared.platform_admins ADD COLUMN totp_secret TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE shared.platform_admins DROP COLUMN totp_secret")
```

`013_manager_role_employee_name.py`:
```python
"""Роль manager + привязка employee_name + индекс под агрегаты (спека §5, §8).

Revision ID: 013
Revises: 012
"""
from typing import Sequence, Union

from alembic import op

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # CHECK создан инлайном в 012 → автоимя users_role_check
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check")
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT users_role_check "
        "CHECK (role IN ('admin', 'viewer', 'manager'))"
    )
    op.execute("ALTER TABLE users ADD COLUMN employee_name TEXT")
    op.execute(
        "CREATE INDEX ix_sessions_employee_created "
        "ON sessions ((metadata->>'employee'), created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_sessions_employee_created")
    op.execute("ALTER TABLE users DROP COLUMN employee_name")
    op.execute("ALTER TABLE users DROP CONSTRAINT users_role_check")
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT users_role_check "
        "CHECK (role IN ('admin', 'viewer'))"
    )
```

- [x] **Step 2: прогон на скретч-БД**

```bash
sudo -u postgres createdb plan3b_scratch 2>/dev/null || sudo -u postgres dropdb plan3b_scratch && sudo -u postgres createdb plan3b_scratch
cd /root/projects/meet-mt/backend
DATABASE_URL_SYNC="postgresql://postgres@/plan3b_scratch?host=/var/run/postgresql" \
DATABASE_URL="postgresql+asyncpg://postgres@/plan3b_scratch?host=/var/run/postgresql" \
SECRET_KEY=x ../.venv/bin/python -m app.migrate
```
Expected: `[migrate] done`, без traceback. Проверить:
```bash
sudo -u postgres psql plan3b_scratch -c "\d shared.platform_admins" | grep totp_secret
```
Expected: строка `totp_secret | text`. (Тенант-трек 013 проверится в Task 19 через provision на скретч.)

- [x] **Step 3: commit**

```bash
cd /root/projects/meet-mt && git add backend/alembic_shared backend/alembic && git commit -m "feat(db): S003 totp_secret + 013 роль manager, employee_name, индекс employee"
```

---

### Task 2: suspended → 403, singleton-реестр с invalidate()

**Files:**
- Modify: `backend/app/tenancy_http.py`
- Modify: `backend/app/main.py` (использовать singleton)
- Modify: `backend/app/routes/tenancy_check.py` (использовать singleton)
- Test: `backend/tests/test_tenant_middleware.py` (дополнить)

- [x] **Step 1: тесты** — в `test_tenant_middleware.py` добавить (следуя стилю существующего `_app()`-хелпера файла):

```python
def test_suspended_tenant_host_returns_403():
    rows = [{"slug": "frozen", "schema_name": "t_frozen", "status": "suspended",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True}]
    client = _client_for(rows)  # хелпер файла; host frozen.silentqa.com
    resp = client.get("/api/sessions", headers={"Host": "frozen.silentqa.com"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "tenant_suspended"


def test_registry_invalidate_drops_cache():
    from app.tenancy_http import TenantRegistry
    reg = TenantRegistry(ttl_seconds=3600)
    reg._rows = [{"slug": "x"}]
    reg._loaded_at = 1e18  # «только что»
    reg.invalidate()
    assert reg._loaded_at == 0.0
```
(адаптировать к фактическим хелперам файла; если `_client_for` нет — собрать client как в conftest, с monkeypatch `all_tenants`.)

- [x] **Step 2: прогон** — `../.venv/bin/python -m pytest tests/test_tenant_middleware.py -q` → новые FAIL (suspended сейчас 404).

ВНИМАНИЕ (ревью): существующий `test_suspended_tenant_404` в этом файле ждёт 404 — переписать его на 403 `tenant_suspended` (и переименовать в `test_suspended_tenant_403`) в этом же шаге.

- [x] **Step 3: имплементация** в `tenancy_http.py`:

```python
# в TenantRegistry:
    def invalidate(self) -> None:
        """Сбросить TTL-кэш (вызывается после мутаций shared.tenants)."""
        self._loaded_at = 0.0

# module-level singleton — main.py и tenancy_check.py импортируют его,
# платформенные роуты дёргают invalidate() после suspend/rotate/create:
registry = TenantRegistry()
```

`_resolve`/`_gate`: suspended больше не «notfound», а отдельный kind:
```python
    @staticmethod
    def _gate(row):
        if row["status"] != "active":
            return "suspended", None
        return "tenant", row
```
в `__call__` после `_resolve`:
```python
        if kind == "suspended":
            resp = JSONResponse({"detail": "tenant_suspended"}, status_code=403)
            return await resp(scope, receive, send)
```
В `main.py`: `app.add_middleware(TenantResolutionMiddleware, registry=registry, ...)` (импорт `from app.tenancy_http import registry`). В `tenancy_check.py`: `_registry = TenantRegistry()` → `from ..tenancy_http import registry as _registry`.

ВАЖНО: docstring модуля «Unknown slug / suspended tenant → 404» поправить на новое поведение.

- [x] **Step 4: прогон** — весь файл PASS + `pytest tests/ -q` (никого не сломали; в `test_domain_check.py` suspended-тенант и так ждёт 404 от domain-check — это другой слой, должен остаться зелёным).

- [x] **Step 5: commit** — `feat(tenancy): suspended-тенант отвечает 403, реестр-синглтон с invalidate()`

---

### Task 3: гейт контуров по пути (платформенный ↔ тенантский)

**Files:**
- Modify: `backend/app/tenancy_http.py`
- Test: `backend/tests/test_contour_gate.py` (создать)

Спека §1: на платформенном хосте живут только `/api/platform/*`, `/api/companies*`, `/api/tenancy/*`; на тенант-хосте эти три семейства → 404, остальные `/api/*` → как раньше. Не-`/api/` пути (статика, /health) не гейтятся.

- [x] **Step 1: тест** `tests/test_contour_gate.py`:

```python
"""Изоляция контуров (спека §1): пути чужого контура → 404 ДО auth."""
import pytest
from starlette.testclient import TestClient

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app)


PLATFORM_ONLY = ["/api/platform/tenants", "/api/platform/auth/me", "/api/companies"]
TENANT_ONLY = ["/api/sessions", "/api/managers", "/api/templates", "/api/user-auth/me"]


@pytest.mark.parametrize("path", PLATFORM_ONLY)
def test_platform_paths_404_on_tenant_host(client, path):
    assert client.get(path, headers={"Host": "acme.silentqa.com"}).status_code == 404


@pytest.mark.parametrize("path", TENANT_ONLY)
def test_tenant_paths_404_on_platform_host(client, path):
    for host in ("silentqa.com", "admin.silentqa.com"):
        assert client.get(path, headers={"Host": host}).status_code == 404


def test_domain_check_alive_on_both(client):
    for host in ("admin.silentqa.com", "acme.silentqa.com"):
        r = client.get("/api/tenancy/domain-check?domain=acme.silentqa.com",
                       headers={"Host": host})
        assert r.status_code == 200
```

- [x] **Step 2: прогон** → FAIL (сейчас тенантские пути на платформе дают 401/404 вразнобой, платформенных роутов нет — но `/api/companies` на тенант-хосте уже 404 через зависимость; тест унифицирует).

- [x] **Step 3: имплементация** — в `TenantResolutionMiddleware.__call__`, после установки contextvar, перед `await self.app(...)`:

```python
# class-level:
    PLATFORM_PREFIXES = ("/api/platform", "/api/companies", "/api/tenancy")

# в __call__ (внутри try, до self.app):
        path = scope.get("path", "")
        if path.startswith("/api/"):
            is_platform_path = path.startswith(self.PLATFORM_PREFIXES)
            on_platform = row is None
            if is_platform_path != on_platform and not path.startswith("/api/tenancy"):
                # /api/tenancy (domain-check) жив на обоих контурах
                resp = JSONResponse({"detail": "Not found"}, status_code=404)
                return await resp(scope, receive, send)
```
Логика: `/api/tenancy/*` — всегда ок; платформенные пути требуют платформенный контур; тенантские — тенантный. После этого 404-ветка `if get_tenant_slug() is None` в `user_auth.login/logout` и `require_platform_admin_basic` становится недостижимой по HTTP (оставить как defense-in-depth — НЕ удалять).

- [x] **Step 4: прогон** — файл PASS, затем `pytest tests/ -q`: «test_companies_platform» останется зелёным (он ходит платформенным хостом); если какой-то тест ходил тенантскими путями без Host — чинить тест, выставив правильный Host, а не ослаблять гейт.

- [x] **Step 5: commit** — `feat(tenancy): 404-гейт путей чужого контура в middleware`

---

### Task 4: platform_sessions.py + рефактор rate-limit

**Files:**
- Modify: `backend/app/auth_sessions.py`
- Create: `backend/app/platform_sessions.py`
- Test: `backend/tests/test_platform_sessions.py` (создать)

- [x] **Step 1: тест** `tests/test_platform_sessions.py`:

```python
import asyncio


def test_platform_session_roundtrip(fake_redis):
    from app import platform_sessions as ps

    async def flow():
        sid = await ps.create_platform_session("a1", "boss@x.io")
        data = await ps.load_platform_session(sid)
        assert data == {"admin_id": "a1", "email": "boss@x.io"}
        assert fake_redis.ttls[f"platform:sess:{sid}"] > 0
        await ps.destroy_platform_session(sid)
        assert await ps.load_platform_session(sid) is None

    asyncio.run(flow())


def test_platform_rate_limit_by_email(fake_redis, monkeypatch):
    from app import platform_sessions as ps
    from app.config import settings
    monkeypatch.setattr(settings, "LOGIN_RATE_MAX_ATTEMPTS", 3)

    async def flow():
        for _ in range(3):
            assert await ps.register_platform_login_attempt("1.1.1.1", "boss@x.io")
        assert not await ps.register_platform_login_attempt("2.2.2.2", "boss@x.io")

    asyncio.run(flow())


def test_tenant_rate_limit_still_works(fake_redis):
    """Рефактор _register_attempt не сломал тенантский путь."""
    import asyncio
    from tenancy.context import set_tenant_schema, reset_tenant_schema
    from app.auth_sessions import register_login_attempt

    async def flow():
        token = set_tenant_schema("t_acme")
        try:
            assert await register_login_attempt("1.1.1.1", "u@x.io")
        finally:
            reset_tenant_schema(token)

    asyncio.run(flow())
```

- [x] **Step 2: прогон** → FAIL (модуля нет).

- [x] **Step 3: имплементация.** В `auth_sessions.py` извлечь ядро лимитера (поведение 1:1):

```python
async def _register_attempt(email_key: str, ip_key: str) -> bool:
    r = get_redis()
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


async def register_login_attempt(ip: str, email: str) -> bool:
    # докстринг прежний (email основной, IP — backstop за relay)
    slug = require_tenant_slug()
    return await _register_attempt(
        f"t:{slug}:rl:login:email:{email.lower()}",
        f"t:{slug}:rl:login:ip:{ip}",
    )
```

`platform_sessions.py` (новый):
```python
"""Сессии платформенного админа: Redis platform:sess:*, cookie sqa_admin.

Отдельный неймспейс от тенантских t:{slug}:sess:* — украденный sid
платформы бесполезен на тенант-хосте и наоборот (разные cookie + разные
ключи). Контур уже отрезан middleware-гейтом, это второй пояс.
"""
from __future__ import annotations

import json
import secrets

from .auth_sessions import _register_attempt
from .config import settings
from .redis_client import get_redis

PLATFORM_COOKIE = "sqa_admin"


def _sess_key(sid: str) -> str:
    return f"platform:sess:{sid}"


async def create_platform_session(admin_id: str, email: str) -> str:
    sid = secrets.token_urlsafe(32)
    payload = json.dumps({"admin_id": admin_id, "email": email})
    await get_redis().set(_sess_key(sid), payload, ex=settings.SESSION_TTL_SECONDS)
    return sid


async def load_platform_session(sid: str) -> dict | None:
    raw = await get_redis().get(_sess_key(sid))
    return json.loads(raw) if raw else None


async def destroy_platform_session(sid: str) -> None:
    await get_redis().delete(_sess_key(sid))


async def register_platform_login_attempt(ip: str, email: str) -> bool:
    return await _register_attempt(
        f"platform:rl:login:email:{email.lower()}",
        f"platform:rl:login:ip:{ip}",
    )
```

- [x] **Step 4: прогон** — файл PASS + `tests/test_auth_sessions.py` PASS (рефактор без регрессии).

- [x] **Step 5: commit** — `feat(auth): платформенные сессии + общий _register_attempt`

---

### Task 5: auth_platform.py — зависимости платформенного контура

**Files:**
- Create: `backend/app/auth_platform.py`
- Test: `backend/tests/test_auth_platform.py` (создать)

- [x] **Step 1: тест**:

```python
import asyncio

import pytest
from fastapi import HTTPException


def _req(cookies=None, tenant=False):
    """Мини-стаб Request: cookies + тенант-контекст через contextvar."""
    class R:
        pass
    r = R()
    r.cookies = cookies or {}
    return r


def test_require_platform_admin_needs_session(fake_redis):
    from app.auth_platform import get_current_platform_admin, require_platform_admin

    async def flow():
        assert await get_current_platform_admin(_req()) is None
        with pytest.raises(HTTPException) as e:
            await require_platform_admin(None)
        assert e.value.status_code == 401
        assert e.value.detail == "platform_auth_required"

    asyncio.run(flow())


def test_platform_admin_none_on_tenant_contour(fake_redis):
    from tenancy.context import set_tenant_schema, reset_tenant_schema
    from app import platform_sessions as ps
    from app.auth_platform import get_current_platform_admin
    from app.platform_sessions import PLATFORM_COOKIE

    async def flow():
        sid = await ps.create_platform_session("a1", "boss@x.io")
        token = set_tenant_schema("t_acme")
        try:
            assert await get_current_platform_admin(_req({PLATFORM_COOKIE: sid})) is None
        finally:
            reset_tenant_schema(token)
        ctx = await get_current_platform_admin(_req({PLATFORM_COOKIE: sid}))
        assert ctx.email == "boss@x.io"

    asyncio.run(flow())
```

- [x] **Step 2: прогон** → FAIL.

- [x] **Step 3: имплементация** `auth_platform.py`:

```python
"""Auth-зависимости платформенного контура (спека §2)."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from tenancy.context import get_tenant_slug

from .platform_sessions import PLATFORM_COOKIE, load_platform_session


@dataclass
class PlatformAdminCtx:
    admin_id: str
    email: str


async def get_current_platform_admin(request: Request) -> PlatformAdminCtx | None:
    """Cookie sqa_admin → ctx; None на тенант-контуре/без сессии."""
    if get_tenant_slug() is not None:
        return None
    sid = request.cookies.get(PLATFORM_COOKIE)
    if not sid:
        return None
    data = await load_platform_session(sid)
    if not data:
        return None
    return PlatformAdminCtx(admin_id=data["admin_id"], email=data["email"])


async def require_platform_admin(
    admin: PlatformAdminCtx | None = Depends(get_current_platform_admin),
) -> PlatformAdminCtx:
    if admin is None:
        # detail-маркер: admin-SPA редиректит на логин только по нему
        raise HTTPException(status_code=401, detail="platform_auth_required")
    return admin
```

- [x] **Step 4: прогон** → PASS.
- [x] **Step 5: commit** — `feat(auth): зависимости платформенного админа`

---

### Task 6: /api/platform/auth — login/logout/me + CLI бутстрапа

**Files:**
- Create: `backend/app/routes/platform_auth.py`
- Create: `backend/app/platform_admin.py`
- Modify: `backend/app/main.py` (включить роутер)
- Test: `backend/tests/test_platform_auth_routes.py` (создать)

- [x] **Step 1: тест** (без БД — мокаем выборку админа; стиль `test_user_auth_routes.py`, посмотреть его и переиспользовать приёмы):

```python
"""Логин платформенного админа. БД мокается: _fetch_admin → словарь."""
import pytest
from starlette.testclient import TestClient

from argon2 import PasswordHasher

ROWS = []  # тенанты не нужны; платформенный хост


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://admin.silentqa.com")


@pytest.fixture
def admin_row(monkeypatch):
    ph = PasswordHasher()
    row = {"id": "a1", "email": "boss@x.io", "password_hash": ph.hash("s3cret")}

    async def fake_fetch(db, email):
        return row if email.lower() == row["email"] else None

    from app.routes import platform_auth
    monkeypatch.setattr(platform_auth, "_fetch_admin", fake_fetch)
    # last_login-апдейт без БД:
    async def fake_touch(db, admin_id):
        return None
    monkeypatch.setattr(platform_auth, "_touch_last_login", fake_touch)
    return row


def test_login_sets_cookie_and_me(client, admin_row):
    r = client.post("/api/platform/auth/login",
                    json={"email": "boss@x.io", "password": "s3cret"})
    assert r.status_code == 200
    assert "sqa_admin" in r.cookies
    me = client.get("/api/platform/auth/me")
    assert me.status_code == 200
    assert me.json() == {"email": "boss@x.io"}


def test_login_wrong_password_401(client, admin_row):
    r = client.post("/api/platform/auth/login",
                    json={"email": "boss@x.io", "password": "nope"})
    assert r.status_code == 401


def test_me_without_session_401(client):
    r = client.get("/api/platform/auth/me")
    assert r.status_code == 401
    assert r.json()["detail"] == "platform_auth_required"


def test_logout_kills_session(client, admin_row):
    client.post("/api/platform/auth/login",
                json={"email": "boss@x.io", "password": "s3cret"})
    client.post("/api/platform/auth/logout")
    assert client.get("/api/platform/auth/me").status_code == 401
```

- [x] **Step 2: прогон** → FAIL (роутера нет).

- [x] **Step 3: имплементация** `routes/platform_auth.py` (зеркало user_auth, но платформа):

```python
"""Логин платформенного админа: /api/platform/auth/* (спека §2)."""
from __future__ import annotations

import logging

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import platform_sessions
from ..auth_platform import PlatformAdminCtx, require_platform_admin
from ..config import settings
from ..database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/platform/auth", tags=["platform-auth"])

_ph = PasswordHasher()
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


async def _fetch_admin(db: AsyncSession, email: str):
    row = (
        await db.execute(
            text("SELECT id, email, password_hash FROM shared.platform_admins "
                 "WHERE LOWER(email) = LOWER(:email)"),
            {"email": email},
        )
    ).first()
    if row is None:
        return None
    return {"id": str(row.id), "email": row.email, "password_hash": row.password_hash}


async def _touch_last_login(db: AsyncSession, admin_id: str) -> None:
    await db.execute(
        text("UPDATE shared.platform_admins SET last_login = now() WHERE id = :id"),
        {"id": admin_id},
    )
    await db.commit()


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response,
                db: AsyncSession = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    if not await platform_sessions.register_platform_login_attempt(ip, body.email):
        raise HTTPException(status_code=429, detail="too_many_attempts")

    row = await _fetch_admin(db, body.email)
    stored = row["password_hash"] if row else _DUMMY_HASH
    if not _verify(stored, body.password) or row is None:
        raise HTTPException(status_code=401, detail="invalid_credentials")

    await _touch_last_login(db, row["id"])
    sid = await platform_sessions.create_platform_session(row["id"], row["email"])
    response.set_cookie(
        platform_sessions.PLATFORM_COOKIE, sid,
        httponly=True, secure=settings.SESSION_COOKIE_SECURE, samesite="lax",
        max_age=settings.SESSION_TTL_SECONDS, path="/",
    )
    logger.info("platform admin login: %s", row["email"])
    return {"email": row["email"]}


@router.post("/logout")
async def logout(request: Request, response: Response):
    sid = request.cookies.get(platform_sessions.PLATFORM_COOKIE)
    if sid:
        await platform_sessions.destroy_platform_session(sid)
    response.delete_cookie(platform_sessions.PLATFORM_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(admin: PlatformAdminCtx = Depends(require_platform_admin)):
    return {"email": admin.email}
```

`platform_admin.py` (CLI):
```python
"""Бутстрап платформенного админа.

  python -m app.platform_admin create --email boss@x.io [--password ...]

Пароль печатается ОДИН раз (если сгенерирован).
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

from argon2 import PasswordHasher

from tenancy.db import shared_connect, get_sync_db_url


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--email", required=True)
    c.add_argument("--password", default="")
    args = p.parse_args()

    if not get_sync_db_url():
        sys.exit("DATABASE_URL_SYNC / DATABASE_URL is not set")

    password = args.password or secrets.token_urlsafe(12)
    conn = shared_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO shared.platform_admins (email, password_hash) "
                "VALUES (%s, %s)",
                (args.email, PasswordHasher().hash(password)),
            )
        conn.commit()
    finally:
        conn.close()
    print(f"platform admin: {args.email}")
    if not args.password:
        print(f"password (shown once, store it now): {password}")


if __name__ == "__main__":
    main()
```

В `main.py`: добавить `platform_auth` в import-кортеж из `app.routes` (это МНОГОСТРОЧНЫЙ кортеж в скобках — менять аккуратно) и `app.include_router(platform_auth.router)`.

- [x] **Step 4: прогон** — файл PASS, `pytest tests/ -q` PASS.
- [x] **Step 5: commit** — `feat(platform): логин админа платформы + CLI бутстрапа`

---

### Task 7: GET / — admin.html на платформенном контуре

**Files:**
- Modify: `backend/app/main.py`
- Create: `backend/static/admin.html` (заглушка, полная версия в Task 17)
- Test: `backend/tests/test_root_dispatch.py` (создать)

- [x] **Step 1: тест**:

```python
import pytest
from starlette.testclient import TestClient

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app)


def test_platform_host_serves_admin_spa(client):
    r = client.get("/", headers={"Host": "admin.silentqa.com"})
    assert r.status_code == 200
    assert 'id="admin-app"' in r.text


def test_tenant_host_serves_client_spa(client):
    r = client.get("/", headers={"Host": "acme.silentqa.com"})
    assert r.status_code == 200
    assert 'id="admin-app"' not in r.text
```

- [x] **Step 2: прогон** → FAIL.

- [x] **Step 3: имплементация.** `static/admin.html` (заглушка):

```html
<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><title>silentqa — админка платформы</title></head>
<body><div id="admin-app">Админка платформы (UI — Task 17)</div></body>
</html>
```

`main.py` — явный роут "/" ПЕРЕД mount (роуты матчатся раньше mount):
```python
from fastapi.responses import FileResponse
from tenancy.context import get_tenant_slug as _get_tenant_slug

@app.get("/", include_in_schema=False)
async def root_page():
    # Платформенный контур (apex и admin.) — админ-SPA; тенант — дашборд
    page = "admin.html" if _get_tenant_slug() is None else "index.html"
    return FileResponse(f"static/{page}")

# существующий mount StaticFiles остаётся ПОСЛЕДНИМ как был
```

- [x] **Step 4: прогон** → PASS (+ суита).
- [x] **Step 5: commit** — `feat(platform): GET / отдаёт admin.html на платформенном контуре`

---

### Task 8: рефактор provision_tenant → вызываемая provision()

**Files:**
- Modify: `backend/app/provision_tenant.py`
- Test: `backend/tests/test_provisioning_unit.py` (создать)

- [x] **Step 1: тест** (юнит на чистые куски; интеграция с БД — Task 19):

```python
def test_generate_api_key_format():
    from app.provision_tenant import generate_api_key
    key, digest = generate_api_key()
    assert key.startswith("sqa_") and len(key) > 20
    import hashlib
    assert hashlib.sha256(key.encode()).hexdigest() == digest


def test_provision_rejects_bad_slug():
    import pytest
    from app.provision_tenant import provision
    with pytest.raises(ValueError):
        provision("Bad Slug!", "x", "a@b.c", "pw")  # до любых коннектов
```

- [x] **Step 2: прогон** → FAIL.

- [x] **Step 3: имплементация.** В `provision_tenant.py`:

```python
from dataclasses import dataclass


class ProvisionError(RuntimeError):
    """Бизнес-отказ провижининга (slug занят и т.п.)."""


@dataclass
class ProvisionResult:
    slug: str
    schema: str
    admin_email: str
    admin_password: str
    api_key: str


def generate_api_key() -> tuple[str, str]:
    key = f"sqa_{secrets.token_urlsafe(32)}"
    return key, hashlib.sha256(key.encode()).hexdigest()


def generate_password() -> str:
    return secrets.token_urlsafe(12)


def _set_api_key(conn, slug: str) -> str:
    key, digest = generate_api_key()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE shared.tenants SET api_key_hash = %s WHERE slug = %s",
            (digest, slug),
        )
    return key


def provision(slug: str, name: str, admin_email: str,
              admin_password: str = "") -> ProvisionResult:
    """Создать тенанта целиком: схема → миграции → admin → API-ключ.

    ValueError — кривой slug (до коннекта); ProvisionError — slug занят.
    Частичный провал после первого commit оставляет полусозданного
    тенанта — зачистка по ранбуку (спека §3.2), автоочистки нет.
    """
    slug = validate_slug(slug)
    schema = schema_for_slug(slug)
    password = admin_password or generate_password()

    conn = shared_connect()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM shared.tenants WHERE slug = %s", (slug,))
            if cur.fetchone() is not None:
                raise ProvisionError(f"tenant {slug!r} already exists")
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(
                "INSERT INTO shared.tenants "
                "(slug, schema_name, display_name, status) "
                "VALUES (%s, %s, %s, 'active')",
                (slug, schema, name or slug),
            )
        conn.commit()
        from app.migrate import run_tenant
        run_tenant(schema)
        _seed_admin(conn, schema, admin_email, password)
        key = _set_api_key(conn, slug)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return ProvisionResult(slug=slug, schema=schema, admin_email=admin_email,
                           admin_password=password, api_key=key)
```

`main()` — переписать на использование `provision()` (ветка без `--seed-only`):
```python
    if args.seed_only:
        # ... существующая ветка остаётся как есть (exists-проверка,
        # _seed_admin + _set_api_key + commit)
    else:
        try:
            res = provision(slug, args.name, args.admin_email, password)
        except ProvisionError as e:
            sys.exit(str(e))
        print(f"tenant: {res.slug}  schema: {res.schema}")
        print(f"admin:  {res.admin_email}")
        if not args.admin_password:
            print(f"admin password (shown once): {res.admin_password}")
        print(f"API key (shown once, store it now): {res.api_key}")
        return
```
ВАЖНО: поведение CLI для существующих сценариев не менять (ранбук ссылается на вывод). `getpass`-запрос пароля оставить только в CLI, НЕ в provision().

- [x] **Step 4: прогон** → PASS (+ суита).
- [x] **Step 5: commit** — `refactor(provision): вызываемая provision() + generate_api_key, CLI — тонкая обёртка`

---

### Task 9: /api/platform/tenants — список со статистикой + создание

**Files:**
- Create: `backend/app/routes/platform_tenants.py`
- Modify: `backend/app/main.py` (включить роутер)
- Test: `backend/tests/test_platform_tenants.py` (создать)

- [x] **Step 1: тест** (БД мокается через monkeypatch внутренних функций; права — без мока):

```python
import asyncio
import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return []

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://admin.silentqa.com")


def _admin_cookie(fake_redis) -> dict:
    from app import platform_sessions as ps

    sid = asyncio.run(ps.create_platform_session("a1", "boss@x.io"))
    return {ps.PLATFORM_COOKIE: sid}


def test_tenants_list_requires_auth(client):
    assert client.get("/api/platform/tenants").status_code == 401


def test_tenants_list_with_stats(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_rows(db):
        return [{"slug": "acme", "display_name": "ACME", "status": "active",
                 "schema_name": "t_acme", "created_at": "2026-06-01T00:00:00Z"}]

    async def fake_stats(db, schema, dt_from, dt_to):
        return {"sessions_count": 5, "minutes": 42, "last_activity": "2026-06-11T10:00:00Z"}

    monkeypatch.setattr(pt, "_tenant_rows", fake_rows)
    monkeypatch.setattr(pt, "_tenant_stats", fake_stats)
    r = client.get("/api/platform/tenants", cookies=_admin_cookie(fake_redis))
    assert r.status_code == 200
    item = r.json()[0]
    assert item["slug"] == "acme" and item["sessions_count"] == 5


def test_create_tenant_returns_secrets_once(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt
    from app.provision_tenant import ProvisionResult

    def fake_provision(slug, name, email, password=""):
        return ProvisionResult(slug=slug, schema=f"t_{slug}", admin_email=email,
                               admin_password="genpw", api_key="sqa_xyz")

    monkeypatch.setattr(pt, "provision", fake_provision)
    r = client.post("/api/platform/tenants", cookies=_admin_cookie(fake_redis),
                    json={"slug": "newco", "display_name": "NewCo",
                          "admin_email": "a@n.co"})
    assert r.status_code == 201
    body = r.json()
    assert body["api_key"] == "sqa_xyz" and body["admin_password"] == "genpw"


def test_create_tenant_conflict_409(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt
    from app.provision_tenant import ProvisionError

    def fake_provision(*a, **k):
        raise ProvisionError("tenant 'x' already exists")

    monkeypatch.setattr(pt, "provision", fake_provision)
    r = client.post("/api/platform/tenants", cookies=_admin_cookie(fake_redis),
                    json={"slug": "x", "display_name": "", "admin_email": "a@b.c"})
    assert r.status_code == 409
```

- [x] **Step 2: прогон** → FAIL.

- [x] **Step 3: имплементация** `routes/platform_tenants.py`:

```python
"""Управление клиентами платформы: /api/platform/tenants* (спека §3)."""
from __future__ import annotations

import datetime as dt
import logging

import anyio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tenancy.identifiers import SCHEMA_RE

from ..auth_platform import require_platform_admin
from ..database import get_db
from ..provision_tenant import ProvisionError, provision
from ..tenancy_http import registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/platform/tenants", tags=["platform-tenants"],
                   dependencies=[Depends(require_platform_admin)])


class CreateTenantBody(BaseModel):
    slug: str
    display_name: str = ""
    admin_email: EmailStr
    admin_password: str = ""


def _month_start() -> dt.datetime:
    now = dt.datetime.now(dt.timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _tenant_rows(db: AsyncSession) -> list[dict]:
    res = await db.execute(text(
        "SELECT slug, schema_name, display_name, status, created_at "
        "FROM shared.tenants ORDER BY created_at"
    ))
    return [dict(r._mapping) for r in res]


async def _tenant_stats(db: AsyncSession, schema: str,
                        dt_from: dt.datetime, dt_to: dt.datetime | None) -> dict:
    if not SCHEMA_RE.fullmatch(schema):  # schema из БД, но пояс не лишний
        raise HTTPException(status_code=500, detail="bad schema name")
    until = "AND created_at < :dt_to" if dt_to else ""
    row = (await db.execute(text(
        f"SELECT count(*) AS sessions_count, "
        f"coalesce(sum(duration_seconds) FILTER (WHERE status = 'completed'), 0) "
        f"  AS total_seconds, "
        f"(SELECT max(created_at) FROM {schema}.sessions) AS last_activity "
        f"FROM {schema}.sessions WHERE created_at >= :dt_from {until}"),
        {"dt_from": dt_from, **({"dt_to": dt_to} if dt_to else {})},
    )).first()
    return {
        "sessions_count": row.sessions_count,
        "minutes": int(row.total_seconds // 60),
        "last_activity": row.last_activity.isoformat() if row.last_activity else None,
    }


@router.get("")
async def list_tenants(dt_from: dt.datetime | None = None,
                       dt_to: dt.datetime | None = None,
                       db: AsyncSession = Depends(get_db)):
    start = dt_from or _month_start()
    out = []
    for t in await _tenant_rows(db):
        stats = await _tenant_stats(db, t["schema_name"], start, dt_to)
        created = t["created_at"]
        out.append({
            "slug": t["slug"], "display_name": t["display_name"],
            "status": t["status"],
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
            **stats,
        })
    return out


@router.post("", status_code=201)
async def create_tenant(body: CreateTenantBody):
    try:
        res = await anyio.to_thread.run_sync(
            lambda: provision(body.slug, body.display_name,
                              str(body.admin_email), body.admin_password)
        )
    except ProvisionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    registry.invalidate()
    logger.info("tenant provisioned via API: %s", res.slug)
    # Секреты в ответе ОДИН раз — нигде больше не доступны
    return {"slug": res.slug, "admin_email": res.admin_email,
            "admin_password": res.admin_password, "api_key": res.api_key}
```

В `main.py`: добавить `platform_tenants` в import-кортеж и `app.include_router(platform_tenants.router)`.

ПРИМЕЧАНИЕ: `provision()` дёргает alembic — секунды; `anyio.to_thread.run_sync` не блокирует loop.

- [x] **Step 4: прогон** → PASS (+ суита).
- [x] **Step 5: commit** — `feat(platform): список клиентов со статистикой + создание клиента из админки`

---

### Task 10: suspend/activate + ротация API-ключа

**Files:**
- Modify: `backend/app/routes/platform_tenants.py`
- Test: `backend/tests/test_platform_tenants.py` (дополнить)

- [x] **Step 1: тесты** (дополнить файл):

```python
def test_patch_status_validates(client, fake_redis):
    r = client.patch("/api/platform/tenants/acme", cookies=_admin_cookie(fake_redis),
                     json={"status": "bogus"})
    assert r.status_code == 422


def test_patch_status_suspend(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt
    calls = {}

    async def fake_set_status(db, slug, status):
        calls["args"] = (slug, status)
        return True  # тенант найден

    monkeypatch.setattr(pt, "_set_status", fake_set_status)
    r = client.patch("/api/platform/tenants/acme", cookies=_admin_cookie(fake_redis),
                     json={"status": "suspended"})
    assert r.status_code == 200
    assert calls["args"] == ("acme", "suspended")


def test_rotate_key_returns_new_key_once(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_rotate(db, slug):
        return "sqa_new"  # None — тенант не найден

    monkeypatch.setattr(pt, "_rotate_key", fake_rotate)
    r = client.post("/api/platform/tenants/acme/rotate-key",
                    cookies=_admin_cookie(fake_redis))
    assert r.status_code == 200
    assert r.json()["api_key"] == "sqa_new"


def test_unknown_tenant_404(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_set_status(db, slug, status):
        return False

    monkeypatch.setattr(pt, "_set_status", fake_set_status)
    r = client.patch("/api/platform/tenants/ghost", cookies=_admin_cookie(fake_redis),
                     json={"status": "active"})
    assert r.status_code == 404
```

- [x] **Step 2: прогон** → FAIL.

- [x] **Step 3: имплементация** (добавить в `platform_tenants.py`):

```python
from typing import Literal

from ..provision_tenant import generate_api_key


class PatchTenantBody(BaseModel):
    status: Literal["active", "suspended"]


async def _set_status(db: AsyncSession, slug: str, status: str) -> bool:
    res = await db.execute(text(
        "UPDATE shared.tenants SET status = :status WHERE slug = :slug"),
        {"status": status, "slug": slug})
    await db.commit()
    return res.rowcount > 0


async def _rotate_key(db: AsyncSession, slug: str) -> str | None:
    key, digest = generate_api_key()
    res = await db.execute(text(
        "UPDATE shared.tenants SET api_key_hash = :h WHERE slug = :slug"),
        {"h": digest, "slug": slug})
    await db.commit()
    return key if res.rowcount > 0 else None


@router.patch("/{slug}")
async def patch_tenant(slug: str, body: PatchTenantBody,
                       db: AsyncSession = Depends(get_db)):
    if not await _set_status(db, slug, body.status):
        raise HTTPException(status_code=404, detail="tenant not found")
    registry.invalidate()
    logger.info("tenant %s status -> %s", slug, body.status)
    return {"slug": slug, "status": body.status}


@router.post("/{slug}/rotate-key")
async def rotate_key(slug: str, db: AsyncSession = Depends(get_db)):
    key = await _rotate_key(db, slug)
    if key is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    registry.invalidate()
    logger.info("tenant %s api key rotated", slug)
    return {"slug": slug, "api_key": key}
```

- [x] **Step 4: прогон** → PASS.
- [x] **Step 5: commit** — `feat(platform): suspend/activate и ротация API-ключа`

---

### Task 11: юзеры клиента из платформенной админки

**Files:**
- Modify: `backend/app/routes/platform_tenants.py`
- Test: `backend/tests/test_platform_tenant_users.py` (создать)

Все операции — schema-qualified SQL `t_<slug>.users` (платформенный контур, search_path не установлен). Схему брать ТОЛЬКО из shared.tenants по slug (не конструировать из slug руками).

- [ ] **Step 1: тесты** (мокаем `_schema_for` и data-хелперы; auth-кейсы живьём):

```python
def test_tenant_users_list(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_schema(db, slug):
        return "t_acme" if slug == "acme" else None

    async def fake_users(db, schema):
        return [{"id": "u1", "email": "a@x.io", "role": "admin",
                 "employee_name": None, "created_at": None, "last_login": None}]

    monkeypatch.setattr(pt, "_schema_for", fake_schema)
    monkeypatch.setattr(pt, "_list_users", fake_users)
    r = client.get("/api/platform/tenants/acme/users", cookies=_admin_cookie(fake_redis))
    assert r.status_code == 200 and r.json()[0]["email"] == "a@x.io"
    assert client.get("/api/platform/tenants/ghost/users",
                      cookies=_admin_cookie(fake_redis)).status_code == 404


def test_tenant_user_reset_password_returns_once(client, fake_redis, monkeypatch):
    from app.routes import platform_tenants as pt

    async def fake_schema(db, slug):
        return "t_acme"

    async def fake_reset(db, schema, user_id, pw_hash):
        return True

    monkeypatch.setattr(pt, "_schema_for", fake_schema)
    monkeypatch.setattr(pt, "_reset_password", fake_reset)
    r = client.post("/api/platform/tenants/acme/users/u1/reset-password",
                    cookies=_admin_cookie(fake_redis))
    assert r.status_code == 200
    assert len(r.json()["password"]) >= 12
```

- [ ] **Step 2: прогон** → FAIL.

- [ ] **Step 3: имплементация** (добавить в `platform_tenants.py`):

```python
import uuid

from argon2 import PasswordHasher

from ..provision_tenant import generate_password

_ph = PasswordHasher()

ROLES = ("admin", "viewer", "manager")


class CreateUserBody(BaseModel):
    email: EmailStr
    role: Literal["admin", "viewer", "manager"] = "viewer"
    employee_name: str | None = None


class PatchUserBody(BaseModel):
    role: Literal["admin", "viewer", "manager"] | None = None
    employee_name: str | None = None


async def _schema_for(db: AsyncSession, slug: str) -> str | None:
    row = (await db.execute(text(
        "SELECT schema_name FROM shared.tenants WHERE slug = :slug"),
        {"slug": slug})).first()
    return row.schema_name if row else None


async def _require_schema(db: AsyncSession, slug: str) -> str:
    schema = await _schema_for(db, slug)
    if schema is None or not SCHEMA_RE.fullmatch(schema):
        raise HTTPException(status_code=404, detail="tenant not found")
    return schema


async def _list_users(db: AsyncSession, schema: str) -> list[dict]:
    res = await db.execute(text(
        f"SELECT id, email, role, employee_name, created_at, last_login "
        f"FROM {schema}.users ORDER BY created_at"))
    return [
        {"id": str(r.id), "email": r.email, "role": r.role,
         "employee_name": r.employee_name,
         "created_at": r.created_at.isoformat() if r.created_at else None,
         "last_login": r.last_login.isoformat() if r.last_login else None}
        for r in res
    ]


async def _insert_user(db, schema, email, role, employee_name, pw_hash) -> str | None:
    try:
        row = (await db.execute(text(
            f"INSERT INTO {schema}.users (email, password_hash, role, employee_name) "
            f"VALUES (:email, :pw, :role, :emp) RETURNING id"),
            {"email": email, "pw": pw_hash, "role": role, "emp": employee_name},
        )).first()
        await db.commit()
        return str(row.id)
    except Exception:  # uq_users_email_lower
        await db.rollback()
        return None


async def _reset_password(db, schema, user_id, pw_hash) -> bool:
    res = await db.execute(text(
        f"UPDATE {schema}.users SET password_hash = :pw WHERE id = :id"),
        {"pw": pw_hash, "id": user_id})
    await db.commit()
    return res.rowcount > 0


@router.get("/{slug}/users")
async def tenant_users(slug: str, db: AsyncSession = Depends(get_db)):
    schema = await _require_schema(db, slug)
    return await _list_users(db, schema)


@router.post("/{slug}/users", status_code=201)
async def tenant_user_create(slug: str, body: CreateUserBody,
                             db: AsyncSession = Depends(get_db)):
    schema = await _require_schema(db, slug)
    password = generate_password()
    uid = await _insert_user(db, schema, str(body.email), body.role,
                             body.employee_name, _ph.hash(password))
    if uid is None:
        raise HTTPException(status_code=409, detail="email already exists")
    return {"id": uid, "email": str(body.email), "role": body.role,
            "password": password}


@router.patch("/{slug}/users/{user_id}")
async def tenant_user_patch(slug: str, user_id: uuid.UUID, body: PatchUserBody,
                            db: AsyncSession = Depends(get_db)):
    schema = await _require_schema(db, slug)
    sets, params = [], {"id": str(user_id)}
    if body.role is not None:
        sets.append("role = :role"); params["role"] = body.role
    if body.employee_name is not None:
        sets.append("employee_name = :emp"); params["emp"] = body.employee_name or None
    if not sets:
        raise HTTPException(status_code=422, detail="nothing to change")
    res = await db.execute(text(
        f"UPDATE {schema}.users SET {', '.join(sets)} WHERE id = :id"), params)
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True}


@router.delete("/{slug}/users/{user_id}", status_code=204)
async def tenant_user_delete(slug: str, user_id: uuid.UUID,
                             db: AsyncSession = Depends(get_db)):
    schema = await _require_schema(db, slug)
    res = await db.execute(text(
        f"DELETE FROM {schema}.users WHERE id = :id"), {"id": str(user_id)})
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="user not found")


@router.post("/{slug}/users/{user_id}/reset-password")
async def tenant_user_reset_password(slug: str, user_id: uuid.UUID,
                                     db: AsyncSession = Depends(get_db)):
    schema = await _require_schema(db, slug)
    password = generate_password()
    if not await _reset_password(db, schema, str(user_id), _ph.hash(password)):
        raise HTTPException(status_code=404, detail="user not found")
    return {"password": password}
```
(Платформенная сторона БЕЗ self-guards — админ платформы не юзер тенанта; guards самовыпила живут в Task 14.)

- [ ] **Step 4: прогон** → PASS.
- [ ] **Step 5: commit** — `feat(platform): управление юзерами клиента из админки`

---

### Task 12: impersonation + расширение тенант-сессий

**Files:**
- Modify: `backend/app/auth_sessions.py` (kwargs create_session)
- Modify: `backend/app/auth_user.py` (UserCtx поля)
- Modify: `backend/app/routes/platform_tenants.py` (выдача токена)
- Modify: `backend/app/routes/user_auth.py` (обмен токена, /me)
- Modify: `backend/tests/conftest.py` (FakeRedis.getdel)
- Test: `backend/tests/test_impersonation.py` (создать)

- [ ] **Step 1: FakeRedis.getdel** в conftest:

```python
    async def getdel(self, key):
        return self.store.pop(key, None)
```

- [ ] **Step 2: тест** `tests/test_impersonation.py`:

```python
import asyncio
import json

import pytest
from starlette.testclient import TestClient

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app)


def _admin_cookie(fake_redis):
    from app import platform_sessions as ps
    sid = asyncio.run(ps.create_platform_session("a1", "boss@x.io"))
    return {ps.PLATFORM_COOKIE: sid}


@pytest.fixture
def schema_mock(monkeypatch):
    """Impersonate-хендлер зовёт _require_schema (SELECT shared.tenants) —
    в юнит-окружении Postgres нет, мокаем как в test_platform_tenants."""
    from app.routes import platform_tenants as pt

    async def fake_require_schema(db, slug):
        if slug != "acme":
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="tenant not found")
        return "t_acme"

    monkeypatch.setattr(pt, "_require_schema", fake_require_schema)


def test_issue_token_and_exchange(client, fake_redis, schema_mock):
    r = client.post("/api/platform/tenants/acme/impersonate",
                    cookies=_admin_cookie(fake_redis),
                    headers={"Host": "admin.silentqa.com"})
    assert r.status_code == 200
    url = r.json()["url"]
    assert url.startswith("https://acme.silentqa.com/api/user-auth/impersonate?token=")
    token = url.split("token=")[1]
    assert fake_redis.ttls[f"platform:imp:{token}"] == 60

    ex = client.get(f"/api/user-auth/impersonate?token={token}",
                    headers={"Host": "acme.silentqa.com"}, follow_redirects=False)
    assert ex.status_code == 303
    assert "sqa_session" in ex.headers.get("set-cookie", "")

    me = client.get("/api/user-auth/me", headers={"Host": "acme.silentqa.com"},
                    cookies={"sqa_session": ex.cookies["sqa_session"]})
    assert me.json()["impersonated_by"] == "boss@x.io"
    assert me.json()["role"] == "admin"


def test_token_single_use(client, fake_redis, schema_mock):
    r = client.post("/api/platform/tenants/acme/impersonate",
                    cookies=_admin_cookie(fake_redis),
                    headers={"Host": "admin.silentqa.com"})
    token = r.json()["url"].split("token=")[1]
    ok = client.get(f"/api/user-auth/impersonate?token={token}",
                    headers={"Host": "acme.silentqa.com"}, follow_redirects=False)
    assert ok.status_code == 303
    again = client.get(f"/api/user-auth/impersonate?token={token}",
                       headers={"Host": "acme.silentqa.com"}, follow_redirects=False)
    assert again.status_code == 401


def test_token_slug_mismatch(client, fake_redis):
    """Токен на acme нельзя обменять... если бы был второй тенант. Стаб: кладём токен руками."""
    asyncio.run(fake_redis.set("platform:imp:tok1", json.dumps(
        {"slug": "other", "admin_email": "boss@x.io"}), ex=60))
    r = client.get("/api/user-auth/impersonate?token=tok1",
                   headers={"Host": "acme.silentqa.com"}, follow_redirects=False)
    assert r.status_code == 401
```

- [ ] **Step 3: прогон** → FAIL.

- [ ] **Step 4: имплементация.**

`auth_sessions.create_session` — расширить (обратная совместимость: все вызовы без новых kwargs работают как раньше):
```python
async def create_session(user_id: str, email: str, role: str, *,
                         employee_name: str | None = None,
                         impersonated_by: str | None = None,
                         ttl: int | None = None) -> str:
    sid = secrets.token_urlsafe(32)
    slug = require_tenant_slug()
    payload = json.dumps({
        "user_id": user_id, "email": email, "role": role,
        "employee_name": employee_name, "impersonated_by": impersonated_by,
    })
    await get_redis().set(_sess_key(slug, sid), payload,
                          ex=ttl or settings.SESSION_TTL_SECONDS)
    return sid
```

`auth_user.UserCtx` + `get_current_user`:
```python
@dataclass
class UserCtx:
    user_id: str
    email: str
    role: str
    employee_name: str | None = None
    impersonated_by: str | None = None

# в get_current_user:
    return UserCtx(user_id=data["user_id"], email=data["email"], role=data["role"],
                   employee_name=data.get("employee_name"),
                   impersonated_by=data.get("impersonated_by"))
```

`platform_tenants.py` — выдача токена:
```python
import json as _json
import secrets as _secrets

from ..config import settings
from ..redis_client import get_redis

IMPERSONATION_TTL = 60          # секунд на переход
IMPERSONATION_SESSION_TTL = 7200  # 2 часа (спека §3.4)


@router.post("/{slug}/impersonate")
async def impersonate(slug: str, db: AsyncSession = Depends(get_db),
                      admin=Depends(require_platform_admin)):
    await _require_schema(db, slug)
    token = _secrets.token_urlsafe(32)
    await get_redis().set(
        f"platform:imp:{token}",
        _json.dumps({"slug": slug, "admin_email": admin.email}),
        ex=IMPERSONATION_TTL,
    )
    logger.info("impersonation token issued: %s -> %s", admin.email, slug)
    return {"url": f"https://{slug}.{settings.BASE_DOMAIN}"
                   f"/api/user-auth/impersonate?token={token}"}
```
ВНИМАНИЕ: `require_platform_admin` уже стоит на роутере целиком (dependencies=). Для доступа к admin.email добавить параметр `admin=Depends(require_platform_admin)` в этот handler — FastAPI кэширует зависимость, второй раз не исполняется.

`user_auth.py` — обмен:
```python
from fastapi.responses import RedirectResponse

from ..redis_client import get_redis
import json as _json

IMPERSONATION_SESSION_TTL = 7200


@router.get("/impersonate")
async def impersonate_exchange(token: str, request: Request):
    slug = get_tenant_slug()
    if slug is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    raw = await get_redis().getdel(f"platform:imp:{token}")
    if not raw:
        raise HTTPException(status_code=401, detail="invalid_token")
    data = _json.loads(raw)
    if data["slug"] != slug:
        raise HTTPException(status_code=401, detail="invalid_token")
    sid = await auth_sessions.create_session(
        "platform-admin", data["admin_email"], "admin",
        impersonated_by=data["admin_email"], ttl=IMPERSONATION_SESSION_TTL,
    )
    logger.info("impersonation login: %s -> %s", data["admin_email"], slug)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        auth_sessions.SESSION_COOKIE, sid,
        httponly=True, secure=settings.SESSION_COOKIE_SECURE, samesite="lax",
        max_age=IMPERSONATION_SESSION_TTL, path="/",
    )
    return resp
```
`/me` — отдать новые поля:
```python
@router.get("/me")
async def me(user: UserCtx | None = Depends(get_current_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="auth_required")
    return {"email": user.email, "role": user.role,
            "employee_name": user.employee_name,
            "impersonated_by": user.impersonated_by}
```

ПРОД-ЗАВИСИМОСТЬ: `getdel` требует Redis ≥ 6.2 — проверить на сервере при деплое (`redis-cli INFO server | grep redis_version`); сервер на Ubuntu 24.04 → Redis 7.x, ок.

- [ ] **Step 5: прогон** — файл PASS + суита. Известные жертвы расширения payload (ревью):
  - `tests/test_auth_sessions.py::test_create_load_destroy_roundtrip` сверяет ТОЧНЫЙ dict — добавить в ожидаемый `"employee_name": None, "impersonated_by": None`;
  - `tests/test_user_auth_routes.py` — если /me сверяется точным dict, добавить новые поля в ожидания.
- [ ] **Step 6: commit** — `feat(platform): impersonation одноразовым токеном + поля сессии`

---

### Task 13: /api/companies → платформенная сессия, выпил Basic

**Files:**
- Modify: `backend/app/routes/companies.py`
- Modify: `backend/app/auth_user.py` (удалить require_platform_admin_basic)
- Modify: `backend/app/config.py` (удалить AUTH_USERNAME/AUTH_PASSWORD)
- Modify: `backend/tests/test_companies_platform.py`
- Modify: `.env.example` (убрать AUTH_*)

- [ ] **Step 1: тест** — переписать `test_companies_platform.py`: вместо Basic-хедеров — платформенная cookie (хелпер `_admin_cookie` как в test_platform_tenants); кейсы: без сессии 401 `platform_auth_required`; с сессией — 200/обычная работа; на тенант-хосте 404 (это уже гейт Task 3 — оставить кейс как регрессию).

- [ ] **Step 2: прогон** → FAIL.

- [ ] **Step 3: имплементация**: в `companies.py` заменить `dependencies=[Depends(require_platform_admin_basic)]` → `dependencies=[Depends(require_platform_admin)]` (импорт из `..auth_platform`). Удалить `require_platform_admin_basic` из `auth_user.py` (и его импорты `base64`, упоминания settings.AUTH_*). Из `config.py` удалить поля `AUTH_USERNAME`/`AUTH_PASSWORD`; `grep -rn "AUTH_USERNAME\|AUTH_PASSWORD" backend/ worker/ --include="*.py"` — должно остаться пусто. Из `.env.example` убрать строки.

- [ ] **Step 4: прогон** → PASS (+ суита).
- [ ] **Step 5: commit** — `feat(platform): /api/companies под платформенной сессией, Basic выпилен`

---

### Task 14: /api/users — «Команда» с guards самовыпила

**Files:**
- Create: `backend/app/routes/users.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/auth_sessions.py` (destroy_user_sessions)
- Modify: `backend/tests/conftest.py` (FakeRedis.scan_iter)
- Test: `backend/tests/test_team_users.py` (создать)

- [ ] **Step 1: FakeRedis.scan_iter** в conftest:

```python
    async def scan_iter(self, match=None):
        import fnmatch
        for k in list(self.store):
            if match is None or fnmatch.fnmatch(k, match):
                yield k
```

- [ ] **Step 2: тесты** `tests/test_team_users.py` (data-слой мокаем; guards — живая логика):

```python
import asyncio
import pytest
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]
HOST = {"Host": "acme.silentqa.com"}


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def _cookie(fake_redis, role="admin", user_id="u1", email="a@x.io"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session(user_id, email, role)
        finally:
            reset_tenant_schema(token)

    return {SESSION_COOKIE: asyncio.run(seed())}


def test_users_admin_only(client, fake_redis):
    assert client.get("/api/users").status_code == 401
    assert client.get("/api/users",
                      cookies=_cookie(fake_redis, role="viewer")).status_code == 403


def test_cannot_delete_self(client, fake_redis, monkeypatch):
    r = client.delete("/api/users/u1", cookies=_cookie(fake_redis, user_id="u1"))
    assert r.status_code == 403
    assert r.json()["detail"] == "cannot_delete_self"


def test_cannot_demote_self(client, fake_redis):
    r = client.patch("/api/users/u1", cookies=_cookie(fake_redis, user_id="u1"),
                     json={"role": "viewer"})
    assert r.status_code == 403
    assert r.json()["detail"] == "cannot_demote_self"


# Жертва — валидный UUID: self-guards сравнивают строки, а uuid-валидация
# (см. Step 4) идёт ПОСЛЕ guards и ДО БД
VICTIM = "00000000-0000-0000-0000-000000000002"


def test_cannot_remove_last_admin(client, fake_redis, monkeypatch):
    from app.routes import users as users_mod

    async def fake_get_user(db, user_id):
        return {"id": VICTIM, "email": "b@x.io", "role": "admin"}

    async def fake_count_other_admins(db, user_id):
        return 0  # других админов нет

    monkeypatch.setattr(users_mod, "_get_user", fake_get_user)
    monkeypatch.setattr(users_mod, "_count_other_admins", fake_count_other_admins)
    r = client.delete(f"/api/users/{VICTIM}", cookies=_cookie(fake_redis, user_id="u1"))
    assert r.status_code == 403
    assert r.json()["detail"] == "last_admin"
    r = client.patch(f"/api/users/{VICTIM}", cookies=_cookie(fake_redis, user_id="u1"),
                     json={"role": "viewer"})
    assert r.status_code == 403


def test_delete_kills_user_sessions(client, fake_redis, monkeypatch):
    from app.routes import users as users_mod

    async def fake_get_user(db, user_id):
        return {"id": VICTIM, "email": "b@x.io", "role": "viewer"}

    async def fake_delete(db, user_id):
        return True

    monkeypatch.setattr(users_mod, "_get_user", fake_get_user)
    monkeypatch.setattr(users_mod, "_delete_user", fake_delete)
    victim = _cookie(fake_redis, role="viewer", user_id=VICTIM, email="b@x.io")
    r = client.delete(f"/api/users/{VICTIM}", cookies=_cookie(fake_redis, user_id="u1"))
    assert r.status_code == 204
    me = client.get("/api/user-auth/me", cookies=victim)
    assert me.status_code == 401  # сессии жертвы убиты


def test_garbage_user_id_404_not_500(client, fake_redis):
    r = client.delete("/api/users/not-a-uuid", cookies=_cookie(fake_redis, user_id="u1"))
    assert r.status_code == 404
```

- [ ] **Step 3: прогон** → FAIL.

- [ ] **Step 4: имплементация.**

`auth_sessions.py` — хелпер:
```python
async def destroy_user_sessions(email: str, keep_sid: str | None = None) -> None:
    """Убить все сессии юзера текущего тенанта (кроме keep_sid)."""
    slug = require_tenant_slug()
    r = get_redis()
    async for key in r.scan_iter(match=f"t:{slug}:sess:*"):
        if keep_sid and key.endswith(keep_sid):
            continue
        raw = await r.get(key)
        if raw and json.loads(raw).get("email", "").lower() == email.lower():
            await r.delete(key)
```
ПРИМЕЧАНИЕ: redis-py возвращает bytes — `key` может быть bytes; нормализовать: `key = key.decode() if isinstance(key, bytes) else key` в начале цикла (FakeRedis отдаёт str, прод — bytes).

`routes/users.py`:
```python
"""Команда тенанта: /api/users (спека §4). Только admin."""
from __future__ import annotations

import uuid

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Literal

from ..auth_sessions import destroy_user_sessions
from ..auth_user import UserCtx, require_admin
from ..database import get_db
from ..provision_tenant import generate_password

router = APIRouter(prefix="/api/users", tags=["users"])

_ph = PasswordHasher()


class CreateUserBody(BaseModel):
    email: EmailStr
    role: Literal["admin", "viewer", "manager"] = "viewer"
    employee_name: str | None = None


class PatchUserBody(BaseModel):
    role: Literal["admin", "viewer", "manager"] | None = None
    employee_name: str | None = None


async def _get_user(db: AsyncSession, user_id: str) -> dict | None:
    row = (await db.execute(text(
        "SELECT id, email, role FROM users WHERE id = :id"),
        {"id": user_id})).first()
    return {"id": str(row.id), "email": row.email, "role": row.role} if row else None


async def _count_other_admins(db: AsyncSession, user_id: str) -> int:
    return (await db.execute(text(
        "SELECT count(*) FROM users WHERE role = 'admin' AND id != :id"),
        {"id": user_id})).scalar_one()


async def _delete_user(db: AsyncSession, user_id: str) -> bool:
    res = await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    await db.commit()
    return res.rowcount > 0


@router.get("", dependencies=[Depends(require_admin)])
async def list_users(db: AsyncSession = Depends(get_db)):
    res = await db.execute(text(
        "SELECT id, email, role, employee_name, created_at, last_login "
        "FROM users ORDER BY created_at"))
    return [
        {"id": str(r.id), "email": r.email, "role": r.role,
         "employee_name": r.employee_name,
         "created_at": r.created_at.isoformat() if r.created_at else None,
         "last_login": r.last_login.isoformat() if r.last_login else None}
        for r in res
    ]


@router.post("", status_code=201)
async def create_user(body: CreateUserBody, db: AsyncSession = Depends(get_db),
                      _: UserCtx = Depends(require_admin)):
    password = generate_password()
    try:
        row = (await db.execute(text(
            "INSERT INTO users (email, password_hash, role, employee_name) "
            "VALUES (:email, :pw, :role, :emp) RETURNING id"),
            {"email": str(body.email), "pw": _ph.hash(password),
             "role": body.role, "emp": body.employee_name},
        )).first()
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=409, detail="email already exists")
    return {"id": str(row.id), "email": str(body.email), "role": body.role,
            "employee_name": body.employee_name, "password": password}


@router.patch("/{user_id}")
async def patch_user(user_id: str, body: PatchUserBody,
                     db: AsyncSession = Depends(get_db),
                     me: UserCtx = Depends(require_admin)):
    if body.role is not None and body.role != "admin" and user_id == me.user_id:
        raise HTTPException(status_code=403, detail="cannot_demote_self")
    if body.role is not None and body.role != "admin":
        target = await _get_user(db, user_id)
        if target and target["role"] == "admin" \
                and await _count_other_admins(db, user_id) == 0:
            raise HTTPException(status_code=403, detail="last_admin")
    sets, params = [], {"id": user_id}
    if body.role is not None:
        sets.append("role = :role"); params["role"] = body.role
    if body.employee_name is not None:
        sets.append("employee_name = :emp"); params["emp"] = body.employee_name or None
    if not sets:
        raise HTTPException(status_code=422, detail="nothing to change")
    res = await db.execute(text(
        f"UPDATE users SET {', '.join(sets)} WHERE id = :id"), params)
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True}


@router.delete("/{user_id}", status_code=204)
async def delete_user(user_id: str, db: AsyncSession = Depends(get_db),
                      me: UserCtx = Depends(require_admin)):
    if user_id == me.user_id:
        raise HTTPException(status_code=403, detail="cannot_delete_self")
    target = await _get_user(db, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if target["role"] == "admin" and await _count_other_admins(db, user_id) == 0:
        raise HTTPException(status_code=403, detail="last_admin")
    await _delete_user(db, user_id)
    await destroy_user_sessions(target["email"])


@router.post("/{user_id}/reset-password")
async def reset_password(user_id: str, db: AsyncSession = Depends(get_db),
                         _: UserCtx = Depends(require_admin)):
    target = await _get_user(db, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    password = generate_password()
    await db.execute(text("UPDATE users SET password_hash = :pw WHERE id = :id"),
                     {"pw": _ph.hash(password), "id": user_id})
    await db.commit()
    await destroy_user_sessions(target["email"])
    return {"password": password}
```
В `main.py`: импорт + `app.include_router(users.router)`.

`user_id` в path — `str` (не `uuid.UUID`): self-guards сравнивают со строковым `me.user_id` из сессии. Чтобы мусорный id не давал 500 на UUID-колонке, в `patch_user`/`delete_user`/`reset_password` СТРОГО в таком порядке: (1) self-guards (`cannot_delete_self`/`cannot_demote_self` — работают на любых строках), затем (2) uuid-валидация:
```python
    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="user not found")
```
затем (3) обращения к БД (last_admin-проверка и мутация). Тесты используют UUID-жертву `VICTIM` — guards и last_admin тестируются ДО и ПОСЛЕ uuid-валидации соответственно (`test_garbage_user_id_404_not_500` фиксирует порядок).

- [ ] **Step 5: прогон** → PASS (+ суита).
- [ ] **Step 6: commit** — `feat(team): /api/users с guards самовыпила и инвалидацией сессий`

---

### Task 15: смена своего пароля

**Files:**
- Modify: `backend/app/routes/user_auth.py`
- Test: `backend/tests/test_change_password.py` (создать)

- [ ] **Step 1: тест**:

```python
import asyncio
import pytest
from starlette.testclient import TestClient
from argon2 import PasswordHasher

from app.auth_sessions import SESSION_COOKIE

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


@pytest.fixture
def pw_store(monkeypatch):
    ph = PasswordHasher()
    store = {"hash": ph.hash("oldpw")}

    async def fake_get_hash(db, user_id):
        return store["hash"]

    async def fake_set_hash(db, user_id, new_hash):
        store["hash"] = new_hash

    from app.routes import user_auth
    monkeypatch.setattr(user_auth, "_get_password_hash", fake_get_hash)
    monkeypatch.setattr(user_auth, "_set_password_hash", fake_set_hash)
    return store


def _cookie(fake_redis, user_id="u1", email="a@x.io", role="viewer"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session(user_id, email, role)
        finally:
            reset_tenant_schema(token)

    return {SESSION_COOKIE: asyncio.run(seed())}


def test_change_password_wrong_old(client, fake_redis, pw_store):
    r = client.post("/api/user-auth/change-password", cookies=_cookie(fake_redis),
                    json={"old_password": "nope", "new_password": "newpw123"})
    assert r.status_code == 403


def test_change_password_kills_other_sessions(client, fake_redis, pw_store):
    mine = _cookie(fake_redis)
    other = _cookie(fake_redis)  # вторая сессия того же юзера
    r = client.post("/api/user-auth/change-password", cookies=mine,
                    json={"old_password": "oldpw", "new_password": "newpw123"})
    assert r.status_code == 200
    assert client.get("/api/user-auth/me", cookies=mine).status_code == 200
    assert client.get("/api/user-auth/me", cookies=other).status_code == 401
```

- [ ] **Step 2: прогон** → FAIL.

- [ ] **Step 3: имплементация** (в `user_auth.py`):

```python
class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


async def _get_password_hash(db: AsyncSession, user_id: str) -> str | None:
    row = (await db.execute(text(
        "SELECT password_hash FROM users WHERE id = :id"), {"id": user_id})).first()
    return row.password_hash if row else None


async def _set_password_hash(db: AsyncSession, user_id: str, new_hash: str) -> None:
    await db.execute(text(
        "UPDATE users SET password_hash = :pw WHERE id = :id"),
        {"pw": new_hash, "id": user_id})
    await db.commit()


@router.post("/change-password")
async def change_password(body: ChangePasswordRequest, request: Request,
                          db: AsyncSession = Depends(get_db),
                          user: UserCtx = Depends(require_viewer)):
    if len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="password_too_short")
    stored = await _get_password_hash(db, user.user_id)
    if stored is None or not _verify(stored, body.old_password):
        raise HTTPException(status_code=403, detail="wrong_password")
    await _set_password_hash(db, user.user_id, _ph.hash(body.new_password))
    keep = request.cookies.get(auth_sessions.SESSION_COOKIE)
    await auth_sessions.destroy_user_sessions(user.email, keep_sid=keep)
    return {"ok": True}
```
Импорт `require_viewer` добавить из `..auth_user`.

- [ ] **Step 4: прогон** → PASS.
- [ ] **Step 5: commit** — `feat(team): смена своего пароля с инвалидацией остальных сессий`

---

### Task 16: роль manager — scoping чтения

**Files:**
- Modify: `backend/app/routes/user_auth.py` (login: employee_name в сессию)
- Modify: `backend/app/auth_user.py` (employee_scope, require_session_access)
- Modify: `backend/app/routes/sessions.py` (фильтр списка; ownership на get/extraction)
- Modify: `backend/app/routes/managers.py` (scoping)
- Modify: `backend/app/routes/transcripts.py`, `analysis.py` (ownership на файловых GET)
- Test: `backend/tests/test_manager_scope.py` (создать)

- [ ] **Step 1: login → employee_name.** В `user_auth.login` SELECT добавить `employee_name`:
```python
            text(
                "SELECT id, email, password_hash, role, employee_name FROM users "
                "WHERE LOWER(email) = LOWER(:email)"
            ),
```
и `create_session(str(row.id), row.email, row.role, employee_name=row.employee_name)`; в ответе логина добавить `"employee_name": row.employee_name`.

Жертвы (ревью) — поправить в этом же шаге `tests/test_user_auth_routes.py`: стаб `_Row` не имеет атрибута `employee_name` (будет AttributeError → 500) — добавить `self.employee_name = None`; ожидания точных dict (ответ логина, /me) расширить новыми полями.

- [ ] **Step 2: зависимости** в `auth_user.py`:

```python
# Сентинел «менеджер без привязки»: фильтр не совпадёт ни с одним
# metadata.employee → видит пусто (спека §5)
_NO_EMPLOYEE = "\x00__unbound__"


async def employee_scope(user: UserCtx = Depends(require_viewer)) -> str | None:
    """None — без фильтра (admin/viewer); строка — only-own (manager)."""
    if user.role == "manager":
        return user.employee_name or _NO_EMPLOYEE
    return None


async def require_session_access(
    session_id: uuid.UUID,
    user: UserCtx = Depends(require_viewer),
) -> UserCtx:
    """Для файловых GET (transcript/analysis/audio/...): manager — только своё.

    Грузит сессию из БД ТОЛЬКО для роли manager (admin/viewer — ноль
    лишних запросов). Чужая/несуществующая → 404 (не раскрываем).
    """
    if user.role != "manager":
        return user
    from .database import async_session
    from .models import Session as SessionModel

    async with async_session() as db:
        row = await db.get(SessionModel, session_id)
    emp = (row.metadata_ or {}).get("employee") if row else None
    if row is None or emp != (user.employee_name or _NO_EMPLOYEE):
        raise HTTPException(status_code=404, detail="Not found")
    return user
```
Импорт `uuid` в auth_user.py.

- [ ] **Step 3: тест** `tests/test_manager_scope.py`:

```python
import asyncio
import pytest
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def _cookie(fake_redis, role="manager", employee=None, email="m@x.io"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session(
                "u9", email, role, employee_name=employee)
        finally:
            reset_tenant_schema(token)

    return {SESSION_COOKIE: asyncio.run(seed())}


def test_manager_foreign_stats_403(client, fake_redis):
    r = client.get("/api/managers/Чужой/sessions",
                   cookies=_cookie(fake_redis, employee="Иванов"))
    assert r.status_code == 403


def test_manager_own_name_passes_scope_gate(client, fake_redis, monkeypatch):
    """Своё имя НЕ отбивается 403 (дальше штатный 404 — звонков нет)."""
    from app.routes import managers as managers_mod

    async def fake_all_sessions(db):
        return []

    monkeypatch.setattr(managers_mod, "_all_sessions", fake_all_sessions)
    r = client.get("/api/managers/Иванов/sessions",
                   cookies=_cookie(fake_redis, employee="Иванов"))
    assert r.status_code == 404  # не 403: scope пройден, данных нет


def test_manager_unbound_sees_empty(client, fake_redis, monkeypatch):
    from app.routes import managers as managers_mod

    async def fake_all_sessions(db):
        class S:  # минимальный стаб Session
            metadata_ = {"employee": "Иванов"}
            created_at = None
        return [S()]

    monkeypatch.setattr(managers_mod, "_all_sessions", fake_all_sessions)
    r = client.get("/api/managers", cookies=_cookie(fake_redis, employee=None))
    assert r.status_code == 200
    assert r.json() == []


def test_manager_management_403(client, fake_redis):
    c = _cookie(fake_redis, employee="Иванов")
    assert client.delete(
        "/api/sessions/00000000-0000-0000-0000-000000000001", cookies=c
    ).status_code == 403  # require_admin
    assert client.get("/api/users", cookies=c).status_code == 403
```

- [ ] **Step 4: прогон** → FAIL; затем имплементация по роутам:

`managers.py` — выделить data-хелпер и применить scope (заодно НЕ переписываем на SQL — это Task плана 3c; только scoping):
```python
from app.auth_user import employee_scope

async def _all_sessions(db):
    result = await db.execute(select(Session))
    return result.scalars().all()

# list_managers(db=..., scope: str | None = Depends(employee_scope)):
#   sessions = await _all_sessions(db)
#   в цикле: if scope is not None and employee != scope: continue

# get_manager_sessions(name, db=..., scope=Depends(employee_scope)):
#   if scope is not None and name != scope:
#       raise HTTPException(status_code=403, detail="foreign_manager")
#   ... существующая логика; ВАЖНО: 404-ветка "No sessions found" остаётся
```

`sessions.py`:
- `list_sessions`: параметр `scope: str | None = Depends(employee_scope)`; в query:
```python
    if scope is not None:
        query = query.where(Session.metadata_["employee"].astext == scope)
```
(применить ко ВСЕМ веткам построения query в функции — прочитать функцию целиком!);
- `get_session` авторизуется `require_ingestion_auth` (cookie ИЛИ ключ) — менеджерский ownership добавить внутрь: параметр `user: UserCtx | None = Depends(get_current_user)`; после загрузки сессии:
```python
    if user is not None and user.role == "manager":
        emp = (session.metadata_ or {}).get("employee")
        if emp != (user.employee_name or "\x00__unbound__"):
            raise HTTPException(status_code=404, detail="Session not found")
```
- `get_session_extraction` (require_viewer): добавить `dependencies=[Depends(require_session_access)]` вместо… НЕТ — у него уже `dependencies=[Depends(require_viewer)]`; заменить на `[Depends(require_session_access)]` (require_session_access уже включает require_viewer через Depends).

`transcripts.py` (оба GET) и `analysis.py` (5 viewer-GET: audio/sentiment/quality/full + transcript-роуты): заменить `dependencies=[Depends(require_viewer)]` → `dependencies=[Depends(require_session_access)]` (имя path-параметра session_id совпадает — FastAPI подставит).

- [ ] **Step 5: прогон** — файл PASS + ВСЯ суита (особо: test_access_matrix VIEWER_GETS — require_session_access для viewer-роли не должен изменить 200/401-семантику).
- [ ] **Step 6: commit** — `feat(rbac): роль manager видит только свои звонки`

---

### Task 17: админ-SPA (admin.html + admin.js)

**Files:**
- Modify: `backend/static/admin.html` (полная версия)
- Create: `backend/static/admin.js`
- Test: ручная проверка + существующий test_root_dispatch

`admin.html` — полная версия (заменить заглушку):

```html
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>silentqa — админка платформы</title>
  <link rel="stylesheet" href="/styles.css">
  <style>
    /* минимальные доп-стили поверх styles.css */
    .pa-wrap { max-width: 1100px; margin: 0 auto; padding: 24px; }
    .pa-card { background: var(--card-bg, #fff); border-radius: 8px;
               padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
    .pa-table { width: 100%; border-collapse: collapse; }
    .pa-table th, .pa-table td { text-align: left; padding: 8px 12px;
                                 border-bottom: 1px solid #eee; }
    .pa-secret { font-family: monospace; background: #fff8e1; padding: 8px;
                 border-radius: 4px; word-break: break-all; }
    .pa-badge-suspended { color: #fff; background: #c62828; border-radius: 4px;
                          padding: 2px 8px; font-size: 12px; }
    .pa-row-actions button { margin-right: 6px; }
    .pa-login { max-width: 360px; margin: 80px auto; }
    .pa-hidden { display: none; }
    .pa-danger { color: #c62828; }
  </style>
</head>
<body>
  <div id="admin-app" class="pa-wrap">
    <div id="pa-login" class="pa-card pa-login pa-hidden">
      <h2>Админка платформы</h2>
      <form id="pa-login-form">
        <input type="email" id="pa-email" placeholder="Email" required><br>
        <input type="password" id="pa-password" placeholder="Пароль" required><br>
        <button type="submit">Войти</button>
        <div id="pa-login-error" class="pa-danger"></div>
      </form>
    </div>
    <div id="pa-main" class="pa-hidden">
      <header style="display:flex;justify-content:space-between;align-items:center">
        <h2>Клиенты платформы</h2>
        <div><span id="pa-whoami"></span> <button id="pa-logout">Выйти</button></div>
      </header>
      <div class="pa-card">
        <button id="pa-show-create">+ Новый клиент</button>
        <form id="pa-create-form" class="pa-hidden">
          <input id="pa-new-slug" placeholder="slug (латиница)" required>
          <input id="pa-new-name" placeholder="Название">
          <input type="email" id="pa-new-email" placeholder="Email админа" required>
          <button type="submit">Создать</button>
          <div id="pa-create-error" class="pa-danger"></div>
        </form>
        <div id="pa-create-result" class="pa-hidden pa-card">
          <h3>Клиент создан — сохрани, больше не покажем:</h3>
          <div>Пароль админа: <span class="pa-secret" id="pa-res-password"></span></div>
          <div>API-ключ: <span class="pa-secret" id="pa-res-key"></span></div>
        </div>
      </div>
      <div class="pa-card">
        <table class="pa-table" id="pa-tenants"></table>
      </div>
      <div class="pa-card pa-hidden" id="pa-tenant-card"></div>
    </div>
  </div>
  <script src="/admin.js"></script>
</body>
</html>
```

`admin.js` (полный):

```javascript
// Админ-SPA платформы. Все запросы — на свой origin (admin.silentqa.com).
const $ = (sel) => document.querySelector(sel);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...opts,
  });
  if (res.status === 401) {
    const body = await res.clone().json().catch(() => ({}));
    if (body.detail === 'platform_auth_required') { showLogin(); throw new Error('auth'); }
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

function showLogin() {
  $('#pa-login').classList.remove('pa-hidden');
  $('#pa-main').classList.add('pa-hidden');
}

function showMain(email) {
  $('#pa-login').classList.add('pa-hidden');
  $('#pa-main').classList.remove('pa-hidden');
  $('#pa-whoami').textContent = email;
}

async function boot() {
  try {
    const me = await api('/api/platform/auth/me');
    showMain(me.email);
    await loadTenants();
  } catch (e) { /* showLogin уже вызван */ }
}

$('#pa-login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('#pa-login-error').textContent = '';
  try {
    const me = await api('/api/platform/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email: $('#pa-email').value, password: $('#pa-password').value }),
    });
    showMain(me.email);
    await loadTenants();
  } catch (e) {
    $('#pa-login-error').textContent = e.message === 'too_many_attempts'
      ? 'Слишком много попыток — подожди' : 'Неверный email или пароль';
  }
});

$('#pa-logout').addEventListener('click', async () => {
  await api('/api/platform/auth/logout', { method: 'POST' }).catch(() => {});
  showLogin();
});

async function loadTenants() {
  const tenants = await api('/api/platform/tenants');
  const rows = tenants.map((t) => `
    <tr>
      <td><a href="#" data-slug="${t.slug}" class="pa-open">${t.slug}</a></td>
      <td>${t.display_name || ''}</td>
      <td>${t.status === 'suspended' ? '<span class="pa-badge-suspended">suspended</span>' : 'active'}</td>
      <td>${t.sessions_count}</td>
      <td>${t.minutes} мин</td>
      <td>${t.last_activity ? t.last_activity.slice(0, 16).replace('T', ' ') : '—'}</td>
    </tr>`).join('');
  $('#pa-tenants').innerHTML = `
    <tr><th>Клиент</th><th>Название</th><th>Статус</th>
        <th>Сессий (месяц)</th><th>Минут</th><th>Активность</th></tr>${rows}`;
  document.querySelectorAll('.pa-open').forEach((a) =>
    a.addEventListener('click', (ev) => { ev.preventDefault(); openTenant(a.dataset.slug); }));
}

$('#pa-show-create').addEventListener('click', () =>
  $('#pa-create-form').classList.toggle('pa-hidden'));

$('#pa-create-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('#pa-create-error').textContent = '';
  try {
    const res = await api('/api/platform/tenants', {
      method: 'POST',
      body: JSON.stringify({
        slug: $('#pa-new-slug').value.trim(),
        display_name: $('#pa-new-name').value.trim(),
        admin_email: $('#pa-new-email').value.trim(),
      }),
    });
    $('#pa-res-password').textContent = res.admin_password;
    $('#pa-res-key').textContent = res.api_key;
    $('#pa-create-result').classList.remove('pa-hidden');
    await loadTenants();
  } catch (e) { $('#pa-create-error').textContent = e.message; }
});

async function openTenant(slug) {
  const users = await api(`/api/platform/tenants/${slug}/users`);
  const userRows = users.map((u) => `
    <tr>
      <td>${u.email}</td><td>${u.role}</td><td>${u.employee_name || '—'}</td>
      <td class="pa-row-actions">
        <button data-act="reset" data-id="${u.id}">Сбросить пароль</button>
        <button data-act="del" data-id="${u.id}" class="pa-danger">Удалить</button>
      </td>
    </tr>`).join('');
  const card = $('#pa-tenant-card');
  card.classList.remove('pa-hidden');
  card.innerHTML = `
    <h3>${slug}</h3>
    <div class="pa-row-actions">
      <button id="pa-imp">Открыть дашборд (режим поддержки)</button>
      <button id="pa-suspend">Suspend</button>
      <button id="pa-activate">Activate</button>
      <button id="pa-rotate" class="pa-danger">Ротация API-ключа</button>
    </div>
    <div id="pa-card-secret"></div>
    <h4>Юзеры</h4>
    <table class="pa-table">${userRows}</table>`;
  $('#pa-imp').onclick = async () => {
    const r = await api(`/api/platform/tenants/${slug}/impersonate`, { method: 'POST' });
    window.open(r.url, '_blank');
  };
  $('#pa-suspend').onclick = async () => {
    if (!confirm(`Приостановить ${slug}? Дашборд и приём записей перестанут работать.`)) return;
    await api(`/api/platform/tenants/${slug}`, { method: 'PATCH', body: JSON.stringify({ status: 'suspended' }) });
    await loadTenants(); await openTenant(slug);
  };
  $('#pa-activate').onclick = async () => {
    await api(`/api/platform/tenants/${slug}`, { method: 'PATCH', body: JSON.stringify({ status: 'active' }) });
    await loadTenants(); await openTenant(slug);
  };
  $('#pa-rotate').onclick = async () => {
    if (!confirm('Старый ключ перестанет работать во ВСЕХ рекордерах клиента. Продолжить?')) return;
    const r = await api(`/api/platform/tenants/${slug}/rotate-key`, { method: 'POST' });
    $('#pa-card-secret').innerHTML =
      `Новый API-ключ (сохрани, больше не покажем): <span class="pa-secret">${r.api_key}</span>`;
  };
  card.querySelectorAll('button[data-act]').forEach((b) => {
    b.onclick = async () => {
      if (b.dataset.act === 'reset') {
        const r = await api(`/api/platform/tenants/${slug}/users/${b.dataset.id}/reset-password`, { method: 'POST' });
        $('#pa-card-secret').innerHTML =
          `Новый пароль (сохрани, больше не покажем): <span class="pa-secret">${r.password}</span>`;
      } else if (b.dataset.act === 'del') {
        if (!confirm('Удалить юзера?')) return;
        await api(`/api/platform/tenants/${slug}/users/${b.dataset.id}`, { method: 'DELETE' });
        await openTenant(slug);
      }
    };
  });
}

boot();
```

- [ ] **Step 1:** написать оба файла.
- [ ] **Step 2:** `pytest tests/test_root_dispatch.py -q` → PASS (маркер `id="admin-app"` сохранён).
- [ ] **Step 3: commit** — `feat(platform): админ-SPA (логин, клиенты, карточка, создание)`

---

### Task 18: клиентский SPA — Команда, Профиль, бейдж поддержки

**Files:**
- Modify: `backend/static/app.js`
- Modify: `backend/static/index.html`

Прочитать `index.html` целиком (62 строки) и навигацию в `app.js` (около строки 30, `$$('.nav-link')`) перед правками. Существующие паттерны: `currentUser = {email, role}` (после Task 12 в `/me` добавились `employee_name`, `impersonated_by`), `isAdmin()`, `api()`-хелпер, `showToast`.

- [ ] **Step 1: index.html** — в навигацию добавить (по образцу существующих nav-link):
```html
<a href="#team" class="nav-link" data-admin-only>Команда</a>
<a href="#profile" class="nav-link">Профиль</a>
```
и контейнеры-секции рядом с существующими view-контейнерами:
```html
<div id="team-view" class="view" style="display:none"></div>
<div id="profile-view" class="view" style="display:none"></div>
```
(сверить классы/структуру с существующими секциями index.html — повторить их точно.)

- [ ] **Step 2: app.js** — добавить:

1. Видимость нав-пункта: после установки `currentUser` (функция, где `currentUser = await api('/api/user-auth/me')`) —
```javascript
document.querySelectorAll('[data-admin-only]').forEach(el => {
  el.style.display = isAdmin() ? '' : 'none';
});
const banner = document.getElementById('support-banner');
if (currentUser.impersonated_by && !banner) {
  const b = document.createElement('div');
  b.id = 'support-banner';
  b.textContent = `Режим поддержки: ${currentUser.impersonated_by}`;
  b.style.cssText = 'background:#ff8f00;color:#fff;text-align:center;padding:4px;';
  document.body.prepend(b);
}
```

2. Роутинг: найти hash-роутер (обработчик `#call/<id>` и навигации) и добавить ветки `#team` → `renderTeam()`, `#profile` → `renderProfile()`. Ветку `#team` гейтить как существующую `#template`-ветку (не-админа редиректить на главную, не полагаясь только на серверный 403).

3. Рендеры (стиль и классы — как соседние секции app.js; `api()` и `showToast` — существующие):

```javascript
async function renderTeam() {
  const view = document.getElementById('team-view');
  const users = await api('/api/users');
  let knownNames = [];
  try { knownNames = (await api('/api/managers')).map(m => m.name); } catch (e) {}
  const options = knownNames.map(n => `<option value="${escapeHtml(n)}">`).join('');
  view.innerHTML = `
    <h2>Команда</h2>
    <datalist id="known-employees">${options}</datalist>
    <table class="pa-table">
      <tr><th>Email</th><th>Роль</th><th>Сотрудник (из рекордера)</th><th></th></tr>
      ${users.map(u => `
        <tr>
          <td>${escapeHtml(u.email)}</td>
          <td>
            <select data-role-for="${u.id}">
              ${['admin','viewer','manager'].map(r =>
                `<option value="${r}" ${u.role===r?'selected':''}>${r}</option>`).join('')}
            </select>
          </td>
          <td><input list="known-employees" data-emp-for="${u.id}"
                     value="${escapeHtml(u.employee_name||'')}" placeholder="—"></td>
          <td>
            <button data-save="${u.id}">Сохранить</button>
            <button data-reset="${u.id}">Сбросить пароль</button>
            <button data-del="${u.id}">Удалить</button>
          </td>
        </tr>`).join('')}
    </table>
    <h3>Добавить юзера</h3>
    <form id="team-add">
      <input type="email" id="team-email" placeholder="Email" required>
      <select id="team-role">
        <option value="viewer">viewer</option>
        <option value="admin">admin</option>
        <option value="manager">manager</option>
      </select>
      <input list="known-employees" id="team-emp" placeholder="Сотрудник (для manager)">
      <button type="submit">Создать</button>
    </form>
    <div id="team-secret"></div>`;
  view.querySelector('#team-add').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = { email: view.querySelector('#team-email').value,
                   role: view.querySelector('#team-role').value };
    const emp = view.querySelector('#team-emp').value.trim();
    if (emp) body.employee_name = emp;
    try {
      const r = await api('/api/users', { method: 'POST', body: JSON.stringify(body) });
      view.querySelector('#team-secret').innerHTML =
        `Пароль для ${escapeHtml(r.email)} (покажем один раз): <code>${r.password}</code>`;
      renderTeam();
    } catch (e) { showToast(e.message, 'error'); }
  });
  view.querySelectorAll('button[data-save]').forEach(b => b.onclick = async () => {
    const id = b.dataset.save;
    try {
      await api(`/api/users/${id}`, { method: 'PATCH', body: JSON.stringify({
        role: view.querySelector(`[data-role-for="${id}"]`).value,
        employee_name: view.querySelector(`[data-emp-for="${id}"]`).value.trim(),
      })});
      showToast('Сохранено');
    } catch (e) { showToast(e.message, 'error'); }
  });
  view.querySelectorAll('button[data-reset]').forEach(b => b.onclick = async () => {
    try {
      const r = await api(`/api/users/${b.dataset.reset}/reset-password`, { method: 'POST' });
      view.querySelector('#team-secret').innerHTML =
        `Новый пароль (покажем один раз): <code>${r.password}</code>`;
    } catch (e) { showToast(e.message, 'error'); }
  });
  view.querySelectorAll('button[data-del]').forEach(b => b.onclick = async () => {
    if (!confirm('Удалить юзера?')) return;
    try { await api(`/api/users/${b.dataset.del}`, { method: 'DELETE' }); renderTeam(); }
    catch (e) { showToast(e.message, 'error'); }
  });
}

async function renderProfile() {
  const view = document.getElementById('profile-view');
  view.innerHTML = `
    <h2>Профиль</h2>
    <div>${escapeHtml(currentUser.email)} (${currentUser.role})</div>
    <h3>Сменить пароль</h3>
    <form id="pw-form">
      <input type="password" id="pw-old" placeholder="Текущий пароль" required>
      <input type="password" id="pw-new" placeholder="Новый пароль (мин. 8)" required minlength="8">
      <button type="submit">Сменить</button>
    </form>`;
  view.querySelector('#pw-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
      await api('/api/user-auth/change-password', { method: 'POST', body: JSON.stringify({
        old_password: view.querySelector('#pw-old').value,
        new_password: view.querySelector('#pw-new').value,
      })});
      showToast('Пароль изменён; остальные сессии разлогинены');
      ev.target.reset();
    } catch (e) {
      showToast(e.message === 'wrong_password' ? 'Неверный текущий пароль' : e.message, 'error');
    }
  });
}
```
Если `escapeHtml` в app.js отсутствует под этим именем — найти существующий эскейпер и использовать его.

4. Роль manager в SPA: серверная фильтрация уже всё прячет; дополнительно скрыть admin-кнопки уже делает `isAdmin()`. Проверить: нав-пункт «Команда» скрыт (data-admin-only), дашборд-лента у менеджера показывает только свои звонки (сервер).

- [ ] **Step 3: вручную** — `cd backend && SECRET_KEY=x ../.venv/bin/python -c "import pathlib; src=pathlib.Path('static/app.js').read_text(); assert 'renderTeam' in src"` + открыть SPA не выйдет без БД: синтаксис-чек `node --check static/app.js && node --check static/admin.js` (node есть на сервере).
- [ ] **Step 4: commit** — `feat(spa): Команда, Профиль, бейдж режима поддержки`

---

### Task 19: матрица доступа, полные прогоны, репетиция на скретч-БД, ранбук

**Files:**
- Modify: `backend/tests/test_access_matrix.py`
- Modify: `docs/superpowers/specs/2026-06-10-phase1-deploy-runbook.md`

- [ ] **Step 1: расширить матрицу** в `test_access_matrix.py`:

```python
# Платформенные мутации: 401 без платформенной сессии (хост admin.)
PLATFORM_MUTATIONS = [
    ("get", "/api/platform/tenants"),
    ("post", "/api/platform/tenants"),
    ("patch", "/api/platform/tenants/acme"),
    ("post", "/api/platform/tenants/acme/rotate-key"),
    ("get", "/api/platform/tenants/acme/users"),
    ("post", "/api/platform/tenants/acme/impersonate"),
    ("get", "/api/companies"),
]

# Команда: admin-only (viewer и manager → 403)
TEAM_ADMIN_ONLY = [
    ("get", "/api/users"),
    ("post", "/api/users"),
    ("patch", "/api/users/00000000-0000-0000-0000-000000000001"),
    ("delete", "/api/users/00000000-0000-0000-0000-000000000001"),
    ("post", "/api/users/00000000-0000-0000-0000-000000000001/reset-password"),
]
```
+ параметризованные тесты: PLATFORM_MUTATIONS на `https://admin.silentqa.com` без cookie → 401 `platform_auth_required`; те же пути на тенант-хосте → 404 (контур-гейт); TEAM_ADMIN_ONLY с viewer-cookie → 403 и с manager-cookie → 403; `/api/user-auth/change-password` с viewer-cookie → НЕ 401/403 (403 у него только wrong_password — мокать не надо: без БД он 500? НЕТ — `_get_password_hash` пойдёт в БД. Для этого кейса достаточно анонима: POST change-password без cookie → 401).

- [ ] **Step 2: полные прогоны**

```bash
cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/ -q
cd /root/projects/meet-mt/worker && set -a; source ../.env 2>/dev/null; set +a; ../.venv/bin/python -m pytest -q
```
Expected: backend все PASS (81 старых + новые), worker 149 passed (не трогали — регресс-чек).

- [ ] **Step 3: репетиция на скретч-БД** (как Task 15 Плана 1):

```bash
sudo -u postgres dropdb plan3b_scratch; sudo -u postgres createdb plan3b_scratch
cd /root/projects/meet-mt/backend
export DATABASE_URL_SYNC="postgresql://postgres@/plan3b_scratch?host=/var/run/postgresql"
export DATABASE_URL="postgresql+asyncpg://postgres@/plan3b_scratch?host=/var/run/postgresql"
export SECRET_KEY=scratch-test
../.venv/bin/python -m app.migrate                       # S001→S003 + тенант-трек
../.venv/bin/python -m app.platform_admin create --email t@t.io --password tt12345
../.venv/bin/python -m app.provision_tenant acme --name ACME \
    --admin-email a@acme.io --admin-password secret123
sudo -u postgres psql plan3b_scratch -c \
  "SELECT column_name FROM information_schema.columns
   WHERE table_schema='t_acme' AND table_name='users'" | grep employee_name
sudo -u postgres psql plan3b_scratch -c \
  "INSERT INTO t_acme.users (email, password_hash, role, employee_name)
   VALUES ('m@acme.io', 'x', 'manager', 'Иванов')"   # CHECK пропускает manager
sudo -u postgres psql plan3b_scratch -c "SELECT email FROM shared.platform_admins"
```
Expected: миграции зелёные, `employee_name` есть, role=manager вставляется, админ платформы посеян. Затем уронить негатив: `INSERT ... role='boss'` → ошибка CHECK (так и должно).

- [ ] **Step 4: ранбук** — в `docs/superpowers/specs/2026-06-10-phase1-deploy-runbook.md`, в раздел «silentqa platform — задеплоено 2026-06-12» дописать подраздел:

```markdown
### Деплой Plan 3b (админка + команда + роль manager)

1. `cd /root/projects/silentqa && git pull origin multi-tenant-core-phase1`
2. `systemctl restart silentqa-backend silentqa-worker` — S003+013 накатятся
   на старте (journalctl: "[migrate] done").
3. Бутстрап владельца:
   `cd backend && set -a; source ../.env; set +a; ../.venv/bin/python -m app.platform_admin create --email alex.chechetov@gmail.com`
   — пароль печатается один раз. (provision_tenant теперь тоже печатает
   сгенерированный пароль админа клиента — изменение вывода CLI.)
4. Из `.env` удалить `AUTH_USERNAME`, `AUTH_PASSWORD` (Basic выпилен) и
   рестартнуть backend ещё раз.
5. Проверить Redis ≥ 6.2 (`redis-cli INFO server | grep redis_version`) —
   impersonation использует GETDEL.
6. Смоук: https://admin.silentqa.com → логин → список клиентов со
   статистикой; impersonate в fulldent (бейдж «режим поддержки»);
   suspend/activate тестом НЕ на живом клиенте; «Команда» у fulldent.
```

- [ ] **Step 5: commit** — `test(matrix): платформенный контур и Команда в матрице доступа + ранбук Plan 3b`

---

## Финал (после Task 19)

1. Полный прогон обеих суит ещё раз + `git log --oneline` — все коммиты на месте.
2. Финальное сквозное ревью изменений ветки (диапазон коммитов Plan 3b) — отдельным ревьюером, фиксы — отдельными коммитами.
3. Деплой по ранбуку — ТОЛЬКО после явного «деплоим» от владельца.

## Самопроверка плана (выполнена)

- Спека §1 → Tasks 3, 7; §2 → Tasks 1, 4, 5, 6, 13; §3.1–3.2 → Tasks 8, 9; §3.3 → Tasks 2, 10, 11; §3.4 → Task 12; §4 → Tasks 14, 15, 18; §5 → Tasks 1, 16; §7 → Tasks 17, 18; §8 → Task 1; §9 → Tasks 2–16, 19; §10 → Task 19 (ранбук). §6 — Plan 3c, не здесь.
- Типы сквозные: `ProvisionResult/ProvisionError/generate_api_key/generate_password` (Task 8) ← Tasks 9, 11, 14; `registry.invalidate()` (Task 2) ← Tasks 9, 10; `PLATFORM_COOKIE/platform_sessions` (Task 4) ← Tasks 5, 6, 9–12; `UserCtx.employee_name/impersonated_by` (Task 12) ← Tasks 16, 18; `destroy_user_sessions` (Task 14) ← Task 15.
```
