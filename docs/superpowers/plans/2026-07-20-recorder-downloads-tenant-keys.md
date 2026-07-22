# Рекордер в браузере, /download за логином, пер-тенантные API-ключи — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть скачивание приложений за логином, добавить страницу браузерной записи `/record` (микрофон + звук вкладки), дать каждому тенанту собственные ключи OpenAI/AssemblyAI/ElevenLabs с фоллбеком на глобальные, оформить mac-загрузки и починить `www.silentqa.com`.

**Architecture:** Явные FastAPI-роуты перекрывают статик-маунт для `/download/*` и `/record` (файл рекордера живёт в `backend/private/`, вне статики). Ключи хранятся Fernet-шифротекстом в новой JSONB-колонке `shared.tenants.api_credentials`; общий крипто-модуль `tenancy/secrets.py`, воркер резолвит через contextvar+`shared_connect` с TTL-кэшем, бэкенд — через `request.state.tenant`. Спека: `docs/superpowers/specs/2026-07-20-recorder-downloads-tenant-keys-design.md`.

**Tech Stack:** FastAPI/Starlette, cryptography (Fernet), Alembic (shared-дерево), vanilla JS (без сборки), pytest + TestClient + FakeRedis (без Postgres/Redis).

## Global Constraints

- Ветка `multi-tenant-core-phase1`; этот чекаут (`/root/projects/silentqa`) — **прод**: код подхватывается только рестартом сервисов, но НЕ трогай `.env`, `data/`, systemd вне задачи 13.
- Комментарии, docstrings, UI-строки — по-русски (как в существующем коде).
- Тесты гоняются из каталога сервиса: `cd backend && python -m pytest tests/ -q`, `cd worker && python -m pytest -q`. Postgres/Redis НЕ нужны.
- Полный API-ключ никогда не попадает в логи, ответы API, отчёты. Наружу — только `{"set": bool, "last4": str|null}`.
- Белый список провайдеров: `openai`, `assemblyai`, `elevenlabs` (env: `OPENAI_API_KEY`, `ASSEMBLYAI_API_KEY`, `ELEVENLABS_API_KEY`). `HF_TOKEN` не трогаем.
- Доступ к БД — только через `tenancy/` (никаких raw `psycopg2.connect` — ловится `worker/tests/test_no_raw_connects.py`).
- Пользовательский текст никогда не интерполируется в inline-`onclick` — только `data-*`-атрибуты + addEventListener.
- Коммит после каждой задачи; сообщения в стиле репо: `feat(worker): …`, `fix(front): …`, по-русски.

---

### Task 1: `tenancy/secrets.py` — Fernet-шифрование пер-тенантных секретов

**Files:**
- Create: `tenancy/secrets.py`
- Modify: `worker/requirements.txt`, `backend/requirements.txt` (добавить `cryptography>=42`)
- Test: `worker/tests/test_tenancy_secrets.py`

**Interfaces:**
- Produces: `encrypt_credential(value: str) -> str` (Fernet-токен), `decrypt_credential(token: str) -> str | None` (`None` при битом токене, без исключения). Ключ детерминирован из env `SECRET_KEY`.

- [ ] **Step 1: Write the failing test**

```python
# worker/tests/test_tenancy_secrets.py
"""Fernet-шифрование пер-тенантных ключей (tenancy/secrets.py)."""
import pytest


@pytest.fixture(autouse=True)
def secret_env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret-key")


def test_roundtrip():
    from tenancy.secrets import decrypt_credential, encrypt_credential
    token = encrypt_credential("sk-proj-abc123")
    assert token != "sk-proj-abc123"
    assert decrypt_credential(token) == "sk-proj-abc123"


def test_decrypt_garbage_returns_none():
    from tenancy.secrets import decrypt_credential
    assert decrypt_credential("not-a-fernet-token") is None
    assert decrypt_credential("") is None


def test_key_depends_on_secret(monkeypatch):
    from tenancy.secrets import decrypt_credential, encrypt_credential
    token = encrypt_credential("sk-x")
    monkeypatch.setenv("SECRET_KEY", "another-secret")
    # чужой SECRET_KEY не расшифрует токен
    assert decrypt_credential(token) is None


def test_missing_secret_fails_loud(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    from tenancy.secrets import encrypt_credential
    with pytest.raises(RuntimeError):
        encrypt_credential("sk-x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && python -m pytest tests/test_tenancy_secrets.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tenancy.secrets'`

- [ ] **Step 3: Write the implementation**

```python
# tenancy/secrets.py
"""Шифрование пер-тенантных секретов (shared.tenants.api_credentials).

Fernet-ключ детерминированно выводится из env SECRET_KEY — оба сервиса
(backend и worker) грузят один .env, отдельный key-management не нужен.
Ротация SECRET_KEY инвалидирует все сохранённые ключи (decrypt → None →
фоллбек на глобальные env-ключи) — деградация мягкая, не падение.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def _fernet() -> Fernet:
    secret = os.getenv("SECRET_KEY", "")
    if not secret or secret == "change-this-in-production":
        raise RuntimeError("SECRET_KEY обязателен для шифрования тенантных ключей")
    digest = hashlib.sha256(f"{secret}:api-credentials".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_credential(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_credential(token: str) -> str | None:
    """None при битом/чужом токене — вызывающий уходит в env-фоллбек."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        logger.warning("api_credentials: невалидный шифротекст — фоллбек на глобальный ключ")
        return None
```

В `worker/requirements.txt` и `backend/requirements.txt` добавить строку (в worker её нет вовсе; в backend cryptography сейчас транзитивная через python-jose — прямая зависимость должна быть явной):

```
cryptography>=42
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd worker && python -m pytest tests/test_tenancy_secrets.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add tenancy/secrets.py worker/tests/test_tenancy_secrets.py worker/requirements.txt backend/requirements.txt
git commit -m "feat(tenancy): secrets.py — Fernet-шифрование пер-тенантных API-ключей (спека C)"
```

---

### Task 2: shared-миграция `s005` — колонка `api_credentials`

**Files:**
- Create: `backend/alembic_shared/versions/s005_tenant_api_credentials.py`
- Test: `backend/tests/test_migration_s005_api_credentials.py`

**Interfaces:**
- Produces: колонка `shared.tenants.api_credentials jsonb NOT NULL DEFAULT '{}'` — читается задачами 3/5, пишется задачами 6/7.

- [ ] **Step 1: Write the failing test** (образец — `test_migration_s004_modules.py`)

```python
# backend/tests/test_migration_s005_api_credentials.py
import importlib.util
from pathlib import Path

MIG = Path(__file__).resolve().parents[1] / "alembic_shared" / "versions" / "s005_tenant_api_credentials.py"


def _load():
    spec = importlib.util.spec_from_file_location("s005", MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _calls(m):
    import unittest.mock as mock
    with mock.patch.object(m.op, "execute") as ex:
        m.upgrade()
    return ex.call_args_list


def test_revision_chain():
    m = _load()
    assert m.revision == "s005"
    assert m.down_revision == "s004"


def test_upgrade_adds_jsonb_column():
    m = _load()
    sql = "\n".join(c.args[0] for c in _calls(m))
    assert "ADD COLUMN IF NOT EXISTS api_credentials jsonb" in sql
    assert "DEFAULT '{}'::jsonb" in sql
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_migration_s005_api_credentials.py -q`
Expected: FAIL — файл миграции не существует

- [ ] **Step 3: Write the migration**

```python
# backend/alembic_shared/versions/s005_tenant_api_credentials.py
"""shared.tenants.api_credentials jsonb — пер-тенантные ключи провайдеров (Fernet-шифротекст).

Revision ID: s005
Revises: s004
"""
from typing import Sequence, Union

from alembic import op

revision: str = "s005"
down_revision: Union[str, None] = "s004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE shared.tenants "
        "ADD COLUMN IF NOT EXISTS api_credentials jsonb NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE shared.tenants DROP COLUMN IF EXISTS api_credentials")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_migration_s005_api_credentials.py tests/test_migrate_check.py -q`
Expected: passed (migrate_check-тесты не должны сломаться от нового файла)

- [ ] **Step 5: Commit**

```bash
git add backend/alembic_shared/versions/s005_tenant_api_credentials.py backend/tests/test_migration_s005_api_credentials.py
git commit -m "feat(db): s005 — shared.tenants.api_credentials (пер-тенантные ключи, спека C)"
```

---

### Task 3: воркер-резолвер `tasks/tenant_keys.py`

**Files:**
- Create: `worker/tasks/tenant_keys.py`
- Test: `worker/tests/test_tenant_keys.py`

