# Quick Wins системного ревью — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть пакет quick wins ревью `docs/reviews/2026-07-05-system-review.md`: D-1.1 (потолки и кэш LLM-вызовов), C-1 (XSS через escapeHtml/onclick), C-2 (лимит тела запроса), C-3 (env-гейт /docs), G-3 (Promise.all в карточке звонка), H-1 (мёртвая зависимость anthropic), H-2 (docstring-дрейф провайдера), H-4 (rogovestate-фолбэк).

**Architecture:** Три задачи по дизъюнктным файлам: (1) worker — параметры LLM-вызовов + docstring'и + requirements; (2) backend — ASGI-middleware лимита тела + пер-роутные капы ингеста + гейт /docs; (3) фронт — quote-safe `escapeHtml`, перевод инъекционных `onclick` на `data-*`, параллельные fetch'и, снятие realestate-фолбэка. Никаких новых зависимостей, никаких миграций.

**Tech Stack:** FastAPI/Starlette (pure-ASGI middleware), pydantic-settings, OpenAI Responses API (SDK ≥2.26, `prompt_cache_key` поддержан), vanilla JS (no-build).

## Global Constraints

- Спека = `docs/reviews/2026-07-05-system-review.md`, секции C-1, C-2, C-3, D-1 (п.1–2 рекомендаций), G-3, H-1, H-2, H-4. Номера строк в спеке частично уплыли — в задачах ниже указаны **актуальные**.
- Русский язык в комментариях/докстрингах — как в окружающем коде.
- Тесты — pure unit (без Postgres/Redis); cwd имеет значение: `cd backend && python -m pytest tests/`, `cd worker && python -m pytest`. Локальный python: `../.venv/bin/python` из backend/worker.
- Фронт: no-build; проверка `node --check backend/static/app.js backend/static/admin.js` и `node --test 'backend/static/tests/*.test.mjs'` (glob обязателен — Node 22 не дискаверит каталог).
- **Агенты НЕ делают git** — коммитит контроллер после верификации (санкционированный SDD-режим).
- Прод/стейдж не трогаем: Caddy-лимит — только нотой в ранбук, применяет владелец.

---

### Task 1: worker-llm — D-1.1 (max_output_tokens + prompt_cache_key) + H-2 (docstring'и) + H-1 (anthropic)

**Files:**
- Modify: `worker/tasks/quality.py` (шапка-докстринг; константы; `plan_next_call` :675; `_assess_with_structured_output` :1228)
- Modify: `worker/tasks/card.py` (константа; вызов :54)
- Modify: `worker/tasks/extract.py` (константа; вызов :81)
- Modify: `worker/tasks/pipeline.py` (только шапка-докстринг :1-10)
- Modify: `worker/tasks/transcribe.py` (только шапка-докстринг :1-7)
- Modify: `worker/requirements.txt` (удалить блок anthropic :21-22)
- Test: `worker/tests/test_llm_call_params.py` (новый)

**Interfaces:**
- Consumes: существующие сигнатуры `_assess_with_structured_output(client, transcript_text, sentiment_json, protocol, custom_instructions, criteria_instructions="", use_extended_schema=False, prior_context=None, template_driven=False, glossary=None)`, `plan_next_call(prior_context, current_quality_report, deal_stage=None)`, `card.run_card_extraction(transcript, company_config, scenario=None)` — **не менять**.
- Produces: модульные константы `quality.QUALITY_MAX_OUTPUT_TOKENS = 16_000`, `quality.PLAN_MAX_OUTPUT_TOKENS = 6_000`, `card.CARD_MAX_OUTPUT_TOKENS = 6_000`, `extract.EXTRACT_MAX_OUTPUT_TOKENS = 6_000`; cache-ключи `"sqa-quality-v{2|4}[-tpl]"`, `"sqa-plan"`, `"sqa-card"`, `f"sqa-extract-{template_id}"`.

- [ ] **Step 1: Написать падающий тест**

