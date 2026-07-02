# SilentQA 2.0 Техдолг — План 1/3: Фундамент (миграции, CORS, CI, стейджинг) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Миграции уходят из lifespan бэкенда в шаг деплоя (сломанная миграция больше не роняет прод), CORS берётся из настроек вместо захардкоженной `["*"]`, появляется CI на GitHub Actions и честный стейджинг silentqa-dev на :8008.

**Architecture:** Код-часть: `app/migrate.py` получает read-only `check()` (сверка `alembic_version` каждой схемы с head'ом скриптов), lifespan `main.py` вместо subprocess-миграции делает check-only с CRITICAL-логом и **не роняет** процесс; CORS-мидлварь получает `origins` из `settings.ALLOWED_ORIGINS`. Ops-часть: `run.sh` и новый `scripts/deploy_prod.sh` гоняют `python -m app.migrate` ДО старта/рестарта; стейджинг — отдельные systemd-юниты (`silentqa-staging-*`, порт 8008) поверх этого чекаута с собственным `.env.staging`, БД `silentqa_staging` и Redis `/5`; стейджинг-бэкенд мигрирует в `ExecStartPre` (fail-fast до прода — в этом его смысл). CI — три джобы (backend / worker / desktop) на pytest + `node --test`.

**Tech Stack:** FastAPI 0.115 / Starlette TestClient, Alembic 1.14 (`ScriptDirectory`), psycopg2 через `tenancy.db.shared_connect`, systemd, GitHub Actions, Node ≥18 (`node --test`).

**Роадмап-источник:** `~/reviews/silentqa-roadmap-2.0-3.0.md`, пункт 2.3 (+CORS-фикс из «Техдолг и риски» №1, №4).

## Global Constraints

- Backend-тесты: `cd backend && python -m pytest tests/`. Worker-тесты: `cd worker && python -m pytest`. Тесты БЕЗ Postgres/Redis (стабы/monkeypatch, `FakeRedis` из `backend/tests/conftest.py`).
- Живые деплои на боксе (realestate/meet/dental/fillers и **прод silentqa на :8007**) не трогаем, кроме явно описанных ops-шагов; Caddy менять только через `caddy validate --config /etc/caddy/Caddyfile` → `systemctl reload caddy` (не restart).
- Схемные имена перед интерполяцией в SQL — только через `tenancy.identifiers.validate_schema_name` (хаус-рул, enforced-паттерн).
- Секреты генерим `openssl rand`, в git не попадают (`.env*` в `.gitignore`; `.env.staging` добавить туда же — Task 7).
- Комментарии/доки — на русском; коммиты — conventional commits в стиле репо (`fix(scope): описание по-русски`).
- Порядок задач важен: Task 2 → Task 3 (lifespan использует `check`); Task 4/5 — до первого прод-деплоя кода из Task 3 (иначе рестарт прода перестанет мигрировать, а деплой-шаг ещё не появился).

---

### Task 1: CORS из настроек вместо хардкода `["*"]`

**Files:**
- Modify: `backend/app/main.py:39-46` (блок CORS; включает уже существующую МЁРТВУЮ `origins` на строке 40 — она вычисляется, но не используется)
- Modify: `.env.example` (комментарий к ALLOWED_ORIGINS)
- Test: `backend/tests/test_cors_config.py` (создать)

**Interfaces:**
- Produces: `app.main.origins: list[str]` — распарсенный `settings.ALLOWED_ORIGINS`, реально передаётся в `CORSMiddleware(allow_origins=origins)`. Дефолт `"*"` сохраняет текущее поведение (расширения/десктоп ходят с origin `chrome-extension://…` / `file://`).
- Consumes: `settings.ALLOWED_ORIGINS` (`backend/app/config.py:10`, уже существует).

- [ ] **Step 1: Написать падающий тест**

Создать `backend/tests/test_cors_config.py`:

```python
"""CORS: allow_origins берётся из settings.ALLOWED_ORIGINS, а не хардкод."""
from fastapi.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient

import app.main as main


def _cors_kwargs():
    entries = [m for m in main.app.user_middleware if m.cls is CORSMiddleware]
    assert len(entries) == 1
    return entries[0].kwargs


def test_allow_origins_uses_settings():
    # плумбинг: в мидлварь уходит именно распарсенный settings-список
    assert _cors_kwargs()["allow_origins"] == main.origins


def test_default_stays_wildcard():
    # дефолт ALLOWED_ORIGINS="*" сохраняет текущее поведение
    assert main.origins == ["*"]


def test_preflight_wildcard():
    c = TestClient(main.app)
    r = c.options("/health", headers={
        "Origin": "https://anywhere.example",
        "Access-Control-Request-Method": "GET",
    })
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
```

- [ ] **Step 2: Прогнать — падает**

При дефолтном `ALLOWED_ORIGINS="*"` хардкод и настройка совпадают (`["*"] == ["*"]`), поэтому красный прогон делаем с переопределением:

Run: `cd backend && ALLOWED_ORIGINS="https://a.example" python -m pytest tests/test_cors_config.py::test_allow_origins_uses_settings -v`
Expected: FAIL — kwargs мидлвари = `["*"]` (хардкод), а `main.origins` = `["https://a.example"]`.

- [ ] **Step 3: Минимальная реализация**

В `backend/app/main.py` заменить блок CORS (строки 39-46; `origins = [...]` на строке 40 уже существует, но мёртвая — замена её «оживляет»):

```python
# CORS: список origin'ов из настроек. Дефолт "*" — расширения и десктоп
# ходят с origin chrome-extension://… / file://; сужать только вместе
# с ревизией клиентов записи (план 2/3).
origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

В `.env.example` после блока `# Multi-tenant (Phase 1)` добавить:

```
# CORS: origin'ы через запятую. "*" = все (нужно расширениям/десктопу —
# их origin chrome-extension://… / file://). Сужать только вместе с
# ревизией клиентов записи.
# ALLOWED_ORIGINS=*
```

- [ ] **Step 4: Прогнать — зелёный, включая прогон с кастомным origin**

Run: `cd backend && python -m pytest tests/test_cors_config.py -v && ALLOWED_ORIGINS="https://a.example" python -m pytest tests/test_cors_config.py::test_allow_origins_uses_settings -v`
Expected: PASS оба прогона (kwargs теперь всегда = `main.origins`). Полный сьют: `python -m pytest tests/` → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py backend/tests/test_cors_config.py .env.example
git commit -m "fix(cors): allow_origins из settings.ALLOWED_ORIGINS вместо хардкода \"*\""
```

---

### Task 2: `app.migrate` — read-only `check()` + CLI `--check`

**Files:**
- Modify: `backend/app/migrate.py`
- Test: `backend/tests/test_migrate_check.py` (создать)

**Interfaces:**
- Produces: `app.migrate.check() -> list[str]` — список схем (`shared` и/или `t_<slug>`), чей `alembic_version` НЕ равен head'у соответствующего дерева скриптов; пустой список = всё актуально; ничего не пишет в БД. `app.migrate.main(argv: list[str] | None = None)` — при `argv == ["--check"]` печатает результат и `SystemExit(1)` при расхождениях; без аргументов — прежнее поведение (upgrade всех). `app.migrate._script_head(ini_name: str, script_dir: str) -> str | None` — head дерева скриптов (для monkeypatch в тестах).
- Consumes: `shared_connect`, `validate_schema_name`, `_config` (все уже в `migrate.py`).

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/tests/test_migrate_check.py`:

```python
"""app.migrate --check: read-only сверка head'ов без применения миграций."""
import pytest

import app.migrate as m


class FakeCursor:
    def __init__(self, versions, tenants):
        self.versions = versions   # schema -> version_num | None (None = нет таблицы)
        self.tenants = tenants     # список schema_name активных тенантов
        self._rows = []

    def execute(self, sql, params=None):
        if "to_regclass" in sql:
            schema = params[0].split(".")[0]
            self._rows = [("x" if self.versions.get(schema) is not None else None,)]
        elif "FROM shared.tenants" in sql:
            self._rows = [(s,) for s in self.tenants]
        elif "alembic_version" in sql:
            schema = sql.split('"')[1]
            self._rows = [(self.versions[schema],)]

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass


def _wire(monkeypatch, versions, tenants):
    monkeypatch.setattr(m, "shared_connect",
                        lambda: FakeConn(FakeCursor(versions, tenants)))
    monkeypatch.setattr(m, "_script_head",
                        lambda ini, d: "H_SHARED" if "shared" in ini else "H_TENANT")


def test_all_current(monkeypatch):
    _wire(monkeypatch, {"shared": "H_SHARED", "t_acme": "H_TENANT"}, ["t_acme"])
    assert m.check() == []


def test_lagging_tenant(monkeypatch):
    _wire(monkeypatch, {"shared": "H_SHARED", "t_acme": "old"}, ["t_acme"])
    assert m.check() == ["t_acme"]


def test_lagging_shared(monkeypatch):
    _wire(monkeypatch, {"shared": "old", "t_acme": "H_TENANT"}, ["t_acme"])
    assert m.check() == ["shared"]


def test_missing_version_table_counts_as_lagging(monkeypatch):
    # свежепровиженный тенант без alembic_version — тоже «отстаёт»
    _wire(monkeypatch, {"shared": "H_SHARED", "t_new": None}, ["t_new"])
    assert m.check() == ["t_new"]


def test_invalid_schema_from_db_fails_loud(monkeypatch):
    _wire(monkeypatch, {"shared": "H_SHARED"}, ["t_acme; DROP TABLE x"])
    with pytest.raises(ValueError):
        m.check()


def test_cli_check_exit_codes(monkeypatch, capsys):
    _wire(monkeypatch, {"shared": "H_SHARED", "t_acme": "H_TENANT"}, ["t_acme"])
    m.main(["--check"])  # не бросает
    assert "heads OK" in capsys.readouterr().out

    _wire(monkeypatch, {"shared": "H_SHARED", "t_acme": "old"}, ["t_acme"])
    with pytest.raises(SystemExit) as e:
        m.main(["--check"])
    assert e.value.code == 1
    assert "t_acme" in capsys.readouterr().out
```

- [ ] **Step 2: Прогнать — падает**

Run: `cd backend && python -m pytest tests/test_migrate_check.py -v`
Expected: FAIL — `AttributeError: module 'app.migrate' has no attribute '_script_head'` (и `check`).

- [ ] **Step 3: Реализация**

В `backend/app/migrate.py`: добавить импорт после `from alembic.config import Config`:

```python
from alembic.script import ScriptDirectory
```

После `run_tenant(...)` добавить:

```python
def _script_head(ini_name: str, script_dir: str) -> str | None:
    return ScriptDirectory.from_config(_config(ini_name, script_dir)).get_current_head()


def _db_version(cur, schema: str) -> str | None:
    """version_num схемы или None, если alembic_version ещё нет."""
    cur.execute("SELECT to_regclass(%s)", (f"{schema}.alembic_version",))
    if cur.fetchone()[0] is None:
        return None
    cur.execute(f'SELECT version_num FROM "{schema}".alembic_version')
    row = cur.fetchone()
    return row[0] if row else None


def check() -> list[str]:
    """Схемы, отстающие от head'а скриптов. Read-only, ничего не мигрирует."""
    mismatched: list[str] = []
    shared_head = _script_head("alembic_shared.ini", "alembic_shared")
    tenant_head = _script_head("alembic.ini", "alembic")
    conn = shared_connect()
    try:
        with conn.cursor() as cur:
            if _db_version(cur, "shared") != shared_head:
                mismatched.append("shared")
            cur.execute(
                "SELECT schema_name FROM shared.tenants "
                "WHERE status = 'active' ORDER BY slug"
            )
            schemas = [r[0] for r in cur.fetchall()]
            for schema in schemas:
                validate_schema_name(schema)  # последний рубеж перед интерполяцией
                if _db_version(cur, schema) != tenant_head:
                    mismatched.append(schema)
    finally:
        conn.close()
    return mismatched
```

Заменить `main()`:

```python
def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--check"]:
        mismatched = check()
        if mismatched:
            print("[migrate] ОТСТАЮТ: " + ", ".join(mismatched), flush=True)
            raise SystemExit(1)
        print("[migrate] heads OK", flush=True)
        return
    run_shared()
    for schema in _active_schemas():
        print(f"[migrate] tenant track → {schema}", flush=True)
        run_tenant(schema)
    print("[migrate] done", flush=True)
```

- [ ] **Step 4: Прогнать — зелёный + полный сьют**

Run: `cd backend && python -m pytest tests/test_migrate_check.py tests/ -v`
Expected: PASS все.

- [ ] **Step 5: Живой смоук на локальной БД (миграции уже применены)**

Run: `cd backend && set -a; source ../.env; set +a; python -m app.migrate --check`
Expected: `[migrate] heads OK`, exit 0.

- [ ] **Step 6: Commit**

```bash
git add backend/app/migrate.py backend/tests/test_migrate_check.py
git commit -m "feat(migrate): read-only check() + CLI --check — сверка head'ов без апгрейда"
```

---

### Task 3: lifespan — check-only вместо миграций, без падения

**Files:**
- Modify: `backend/app/main.py:1-34` (lifespan + импорты) + readiness-проба рядом с `/health` (строки 78-80)
- Test: `backend/tests/test_startup_check.py` (создать)

**Interfaces:**
- Consumes: `app.migrate.check` из Task 2 (импортируется как `migrate_check` — модульный алиас, через который тесты monkeypatch'ат).
- Produces: старт бэкенда БОЛЬШЕ НЕ применяет миграции и не падает при их отставании/недоступной БД — только CRITICAL-лог. Инвариант для Task 4/5: применение миграций теперь обязано происходить в деплой-шаге.
- Produces: `GET /health/ready` — readiness-проба (`SELECT 1` через async-engine): `{"status":"ready"}` при живой БД, 503 `{"status":"degraded","db":"unreachable"}` при мёртвой. `/health` остаётся статическим liveness. Нужна деплой-чеку (Task 5): после ухода auto-migrate из lifespan процесс поднимается и при мёртвой БД — «deploy OK» по одному `/health` стал бы ложным.

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/tests/test_startup_check.py`:

```python
"""Старт бэкенда: миграции НЕ применяются; head-check только логирует."""
import logging

from starlette.testclient import TestClient

import app.main as main


def test_mismatch_warns_but_starts(monkeypatch, caplog):
    monkeypatch.setattr(main, "migrate_check", lambda: ["t_lag"])
    with caplog.at_level(logging.CRITICAL):
        with TestClient(main.app) as c:  # контекст-менеджер гоняет lifespan
            assert c.get("/health").json() == {"status": "ok"}
    assert any("t_lag" in r.getMessage() for r in caplog.records)


def test_clean_start_silent(monkeypatch, caplog):
    monkeypatch.setattr(main, "migrate_check", lambda: [])
    with caplog.at_level(logging.CRITICAL):
        with TestClient(main.app):
            pass
    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]


def test_db_down_still_starts(monkeypatch, caplog):
    def boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(main, "migrate_check", boom)
    with caplog.at_level(logging.CRITICAL):
        with TestClient(main.app) as c:
            assert c.get("/health").status_code == 200
    assert any("migrate check" in r.getMessage() for r in caplog.records)


def test_ready_ok(monkeypatch):
    async def ping_ok():
        return None
    monkeypatch.setattr(main, "_db_ping", ping_ok)
    assert TestClient(main.app).get("/health/ready").status_code == 200


def test_ready_degraded_when_db_down(monkeypatch):
    async def ping_boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(main, "_db_ping", ping_boom)
    r = TestClient(main.app).get("/health/ready")
    assert r.status_code == 503
    assert r.json()["db"] == "unreachable"
```

- [ ] **Step 2: Прогнать — падает**

Run: `cd backend && python -m pytest tests/test_startup_check.py -v`
Expected: FAIL — `AttributeError: <module 'app.main'> has no attribute 'migrate_check'` (а без monkeypatch lifespan полез бы в subprocess-миграцию).

- [ ] **Step 3: Реализация**

В `backend/app/main.py` заменить блок импортов и lifespan (строки 1-34):

```python
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root → tenancy

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.migrate import check as migrate_check
from app.routes import (
    chunks, sessions, companies, transcripts, analysis, managers,
    amocrm, templates, complexes, knowledge, auth, user_auth, tenancy_check, platform_auth,
    platform_tenants, users, eval_profiles,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Миграции применяются на ДЕПЛОЕ (scripts/deploy_prod.sh, run.sh,
    # staging ExecStartPre), не на старте: упавшая миграция одного тенанта
    # не должна ронять бэкенд для всех. Здесь — только быстрая read-only
    # сверка head'ов с CRITICAL-логом.
    try:
        mismatched = await run_in_threadpool(migrate_check)
    except Exception:
        logging.getLogger(__name__).critical(
            "migrate check failed (БД недоступна?)", exc_info=True)
    else:
        if mismatched:
            logging.getLogger(__name__).critical(
                "Alembic heads расходятся: %s — запусти `python -m app.migrate`",
                ", ".join(mismatched))
    yield
```

(`import subprocess` из шапки удалить — больше не используется.)

Рядом с `/health` (строки 78-80) добавить readiness-пробу; `JSONResponse` дописать в существующий импорт `from fastapi.responses import FileResponse`, `text` — из `sqlalchemy`, `engine` — из `app.database`:

```python
async def _db_ping() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@app.get("/health/ready")
async def health_ready():
    # Readiness для deploy_prod.sh: процесс жив И БД доступна.
    # /health остаётся статическим liveness.
    try:
        await _db_ping()
    except Exception:
        logging.getLogger(__name__).warning("readiness: БД недоступна", exc_info=True)
        return JSONResponse({"status": "degraded", "db": "unreachable"}, status_code=503)
    return {"status": "ready"}
```

- [ ] **Step 4: Прогнать — зелёный + полный сьют**

Run: `cd backend && python -m pytest tests/test_startup_check.py tests/ -v`
Expected: PASS все (существующие тесты lifespan не гоняют — TestClient без контекст-менеджера его не запускает).

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py backend/tests/test_startup_check.py
git commit -m "feat(startup): lifespan — check-only сверка миграций (без падения) + readiness-проба /health/ready"
```

---

### Task 4: `run.sh` — миграции перед стартом бэкенда

**Files:**
- Modify: `run.sh` (функции `start_backend`, `start_all`)

**Interfaces:**
- Consumes: `python -m app.migrate` (полный прогон, Task 2 сохранил это поведение по умолчанию).
- Produces: `./run.sh backend` и `./run.sh all` применяют миграции ДО uvicorn — локальный дев-флоу не деградирует после Task 3.

- [ ] **Step 1: Правка**

В `run.sh` после блока `mkdir -p "$AUDIO_STORAGE_PATH" ...` добавить функцию:

```bash
run_migrations() {
    echo "Applying migrations (shared + все тенанты)..."
    (cd backend && python -m app.migrate)
}
```

В `start_backend()` первой строкой тела (до `echo "Starting backend..."`) и в `start_all()` после `mkdir -p data/audio data/results` добавить вызов:

```bash
    run_migrations
```

- [ ] **Step 2: Синтаксис-проверка**

Run: `bash -n run.sh`
Expected: пусто, exit 0.

- [ ] **Step 3: Живой смоук**

Run: `./run.sh backend` (Ctrl+C после старта)
Expected: сначала `[migrate] ... done`, затем uvicorn на :8002; в логе старта НЕТ subprocess-миграции (lifespan печатает только check).

- [ ] **Step 4: Commit**

```bash
git add run.sh
git commit -m "feat(run.sh): миграции до старта бэкенда — компенсация ухода auto-migrate из lifespan"
```

---

### Task 5: `scripts/deploy_prod.sh` + обновление ранбука

**Files:**
- Create: `scripts/deploy_prod.sh`
- Modify: `docs/superpowers/specs/2026-06-10-phase1-deploy-runbook.md` (дописать секцию)

**Interfaces:**
- Produces: единственный санкционированный способ деплоя прода: `scripts/deploy_prod.sh` = rollback-точка (SHA до merge печатается в лог) → FF-merge → pip install → **миграции** → рестарт юнитов → readiness-check (`/health/ready`, ретраи до ~30с — одиночный curl после `sleep 3` хрупок, а статический `/health` не видит мёртвую БД). Env-переопределения: `PROD_DIR` (дефолт `/root/projects/silentqa`), `BRANCH` (дефолт `multi-tenant-core-phase1`).
- КРИТИЧНО: выполнить до/вместе с первым прод-деплоем Task 3 — после него рестарт юнита сам по себе миграции не применяет.

- [ ] **Step 1: Создать `scripts/deploy_prod.sh`**

```bash
#!/usr/bin/env bash
# Деплой прода SilentQA (/root/projects/silentqa): FF-merge → миграции → рестарт.
# Миграции ДО рестарта: если упали — старый код продолжает работать,
# даунтайма нет (раньше миграция жила в lifespan и роняла старт).
set -euo pipefail

PROD_DIR="${PROD_DIR:-/root/projects/silentqa}"
BRANCH="${BRANCH:-multi-tenant-core-phase1}"

cd "$PROD_DIR"
PREV_SHA=$(git rev-parse HEAD)
echo "rollback point: $PREV_SHA (откат: git reset --hard $PREV_SHA && systemctl restart silentqa-backend silentqa-worker)"
git fetch origin
git merge --ff-only "origin/$BRANCH"

# Зависимости (no-op, если requirements не менялись)
.venv/bin/pip install -q -r backend/requirements.txt -r worker/requirements.txt

# Миграции с прод-окружением — до рестарта сервисов
set -a; source .env; set +a
(cd backend && "$PROD_DIR/.venv/bin/python" -m app.migrate)

systemctl restart silentqa-backend silentqa-worker

# Readiness с ретраями (до ~30с): /health/ready = процесс жив И БД доступна
for i in $(seq 1 15); do
    sleep 2
    if curl -sf localhost:8007/health/ready >/dev/null; then
        echo "deploy OK: /health/ready отвечает (попытка $i)"
        exit 0
    fi
done
echo "deploy FAILED: /health/ready не отвечает 30с — journalctl -u silentqa-backend -n 50"
echo "откат: git reset --hard $PREV_SHA && systemctl restart silentqa-backend silentqa-worker"
exit 1
```

Run: `chmod +x scripts/deploy_prod.sh && bash -n scripts/deploy_prod.sh`
Expected: exit 0.

- [ ] **Step 2: Проверить прод-remote**

Run: `git -C /root/projects/silentqa remote -v`
Expected: `origin https://github.com/Chechetov/silentqa-platform.git`. Если прод тянет из другого remote — поправить `git fetch origin` под фактический (зафиксировать в ранбуке).

- [ ] **Step 3: Дописать ранбук**

В `docs/superpowers/specs/2026-06-10-phase1-deploy-runbook.md` добавить в конец секцию:

```markdown
## Деплой после 2026-07: миграции — шаг деплоя, не старта

С коммита «feat(startup): lifespan — check-only …» рестарт юнита сам
миграции НЕ применяет (старт делает только read-only сверку и CRITICAL-лог).
Канонический деплой прода:

    /root/projects/silentqa-dev/scripts/deploy_prod.sh

(rollback-SHA в лог → FF-merge origin/multi-tenant-core-phase1 → pip install
→ python -m app.migrate → systemctl restart silentqa-backend silentqa-worker
→ curl /health/ready с ретраями до 30с).
Откат миграции при фейле: старый код продолжает работать; чинить миграцию
на стейджинге (silentqa-staging-*, :8008) и повторять деплой. Откат кода:
git reset --hard <SHA из лога деплоя> && systemctl restart silentqa-backend silentqa-worker.
```

- [ ] **Step 4: Commit**

```bash
git add scripts/deploy_prod.sh docs/superpowers/specs/2026-06-10-phase1-deploy-runbook.md
git commit -m "feat(deploy): scripts/deploy_prod.sh — FF-merge → миграции → рестарт → health-check"
```

---

### Task 6: CI — GitHub Actions (backend + worker + desktop)

**Files:**
- Create: `.github/workflows/tests.yml`
- Modify: `desktop-app/package.json` (добавить script `test`)

**Interfaces:**
- Produces: workflow `tests` на push/PR: джоба `backend` (pip install backend/requirements.txt → pytest), джоба `worker` (CPU-torch с индекса pytorch → requirements → pytest; ffmpeg на всякий случай), джоба `desktop` (`npm test` = `node --test tests/`, без npm install — тестам зависимости не нужны).
- Constraint: worker-тесты импортируют `tasks.pipeline` → тянут torch/pyannote — потому CPU-wheel (`--index-url https://download.pytorch.org/whl/cpu`), иначе CI качает CUDA-сборку ~2.5 ГБ.

- [ ] **Step 1: Добавить npm-скрипт**

В `desktop-app/package.json` в `"scripts"` добавить:

```json
    "test": "node --test tests/"
```

Run: `cd desktop-app && node --version && npm test`
Expected: Node ≥18; оба файла (`isApiKeyMode.test.js`, `normalizeServerUrl.test.js`) pass (plain-assert скрипты: exit 0 = pass у `node --test`).

- [ ] **Step 2: Создать `.github/workflows/tests.yml`**

```yaml
name: tests

on:
  push:
    branches: ["**"]
  pull_request:

jobs:
  backend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: backend/requirements.txt
      - run: pip install -r backend/requirements.txt
      - run: python -m pytest tests/ -v
        working-directory: backend

  worker:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: worker/requirements.txt
      - run: sudo apt-get update && sudo apt-get install -y ffmpeg
      # CPU-сборка torch — иначе тянется CUDA ~2.5ГБ; версии = пины requirements
      - run: pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cpu
      - run: pip install -r worker/requirements.txt
      - run: python -m pytest -v
        working-directory: worker

  desktop:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - run: npm test
        working-directory: desktop-app
```

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/tests.yml')); print('yaml ok')"`
Expected: `yaml ok` (pyyaml есть в venv; если нет — `pip install pyyaml`).

- [ ] **Step 3: Commit + push + проверить прогон**

```bash
git add .github/workflows/tests.yml desktop-app/package.json
git commit -m "ci: GitHub Actions — backend/worker/desktop тесты на push и PR"
git push origin multi-tenant-core-phase1
```

Run: `gh run watch --repo Chechetov/silentqa-platform` (или `gh run list -L 3`)
Expected: все 3 джобы зелёные. Если worker-джоба падает на установке зависимостей — зафиксировать лог и чинить пином/системным пакетом (НЕ ослаблять тесты).

---

### Task 7: Честный стейджинг silentqa-dev на :8008

**Files (всё — ops, вне git, кроме .gitignore):**
- Modify: `.gitignore` (строка `.env.staging`)
- Create (на боксе): `/root/projects/silentqa-dev/.env.staging`, `/etc/systemd/system/silentqa-staging-backend.service`, `/etc/systemd/system/silentqa-staging-worker.service`

**Interfaces:**
- Produces: стейджинг-контур: БД `silentqa_staging` (Postgres :5432), Redis `redis://localhost:6379/5`, backend :8008, `BASE_DOMAIN=staging.silentqa.com`, тенант `stagetest`. Backend-юнит мигрирует в `ExecStartPre` — сломанная миграция валит СТЕЙДЖИНГ до того, как доедет до прода (это его работа).
- Constraint: прод (`:8007`, Redis `/4`, БД `silentqa`) не трогаем; `.env` этого чекаута (локальный дев на :5434/:6381) не трогаем — стейджинг живёт в отдельном `.env.staging`.

- [ ] **Step 1: .gitignore**

В `.gitignore` после строки `.env` добавить строку `.env.staging`. Commit:

```bash
git add .gitignore && git commit -m "chore: .env.staging в .gitignore (стейджинг-контур)"
```

- [ ] **Step 2: БД и роль**

```bash
PG_PWD=$(openssl rand -hex 16)
sudo -u postgres psql -c "CREATE ROLE silentqa_staging LOGIN PASSWORD '$PG_PWD'" \
                      -c "CREATE DATABASE silentqa_staging OWNER silentqa_staging"
echo "silentqa_staging pwd: $PG_PWD"   # сохранить в .env.staging, больше никуда
```

- [ ] **Step 3: `.env.staging`**

Создать `/root/projects/silentqa-dev/.env.staging` (не в git):

```
DATABASE_URL=postgresql+asyncpg://silentqa_staging:<PG_PWD>@localhost:5432/silentqa_staging
DATABASE_URL_SYNC=postgresql+psycopg2://silentqa_staging:<PG_PWD>@localhost:5432/silentqa_staging
REDIS_URL=redis://localhost:6379/5
AUDIO_STORAGE_PATH=/root/projects/silentqa-dev/data-staging/audio
RESULTS_STORAGE_PATH=/root/projects/silentqa-dev/data-staging/results
COMPANIES_PATH=/root/projects/silentqa-dev/data-staging/companies
SECRET_KEY=<openssl rand -hex 32>
BASE_DOMAIN=staging.silentqa.com
DEFAULT_TENANT=
BROKER_JWT_TENANT_GRACE_UNTIL=
ASR_ENGINE=assemblyai
ASSEMBLYAI_API_KEY=<из прод-.env>
OPENAI_API_KEY=<из прод-.env>
HF_TOKEN=<из прод-.env>
CELERY_CONCURRENCY=1
```

```bash
mkdir -p /root/projects/silentqa-dev/data-staging/{audio,results}
# companies — КОПИЯ: routes/companies.py пишет И УДАЛЯЕТ json по COMPANIES_PATH,
# а companies/ dev-чекаута git-tracked — стейджинг не должен мутировать
# рабочее дерево, из которого коммитим
cp -a /root/projects/silentqa-dev/companies/. /root/projects/silentqa-dev/data-staging/companies/
```

- [ ] **Step 4: venv (если ещё нет)**

Run: `ls /root/projects/silentqa-dev/.venv/bin/python || (cd /root/projects/silentqa-dev && python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt -r worker/requirements.txt)`

- [ ] **Step 5: Миграции вручную ДО юнитов**

```bash
cd /root/projects/silentqa-dev/backend
set -a; source ../.env.staging; set +a
../.venv/bin/python -m app.migrate && ../.venv/bin/python -m app.migrate --check
```
Expected: `[migrate] done`, затем `[migrate] heads OK`.

- [ ] **Step 6: systemd-юниты**

`/etc/systemd/system/silentqa-staging-backend.service`:

```ini
[Unit]
Description=SilentQA STAGING Backend (uvicorn, :8008)
After=network.target postgresql.service redis-server.service
Requires=postgresql.service redis-server.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/projects/silentqa-dev/backend
EnvironmentFile=/root/projects/silentqa-dev/.env.staging
Environment=PATH=/root/projects/silentqa-dev/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=VIRTUAL_ENV=/root/projects/silentqa-dev/.venv
# Стейджинг мигрирует на старте НАМЕРЕННО (fail-fast до прода)
ExecStartPre=/root/projects/silentqa-dev/.venv/bin/python -m app.migrate
ExecStart=/root/projects/silentqa-dev/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8008
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/silentqa-staging-worker.service` — как прод-юнит, но WorkingDirectory/EnvironmentFile/PATH/VIRTUAL_ENV → silentqa-dev, `--concurrency=1`:

```ini
[Unit]
Description=SilentQA STAGING Worker (celery)
After=network.target postgresql.service redis-server.service
Requires=postgresql.service redis-server.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/projects/silentqa-dev/worker
EnvironmentFile=/root/projects/silentqa-dev/.env.staging
Environment=PATH=/root/projects/silentqa-dev/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=VIRTUAL_ENV=/root/projects/silentqa-dev/.venv
ExecStart=/root/projects/silentqa-dev/.venv/bin/celery -A tasks.celery_app worker -B --loglevel=info --concurrency=1 --max-tasks-per-child=10 -Q default,transcription
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now silentqa-staging-backend silentqa-staging-worker
journalctl -u silentqa-staging-backend -n 20   # ExecStartPre migrate → uvicorn :8008
curl -s localhost:8008/health                   # {"status":"ok"}
```

- [ ] **Step 7: Тенант + смоук**

```bash
cd /root/projects/silentqa-dev/backend
set -a; source ../.env.staging; set +a
../.venv/bin/python -m app.provision_tenant stagetest --name "Staging Test" \
    --admin-email alex.chechetov@gmail.com
# API-ключ печатается ОДИН раз — сохранить
curl -s -H "Host: staging.silentqa.com" localhost:8008/            # admin.html (платформа)
curl -s -H "Host: stagetest.staging.silentqa.com" localhost:8008/  # index.html (тенант)
curl -s -H "Host: ghost.staging.silentqa.com" localhost:8008/api/sessions -o /dev/null -w "%{http_code}"  # 404
```

- [ ] **Step 8: Прод не задет — проверка**

Run: `curl -s localhost:8007/health && systemctl is-active silentqa-backend silentqa-worker`
Expected: `{"status":"ok"}` + `active active`.

---

### Task 8: Caddy для стейджинга (публичный доступ) + актуализация CLAUDE.md

**Files:**
- Modify (на боксе): `/etc/caddy/Caddyfile`
- Modify: `CLAUDE.md` (2 фрагмента)

**Interfaces:**
- Produces: `https://staging.silentqa.com` (платформенный контур стейджинга) и `https://staging-t.silentqa.com` (тенант stagetest через `custom_domains` — одноуровневый поддомен, покрыт существующей wildcard-DNS `*.silentqa.com` и relay-passthrough). Без on_demand — явные хосты, обычный ACME.
- Constraint: глобальный `on_demand_tls ask` (→ :8007) не трогаем.

- [ ] **Step 1: custom_domain для тенанта**

```bash
sudo -u postgres psql silentqa_staging -c \
  "UPDATE shared.tenants SET custom_domains = ARRAY['staging-t.silentqa.com'] WHERE slug='stagetest'"
```
(Кэш реестра — TTL 30с, ждать или рестартнуть staging-backend.)

- [ ] **Step 2: Caddy-блок**

В `/etc/caddy/Caddyfile` добавить сайт:

```
staging.silentqa.com, staging-t.silentqa.com {
    encode gzip zstd
    reverse_proxy localhost:8008
}
```

```bash
caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy
curl -s https://staging.silentqa.com/health     # {"status":"ok"} (через relay)
```

Expected: серты выпустились (хосты покрыты `A *.silentqa.com` → relay 89.207.255.231 → passthrough на бокс).

- [ ] **Step 3: CLAUDE.md — два устаревших места**

1. В секции «Gotchas & known drift» УДАЛИТЬ пункт «**`openai` is missing from `worker/requirements.txt`.** …» целиком (он неверен: `openai>=2.26.0` в worker/requirements.txt есть с явным комментарием).
2. Абзац «Backend startup runs `python -m app.migrate` in its lifespan (`backend/app/main.py:21-34`), so **a reachable Postgres is required to start the server** even though tests don't need one.» заменить на:

```markdown
Backend startup no longer migrates: the lifespan only runs a read-only
`python -m app.migrate --check` head-comparison and logs CRITICAL on drift
(it does not crash). Migrations are a deploy step: `run.sh` (local),
`scripts/deploy_prod.sh` (prod), staging unit's `ExecStartPre`.
```

3. В секции «Commands» после блока миграций добавить строку:

```markdown
python -m app.migrate --check   # read-only: какие схемы отстают от head (exit 1 при дрейфе)
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): миграции — деплой-шаг; убрать устаревшую готчу про openai в worker"
```

---

## Порядок и зависимости

1 (CORS) — независим. 2 → 3 (lifespan импортирует `check`). 4, 5 — сразу за 3 и ДО первого прод-деплоя этих коммитов. 6 (CI) — в любой момент, лучше раньше. 7 → 8 (Caddy после юнитов). Прод-деплой всей пачки — через новый `scripts/deploy_prod.sh` (его первый прогон = приёмка Task 5).

## Verification (итоговая)

- `cd backend && python -m pytest tests/` и `cd worker && python -m pytest` — зелёные локально и в Actions.
- `journalctl -u silentqa-staging-backend` показывает ExecStartPre-миграцию; `curl localhost:8008/health` ok.
- Прод: `scripts/deploy_prod.sh` прошёл, `/health/ready` ok, rollback-SHA напечатан, в логе старта прод-бэкенда НЕТ применения миграций, есть «heads OK»-тишина (CRITICAL отсутствует).