**Interfaces:**
- Consumes: `tenancy.secrets.decrypt_credential` (Task 1), `tenancy.db.shared_connect`, `tenancy.context.get_tenant_slug`.
- Produces: `api_key(provider: str) -> str | None` — тенантный ключ или env-фоллбек; `ENV_NAMES: dict`; module-level `_cache` (тесты чистят). Task 4 импортирует как `from tasks import tenant_keys`.

- [ ] **Step 1: Write the failing test** (образец фейкового соединения — `test_company_from_tenant.py`)

```python
# worker/tests/test_tenant_keys.py
"""Резолвер пер-тенантных ключей: тенантный → env-фоллбек (спека C)."""
from unittest import mock

import pytest

from tenancy.context import reset_tenant_schema, set_tenant_schema


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-global")
    import tasks.tenant_keys as tk
    tk._cache.clear()
    yield
    tk._cache.clear()


def _fake_conn(creds):
    conn = mock.MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (creds,)
    return conn


def _in_tenant(fn):
    token = set_tenant_schema("t_acme")
    try:
        return fn()
    finally:
        reset_tenant_schema(token)


def test_tenant_key_wins(monkeypatch):
    import tasks.tenant_keys as tk
    from tenancy.secrets import encrypt_credential
    creds = {"openai": encrypt_credential("sk-tenant")}
    monkeypatch.setattr(tk, "shared_connect", lambda: _fake_conn(creds))
    assert _in_tenant(lambda: tk.api_key("openai")) == "sk-tenant"


def test_env_fallback_when_not_set(monkeypatch):
    import tasks.tenant_keys as tk
    monkeypatch.setattr(tk, "shared_connect", lambda: _fake_conn({}))
    assert _in_tenant(lambda: tk.api_key("openai")) == "sk-global"


def test_env_fallback_on_broken_ciphertext(monkeypatch):
    import tasks.tenant_keys as tk
    monkeypatch.setattr(tk, "shared_connect", lambda: _fake_conn({"openai": "garbage"}))
    assert _in_tenant(lambda: tk.api_key("openai")) == "sk-global"


def test_no_tenant_context_uses_env(monkeypatch):
    import tasks.tenant_keys as tk
    monkeypatch.setattr(tk, "shared_connect", mock.MagicMock(side_effect=AssertionError("не должен ходить в БД")))
    assert tk.api_key("openai") == "sk-global"


def test_cache_hits_within_ttl(monkeypatch):
    import tasks.tenant_keys as tk
    monkeypatch.setattr(tk, "shared_connect", lambda: _fake_conn({}))
    _in_tenant(lambda: tk.api_key("openai"))
    monkeypatch.setattr(tk, "shared_connect", mock.MagicMock(side_effect=AssertionError("кэш должен был сработать")))
    assert _in_tenant(lambda: tk.api_key("openai")) == "sk-global"


def test_unknown_provider_raises():
    import tasks.tenant_keys as tk
    with pytest.raises(KeyError):
        tk.api_key("anthropic")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && python -m pytest tests/test_tenant_keys.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tasks.tenant_keys'`

- [ ] **Step 3: Write the implementation**

```python
# worker/tasks/tenant_keys.py
"""Пер-тенантные API-ключи провайдеров с фоллбеком на глобальные env (спека C).

В отличие от company_config_id (кэш на процесс), ключи меняются на лету из
админки — поэтому TTL-кэш ~60 с, как у HTTP-реестра.
"""
from __future__ import annotations

import logging
import os
import time

from tenancy.context import get_tenant_slug
from tenancy.db import shared_connect
from tenancy.secrets import decrypt_credential

logger = logging.getLogger(__name__)

ENV_NAMES = {
    "openai": "OPENAI_API_KEY",
    "assemblyai": "ASSEMBLYAI_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
}

_TTL_SECONDS = 60.0
_cache: dict[str, tuple[float, dict]] = {}  # slug -> (loaded_at, api_credentials)


def _tenant_credentials(slug: str) -> dict:
    now = time.monotonic()
    hit = _cache.get(slug)
    if hit and now - hit[0] < _TTL_SECONDS:
        return hit[1]
    conn = shared_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT api_credentials FROM shared.tenants WHERE slug = %s", (slug,)
            )
            row = cur.fetchone()
    finally:
        conn.close()
    creds = dict(row[0] or {}) if row else {}
    _cache[slug] = (now, creds)
    return creds


def api_key(provider: str) -> str | None:
    """Ключ провайдера: тенантный (расшифрованный) → иначе глобальный env.

    Неизвестный провайдер — KeyError (fail-fast: опечатка в коде, не данные).
    """
    env_name = ENV_NAMES[provider]
    slug = get_tenant_slug()
    if slug:
        token = _tenant_credentials(slug).get(provider)
        if token:
            value = decrypt_credential(token)
            if value:
                return value
    return os.getenv(env_name) or None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd worker && python -m pytest tests/test_tenant_keys.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/tenant_keys.py worker/tests/test_tenant_keys.py
git commit -m "feat(worker): tenant_keys — резолвер пер-тенантных ключей с env-фоллбеком (спека C)"
```

---

### Task 4: вайринг резолвера во все LLM/ASR-вызовы воркера

**Files:**
- Modify: `worker/tasks/transcribe.py:88,151`, `worker/tasks/quality.py:659,1081,1107,1128,1177`, `worker/tasks/coaching.py:216`, `worker/tasks/card.py:57`, `worker/tasks/extract.py:83`
- Test: `worker/tests/test_tenant_keys_wiring.py`

**Interfaces:**
- Consumes: `tasks.tenant_keys.api_key(provider)` (Task 3).
- Produces: все 7 вызовов провайдеров идут через резолвер; `_assess_with_structured_output` получает новый kwarg `api_key=None` и пробрасывает его в `structured_completion`.

- [ ] **Step 1: Write the failing wiring test**

```python
# worker/tests/test_tenant_keys_wiring.py
"""LLM/ASR-вызовы берут ключ через tasks.tenant_keys, а не напрямую из env."""
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)


def test_coaching_uses_resolver(monkeypatch):
    import tasks.coaching as coaching
    monkeypatch.setattr(coaching.tenant_keys, "api_key", lambda p: "sk-tenant")
    captured = {}

    def fake_sc(**kwargs):
        captured.update(kwargs)
        return {"summary": "ok"}

    monkeypatch.setattr(coaching, "structured_completion", fake_sc)
    out = coaching.generate_coaching(
        [{"speaker": "A", "text": "Привет, это тест"}], {"speaker_roles": {}})
    assert out == {"summary": "ok"}
    assert captured["api_key"] == "sk-tenant"


def test_coaching_skips_without_any_key(monkeypatch):
    import tasks.coaching as coaching
    monkeypatch.setattr(coaching.tenant_keys, "api_key", lambda p: None)
    assert coaching.generate_coaching(
        [{"speaker": "A", "text": "Привет, это тест"}], {}) is None


def test_assemblyai_fails_loud_without_any_key(monkeypatch):
    import tasks.transcribe as tr
    monkeypatch.setattr(tr.tenant_keys, "api_key", lambda p: None)
    with pytest.raises(RuntimeError, match="ASSEMBLYAI_API_KEY"):
        tr._transcribe_assemblyai("/tmp/x.wav")


def test_quality_assess_threads_api_key(monkeypatch):
    import tasks.quality as q
    monkeypatch.setattr(q.tenant_keys, "api_key", lambda p: "sk-tenant")
    captured = {}

    def fake_sc(**kwargs):
        captured.update(kwargs)
        return {"overall_score": 5}

    monkeypatch.setattr(q, "structured_completion", fake_sc)
    q._assess_with_structured_output("текст", "{}", None, "", api_key="sk-tenant")
    assert captured["api_key"] == "sk-tenant"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && python -m pytest tests/test_tenant_keys_wiring.py -q`
Expected: FAIL — `AttributeError: module 'tasks.coaching' has no attribute 'tenant_keys'`

- [ ] **Step 3: Wire the resolver**

В каждый из пяти модулей добавить импорт (рядом с существующими `from tasks...`/`from .`-импортами; в этих модулях принят стиль `from tasks.X import Y` или относительный — смотри соседние строки):

```python
from tasks import tenant_keys
```

Замены (`os.getenv(...)` → резолвер):

```python
# transcribe.py:88 (_transcribe_assemblyai)
    api_key = tenant_keys.api_key("assemblyai")
    if not api_key:
        raise RuntimeError("ASSEMBLYAI_API_KEY not set")

# transcribe.py:151 (_transcribe_elevenlabs)
    api_key = tenant_keys.api_key("elevenlabs")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY not set")

# quality.py:659 (план — api_key уже пробрасывается в structured_completion:690)
    api_key = tenant_keys.api_key("openai")

# quality.py:1081 (оценка)
    api_key = tenant_keys.api_key("openai")

# coaching.py:216
    api_key = tenant_keys.api_key("openai")
```

