# silentqa Platform Deploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Поднять отдельный мультитенантный деплой `/root/projects/silentqa` (порт 8007) с автоматическими клиентскими поддоменами `*.silentqa.com` через KZ-relay и on-demand TLS; создать клиентов `fulldent` и `shuravin`.

**Architecture:** Спека `docs/superpowers/specs/2026-06-12-silentqa-platform-deploy-design.md`. Один новый код-компонент — `GET /api/tenancy/domain-check` (гейт для Caddy `on_demand_tls ask`), остальное — ops: клон ветки, БД/env/юниты, Caddy-блок, DNS (Cloudflare, вручную владельцем), провижининг.

**Tech Stack:** FastAPI (ветка multi-tenant-core-phase1), Caddy 2.6.2 on-demand TLS, systemd, Postgres 5432, Redis 6379/4, Cloudflare DNS.

**Инварианты:** живые деплои realestate/meet/dental/fillers НЕ трогаем; Caddy менять только через `caddy validate` → `reload` (не restart); пароли/ключи генерим `openssl rand`, API-ключ тенанта печатается ОДИН раз — сохранить для передачи; кеш моделей общий через /root/.cache (в юнитах HOME не переопределять).

---

### Task 1: `GET /api/tenancy/domain-check` (код, TDD — в worktree /root/projects/meet-mt)

**Files:**
- Create: `backend/app/routes/tenancy_check.py`
- Modify: `backend/app/main.py` (import + include_router)
- Test: `backend/tests/test_domain_check.py`

- [x] **Step 1: Failing-тест** `backend/tests/test_domain_check.py`:

```python
"""domain-check: гейт для Caddy on_demand_tls ask (спека Plan 3a §3)."""
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.routes import tenancy_check


class StubRegistry:
    def __init__(self):
        self.rows = [
            {"slug": "fulldent", "schema_name": "t_fulldent", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True},
            {"slug": "legacy", "schema_name": "t_legacy", "status": "active",
             "custom_domains": ["old.example.com"], "api_key_hash": None,
             "api_key_required": True},
            {"slug": "frozen", "schema_name": "t_frozen", "status": "suspended",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True},
        ]

    async def all_tenants(self):
        return self.rows


def make_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(tenancy_check, "_registry", StubRegistry())
    app = FastAPI()
    app.include_router(tenancy_check.router)
    return TestClient(app)


def test_apex_and_admin_allowed(monkeypatch):
    c = make_client(monkeypatch)
    assert c.get("/api/tenancy/domain-check?domain=silentqa.com").status_code == 200
    assert c.get("/api/tenancy/domain-check?domain=admin.silentqa.com").status_code == 200


def test_active_tenant_subdomain_allowed(monkeypatch):
    c = make_client(monkeypatch)
    assert c.get("/api/tenancy/domain-check?domain=fulldent.silentqa.com").status_code == 200
    # регистр и трейлинг-точка нормализуются
    assert c.get("/api/tenancy/domain-check?domain=FullDent.SilentQA.com.").status_code == 200


def test_custom_domain_allowed(monkeypatch):
    c = make_client(monkeypatch)
    assert c.get("/api/tenancy/domain-check?domain=old.example.com").status_code == 200


def test_rejections(monkeypatch):
    c = make_client(monkeypatch)
    for bad in ("ghost.silentqa.com",          # незарегистрирован
                "frozen.silentqa.com",          # suspended
                "a.b.silentqa.com",             # мульти-уровневый
                "silentqa.com.evil.com",        # суффикс-спуфинг
                ""):                            # пусто
        r = c.get("/api/tenancy/domain-check", params={"domain": bad})
        assert r.status_code == 404, bad
```

- [x] **Step 2: Прогнать — падает** (`ModuleNotFoundError: app.routes.tenancy_check`)

Run: `cd /root/projects/meet-mt/backend && ../.venv/bin/python -m pytest tests/test_domain_check.py -v`

- [x] **Step 3: Реализация** `backend/app/routes/tenancy_check.py`:

```python
"""GET /api/tenancy/domain-check — гейт для Caddy on_demand_tls ask.

Caddy перед выпуском сертификата спрашивает: 200 = хост наш (apex, admin,
поддомен активного тенанта или его custom_domain), 404 = серт не минтить.
Открытый эндпоинт без тенант-контекста (Caddy ходит на localhost); утечки
нет — список хостов и так публичен через DNS/CT-логи.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.tenancy_http import TenantRegistry

router = APIRouter(prefix="/api/tenancy", tags=["tenancy"])

_registry = TenantRegistry()


@router.get("/domain-check")
async def domain_check(domain: str = ""):
    host = (domain or "").strip().lower().rstrip(".")
    base = settings.BASE_DOMAIN.lower()
    if not host:
        raise HTTPException(status_code=404, detail="unknown domain")
    if host in (base, f"admin.{base}"):
        return {"ok": True}
    for r in await _registry.all_tenants():
        if r["status"] != "active":
            continue
        if host == f"{r['slug']}.{base}" or host in r["custom_domains"]:
            return {"ok": True}
    raise HTTPException(status_code=404, detail="unknown domain")
```

В `backend/app/main.py`: добавить `tenancy_check` в импорт роутов и `app.include_router(tenancy_check.router)` после `user_auth.router`.