Создать `worker/tests/test_llm_call_params.py`:

```python
"""D-1.1: все LLM-вызовы несут max_output_tokens и prompt_cache_key.

Функциональная проверка quality/plan/card (фейк-клиент ловит kwargs);
для extract.py — source-проверка (живой запуск требует БД и тенант-контекста).
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import tasks.card as card
import tasks.quality as quality


class FakeResponses:
    def __init__(self, payload):
        self._payload = payload
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(output_text=json.dumps(self._payload), status="completed")


class FakeClient:
    def __init__(self, payload):
        self.responses = FakeResponses(payload)


def test_quality_structured_v2_params():
    fake = FakeClient({"overall_score": 7})
    quality._assess_with_structured_output(fake, "транскрипт", "{}", None, "", "")
    kw = fake.responses.kwargs
    assert kw["max_output_tokens"] == quality.QUALITY_MAX_OUTPUT_TOKENS
    assert kw["prompt_cache_key"] == "sqa-quality-v2"


def test_quality_structured_v4_template_cache_key():
    fake = FakeClient({"overall_score": 7})
    quality._assess_with_structured_output(
        fake, "транскрипт", "{}", None, "", "",
        use_extended_schema=True, template_driven=True,
    )
    kw = fake.responses.kwargs
    assert kw["max_output_tokens"] == quality.QUALITY_MAX_OUTPUT_TOKENS
    assert kw["prompt_cache_key"] == "sqa-quality-v4-tpl"


def test_plan_next_call_params(monkeypatch):
    fake = FakeClient({"goals": []})
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(quality, "OpenAI", lambda api_key=None: fake)
    plan = quality.plan_next_call({"calls": []}, {"overall_score": 5})
    assert plan == {"goals": []}
    kw = fake.responses.kwargs
    assert kw["max_output_tokens"] == quality.PLAN_MAX_OUTPUT_TOKENS
    assert kw["prompt_cache_key"] == "sqa-plan"


def test_card_extraction_params(monkeypatch):
    fake = FakeClient({"result": "ok"})
    monkeypatch.setattr(card, "OpenAI", lambda: fake)
    cfg = {"card_extraction": {"prompt": "p", "json_schema": {"type": "object"}}}
    out = card.run_card_extraction([{"speaker": "A", "text": "привет"}], cfg)
    assert out == {"result": "ok"}
    kw = fake.responses.kwargs
    assert kw["max_output_tokens"] == card.CARD_MAX_OUTPUT_TOKENS
    assert kw["prompt_cache_key"] == "sqa-card"


def test_extract_source_has_params():
    src = (Path(__file__).resolve().parents[1] / "tasks" / "extract.py").read_text()
    assert "max_output_tokens=EXTRACT_MAX_OUTPUT_TOKENS" in src
    assert "prompt_cache_key" in src


def test_no_anthropic_in_requirements():
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    assert "anthropic" not in req.lower()
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `cd worker && ../.venv/bin/python -m pytest tests/test_llm_call_params.py -v`
Expected: FAIL — `KeyError: 'max_output_tokens'` (kwargs без новых параметров), `AttributeError: ... has no attribute 'QUALITY_MAX_OUTPUT_TOKENS'`, assert по requirements.

- [ ] **Step 3: Реализация**

**3a. `worker/tasks/quality.py`** — после `logger = logging.getLogger(__name__)` добавить:

```python
# Потолки выхода structured-вызовов (D-1.1): JSON-схема фиксирует форму
# ответа, потолок страхует расход — без него платим по максимуму модели.
# Reasoning-токены GPT-5.4 тоже входят в max_output_tokens, поэтому запас
# кратен типовому отчёту (2-4K токенов).
QUALITY_MAX_OUTPUT_TOKENS = 16_000
PLAN_MAX_OUTPUT_TOKENS = 6_000
```

В `_assess_with_structured_output` (:1228) дополнить вызов:

```python
    response = client.responses.create(
        model="gpt-5.4",
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_output_tokens=QUALITY_MAX_OUTPUT_TOKENS,
        # Роутинг-ключ OpenAI prompt caching: system-промпт статичен в пределах
        # (version, template_driven) — cached-вход в 10 раз дешевле; точное
        # совпадение префикса OpenAI сверяет сам, ключ лишь улучшает роутинг.
        prompt_cache_key=f"sqa-quality-v{version}" + ("-tpl" if template_driven else ""),
        text={
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema["schema"],
            }
        },
    )
    if getattr(response, "status", None) == "incomplete":
        logger.warning(
            "Quality LLM output truncated at %s tokens", QUALITY_MAX_OUTPUT_TOKENS)