Пробросить ключ в основную оценку (сейчас `structured_completion` на строке 1177 создаёт клиент из env — тенантный ключ туда не доходит):

```python
# quality.py: вызов _assess_with_structured_output (~строка 1107) — добавить kwarg
        result = _assess_with_structured_output(
            transcript_text, sentiment_json, protocol,
            custom_instructions, criteria_instructions,
            use_extended_schema=use_extended_schema,
            prior_context=prior_context,
            template_driven=template_driven,
            glossary=glossary,
            api_key=api_key,
        )

# quality.py:1128 — сигнатура
def _assess_with_structured_output(
    transcript_text, sentiment_json, protocol, custom_instructions,
    criteria_instructions="", use_extended_schema=False,
    prior_context=None, template_driven=False, glossary=None,
    api_key=None,
):

# quality.py:1177 — в вызов structured_completion добавить
        api_key=api_key,
```

В `card.py:57` и `extract.py:83` (сейчас клиент молча берёт env) — добавить в kwargs вызова `structured_completion`:

```python
        api_key=tenant_keys.api_key("openai"),
```

- [ ] **Step 4: Run the full worker suite**

Run: `cd worker && python -m pytest -q`
Expected: все зелёные (включая test_llm_call_params, test_coaching, test_transcribe_elevenlabs — если какой-то из них мокал `os.getenv`, перевести мок на `tasks.tenant_keys.api_key` по образцу шага 1)

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/ worker/tests/test_tenant_keys_wiring.py
git commit -m "feat(worker): все LLM/ASR-вызовы — через пер-тенантный резолвер ключей (спека C)"
```

---

### Task 5: бэкенд — колонка в реестре, `app/tenant_keys.py`, вайринг eval-rewrite

**Files:**
- Modify: `backend/app/tenancy_http.py:36-53` (SELECT + row dict), `backend/app/eval_prompt_rewrite.py:55-80`, `backend/app/routes/eval_profiles.py:180-190`
- Create: `backend/app/tenant_keys.py`
- Test: `backend/tests/test_tenant_keys_http.py`

**Interfaces:**
- Consumes: `tenancy.secrets` (Task 1).
- Produces: `app.tenant_keys.PROVIDERS = ("openai", "assemblyai", "elevenlabs")`; `tenant_api_key(request, provider) -> str | None`; `masked_credentials(creds: dict) -> dict` (`{prov: {"set": bool, "last4": str|None}}`); `apply_credentials(creds: dict, updates: dict) -> list[str]` (мутирует creds; `""` — удалить, `None` — не трогать, строка — зашифровать; возвращает изменённых провайдеров). Task 6 и 7 используют все три.
- `rewrite_eval_prompt(...)` получает kwarg `api_key: str | None = None`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tenant_keys_http.py
"""HTTP-плоскость пер-тенантных ключей: резолвер, маскирование, apply (спека C)."""
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-global")


def _req(creds):
    return SimpleNamespace(state=SimpleNamespace(tenant={"api_credentials": creds}))


def test_tenant_key_wins():
    from app.tenant_keys import tenant_api_key
    from tenancy.secrets import encrypt_credential
    req = _req({"openai": encrypt_credential("sk-tenant")})
    assert tenant_api_key(req, "openai") == "sk-tenant"


def test_env_fallback_without_tenant_key():
    from app.tenant_keys import tenant_api_key
    assert tenant_api_key(_req({}), "openai") == "sk-global"


def test_env_fallback_on_platform_contour():
    from app.tenant_keys import tenant_api_key
    req = SimpleNamespace(state=SimpleNamespace(tenant=None))
    assert tenant_api_key(req, "openai") == "sk-global"


def test_masked_never_leaks_value():
    from app.tenant_keys import masked_credentials
    from tenancy.secrets import encrypt_credential
    out = masked_credentials({"openai": encrypt_credential("sk-proj-abcd1234")})
    assert out["openai"] == {"set": True, "last4": "1234"}
    assert out["assemblyai"] == {"set": False, "last4": None}
    assert "sk-proj" not in str(out)


def test_apply_set_and_clear():
    from app.tenant_keys import apply_credentials
    from tenancy.secrets import decrypt_credential
    creds = {}
    changed = apply_credentials(creds, {"openai": "sk-new", "assemblyai": None})
    assert changed == ["openai"]
    assert decrypt_credential(creds["openai"]) == "sk-new"
    changed = apply_credentials(creds, {"openai": ""})
    assert changed == ["openai"] and "openai" not in creds


def test_rewrite_eval_prompt_uses_passed_key(monkeypatch):
    import app.eval_prompt_rewrite as epr
    captured = {}

    def fake_sc(**kwargs):
        captured.update(kwargs)
        return {"rewritten_prompt": "p", "rationale": "r", "suggested_criteria": []}

    monkeypatch.setattr(epr, "structured_completion", fake_sc)
    epr.rewrite_eval_prompt(None, None, "строже", api_key="sk-tenant")
    assert captured["api_key"] == "sk-tenant"
```

Примечание: `SECRET_KEY` уже проставлен conftest'ом бэкенда (`unit-test-secret-key`).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_tenant_keys_http.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.tenant_keys'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/tenant_keys.py
"""Пер-тенантные ключи на HTTP-плоскости + общие утилиты masked/apply (спека C)."""
from __future__ import annotations

import os

from tenancy.secrets import decrypt_credential, encrypt_credential

PROVIDERS = ("openai", "assemblyai", "elevenlabs")

ENV_NAMES = {
    "openai": "OPENAI_API_KEY",
    "assemblyai": "ASSEMBLYAI_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
}


def tenant_api_key(request, provider: str) -> str | None:
    """Ключ провайдера: из request.state.tenant → иначе глобальный env."""
    tenant = getattr(request.state, "tenant", None) or {}
    token = (tenant.get("api_credentials") or {}).get(provider)
    if token:
        value = decrypt_credential(token)
        if value:
            return value
    return os.getenv(ENV_NAMES[provider]) or None


def masked_credentials(creds: dict) -> dict:
    """{prov: {set, last4}} — полный ключ наружу не отдаём никогда."""
    out = {}
    for p in PROVIDERS:
        token = creds.get(p)
        value = decrypt_credential(token) if token else None
        out[p] = {"set": bool(value), "last4": value[-4:] if value else None}
    return out


def apply_credentials(creds: dict, updates: dict) -> list[str]:
    """Мутирует creds: "" — удалить, None — не трогать, строка — зашифровать."""
    changed = []
    for p in PROVIDERS:
        v = updates.get(p)
        if v is None:
            continue
        if v == "":
            if creds.pop(p, None) is not None:
                changed.append(p)
        else:
            creds[p] = encrypt_credential(v)
            changed.append(p)
    return changed
```

В `backend/app/tenancy_http.py` добавить колонку в SELECT (строки 36-40) и в словарь строки (42-55):

```python
                    text(
                        "SELECT slug, schema_name, status, custom_domains, "
                        "api_key_hash, api_key_required, modules, display_name, "
                        "company_config_id, api_credentials "
                        "FROM shared.tenants"
                    )
```

```python
                        "company_config_id": r.company_config_id,
                        "api_credentials": dict(r.api_credentials or {}),
```

В `backend/app/eval_prompt_rewrite.py` (строки 57-62):

```python
def rewrite_eval_prompt(current_prompt: str | None,
                        current_criteria: list | None,
                        wishes: str,
                        api_key: str | None = None) -> dict:
    """Вернуть {rewritten_prompt, rationale, suggested_criteria[]}. Бросает при отсутствии ключа."""
    api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