- [x] **Step 4: Прогнать — зелёный + оба полных сьюта**

- [x] **Step 5: Commit**

```bash
cd /root/projects/meet-mt && git add backend/app/routes/tenancy_check.py backend/app/main.py backend/tests/test_domain_check.py
git commit -m "feat(tenancy): domain-check endpoint for caddy on-demand tls ask"
```

---

### Task 2: Клон деплоя + venv

- [x] `git clone /root/projects/meet /root/projects/silentqa && cd /root/projects/silentqa && git fetch origin && git checkout multi-tenant-core-phase1` (ветка в клоне видна, т.к. clone локального репо несёт все ветки; worktree-коммиты в общем .git)
- [x] `python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -r backend/requirements.txt -r worker/requirements.txt` (долго: torch; кеш pip ускорит)
- [x] Смоук: `cd worker && ../.venv/bin/python -c "import tasks.celery_app; print('ok')"` (с env из шага Task 3)

### Task 3: БД + .env

- [x] Роль+БД: `sudo -u postgres psql -c "CREATE ROLE silentqa LOGIN PASSWORD '<PG_PWD>'" -c "CREATE DATABASE silentqa OWNER silentqa"` (PG_PWD = `openssl rand -hex 16`)
- [x] `.env` на основе `/root/projects/meet/.env`: заменить DATABASE_URL/DATABASE_URL_SYNC (user silentqa, db silentqa, host localhost:5432), `REDIS_URL=redis://localhost:6379/4`, новый `SECRET_KEY` (openssl rand -hex 32), `AUTH_USERNAME=admin`/новый `AUTH_PASSWORD` (interim-гейт /api/companies), удалить DELETE_PASSWORD, `BASE_DOMAIN=silentqa.com`, `DEFAULT_TENANT=`, `BROKER_JWT_TENANT_GRACE_UNTIL=` (пусто — легаси-JWT нет), пути хранилища абсолютные `/root/projects/silentqa/data/{audio,results}`, COMPANIES_PATH=/root/projects/silentqa/companies; API-ключи (ASSEMBLYAI/OPENAI/HF_TOKEN) — унаследовать из meet/.env. `mkdir -p data/audio data/results`
- [x] Проверка миграций вручную ДО юнитов: `cd backend && set -a; source ../.env; set +a; ../.venv/bin/python -m app.migrate` → "[migrate] done"; `psql ... -c "\dt shared.*"` → tenants, platform_admins, alembic_version

### Task 4: systemd-юниты

- [x] `/etc/systemd/system/silentqa-backend.service` и `silentqa-worker.service` по образцу meet-* (WorkingDirectory/EnvironmentFile/PATH/VIRTUAL_ENV → /root/projects/silentqa; uvicorn `--port 8007`; worker: `celery -A tasks.celery_app worker -B --loglevel=info --concurrency=2 --max-tasks-per-child=10 -Q default,transcription`); HOME не переопределять (общий кеш моделей)
- [x] `systemctl daemon-reload && systemctl enable --now silentqa-backend silentqa-worker`; `journalctl -u silentqa-backend -n 20` → migrate done + uvicorn on 8007; `curl -s localhost:8007/health` → ok

### Task 5: Caddy

- [x] Глобальные опции (head Caddyfile — проверить, есть ли блок `{...}` до первого сайта; добавить):
```
on_demand_tls {
    ask http://localhost:8007/api/tenancy/domain-check
}
```
- [x] Сайт-блок:
```
silentqa.com, *.silentqa.com {
    encode gzip zstd
    tls {
        on_demand
    }
    reverse_proxy localhost:8007
}
```
- [x] `caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy`; негатив-проверка: `curl -s "localhost:8007/api/tenancy/domain-check?domain=ghost.silentqa.com"` → 404, `?domain=silentqa.com` → 200

### Task 6: DNS (зависимость от владельца)

- [ ] В Cloudflare-зоне silentqa.com (вручную или по токену): `A *.silentqa.com → 89.207.255.231` и `A silentqa.com → 89.207.255.231`, ОБЕ записи DNS-only (серая тучка!). Проверка: `dig +short fulldent.silentqa.com` → 89.207.255.231

### Task 7: Провижининг fulldent + shuravin + smoke

- [x] `cd /root/projects/silentqa/backend && set -a; source ../.env; set +a; ../.venv/bin/python -m app.provision_tenant fulldent --name "FullDent" --admin-email alex.chechetov@gmail.com --admin-password <gen1>` (gen = openssl rand -base64 18); записать API-ключ. Аналогично `shuravin --name "Shuravin"`.
- [x] Smoke до DNS (локально, Host-заголовком): login admin'ом fulldent → 200+cookie; `POST /api/sessions` без ключа → 401, с ключом fulldent → 201, с ключом shuravin на хосте fulldent → 403; сессии fulldent не видны из shuravin (GET /api/sessions под cookie каждого).
- [x] Smoke после DNS: `https://fulldent.silentqa.com` в браузере — серт выпустился, SPA-логин живой; `https://ghost.silentqa.com` — TLS-ошибка (ask отбил).

### Task 8: Доки + чекбоксы

- [x] Дописать в ранбук секцию "silentqa platform deploy — выполнено" с фактическими портами/именами; отметить чекбоксы этого плана; commit в meet-mt; обновить память проекта.