```

В `plan_next_call` (:675) дополнить вызов теми же двумя параметрами:

```python
        response = client.responses.create(
            model="gpt-5.4",
            input=[
                {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
            ],
            max_output_tokens=PLAN_MAX_OUTPUT_TOKENS,
            prompt_cache_key="sqa-plan",
            text={
                "format": {
                    "type": "json_schema",
                    "name": NEXT_CALL_PLAN_SCHEMA["name"],
                    "strict": True,
                    "schema": NEXT_CALL_PLAN_SCHEMA["schema"],
                }
            },
        )
```

Шапку-докстринг quality.py (:1-10) заменить на:

```python
"""
Оценка качества разговора менеджера через OpenAI GPT-5.4
(Responses API, structured output).

Runtime-режимы:
- Базовый (v2): стандартная оценка для любых звонков
- Расширенный (v4): чек-лист скрипта, классификация, возражения, playbook
  назначения встречи — для сценариев с кастомным prompt

V3-промпты/схемы живут в коде только как строительные блоки V4
(SYSTEM_PROMPT_V4 = V3 + контекст + playbook); как самостоятельная
версия отчёта V3 не выбирается.
"""
```

**3b. `worker/tasks/card.py`** — после `logger = ...` добавить `CARD_MAX_OUTPUT_TOKENS = 6_000` (комментарий: `# Потолок выхода card-вызова (D-1.1)`), в `client.responses.create(` (:54) добавить:

```python
            max_output_tokens=CARD_MAX_OUTPUT_TOKENS,
            prompt_cache_key="sqa-card",
```

**3c. `worker/tasks/extract.py`** — после импортов добавить `EXTRACT_MAX_OUTPUT_TOKENS = 6_000` (тот же комментарий), в `client.responses.create(` (:81) добавить:

```python
        max_output_tokens=EXTRACT_MAX_OUTPUT_TOKENS,
        prompt_cache_key=f"sqa-extract-{template_id}",
```

**3d. `worker/tasks/pipeline.py`** — в шапке (:1-10) заменить две строки:
- `2. Транскрипция (faster-whisper)` → `2. Транскрипция (облачный ASR: ElevenLabs/AssemblyAI, whisper — последний фолбэк)`
- `6. Оценка качества (Claude LLM)` → `6. Оценка качества (OpenAI GPT-5.4)`

**3e. `worker/tasks/transcribe.py`** — шапку (:1-7) заменить на:

```python
"""
Транскрипция аудио.
Движки — упорядоченная цепочка фолбэков [ASR_ENGINE] + [elevenlabs, assemblyai, whisper]:
  - elevenlabs: облачный Scribe v2, встроенная диаризация — основной в проде
  - assemblyai: облачный API, word_boost
  - whisper: локальный faster-whisper (CPU) — только последний фолбэк,
    заметно хуже по качеству; фактический прогон через whisper — красный флаг
Выбор primary — env ASR_ENGINE (код-дефолт whisper обманчив: деплои ставят облачный).
"""
```

**3f. `worker/requirements.txt`** — удалить строки:

```
# --- LLM: Anthropic Claude API ---
anthropic>=0.49.0
```

(и оставшуюся от блока пустую строку, чтобы не было двойного пропуска).

Там же поднять пин `openai>=2.26.0` → `openai>=2.44.0`: параметр `prompt_cache_key`
гарантированно есть в 2.44 (проверено на локальном venv); на более старом SDK
вызов упал бы TypeError'ом → лишний fallback-прогон.

- [ ] **Step 4: Прогнать фокусные тесты**

Run: `cd worker && ../.venv/bin/python -m pytest tests/test_llm_call_params.py -v`
Expected: 6 passed.

Затем smoke-импорт: `cd worker && ../.venv/bin/python -c "import tasks.quality, tasks.card, tasks.extract, tasks.pipeline, tasks.transcribe; print('ok')"`
Expected: `ok`.

- [ ] **Step 5: Коммит (контроллер)**

`fix(worker): D-1.1 потолки+prompt-cache LLM-вызовов, H-2 докстринги, H-1 anthropic`

---

### Task 2: backend-http — C-2 (лимит тела) + C-3 (гейт /docs)

**Files:**
- Create: `backend/app/body_limit.py`
- Modify: `backend/app/main.py:42-53`
- Modify: `backend/app/config.py`
- Modify: `backend/app/routes/chunks.py` (:92 чанк, :190-221 upload-audio)
- Modify: `.env.example` (новые переменные; удалить `# ANTHROPIC_API_KEY=` — H-1)
- Modify: `run.sh` (env-блок ~:20-24)
- Modify: `docs/superpowers/specs/2026-06-10-phase1-deploy-runbook.md` (нота про Caddy, в конец)
- Test: `backend/tests/test_body_limit_docs.py` (новый)

**Interfaces:**
- Produces: `app.body_limit.BodySizeLimitMiddleware(app, max_bytes: int)` — pure-ASGI; `Settings.MAX_REQUEST_BODY_MB: int = 600`, `Settings.MAX_CHUNK_UPLOAD_MB: int = 32`, `Settings.MAX_AUDIO_UPLOAD_MB: int = 512`, `Settings.ENABLE_API_DOCS: bool = False`; `chunks._enforce_upload_cap(size_bytes, max_mb, what)`.
- Consumes: фикстуры `fake_redis` из `backend/tests/conftest.py`; паттерн `client`-фикстуры — из `backend/tests/test_root_dispatch.py:1-17`.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/tests/test_body_limit_docs.py`:

```python
"""C-2: глобальный лимит тела + пер-роутные капы; C-3: /docs выключены по умолчанию."""
import asyncio

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


# --- BodySizeLimitMiddleware как чистый ASGI-юнит ---

def _scope(headers):
    return {"type": "http", "method": "POST", "path": "/x", "headers": headers}


async def _run(mw, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await mw(scope, receive, send)
    return sent


def test_middleware_rejects_oversized():
    from app.body_limit import BodySizeLimitMiddleware

    async def inner_app(scope, receive, send):
        raise AssertionError("до приложения дойти не должно")

    mw = BodySizeLimitMiddleware(inner_app, max_bytes=10)
    sent = asyncio.run(_run(mw, _scope([(b"content-length", b"11")])))
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_middleware_passes_at_limit_and_without_length():
    from app.body_limit import BodySizeLimitMiddleware
    reached = []

    async def inner_app(scope, receive, send):
        reached.append(scope["path"])

    mw = BodySizeLimitMiddleware(inner_app, max_bytes=10)
    asyncio.run(_run(mw, _scope([(b"content-length", b"10")])))
    asyncio.run(_run(mw, _scope([])))          # chunked / нет заголовка — пропускаем
    asyncio.run(_run(mw, _scope([(b"content-length", b"garbage")])))  # мусор — пропускаем
    assert len(reached) == 3


# --- вайринг в приложение ---

def test_app_has_body_limit_middleware(client):
    from app.body_limit import BodySizeLimitMiddleware
    from app.main import app
    assert any(m.cls is BodySizeLimitMiddleware for m in app.user_middleware)


# --- пер-роутный кап ингеста ---

def test_enforce_upload_cap():
    from fastapi import HTTPException
    from app.routes.chunks import _enforce_upload_cap

    _enforce_upload_cap(5, 1, "chunk")      # 5 байт < 1 МБ — ок
    _enforce_upload_cap(None, 1, "chunk")   # размер неизвестен — пропускаем
    with pytest.raises(HTTPException) as exc:
        _enforce_upload_cap(2 * 1024 * 1024, 1, "chunk")
    assert exc.value.status_code == 413


# --- C-3: schema-эндпоинты ---

def test_docs_disabled_by_default(client):
    for host in ("acme.silentqa.com", "admin.silentqa.com"):
        for path in ("/docs", "/redoc", "/openapi.json"):
            r = client.get(path, headers={"Host": host})
            assert r.status_code == 404, f"{host}{path}"


def test_enable_api_docs_flag_parses(monkeypatch):
    monkeypatch.setenv("ENABLE_API_DOCS", "1")
    from app.config import Settings
    assert Settings().ENABLE_API_DOCS is True
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_body_limit_docs.py -v`
Expected: FAIL — `ModuleNotFoundError: app.body_limit`, `ImportError: _enforce_upload_cap`, `/docs` возвращает 200, у `Settings` нет `ENABLE_API_DOCS`.

- [ ] **Step 3: Реализация**

**3a. `backend/app/config.py`** — добавить в `Settings` после блока Multi-tenancy:

```python
    # Лимиты тела запроса, МБ (ревью C-2). Первая линия — глобальный
    # ASGI-middleware по Content-Length; вторая — пер-роутные капы ингеста
    # (по фактическим байтам); внешний слой — request_body в Caddy (ранбук).
    MAX_REQUEST_BODY_MB: int = 600
    MAX_CHUNK_UPLOAD_MB: int = 32    # один 10-секундный WebM/Opus-чанк
    MAX_AUDIO_UPLOAD_MB: int = 512   # файл-аплоуд целого звонка
    # /docs, /redoc, /openapi.json (ревью C-3): по умолчанию выключены,
    # включать только на стейдже/локали.
    ENABLE_API_DOCS: bool = False
```

**3b. Создать `backend/app/body_limit.py`:**

```python
"""Глобальный потолок размера тела запроса (ревью C-2).

Чистый ASGI-слой: отклоняет запрос 413-м по заголовку Content-Length до
чтения тела. Клиенты без Content-Length (chunked) проходят дальше — для
тяжёлых маршрутов ингеста есть вторая линия по фактическим байтам
(routes/chunks.py:_enforce_upload_cap); внешний слой — request_body в Caddy.
"""
import json


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name != b"content-length":
                    continue
                try:
                    length = int(value)
                except ValueError:
                    break  # мусорный заголовок — пусть падает глубже по стеку
                if length > self.max_bytes:
                    body = json.dumps({"detail": "Request body too large"}).encode()
                    await send({
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    })
                    await send({"type": "http.response.body", "body": body})
                    return
                break
        await self.app(scope, receive, send)
```

**3c. `backend/app/main.py`** — конструктор приложения (:42) заменить на:

```python
app = FastAPI(
    title="Meeting Recorder", version="1.0.0", lifespan=lifespan,
    # C-3: schema-эндпоинты живут вне /api/* и не покрываются контур-гейтом —
    # наружу их не светим; включаются явным флагом (стейдж/локаль).
    docs_url="/docs" if settings.ENABLE_API_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if settings.ENABLE_API_DOCS else None,
)

# Глобальный потолок тела (C-2). Добавлен ДО CORS: последний add_middleware —
# внешний, т.е. стек tenant → CORS → body-limit, и 413 уходит с CORS-заголовками.
app.add_middleware(
    BodySizeLimitMiddleware, max_bytes=settings.MAX_REQUEST_BODY_MB * 1024 * 1024)
```

Импорт — к остальным `from app...` в шапке: `from app.body_limit import BodySizeLimitMiddleware`.

**3d. `backend/app/routes/chunks.py`** — добавить перед первым роутом:

```python
def _enforce_upload_cap(size_bytes: int | None, max_mb: int, what: str) -> None:
    """413, если размер известен и превышает потолок. Вторая линия за
    глобальным middleware — ловит по фактическим байтам клиентов без/с
    лживым Content-Length."""
    if size_bytes is not None and size_bytes > max_mb * 1024 * 1024:
        raise HTTPException(
            status_code=413, detail=f"{what} exceeds {max_mb} MB limit")
```

В чанк-роуте сразу после `content = await file.read()` (:92):

```python
    content = await file.read()
    _enforce_upload_cap(len(content), settings.MAX_CHUNK_UPLOAD_MB, "chunk")
```

В `upload_audio_file` заменить блок записи (:217-221):

```python
    # Starlette уже принял тело в спул (диск после 1 МБ) — размер известен
    # до чтения в память: отбиваем негабарит и стримим копию без RAM-спайка.
    upload_size = getattr(file, "size", None)
    if upload_size is None:
        file.file.seek(0, os.SEEK_END)
        upload_size = file.file.tell()
        file.file.seek(0)
    _enforce_upload_cap(upload_size, settings.MAX_AUDIO_UPLOAD_MB, "audio file")

    async with aiofiles.open(original_path, "wb") as f:
        while chunk_bytes := await file.read(1024 * 1024):
            await f.write(chunk_bytes)

    file_size = upload_size
```

(строка `file_size = len(content)` уходит; `session.file_size_bytes = file_size` остаётся; `import os` в файле уже есть — проверить.)

**3e. `.env.example`** — удалить строку `# ANTHROPIC_API_KEY=` (H-1: провайдер мёртв); после блока Multi-tenant добавить:

```
# Лимиты тела запроса, МБ: глобальный middleware + пер-роутные капы ингеста
# MAX_REQUEST_BODY_MB=600
# MAX_CHUNK_UPLOAD_MB=32
# MAX_AUDIO_UPLOAD_MB=512

# Swagger /docs, /redoc, /openapi.json: по умолчанию выключены (не светить
# схему API наружу); включать на стейдже/локали.
# ENABLE_API_DOCS=1
```

**3f. `run.sh`** — в env-блок (после RESULTS_STORAGE_PATH) добавить:

```bash
export ENABLE_API_DOCS="${ENABLE_API_DOCS:-1}"  # локальная разработка — /docs удобен
```

**3g. Ранбук `docs/superpowers/specs/2026-06-10-phase1-deploy-runbook.md`** — добавить в конец:

```markdown
## Body-size limit в Caddy (ревью C-2, добавлено 2026-07-06)

Приложение держит потолки само (`MAX_REQUEST_BODY_MB=600` middleware +
капы ингеста в chunks.py). Внешний слой на проде — Caddy: в блок
`silentqa.com, *.silentqa.com` добавить

    request_body {
        max_size 600MB
    }

и `systemctl reload caddy`. Применяет владелец при ближайшем деплое.
```

- [ ] **Step 4: Прогнать фокусные тесты**

Run: `cd backend && ../.venv/bin/python -m pytest tests/test_body_limit_docs.py -v`
Expected: 6 passed.

Смежные сьюты обязаны выжить (главный риск — `/docs`-зависимые и chunks-тесты): `cd backend && ../.venv/bin/python -m pytest tests/ -q`
Expected: всё зелёное (278+ passed до этой волны).

- [ ] **Step 5: Коммит (контроллер)**

`fix(backend): C-2 лимиты тела (middleware+капы ингеста), C-3 env-гейт /docs`

---

### Task 3: frontend — C-1 (XSS) + G-3 (Promise.all) + H-4 (rogovestate)

**Files:**
- Modify: `backend/static/app.js` (:19 amoBase; :267 escapeHtml; :496-512 renderCallDetail; :620 data-term; :2093, :2110-2111, :2262, :3444-3445 onclick-сайты; :2163, :2192, :2224, :2383, :3460, :3475 сигнатуры обработчиков; :3139, :3186 amoBase-usages)
- Modify: `backend/static/admin.js` (:4 escapeHtml)
- Modify: `.github/workflows/tests.yml` (в джобе clients добавить `backend/static/admin.js` в `node --check`)

**Interfaces:**
- Consumes: `escapeHtml`, `moduleOn`, `isAdmin`, `api` — глобальные функции app.js.
- Produces: обработчики `kbDeleteCategory(btn)`, `kbDeleteEntry(btn)`, `kbShowMentions(btn)`, `deleteTemplate(btn)`, `renameComplex(btn)`, `deleteComplex(btn)` — принимают DOM-элемент, читают `btn.dataset.id` + `btn.dataset.name`/`btn.dataset.term`. Других вызовов этих функций в кодовой базе нет (проверено grep'ом), менять сигнатуру безопасно.

- [ ] **Step 1: quote-safe `escapeHtml` (оба файла)**

В `app.js` (:267) и `admin.js` (:4) заменить одинаковую реализацию на:

```js
function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
```

(Поведенческая разница со старым `textContent→innerHTML`: `undefined` теперь даёт `''`, не `"undefined"` — это исправление, не регресс.)

- [ ] **Step 2: перевести 6 инъекционных onclick на data-\* + this**

Каждый сайт: имя уходит из инлайн-JS в атрибут через quote-safe `escapeHtml`, обработчик получает `this` и читает `dataset` (браузер сам декодирует entities → в функции оригинальная строка).

`app.js:2093`:
```js
${isAdmin() ? `<button class="btn btn-danger btn-sm" title="Удалить категорию" data-id="${cat.id}" data-name="${escapeHtml(cat.name)}" onclick="kbDeleteCategory(this)">✕</button>` : ''}
```

`app.js:2110-2111`:
```js
<button class="btn btn-secondary btn-sm" data-id="${e.id}" data-term="${escapeHtml(e.term)}" onclick="kbShowMentions(this)">Упоминания</button>
${isAdmin() ? `<button class="btn btn-danger btn-sm" data-id="${e.id}" data-term="${escapeHtml(e.term)}" onclick="kbDeleteEntry(this)">✕</button>` : ''}
```

`app.js:2262` (внутри — сохранить preventDefault/stopPropagation):
```js
onclick="event.preventDefault();event.stopPropagation();deleteTemplate(this)" data-id="${t.id}" data-name="${escapeHtml(t.name)}"
```

`app.js:3444-3445`:
```js
<button class="btn btn-secondary btn-sm" data-id="${c.id}" data-name="${escapeHtml(c.name)}" onclick="renameComplex(this)">Переименовать</button>
<button class="btn btn-danger btn-sm" data-id="${c.id}" data-name="${escapeHtml(c.name)}" onclick="deleteComplex(this)">Удалить профиль</button>
```

Сигнатуры (тела функций не менять, только первая строка):

```js
async function kbDeleteCategory(btn) {
  const { id: catId, name } = btn.dataset;
  ...
async function kbDeleteEntry(btn) {
  const { id: entryId, term } = btn.dataset;
  ...
async function kbShowMentions(btn) {
  const { id: entryId, term } = btn.dataset;
  ...
async function deleteTemplate(btn) {
  const { id, name } = btn.dataset;
  ...
async function renameComplex(btn) {
  const { id, name: currentName } = btn.dataset;
  ...
async function deleteComplex(btn) {
  const { id, name } = btn.dataset;
  ...
```

- [ ] **Step 3: убрать ставший лишним точечный replace**

`app.js:620`: `data-term="${escapeHtml(t.term).replace(/"/g, '&quot;')}"` → `data-term="${escapeHtml(t.term)}"`.

- [ ] **Step 4: G-3 — Promise.all в renderCallDetail**

Блок `app.js:496-512` (от `const session = await api(...)` до `isAdmin() ? ... transcript-variants`) заменить на:

```js
    const session = await api(`/api/sessions/${id}`);
    // Независимые куски карточки — параллельно (латентность = max, не сумма)
    const opt = (p) => p.catch(() => null);
    let [transcript, analysis, sentiment, extraction, kbTags, card, asrVariants] =
      await Promise.all([
        opt(api(`/api/sessions/${id}/transcript`)),
        opt(api(`/api/sessions/${id}/analysis`)),
        opt(api(`/api/sessions/${id}/sentiment`)),
        moduleOn('complexes') ? opt(api(`/api/sessions/${id}/extraction`)) : null,
        moduleOn('knowledge_base') ? api(`/api/sessions/${id}/tags`).catch(() => []) : [],
        opt(api(`/api/sessions/${id}/card`)),
        isAdmin() ? opt(api(`/api/sessions/${id}/transcript-variants`)) : null,  // ASR-сравнение (админ-тюнинг)
      ]);
```

`let` обязателен: дальше по функции переменные могут переприсваиваться. Семантика дефолтов сохранена: `kbTags → []`, остальные → `null`.

- [ ] **Step 5: H-4 — снять rogovestate-фолбэк**

`app.js:19`:
```js
const amoBase = () => features.amocrm_subdomain || null; // из company-config через /features; без субдомена ссылку не рендерим
```

`app.js:3139` — условие ссылки дополнить `amoBase()`:
```js
Текущая привязка: ${moduleOn('amocrm') && amoBase() ? `<a href="https://${amoBase()}/leads/detail/${currentLeadId}" target="_blank" rel="noopener">сделка #${currentLeadId}</a>` : `сделка #${currentLeadId}`}
```

`app.js:3186` — аналогично:
```js
${moduleOn('amocrm') && amoBase() ? `<a href="https://${amoBase()}/leads/detail/${leadId}" target="_blank" rel="noopener" onclick="event.stopPropagation()">https://${amoBase()}/leads/detail/${leadId}</a>` : `#${leadId}`}
```

- [ ] **Step 6: CI-чек для admin.js**

В `.github/workflows/tests.yml`, джоба `clients`: в команду `node --check` рядом с `backend/static/app.js backend/static/charts.js` добавить `backend/static/admin.js` (admin.js теперь редактируется — синтакс-чек должен покрывать и его).

- [ ] **Step 7: Верификация**

Run: `node --check backend/static/app.js && node --check backend/static/admin.js`
Expected: тишина (exit 0).

Run: `node --test 'backend/static/tests/*.test.mjs'`
Expected: 6/6 pass (charts-тесты не задеты).

Самопроверка агента: `grep -n "JSON.stringify" backend/static/app.js | grep onclick` — пусто; `grep -c rogovestate backend/static/app.js` — 0.

- [ ] **Step 8: Коммит (контроллер)**

`fix(front): C-1 quote-safe escapeHtml + data-атрибуты вместо инъекций в onclick, G-3 параллельная загрузка карточки, H-4 без rogovestate-фолбэка`

---

## Волны исполнения (SDD)

- **W1 = [Task 1, Task 2, Task 3] параллельно** — файловые множества дизъюнктны (worker/* ↔ backend/app/* ↔ backend/static/*; `.env.example` только у Task 2).
- Контроллер после волны: полные сьюты `backend` + `worker` + node, пер-таск ревью (Opus), коммиты по задаче, финальное ревью диффа ветки, push, CI.

## Definition of Done

- Все три задачи закоммичены, полные сьюты зелёные локально и в CI.
- `grep -ri anthropic worker/requirements.txt .env.example` — пусто.
- `/docs` на дефолтной конфигурации — 404; `ENABLE_API_DOCS=1` возвращает Swagger.
- Ни одного `JSON.stringify` внутри inline-обработчиков; `rogovestate` в app.js отсутствует.
- Каждый structured LLM-вызов несёт `max_output_tokens` и `prompt_cache_key`.