```

(вызов `structured_completion(...)` ниже уже несёт `api_key=api_key` — не трогать).

В `backend/app/routes/eval_profiles.py` (строки ~180-190): убедиться, что у эндпоинта есть параметр `request: Request` (добавить импорт/параметр, если нет), и заменить вызов:

```python
    from app.eval_prompt_rewrite import rewrite_eval_prompt
    from app.tenant_keys import tenant_api_key

    key = tenant_api_key(request, "openai")
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: rewrite_eval_prompt(cur_prompt, cur_criteria, body.wishes, api_key=key))
```

(строку executor'а подогнать под фактическую в файле — меняется только lambda).

- [ ] **Step 4: Run the backend suite**

Run: `cd backend && python -m pytest tests/ -q`
Expected: все зелёные (в т.ч. test_eval_prompt_rewrite.py, test_tenant_middleware.py)

- [ ] **Step 5: Commit**

```bash
git add backend/app/tenant_keys.py backend/app/tenancy_http.py backend/app/eval_prompt_rewrite.py backend/app/routes/eval_profiles.py backend/tests/test_tenant_keys_http.py
git commit -m "feat(api): пер-тенантные ключи на HTTP-плоскости + вайринг eval-rewrite (спека C)"
```

---

### Task 6: платформенный API ключей — `GET/PUT /api/platform/tenants/{slug}/keys`

**Files:**
- Modify: `backend/app/routes/platform_tenants.py` (после `rotate_key`, ~строка 150)
- Test: `backend/tests/test_platform_tenant_keys.py`

**Interfaces:**
- Consumes: `app.tenant_keys.masked_credentials/apply_credentials` (Task 5), `registry.invalidate()` (уже импортирован в модуле).
- Produces: `GET /{slug}/keys` → masked dict; `PUT /{slug}/keys` c телом `{"openai": "sk-..."} | {"openai": ""}` → masked dict; 404 на неизвестный slug; 422 на неизвестный провайдер.

- [ ] **Step 1: Write the failing test** (образец клиента/куки — `test_platform_tenants.py`)

```python
# backend/tests/test_platform_tenant_keys.py
"""Платформенный API пер-тенантных ключей: write-only, маскирование (спека C)."""
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


class FakeDB:
    """Мок get_db: одна строка shared.tenants c api_credentials."""

    def __init__(self, creds):
        self.creds = creds
        self.saved = None

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        fake = self

        class Res:
            def first(self_inner):
                if fake.creds is None:
                    return None
                row = type("Row", (), {"api_credentials": dict(fake.creds)})
                return row()

        if sql.startswith("UPDATE"):
            import json
            fake.saved = json.loads(params["c"])
            fake.creds = fake.saved

            class UpdRes:
                rowcount = 1
            return UpdRes()
        return Res()

    async def commit(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB(creds={})
    from app.database import get_db
    from app.main import app

    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    yield db
    app.dependency_overrides.pop(get_db, None)


def test_keys_require_platform_auth(client, fake_db):
    assert client.get("/api/platform/tenants/acme/keys").status_code == 401


def test_get_masked_empty(client, fake_redis, fake_db):
    r = client.get("/api/platform/tenants/acme/keys", cookies=_admin_cookie(fake_redis))
    assert r.status_code == 200
    assert r.json()["openai"] == {"set": False, "last4": None}


def test_put_stores_ciphertext_and_masks(client, fake_redis, fake_db):
    r = client.put("/api/platform/tenants/acme/keys", cookies=_admin_cookie(fake_redis),
                   json={"openai": "sk-proj-abcd1234"})
    assert r.status_code == 200
    assert r.json()["openai"] == {"set": True, "last4": "1234"}
    assert "sk-proj-abcd1234" not in str(r.json())
    # в БД ушёл шифротекст, не открытый ключ
    from tenancy.secrets import decrypt_credential
    assert fake_db.saved["openai"] != "sk-proj-abcd1234"
    assert decrypt_credential(fake_db.saved["openai"]) == "sk-proj-abcd1234"


def test_put_empty_string_clears(client, fake_redis, fake_db):
    client.put("/api/platform/tenants/acme/keys", cookies=_admin_cookie(fake_redis),
               json={"openai": "sk-x"})
    r = client.put("/api/platform/tenants/acme/keys", cookies=_admin_cookie(fake_redis),
                   json={"openai": ""})
    assert r.json()["openai"]["set"] is False
    assert fake_db.saved == {}


def test_unknown_provider_422(client, fake_redis, fake_db):
    r = client.put("/api/platform/tenants/acme/keys", cookies=_admin_cookie(fake_redis),
                   json={"anthropic": "sk-x"})
    assert r.status_code == 422


def test_unknown_tenant_404(client, fake_redis, fake_db):
    fake_db.creds = None
    r = client.get("/api/platform/tenants/ghost/keys", cookies=_admin_cookie(fake_redis))
    assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_platform_tenant_keys.py -q`
Expected: FAIL — 404/405 (роут не существует)

- [ ] **Step 3: Write the endpoints** (в `platform_tenants.py`, после `rotate_key`; `json` уже импортирован в модуле — если нет, добавить)

```python
# ── Пер-тенантные API-ключи провайдеров (спека C) ─────────────────────────────

from pydantic import ConfigDict

from ..tenant_keys import apply_credentials, masked_credentials


class TenantKeysBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    openai: str | None = None
    assemblyai: str | None = None
    elevenlabs: str | None = None


async def _get_credentials(db: AsyncSession, slug: str) -> dict | None:
    row = (await db.execute(text(
        "SELECT api_credentials FROM shared.tenants WHERE slug = :slug"),
        {"slug": slug})).first()
    return dict(row.api_credentials or {}) if row else None


@router.get("/{slug}/keys")
async def get_tenant_keys(slug: str, db: AsyncSession = Depends(get_db)):
    creds = await _get_credentials(db, slug)
    if creds is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return masked_credentials(creds)


@router.put("/{slug}/keys")
async def put_tenant_keys(slug: str, body: TenantKeysBody,
                          db: AsyncSession = Depends(get_db)):
    creds = await _get_credentials(db, slug)
    if creds is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    changed = apply_credentials(creds, body.model_dump())
    await db.execute(text(
        "UPDATE shared.tenants SET api_credentials = CAST(:c AS jsonb) "
        "WHERE slug = :slug"),
        {"c": json.dumps(creds), "slug": slug})
    await db.commit()
    registry.invalidate()
    # ключи в лог не пишем — только имена провайдеров
    logger.info("tenant %s api_credentials updated: %s", slug, ", ".join(changed) or "—")
    return masked_credentials(creds)
```

Роутер уже висит под `require_platform_admin` (dependencies на APIRouter, строка 31) — отдельная проверка не нужна.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_platform_tenant_keys.py tests/test_platform_tenants.py -q`
Expected: passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/platform_tenants.py backend/tests/test_platform_tenant_keys.py
git commit -m "feat(api): GET/PUT /api/platform/tenants/{slug}/keys — ключи провайдеров (спека C)"
```

---

### Task 7: тенантный API — `GET/PUT /api/integrations/keys`

**Files:**
- Create: `backend/app/routes/integrations.py`
- Modify: `backend/app/main.py:17-21` (импорт), `:96` (include_router)
- Test: `backend/tests/test_integrations_api.py`

**Interfaces:**
- Consumes: `app.tenant_keys` (Task 5), `require_admin`, `require_tenant_slug`, `registry`.
- Produces: `GET/PUT /api/integrations/keys` (та же семантика, что Task 6, но slug — текущий тенант); admin-only (viewer/manager → 403, аноним → 401).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_integrations_api.py
"""Тенантный API ключей интеграций: admin-only, write-only (спека C)."""
import asyncio

import pytest
from starlette.testclient import TestClient

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True,
         "modules": {}, "display_name": "ACME", "company_config_id": None,
         "api_credentials": {}}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com")


def _cookie(fake_redis, role="admin"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def make():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u1", "a@x.io", role)
        finally:
            reset_tenant_schema(token)

    return {auth_sessions.SESSION_COOKIE: asyncio.run(make())}


class FakeDB:
    def __init__(self):
        self.creds = {}
        self.saved = None

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        fake = self
        if sql.startswith("UPDATE"):
            import json
            fake.saved = json.loads(params["c"])
            fake.creds = fake.saved
            return type("R", (), {"rowcount": 1})()

        class Res:
            def first(self_inner):
                return type("Row", (), {"api_credentials": dict(fake.creds)})()
        return Res()

    async def commit(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    from app.database import get_db
    from app.main import app

    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    yield db
    app.dependency_overrides.pop(get_db, None)


def test_requires_auth(client, fake_db):
    assert client.get("/api/integrations/keys").status_code == 401


def test_viewer_forbidden(client, fake_redis, fake_db):
    r = client.get("/api/integrations/keys", cookies=_cookie(fake_redis, role="viewer"))
    assert r.status_code == 403


def test_admin_put_and_masked_get(client, fake_redis, fake_db):
    c = _cookie(fake_redis)
    r = client.put("/api/integrations/keys", cookies=c, json={"elevenlabs": "sk_11abcd"})
    assert r.status_code == 200
    assert r.json()["elevenlabs"] == {"set": True, "last4": "abcd"}
    from tenancy.secrets import decrypt_credential
    assert decrypt_credential(fake_db.saved["elevenlabs"]) == "sk_11abcd"
    r = client.get("/api/integrations/keys", cookies=c)
    assert "sk_11abcd" not in r.text


def test_platform_contour_404(client, fake_redis, fake_db):
    # контур-гейт: тенантный путь на apex — 404 до auth
    r = client.get("/api/integrations/keys", headers={"Host": "silentqa.com"})
    assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_integrations_api.py -q`
Expected: FAIL — 404 (роут не существует)

- [ ] **Step 3: Write the router**

```python
# backend/app/routes/integrations.py
"""Ключи интеграций тенанта (спека C): admin тенанта управляет своими ключами.

Write-only семантика: назад отдаём только {set, last4}; пустая строка в PUT —
удалить ключ (возврат на общий ключ платформы); None — не трогать.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tenancy.context import require_tenant_slug

from ..auth_user import require_admin
from ..database import get_db
from ..tenant_keys import apply_credentials, masked_credentials
from ..tenancy_http import registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations", tags=["integrations"],
                   dependencies=[Depends(require_admin)])


class KeysBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    openai: str | None = None
    assemblyai: str | None = None
    elevenlabs: str | None = None


async def _load(db: AsyncSession) -> dict:
    row = (await db.execute(text(
        "SELECT api_credentials FROM shared.tenants WHERE slug = :slug"),
        {"slug": require_tenant_slug()})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return dict(row.api_credentials or {})


@router.get("/keys")
async def get_keys(db: AsyncSession = Depends(get_db)):
    return masked_credentials(await _load(db))


@router.put("/keys")
async def put_keys(body: KeysBody, db: AsyncSession = Depends(get_db)):
    creds = await _load(db)
    changed = apply_credentials(creds, body.model_dump())
    await db.execute(text(
        "UPDATE shared.tenants SET api_credentials = CAST(:c AS jsonb) "
        "WHERE slug = :slug"),
        {"c": json.dumps(creds), "slug": require_tenant_slug()})
    await db.commit()
    registry.invalidate()
    logger.info("integrations keys updated: %s", ", ".join(changed) or "—")
    return masked_credentials(creds)
```

В `backend/app/main.py`: добавить `integrations` в импорт из `app.routes` (строки 17-21) и `app.include_router(integrations.router)` после `eval_profiles` (строка 96).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_integrations_api.py tests/test_contour_gate.py -q`
Expected: passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/integrations.py backend/app/main.py backend/tests/test_integrations_api.py
git commit -m "feat(api): GET/PUT /api/integrations/keys — свои ключи тенанта (спека C)"
```

---

### Task 8: гейт `/download` — только после входа

**Files:**
- Create: `backend/app/routes/downloads.py`
- Modify: `backend/app/main.py` (импорт + include_router)
- Test: `backend/tests/test_download_gate.py`

**Interfaces:**
- Consumes: `get_current_user` (тенант-контур), `get_current_platform_admin` (платформенный), `get_tenant_slug`.
- Produces: роуты `GET /download` (307 → `/download/`), `GET /download/{path:path}` (гейт: аноним → 302 `/`; авторизованный: `""|index.html` → страница, `dl/<имя-из-каталога>` → файл, иначе 404). Task 9 добавит в этот же модуль `GET /record`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_download_gate.py
"""/download — только после входа (спека A): tenant-юзер или платформенный админ."""
import asyncio

import pytest
from starlette.testclient import TestClient

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True,
         "modules": {}, "display_name": "ACME", "company_config_id": None,
         "api_credentials": {}}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com", follow_redirects=False)


def _cookie(fake_redis, role="viewer"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def make():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u1", "a@x.io", role)
        finally:
            reset_tenant_schema(token)

    return {auth_sessions.SESSION_COOKIE: asyncio.run(make())}


def _admin_cookie(fake_redis):
    from app import platform_sessions as ps
    sid = asyncio.run(ps.create_platform_session("a1", "boss@x.io"))
    return {ps.PLATFORM_COOKIE: sid}


def test_anon_redirected_to_login(client):
    assert client.get("/download/").status_code == 302
    assert client.get("/download/").headers["location"] == "/"
    assert client.get("/download/index.html").status_code == 302
    assert client.get("/download/dl/SilentQA-Recorder-Setup-4.0.0.exe").status_code == 302


def test_download_root_redirects_to_slash(client):
    r = client.get("/download")
    assert r.status_code == 307
    assert r.headers["location"] == "/download/"


def test_any_role_gets_page(client, fake_redis):
    for role in ("viewer", "manager", "admin"):
        r = client.get("/download/", cookies=_cookie(fake_redis, role=role))
        assert r.status_code == 200, role
        assert "Рекордер" in r.text


def test_platform_admin_on_platform_contour(client, fake_redis):
    r = client.get("/download/", cookies=_admin_cookie(fake_redis),
                   headers={"Host": "silentqa.com"})
    assert r.status_code == 200


def test_unknown_artifact_404(client, fake_redis):
    r = client.get("/download/dl/nope.exe", cookies=_cookie(fake_redis))
    assert r.status_code == 404


def test_traversal_404(client, fake_redis):
    # httpx нормализует "/../" до отправки, поэтому проверяем encoded-вариант:
    # path-параметр декодируется в "dl/../../app/config.py" и режется членством в листинге
    r = client.get("/download/dl/..%2f..%2fapp%2fconfig.py", cookies=_cookie(fake_redis))
    assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_download_gate.py -q`
Expected: FAIL — аноним получает 200/307 от StaticFiles вместо 302

- [ ] **Step 3: Write the router**

```python
# backend/app/routes/downloads.py
"""Гейт загрузок (спека A): /download/* — только после входа.

Файлы физически остаются в static/download/ (туда их кладёт сборка),
но эти роуты объявлены ДО статик-маунта и перекрывают весь префикс —
включая /download/index.html, который иначе отдал бы StaticFiles.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from tenancy.context import get_tenant_slug

from ..auth_platform import get_current_platform_admin
from ..auth_user import get_current_user

router = APIRouter(tags=["downloads"])

_BACKEND_DIR = Path(__file__).resolve().parents[2]
DOWNLOAD_DIR = _BACKEND_DIR / "static" / "download"


async def _authorized(request: Request) -> bool:
    """Тенант-контур — кука sqa_session (любая роль); платформа — sqa_admin."""
    if get_tenant_slug() is None:
        return await get_current_platform_admin(request) is not None
    return await get_current_user(request) is not None


@router.get("/download", include_in_schema=False)
async def download_root():
    return RedirectResponse("/download/", status_code=307)


@router.get("/download/{path:path}", include_in_schema=False)
async def download_gate(path: str, request: Request):
    if not await _authorized(request):
        return RedirectResponse("/", status_code=302)
    if path in ("", "index.html"):
        return FileResponse(DOWNLOAD_DIR / "index.html")
    if path.startswith("dl/"):
        name = path[3:]
        dl_dir = DOWNLOAD_DIR / "dl"
        # членство в фактическом листинге каталога — никакой интерполяции пути
        names = {p.name for p in dl_dir.iterdir() if p.is_file()} if dl_dir.is_dir() else set()
        if name in names:
            return FileResponse(dl_dir / name)
    raise HTTPException(status_code=404, detail="Not found")
```

В `backend/app/main.py`: добавить `downloads` в импорт из `app.routes` и `app.include_router(downloads.router)` рядом с остальными (до статик-маунта).

Примечание для теста артефактов: в юнит-окружении `static/download/dl/` может быть пуст (артефакты не коммитятся) — `test_unknown_artifact_404` от этого не зависит; тест успешной отдачи файла не пишем (файл 75МБ), отдачу проверит задача 13 на проде.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_download_gate.py tests/test_root_dispatch.py -q`
Expected: passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/downloads.py backend/app/main.py backend/tests/test_download_gate.py
git commit -m "feat(api): /download за логином — гейт поверх статики (спека A)"
```

---

### Task 9: страница записи `/record` (микрофон + звук вкладки)

**Files:**
- Create: `backend/private/record.html`
- Modify: `backend/app/routes/downloads.py` (роут `/record`)
- Test: `backend/tests/test_record_page.py`

**Interfaces:**
- Consumes: гейт-хелпер `_authorized` не используется — у `/record` своя логика (платформенный контур → 404); `get_current_user`.
- Produces: `GET /record` — страница рекордера для залогиненных пользователей тенанта.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_record_page.py
"""/record — браузерный рекордер, только тенант-контур и только после входа (спека B)."""
import asyncio

import pytest
from starlette.testclient import TestClient

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True,
         "modules": {}, "display_name": "ACME", "company_config_id": None,
         "api_credentials": {}}]


@pytest.fixture
def client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self):
        return ROWS

    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://acme.silentqa.com", follow_redirects=False)


def _cookie(fake_redis, role="manager"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def make():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u1", "a@x.io", role)
        finally:
            reset_tenant_schema(token)

    return {auth_sessions.SESSION_COOKIE: asyncio.run(make())}


def test_anon_redirected(client):
    r = client.get("/record")
    assert r.status_code == 302 and r.headers["location"] == "/"


def test_user_gets_recorder(client, fake_redis):
    r = client.get("/record", cookies=_cookie(fake_redis))
    assert r.status_code == 200
    assert "MediaRecorder" in r.text and "getDisplayMedia" in r.text


def test_platform_contour_404(client):
    assert client.get("/record", headers={"Host": "silentqa.com"}).status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_record_page.py -q`
Expected: FAIL — 404 на тенант-контуре (роут не существует)

- [ ] **Step 3: Add the route** (в конец `backend/app/routes/downloads.py`)

```python
PRIVATE_DIR = _BACKEND_DIR / "private"


@router.get("/record", include_in_schema=False)
async def record_page(request: Request):
    """Браузерный рекордер (спека B). Платформенный контур — 404: запись
    имеет смысл только внутри тенанта."""
    if get_tenant_slug() is None:
        raise HTTPException(status_code=404, detail="Not found")
    if await get_current_user(request) is None:
        return RedirectResponse("/", status_code=302)
    return FileResponse(PRIVATE_DIR / "record.html")
```

- [ ] **Step 4: Create the page** — `backend/private/record.html` целиком:

```html
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Запись звонка — SilentQA</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    :root {
      --bg: #191816; --surface: #252220; --surface2: #2e2a27; --border: #393431;
      --text: #f4f1ea; --text2: #b2a99b; --text3: #746b5e;
      --accent: #d97757; --red: #e5484d; --radius: 12px;
    }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: var(--bg); color: var(--text); min-height: 100dvh;
      display: flex; align-items: center; justify-content: center; padding: 24px;
    }
    .container { width: 100%; max-width: 560px; }
    h1 { font-size: 22px; text-align: center; margin-bottom: 6px; }
    .sub { text-align: center; color: var(--text2); font-size: 13px; margin-bottom: 20px; }
    .panel {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 20px; margin-bottom: 14px;
    }
    label { display: block; font-size: 13px; color: var(--text2); margin: 10px 0 4px; }
    input[type="text"] {
      width: 100%; background: var(--bg); border: 1px solid var(--border);
      border-radius: 8px; padding: 9px 12px; color: var(--text); font-size: 14px;
    }
    input[readonly] { color: var(--text2); }
    .chk { display: flex; align-items: flex-start; gap: 8px; margin-top: 14px; font-size: 14px; cursor: pointer; }
    .chk input { margin-top: 3px; }
    .hint { font-size: 12px; color: var(--text3); margin: 6px 0 0 24px; line-height: 1.5; }
    button {
      border: none; border-radius: 8px; padding: 11px 20px; font-size: 15px;
      font-weight: 600; cursor: pointer; width: 100%; margin-top: 16px;
    }
    #btnStart { background: var(--accent); color: #fff; }
    #btnStop { background: var(--red); color: #fff; }
    button:disabled { opacity: .5; cursor: default; }
    .live-row { display: flex; align-items: center; gap: 10px; font-size: 15px; }
    .rec-dot { width: 12px; height: 12px; border-radius: 50%; background: var(--red); animation: blink 1.2s infinite; }
    @keyframes blink { 50% { opacity: .25; } }
    #timer { font-variant-numeric: tabular-nums; font-weight: 700; font-size: 20px; }
    .muted { color: var(--text3); font-size: 12px; margin-top: 8px; }
    .note { color: var(--text2); font-size: 13px; margin-top: 10px; line-height: 1.5; }
    .error { background: rgba(229,72,77,.12); border: 1px solid var(--red);
      border-radius: 8px; padding: 10px 14px; color: #ff9a9e; font-size: 13px; margin-top: 12px; }
    .hidden { display: none; }
    a { color: var(--accent); }
    .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border);
      border-top-color: var(--accent); border-radius: 50%; animation: spin 1s linear infinite; vertical-align: -2px; }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div class="container">
    <h1>Запись звонка</h1>
    <p class="sub">Микрофон — всегда; звук вкладки — для Zoom/Meet в браузере</p>

    <div class="panel" id="setupPanel">
      <label for="employeeName">Сотрудник</label>
      <input type="text" id="employeeName" placeholder="Имя для атрибуции звонка">
      <label for="clientNote">Клиент / комментарий (необязательно)</label>
      <input type="text" id="clientNote" placeholder="Напр.: ООО Ромашка, повторный созвон">
      <label class="chk">
        <input type="checkbox" id="tabAudio">
        <span>Писать звук вкладки (собеседник в Zoom/Meet в браузере)</span>
      </label>
      <div class="hint">Браузер попросит выбрать вкладку — отметьте
        «Также предоставить доступ к аудио вкладки». В Safari недоступно —
        запишется только микрофон.</div>
      <button id="btnStart">● Начать запись</button>
    </div>

    <div class="panel hidden" id="livePanel">
      <div class="live-row">
        <div class="rec-dot"></div>
        <span id="timer">00:00</span>
        <span id="srcLabel" class="muted"></span>
      </div>
      <div class="muted">Отправлено кусков: <span id="chunksCount">0</span></div>
      <div class="note hidden" id="liveNote"></div>
      <button id="btnStop">■ Завершить и отправить</button>
    </div>

    <div class="panel hidden" id="donePanel">
      <div id="doneStatus"><span class="spinner"></span> Обработка записи…</div>
      <div class="note" id="doneLink"></div>
      <button id="btnAgain" class="hidden" style="background:var(--surface2);color:var(--text2)">Записать ещё</button>
    </div>

    <div class="error hidden" id="errorText"></div>
  </div>

<script>
const $id = (x) => document.getElementById(x);
let micStream = null, dispStream = null, audioCtx = null, mediaRecorder = null;
let sessionId = null, chunksSeen = 0, chunksUploaded = 0, failedChunks = 0;
let pending = Promise.resolve();
let timerInterval = null, startedAt = 0;

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const showError = (m) => { const e = $id('errorText'); e.textContent = m; e.classList.remove('hidden'); };
const hideError = () => $id('errorText').classList.add('hidden');
const note = (m) => { const n = $id('liveNote'); n.textContent = m; n.classList.remove('hidden'); };

// ── Boot: кто я (та же сессия, что и в дашборде) ─────────────────
(async function boot() {
  let r;
  try { r = await fetch('/api/user-auth/me'); } catch (e) { showError('Сервер недоступен'); return; }
  if (r.status === 401) { location.href = '/'; return; }
  const me = await r.json();
  const inp = $id('employeeName');
  inp.value = me.employee_name || '';
  if (me.role === 'manager') {
    inp.readOnly = true;
    inp.title = 'Имя закреплено за вашим аккаунтом';
  }
})();

// ── API (кука сессии уходит сама: same-origin) ───────────────────
async function createSession() {
  const metadata = { source: 'web-recorder', recordedAt: new Date().toISOString() };
  const emp = $id('employeeName').value.trim();
  if (emp) metadata.employee = emp;
  const cl = $id('clientNote').value.trim();
  if (cl) metadata.client_company = cl;
  const resp = await fetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ metadata }),
  });
  if (!resp.ok) throw new Error('HTTP ' + resp.status);
  return (await resp.json()).id;
}

function queueUpload(blob) {
  const n = ++chunksSeen;
  pending = pending.then(() => uploadChunk(blob, n)).catch(() => {});
}

async function uploadChunk(blob, n, attempt = 1) {
  const fd = new FormData();
  fd.append('file', blob, 'chunk_' + String(n).padStart(3, '0') + '.webm');
  let resp = null;
  try { resp = await fetch('/api/sessions/' + sessionId + '/chunks', { method: 'POST', body: fd }); }
  catch (e) { /* сеть моргнула — ретрай ниже */ }
  if (resp && resp.ok) {
    chunksUploaded++;
    $id('chunksCount').textContent = chunksUploaded;
    return;
  }
  if (attempt < 3) { await sleep(1500 * attempt); return uploadChunk(blob, n, attempt + 1); }
  failedChunks++;
}

// ── Захват и смешивание ──────────────────────────────────────────
function buildMixedStream() {
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const dest = audioCtx.createMediaStreamDestination();
  audioCtx.createMediaStreamSource(micStream).connect(dest);
  if (dispStream && dispStream.getAudioTracks().length) {
    audioCtx.createMediaStreamSource(new MediaStream(dispStream.getAudioTracks())).connect(dest);
  }
  return dest.stream;
}

function stopStreams() {
  [micStream, dispStream].forEach(s => { if (s) s.getTracks().forEach(t => t.stop()); });
  micStream = dispStream = null;
  if (audioCtx) { audioCtx.close().catch(() => {}); audioCtx = null; }
}

$id('btnStart').addEventListener('click', async () => {
  hideError();
  const btn = $id('btnStart');
  btn.disabled = true;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
  } catch (e) {
    showError('Нет доступа к микрофону: разрешите доступ в браузере и попробуйте снова.');
    btn.disabled = false;
    return;
  }
  let withTab = false;
  if ($id('tabAudio').checked) {
    try {
      dispStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      dispStream.getVideoTracks().forEach(t => t.stop()); // нужен только звук
      if (dispStream.getAudioTracks().length) {
        withTab = true;
        dispStream.getAudioTracks()[0].addEventListener('ended', () => {
          note('Доступ к звуку вкладки остановлен — запись продолжается с микрофона.');
          $id('srcLabel').textContent = 'микрофон';
        });
      } else {
        note('Вкладка выбрана без звука — пишется только микрофон. (Отмечайте «предоставить доступ к аудио вкладки».)');
        dispStream.getTracks().forEach(t => t.stop());
        dispStream = null;
      }
    } catch (e) {
      note('Звук вкладки недоступен — пишется только микрофон.');
      dispStream = null;
    }
  }
  try { sessionId = await createSession(); }
  catch (e) {
    showError('Не удалось создать сессию записи: ' + e.message);
    stopStreams();
    btn.disabled = false;
    return;
  }
  chunksSeen = chunksUploaded = failedChunks = 0;
  $id('chunksCount').textContent = '0';
  const mixed = buildMixedStream();
  mediaRecorder = new MediaRecorder(mixed, { mimeType: 'audio/webm;codecs=opus' });
  mediaRecorder.ondataavailable = (ev) => { if (ev.data && ev.data.size) queueUpload(ev.data); };
  mediaRecorder.onstop = onRecorderStop;
  mediaRecorder.start(10000); // чанк раз в 10 секунд — как расширение/десктоп
  $id('setupPanel').classList.add('hidden');
  $id('livePanel').classList.remove('hidden');
  $id('srcLabel').textContent = withTab ? 'микрофон + вкладка' : 'микрофон';
  startedAt = Date.now();
  timerInterval = setInterval(() => {
    const s = Math.floor((Date.now() - startedAt) / 1000);
    $id('timer').textContent =
      String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  }, 500);
  btn.disabled = false;
});

$id('btnStop').addEventListener('click', () => {
  $id('btnStop').disabled = true;
  if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
});

async function onRecorderStop() {
  clearInterval(timerInterval);
  stopStreams();
  $id('livePanel').classList.add('hidden');
  $id('donePanel').classList.remove('hidden');
  await pending; // дождаться хвоста аплоадов
  if (failedChunks > 0) {
    note('');
    showError('Часть кусков (' + failedChunks + ') не загрузилась — запись может быть неполной.');
  }
  try {
    await fetch('/api/sessions/' + sessionId + '/finish', { method: 'POST' });
  } catch (e) {
    $id('doneStatus').textContent = 'Не удалось завершить сессию — проверьте связь и обновите страницу.';
    return;
  }
  $id('doneLink').innerHTML =
    'Звонок появится в дашборде: <a href="/#call/' + sessionId + '">открыть карточку</a>';
  pollStatus();
}

async function pollStatus() {
  for (let i = 0; i < 120; i++) { // до 10 минут
    await sleep(5000);
    let s = null;
    try { s = await (await fetch('/api/sessions/' + sessionId)).json(); } catch (e) { continue; }
    if (s.status === 'completed') {
      $id('doneStatus').textContent = '✓ Готово — звонок обработан.';
      $id('btnAgain').classList.remove('hidden');
      return;
    }
    if (s.status === 'failed') {
      $id('doneStatus').textContent = 'Обработка не удалась — запись сохранена, обратитесь к администратору.';
      $id('btnAgain').classList.remove('hidden');
      return;
    }
  }
  $id('doneStatus').textContent = 'Обработка продолжается — результат появится в дашборде.';
  $id('btnAgain').classList.remove('hidden');
}

$id('btnAgain').addEventListener('click', () => location.reload());
window.addEventListener('beforeunload', (ev) => {
  if (mediaRecorder && mediaRecorder.state === 'recording') ev.preventDefault();
});
</script>
</body>
</html>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_record_page.py tests/test_download_gate.py -q`
Expected: passed

- [ ] **Step 6: Commit**

```bash
git add backend/private/record.html backend/app/routes/downloads.py backend/tests/test_record_page.py
git commit -m "feat(front): /record — браузерный рекордер (микрофон + звук вкладки, спека B)"
```

---

### Task 10: дашборд — пункты навигации + страница «Интеграции»

**Files:**
- Modify: `backend/static/index.html` (nav, после блока `#team`), `backend/static/app.js` (router ~строка 85, новые функции в конец файла)

**Interfaces:**
- Consumes: `GET/PUT /api/integrations/keys` (Task 7), страницы `/record` и `/download/` (Tasks 8-9); существующие хелперы app.js: `api()`, `$`, `$$`, `escapeHtml`, `showToast`, `showLoading`, `isAdmin`, `navigate`.
- Produces: nav-пункты «Запись», «Скачать приложения», «Интеграции» (admin-only); роут `#integrations`.

- [ ] **Step 1: Add nav items** — в `backend/static/index.html` после `</a></li>` пункта `#team` (перед пунктом `#profile`) вставить:

```html
      <li><a href="/record" class="nav-link">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v1a7 7 0 0014 0v-1"/><line x1="12" y1="20" x2="12" y2="23"/></svg>
        <span>Запись</span>
      </a></li>
      <li><a href="/download/" class="nav-link">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        <span>Скачать приложения</span>
      </a></li>
      <li><a href="#integrations" data-route="integrations" class="nav-link" data-admin-only>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 11-7.778 7.778 5.5 5.5 0 017.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg>
        <span>Интеграции</span>
      </a></li>
```

(`/record` и `/download/` — обычные ссылки без `data-route`: это отдельные страницы, не hash-роуты SPA.)

- [ ] **Step 2: Add the route** — в `backend/static/app.js`, в `router()` перед веткой `route === 'profile'` (~строка 85):

```js
  } else if (route === 'integrations') {
    if (!isAdmin()) { navigate('#calls'); return; }
    await renderIntegrations();
```

- [ ] **Step 3: Add the page** — в конец `backend/static/app.js`:

```js
// ---- Интеграции: пер-тенантные API-ключи (спека C) ----
async function renderIntegrations() {
  showLoading();
  let keys;
  try { keys = await api('/api/integrations/keys'); }
  catch (err) { app.innerHTML = `<div class="empty-state"><p>Ошибка: ${escapeHtml(err.message)}</p></div>`; return; }
  const row = (p, label, hint) => {
    const k = keys[p] || {};
    const state = k.set
      ? `Задан свой ключ (…${escapeHtml(k.last4 || '')})`
      : 'Используется общий ключ платформы';
    return `
    <div class="card">
      <h3>${label}</h3>
      <p style="color:var(--text-muted);font-size:13px">${state}. ${hint}</p>
      <div style="display:flex;gap:8px;max-width:560px">
        <input type="password" id="intg-${p}" placeholder="Вставьте новый ключ" style="flex:1" autocomplete="off">
        <button class="btn btn-primary" data-intg-save="${p}">Сохранить</button>
        ${k.set ? `<button class="btn btn-secondary" data-intg-clear="${p}">Сбросить</button>` : ''}
      </div>
    </div>`;
  };
  app.innerHTML = `
    <div class="page-header"><h1>Интеграции</h1></div>
    <p style="color:var(--text-muted);font-size:13px;margin-bottom:12px">
      Ключи хранятся в зашифрованном виде и назад не показываются.
      «Сбросить» — вернуться на общий ключ платформы.</p>
    ${row('openai', 'OpenAI', 'Оценка качества, план следующего звонка, коучинг.')}
    ${row('assemblyai', 'AssemblyAI', 'Транскрибация звонков.')}
    ${row('elevenlabs', 'ElevenLabs', 'Транскрибация (Scribe).')}`;
  $$('[data-intg-save]').forEach(b => b.addEventListener('click', () => {
    const p = b.dataset.intgSave;
    const v = $(`#intg-${p}`).value.trim();
    if (!v) { showToast('Введите ключ'); return; }
    _intgPut(p, v);
  }));
  $$('[data-intg-clear]').forEach(b => b.addEventListener('click', () => {
    if (confirm('Сбросить ключ и вернуться на общий ключ платформы?')) _intgPut(b.dataset.intgClear, '');
  }));
}

async function _intgPut(provider, value) {
  try {
    await api('/api/integrations/keys', {
      method: 'PUT', body: JSON.stringify({ [provider]: value }),
    });
    showToast(value ? 'Ключ сохранён' : 'Ключ сброшен');
    currentRoute = ''; router(); // перерисовать страницу свежим состоянием
  } catch (err) {
    showToast('Ошибка: ' + err.message);
  }
}
```

Сигнатуру `api()` и `showToast()` сверить по файлу (см. `evalSaveCriteria` ~строка 3135 — образец использования); если `showToast` принимает второй аргумент типа ошибки — использовать его.

- [ ] **Step 4: Verify**

Run: `cd backend && python -m pytest tests/ -q` (регрессии нет — JS не тестируется юнитами)
Затем smoke в браузере на локали ИЛИ отложить до задачи 13 (staging): `#integrations` виден только админу, сохранение/сброс ключа работает, `/record` и `/download/` открываются из меню.

- [ ] **Step 5: Commit**

```bash
git add backend/static/index.html backend/static/app.js
git commit -m "feat(front): нав-пункты Запись/Скачать + страница Интеграции с ключами (спека C)"
```

---

### Task 11: платформенная админка — блок «API-ключи» в карточке тенанта

**Files:**
- Modify: `backend/static/admin.js` (функция `openTenant`, строки 110-165)

**Interfaces:**
- Consumes: `GET/PUT /api/platform/tenants/{slug}/keys` (Task 6).

- [ ] **Step 1: Load keys** — в начале `openTenant(slug)` (после запроса users):

```js
  const keys = await api(`/api/platform/tenants/${slug}/keys`);
```

- [ ] **Step 2: Render the block** — в `card.innerHTML` после кнопок `pa-row-actions` (перед `<div id="pa-card-secret">`):

```js
  const provRows = ['openai', 'assemblyai', 'elevenlabs'].map((p) => {
    const k = keys[p] || {};
    return `
    <tr>
      <td>${p}</td>
      <td>${k.set ? `свой (…${escapeHtml(k.last4 || '')})` : 'общий ключ платформы'}</td>
      <td class="pa-row-actions">
        <input type="password" id="pa-key-${p}" placeholder="новый ключ" autocomplete="off">
        <button data-keysave="${p}">Сохранить</button>
        <button data-keyclear="${p}" class="pa-danger" ${k.set ? '' : 'disabled'}>Сбросить</button>
      </td>
    </tr>`;
  }).join('');
```

и в шаблон карточки (между `pa-row-actions` и `pa-card-secret`):

```html
    <h4>API-ключи провайдеров</h4>
    <table class="pa-table">
      <tr><th>Провайдер</th><th>Состояние</th><th></th></tr>${provRows}
    </table>
```

- [ ] **Step 3: Handlers** — после существующих обработчиков в `openTenant`:

```js
  card.querySelectorAll('button[data-keysave]').forEach((b) => {
    b.onclick = async () => {
      const p = b.dataset.keysave;
      const v = document.getElementById(`pa-key-${p}`).value.trim();
      if (!v) return;
      await api(`/api/platform/tenants/${slug}/keys`, {
        method: 'PUT', body: JSON.stringify({ [p]: v }),
      });
      await openTenant(slug);
    };
  });
  card.querySelectorAll('button[data-keyclear]').forEach((b) => {
    b.onclick = async () => {
      if (!confirm('Сбросить ключ — тенант вернётся на общий ключ платформы. Продолжить?')) return;
      await api(`/api/platform/tenants/${slug}/keys`, {
        method: 'PUT', body: JSON.stringify({ [b.dataset.keyclear]: '' }),
      });
      await openTenant(slug);
    };
  });
```

- [ ] **Step 4: Verify & commit**

Smoke: на staging/локали открыть карточку тенанта — блок рендерится, сохранение/сброс перерисовывает состояние.

```bash
git add backend/static/admin.js
git commit -m "feat(admin): блок API-ключей провайдеров в карточке тенанта (спека C)"
```

---

### Task 12: mac-оформление /download + докдолг

**Files:**
- Modify: `backend/static/download/index.html`, `CLAUDE.md`

- [ ] **Step 1: macOS-заметка** — в `static/download/index.html` после блока `winNote` (строка ~119) добавить:

```html
    <div class="note" id="macNote">
      <b>macOS:</b> приложение без нотаризации Apple — при первом запуске откройте через
      <b>правый клик по приложению → «Открыть» → «Открыть»</b>. Если система всё равно блокирует:
      <b>Системные настройки → Конфиденциальность и безопасность → «Подтвердить открытие»</b>,
      либо в Терминале: <span style="font-family:ui-monospace,Menlo,monospace">xattr -cr "/Applications/SilentQA Recorder.app"</span>.
      Сборка — для Apple Silicon (M1–M4); на Intel-Mac не запустится.
    </div>
```

- [ ] **Step 2: Show it only on mac** — в скрипте той же страницы (строки ~146-153), рядом с логикой `winNote`:

```js
        if(os!=='windows'){ var n=document.getElementById('winNote'); if(n) n.style.display='none'; }
        if(os!=='mac'){ var m=document.getElementById('macNote'); if(m) m.style.display='none'; }
```

- [ ] **Step 3: CLAUDE.md** — в разделе «Client surfaces» заменить фразу:

`**\`#upload\` is file-upload only — there is no live recorder in the dashboard**, live capture is the extension/desktop app`

на:

`**\`#upload\` is file-upload only**; live capture is the extension/desktop app **or the \`/record\` page** (browser recorder: mic + optional tab audio, gated by the dashboard session — served from \`backend/private/\`, NOT from the static mount). \`/download/*\` is auth-gated the same way (\`routes/downloads.py\`)`

и в раздел «Auth layers» / gotchas добавить строку про ключи (в конец таблицы Auth не надо — добавить в «Gotchas & known drift» пункт):

```markdown
- **Per-tenant API keys** (`shared.tenants.api_credentials`, Fernet поверх `SECRET_KEY` — `tenancy/secrets.py`): OpenAI/AssemblyAI/ElevenLabs резолвятся «тенантный → глобальный env» (`worker/tasks/tenant_keys.py`, `backend/app/tenant_keys.py`). Наружу ключи не возвращаются (только `set`/`last4`); правятся в платформенной админке и на странице «Интеграции» дашборда.
```

- [ ] **Step 4: Commit**

```bash
git add backend/static/download/index.html CLAUDE.md
git commit -m "docs+front: mac-инструкция на /download, CLAUDE.md — /record, гейт загрузок, пер-тенантные ключи"
```

---

### Task 13: выкладка (прод + staging) и верификация

**Files:**
- Modify: `/etc/caddy/Caddyfile` (вне репо)

- [ ] **Step 1: Full test pass**

```bash
cd backend && python -m pytest tests/ -q && cd ../worker && python -m pytest -q
```
Expected: всё зелёное.

- [ ] **Step 2: Push + staging**

```bash
git push github multi-tenant-core-phase1
cd /root/projects/silentqa-dev && git pull
sudo systemctl restart silentqa-staging-backend silentqa-staging-worker silentqa-staging-worker-io
```
(ExecStartPre staging сам прогонит `python -m app.migrate` → применит s005.)

Ручная проверка на staging: `/record` в Chrome — запись микрофон+вкладка доходит до `completed`; `/download/` без логина → редирект на `/`; ключ тенанта сохраняется в админке и на «Интеграциях».

- [ ] **Step 3: Prod deploy**

```bash
bash scripts/deploy_prod.sh
```
(скрипт: ff-merge из github, pip install, `python -m app.migrate` (s005 на все схемы), рестарт backend+оба воркера, readiness-чек.)

- [ ] **Step 4: Caddy — www-редирект.** В `/etc/caddy/Caddyfile` ПЕРЕД блоком `silentqa.com, *.silentqa.com {` (строка ~224) вставить:

```
www.silentqa.com {
	redir https://silentqa.com{uri} permanent
}
```

Затем:

```bash
caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

- [ ] **Step 5: Verify prod**

```bash
curl -sI https://www.silentqa.com | head -3          # HTTP/2 301 → https://silentqa.com/
curl -sI https://silentqa.com/download/ | head -3    # 302 → / (без куки)
curl -sI https://chechetov.silentqa.com/record | head -3  # 302 → / (без куки)
# с браузером: логин на тенанте → /download/ отдаёт страницу, все 5 артефактов качаются,
# /record пишет и звонок доходит до completed
```

- [ ] **Step 6: Final commit / merge bookkeeping**

Если в ходе staging-проверки были фиксы — закоммитить и повторить шаги 2-3.
