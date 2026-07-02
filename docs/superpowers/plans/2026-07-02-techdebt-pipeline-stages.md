# SilentQA 2.0 Техдолг — План 3/3: Разрез пайплайна на стадии + очереди + fairness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Монолитная 60-90-минутная таска `pipeline.process_session` разрезается на 2 стадии: CPU-стадия (merge+гейты+ASR+диаризация, очередь `transcription`) и IO-стадия `pipeline.analyze_session` (KB+sentiment+LLM+AmoCRM, новая очередь `analysis`) — LLM-фаза перестаёт занимать CPU-слот; плюс per-tenant fairness-семафор (залповая загрузка одного тенанта не выстраивает всех в очередь), колонка `sessions.processing_started_at` и watchdog-правила 3a/3b для зависших `processing` — по метке фактического старта стадии, НЕ по `created_at` (иначе ложно убивались бы reprocess старых звонков и сессии, ждущие fairness-слот).

**Architecture:** Межэтапное состояние УЖЕ живёт на диске (`transcript_raw.json` → `transcript.json` → `sentiment.json` → `quality.json`) — стадии обмениваются только `session_id`/`audio_path`/`config` через kwargs, ничего тяжёлого в Redis. Разрез по шву после speaker-merge (`pipeline.py:636`): стадия 1 сохраняет `transcript.json` ДО KB-нормализации; analyze грузит его, нормализует (KB layer-1 идемпотентен — комментарий `pipeline.py:638`) и пересохраняет. Гейты (short-call/broken-recording) завершают сессию прямо в стадии 1 без handoff'а. `lead_lock` переезжает в analyze (только она трогает prior_context/AmoCRM). Fairness — N слотов тенанта в Redis (`SET NX EX`, паттерн `lead_lock`); нет слота → `task.retry(countdown)` — слот воркера освобождается для других тенантов. Ретраи analyze — только на транзиентных ошибках (`_is_transient` по имени класса исключения). Прогресс-UI не ломается: бэкенд поллит `sessions.status`, `task.update_state` никем не читается (в модели Session нет task_id — проверено). **Компат для AmoCRM:** `amocrm_poll.py:465/473` зовёт `_run_pipeline` синхронно и читает из результата `quality_report`/`use_extended` (плюс `test_amocrm_inbound_and_push.py:71` стабит это имя) — поэтому `_run_pipeline` СОХРАНЯЕТСЯ с прежним именем/сигнатурой/контрактом как синхронный полный прогон (гейты → транскрибация → `_analyze_inner` инлайн, без handoff), handoff-вариант — новая `_run_stage1`, общие гейты выносятся в `_run_gates`. `analyze_session` объявляется с `acks_late=True` + `reject_on_worker_lost=True`: стадия идемпотентна (вход — `transcript.json` с диска), убитый io-воркер не теряет задачу. Осознанная смена семантики диска: между стадиями `transcript.json` лежит НЕнормализованным (сегодня он пишется после KB) — analyze нормализует и пересохранит; окно «сырого» транскрипта в дашборде = время в очереди analysis.

**Tech Stack:** Celery 5.4 (Redis broker), две очереди воркеров (systemd), psycopg2 через `tenancy.db`, pytest со стабами (без Redis/Postgres).

**Роадмап-источник:** `~/reviews/silentqa-roadmap-2.0-3.0.md`, пункт 2.2 (+«Техдолг и риски» №3).

## Global Constraints

- Worker-тесты: `cd worker && python -m pytest` (pytest.ini ставит `pythonpath=.`), БЕЗ Redis/Postgres — всё через monkeypatch/стабы (идиома `test_kb_pipeline_wiring.py`, `test_tenant_propagation.py`).
- **Имена, которые НЕЛЬЗЯ ломать** (на них завязаны существующие тесты): `merge_chunks`, `save_results`, `_push_to_amocrm`, `_merge_kb_keyterms`, `_build_short_call_report`, `SHORT_CALL_THRESHOLD_SEC`, `_build_broken_recording_report`, `_detect_broken_recording`, `BROKEN_RECORDING_MIN_DURATION_SEC`, `SILENCE_DB_THRESHOLD`; сигнатура `process_session(self, session_id, config=None, tenant_schema=None)` и её fail-fast на пустой `tenant_schema`; **`_run_pipeline`** — имя, сигнатура И контракт возврата (`{"transcript_with_speakers", "quality_report", "use_extended"}`): его зовёт `worker/tasks/amocrm_poll.py:465/473` (прод realestate) и стабит `worker/tests/test_amocrm_inbound_and_push.py:71`; **`_process_session_body`** — имя/сигнатура (стабится `test_tenant_propagation.py:20`).
- Слот-семафор — ВНУТРИ `_process_session_body` (не в таск-обёртке): `test_tenant_propagation.py::test_process_session_sets_and_resets_context` стабит тело целиком и не должен полезть в Redis.
- Бэкенд: контракт входа прежний (`send_task("pipeline.process_session", queue="transcription")`); правки бэкенда точечные и только в Task 2 — колонка `processing_started_at` (модель + тенант-миграция) и её сброс в NULL при постановке в обработку (finish/reprocess/link-lead). Совместимость выката: старые in-flight сообщения в `transcription` исполняются новым кодом той же сигнатуры; миграция применяется деплой-шагом ДО рестарта воркеров (`deploy_prod.sh` / staging `ExecStartPre`), так что watchdog не увидит несуществующую колонку.
- Каждая новая Celery-таска обязана требовать `tenant_schema` kwarg и set/reset contextvar в try/finally (хаус-рул, fail-fast).
- `task.update_state(...)` в стадиях сохраняем (косметика для Flower/отладки; UI это не читает).
- Комментарии — на русском; conventional commits.
- Ops-шаги (Task 4-5): живые деплои не трогаем кроме описанного; при выкате в прод рестартовать воркеры ПОСЛЕ бэкенда не требуется (контракт entry-таски не менялся).

---

### Task 1: `worker/tasks/tenant_slots.py` — fairness-семафор

**Files:**
- Create: `worker/tasks/tenant_slots.py`
- Test: `worker/tests/test_tenant_slots.py` (создать)

**Interfaces:**
- Produces: `try_acquire(slug, *, client=None, max_slots=None, ttl=DEFAULT_TTL_SEC) -> tuple[str, str] | None` — занять один из N слотов тенанта (`tslot:{slug}:{i}`, `SET NX EX`); None = все заняты. `release(key, token, *, client=None)` — освобождение через compare-del Lua (как `lead_lock`). Env: `TENANT_MAX_CONCURRENT` (default 2), `TENANT_SLOT_TTL_SEC` (default 5700 — > `task_time_limit=5400`, та же логика, что у `lead_lock.DEFAULT_TIMEOUT_SEC`, `lead_lock.py:21-28`: TTL страхует утечку слота при жёстком падении воркера).
- Consumes: `redis` (тот же клиент-паттерн, что `lead_lock.py:38-43`), но дефолтный URL — как у брокера в `celery_app.py` (`redis://localhost:6381/0`), НЕ легаси-дефолт lead_lock (`6379/3`): рассинхрон дефолтов молча уводит семафор в другой Redis при незаданном `REDIS_URL`.

- [ ] **Step 1: Написать падающий тест**

Создать `worker/tests/test_tenant_slots.py`:

```python
"""Fairness-семафор: N слотов тенанта в Redis, compare-del release."""
import tasks.tenant_slots as ts


class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def eval(self, script, numkeys, key, token):
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


def test_acquire_up_to_limit_then_none():
    r = FakeRedis()
    s1 = ts.try_acquire("acme", client=r, max_slots=2)
    s2 = ts.try_acquire("acme", client=r, max_slots=2)
    assert s1 is not None and s2 is not None
    assert s1[0] != s2[0]                       # разные ключи-слоты
    assert ts.try_acquire("acme", client=r, max_slots=2) is None


def test_slots_are_per_tenant():
    r = FakeRedis()
    assert ts.try_acquire("acme", client=r, max_slots=1) is not None
    assert ts.try_acquire("globex", client=r, max_slots=1) is not None  # чужой лимит не мешает


def test_release_frees_slot():
    r = FakeRedis()
    key, token = ts.try_acquire("acme", client=r, max_slots=1)
    assert ts.try_acquire("acme", client=r, max_slots=1) is None
    ts.release(key, token, client=r)
    assert ts.try_acquire("acme", client=r, max_slots=1) is not None


def test_release_wrong_token_is_noop():
    r = FakeRedis()
    key, token = ts.try_acquire("acme", client=r, max_slots=1)
    ts.release(key, "чужой-токен", client=r)
    assert ts.try_acquire("acme", client=r, max_slots=1) is None  # слот всё ещё занят
```

- [ ] **Step 2: Прогнать — падает**

Run: `cd worker && python -m pytest tests/test_tenant_slots.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tasks.tenant_slots'`.

- [ ] **Step 3: Реализация `worker/tasks/tenant_slots.py`**

```python
"""Per-tenant fairness: лимит одновременных тяжёлых задач на тенанта.

N именованных слотов в Redis (SET NX EX). Задача без слота уходит в
self.retry с countdown — слот ВОРКЕРА освобождается для задач других
тенантов, а сама задача вернётся позже. TTL страхует утечку слота при
жёстком падении воркера (та же логика, что у lead_lock).
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import redis

logger = logging.getLogger(__name__)

# TTL > task_time_limit (5400s в celery_app.py) — иначе слот истечёт под
# живой длинной задачей и лимит перестанет работать ровно там, где нужен.
DEFAULT_TTL_SEC = int(os.getenv("TENANT_SLOT_TTL_SEC", "5700"))
DEFAULT_MAX_SLOTS = int(os.getenv("TENANT_MAX_CONCURRENT", "2"))

_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

_default_client: redis.Redis | None = None


def _get_default_client() -> redis.Redis:
    global _default_client
    if _default_client is None:
        # Дефолт = дефолт брокера (celery_app.py:13), НЕ легаси lead_lock (6379/3)
        url = os.getenv("REDIS_URL", "redis://localhost:6381/0")
        _default_client = redis.Redis.from_url(url)
    return _default_client


def try_acquire(slug: str, *, client: Any | None = None,
                max_slots: int | None = None,
                ttl: int = DEFAULT_TTL_SEC) -> tuple[str, str] | None:
    """Занять слот тенанта. None — все слоты заняты (вызывающий должен retry)."""
    redis_client = client or _get_default_client()
    n = max_slots if max_slots is not None else DEFAULT_MAX_SLOTS
    token = str(uuid.uuid4())
    for i in range(n):
        key = f"tslot:{slug}:{i}"
        if redis_client.set(key, token, nx=True, ex=ttl):
            logger.debug(f"tenant slot acquired: {key}")
            return key, token
    return None


def release(key: str, token: str, *, client: Any | None = None) -> None:
    """Освободить слот (compare-del: чужой токен не трогаем)."""
    redis_client = client or _get_default_client()
    try:
        redis_client.eval(_RELEASE_LUA, 1, key, token)
    except Exception:
        logger.exception(f"tenant slot release failed: {key}")
```

- [ ] **Step 4: Прогнать — зелёный**

Run: `cd worker && python -m pytest tests/test_tenant_slots.py -v`
Expected: PASS (4 теста).

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/tenant_slots.py worker/tests/test_tenant_slots.py
git commit -m "feat(worker): tenant_slots — per-tenant лимит одновременных тяжёлых задач"
```

---

### Task 2: `processing_started_at` + watchdog-правила 3a/3b (зависшие `processing` → `failed`)

**Files:**
- Create: тенант-миграция (`cd backend && alembic -c alembic.ini revision -m "sessions.processing_started_at"`)
- Modify: `backend/app/models.py` (Session), `backend/app/routes/sessions.py` (finish `:566` / reprocess `:368` / link-lead `:481`), `worker/tasks/pipeline.py` (`update_session_status`, `:149`), `worker/tasks/session_watchdog.py` (`_sweep_for_current_tenant`)
- Test: `worker/tests/test_watchdog_processing_stale.py` (создать), `backend/tests/test_processing_started_reset.py` (создать)

**Interfaces:**
- Produces: `sessions.processing_started_at TIMESTAMPTZ NULL` — момент фактического старта стадии 1 в воркере. Бэкенд СБРАСЫВАЕТ её в NULL везде, где ставит `status='processing'` при постановке в обработку; воркер проставляет `NOW()` внутри `update_session_status(sid, "processing")`. Watchdog-правила: **3a** — `processing AND processing_started_at IS NOT NULL AND processing_started_at < NOW()-6h → failed` (env `SESSION_PROCESSING_STALE_HOURS`, default 6 — щедро больше hard limit 90 мин + ретраи analyze); **3b** — `processing AND processing_started_at IS NULL AND created_at < NOW()-24h → failed` (env `SESSION_ENQUEUED_STALE_HOURS`, default 24 — задача потерялась до старта / вечное ожидание слота). Возврат `_sweep_for_current_tenant()` получает ключи `stale_processing: int` и `stale_enqueued: int`.
- **Почему НЕ по `created_at`:** reprocess старого звонка (продовая фича «переоценка по scenario_id») ставит `processing`, не трогая `created_at` (`sessions.py:368`), а finish ставит `processing` ДО `send_task` (`:566`) — сессия, ждущая fairness-слот, тоже стоит в `processing`. Правило по `created_at` убивало бы обоих первым же 5-минутным свипом. Сброс в NULL при постановке обязателен: без него у reprocess останется `processing_started_at` ПРЕДЫДУЩЕГО прогона (старше 6ч) и 3a убьёт сессию, пока она ждёт слот.
- Constraint: у модели Session нет ни `updated_at`, ни `started_at` (`models.py:22-37`) — без новой колонки правило опереть не на что. Watchdog-SQL с новой колонкой выкатывается тем же деплоем, что миграция: `deploy_prod.sh`/staging `ExecStartPre` применяют миграции ДО рестарта воркеров.

- [ ] **Step 1: Миграция + модель**

Тенант-дерево: `alembic -c alembic.ini revision -m "sessions.processing_started_at"`; в миграции `op.add_column("sessions", sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True))`, downgrade — `op.drop_column`. В `models.py` — колонка после `finished_at`. Применить и проверить:

Run: `cd backend && set -a; source ../.env; set +a; python -m app.migrate && python -m app.migrate --check` (если План 1 Task 2 ещё не сделан — без `--check`)
Expected: `[migrate] done` (+ `[migrate] heads OK`).

- [ ] **Step 2: Падающий backend-тест на сброс**

Создать `backend/tests/test_processing_started_reset.py` по паттерну существующих тестов reprocess (стаб `get_db`): подготовить сессию с непустым `processing_started_at`, дёрнуть `POST /api/sessions/{id}/reprocess` → `status == "processing"` И `processing_started_at is None`; аналогичная проверка для finish-эндпоинта.

Run: `cd backend && python -m pytest tests/test_processing_started_reset.py -v`
Expected: FAIL (колонка не сбрасывается).

- [ ] **Step 3: Бэкенд-сброс**

В `routes/sessions.py` рядом с каждой установкой `status = SessionStatus.processing` при постановке в обработку (finish `:566`, reprocess `:368`, link-lead `:481`) добавить `<session>.processing_started_at = None` (имя переменной сверить по месту).

Run: `cd backend && python -m pytest tests/test_processing_started_reset.py tests/ -v`
Expected: PASS все.

- [ ] **Step 4: Воркер проставляет метку старта**

В `worker/tasks/pipeline.py` `update_session_status` (`:149`): при `status == "processing"` дополнять UPDATE выражением `processing_started_at = NOW()`; при других статусах колонку не трогать. (Покрывает обе body-функции; amocrm-путь, если он ходит через `update_session_status`, получает метку автоматически.)

- [ ] **Step 5: Падающий watchdog-тест**

Создать `worker/tests/test_watchdog_processing_stale.py`:

```python
"""Watchdog rules 3a/3b: processing по processing_started_at, НЕ по created_at."""
import tasks.session_watchdog as sw


class FakeCursor:
    def __init__(self):
        self.executed = []
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.executed.append(s)
        if s.startswith("UPDATE sessions") and "'processing'" in s:
            if "processing_started_at IS NULL" in s:
                self._rows = [("lost-1",)]                 # 3b: один потерянный до старта
            else:
                self._rows = [("dead-1",), ("dead-2",)]    # 3a: два зависших после старта
        else:
            self._rows = []                                # rules 1-2: пусто

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

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_stale_rules(monkeypatch):
    cur = FakeCursor()
    monkeypatch.setattr(sw, "_get_sync_db_url", lambda: "postgresql://x")
    monkeypatch.setattr(sw, "tenant_connect", lambda: FakeConn(cur))

    out = sw._sweep_for_current_tenant()

    assert out["stale_processing"] == 2
    assert out["stale_enqueued"] == 1
    joined = " | ".join(cur.executed)
    # 3a — по метке старта стадии, НЕ по created_at
    assert "processing_started_at IS NOT NULL" in joined
    assert "processing_started_at <" in joined
    # 3b — потерянные до старта: по created_at, но ТОЛЬКО при IS NULL
    assert "processing_started_at IS NULL" in joined
    assert "'failed'" in joined


def test_default_thresholds():
    assert sw.PROCESSING_STALE_HOURS == 6
    assert sw.ENQUEUED_STALE_HOURS == 24
```

Run: `cd worker && python -m pytest tests/test_watchdog_processing_stale.py -v`
Expected: FAIL — `AttributeError: module 'tasks.session_watchdog' has no attribute 'PROCESSING_STALE_HOURS'`.

- [ ] **Step 6: Watchdog-реализация**

В `worker/tasks/session_watchdog.py` после `CREATED_STALE_MIN = ...` (строка 27) добавить:

```python
# Rule 3a: стадия стартовала (processing_started_at проставлен воркером) и висит
# дольше N часов — воркер умер / задача потерялась. Порог щедрый: > hard limit
# 90 мин + ретраи analyze. НЕ по created_at: reprocess старых звонков и
# ожидание fairness-слота не должны попадать под нож.
PROCESSING_STALE_HOURS = int(os.getenv("SESSION_PROCESSING_STALE_HOURS", "6"))
# Rule 3b: в processing, но стадия так и не стартовала (NULL) — задача потеряна
# до старта. Порог суточный: ожидание слота при залпе легитимно длится часами.
ENQUEUED_STALE_HOURS = int(os.getenv("SESSION_ENQUEUED_STALE_HOURS", "24"))
```

В `_sweep_for_current_tenant`: инициализацию результатов (строки 39-40) дополнить `stale_processing: list[str] = []` и `stale_enqueued: list[str] = []`; внутри курсора ПОСЛЕ цикла «Fail empty sessions» (после строки 93, `now` уже определён на строке 86) добавить:

```python
                # 3a. Стадия стартовала и висит > N часов → failed
                cur.execute(
                    """
                    UPDATE sessions
                    SET status = 'failed', finished_at = %s
                    WHERE status = 'processing'
                      AND processing_started_at IS NOT NULL
                      AND processing_started_at < NOW() - (%s || ' hours')::interval
                    RETURNING id::text
                    """,
                    (now, PROCESSING_STALE_HOURS),
                )
                stale_processing = [r[0] for r in cur.fetchall()]

                # 3b. Поставлена в обработку, но стадия не стартовала > N часов → failed
                cur.execute(
                    """
                    UPDATE sessions
                    SET status = 'failed', finished_at = %s
                    WHERE status = 'processing'
                      AND processing_started_at IS NULL
                      AND created_at < NOW() - (%s || ' hours')::interval
                    RETURNING id::text
                    """,
                    (now, ENQUEUED_STALE_HOURS),
                )
                stale_enqueued = [r[0] for r in cur.fetchall()]
```

После цикла логирования `failed` (строка 112-113) добавить warning на каждый sid обоих списков (по образцу существующего). Оба `return` в функции дополнить ключами: успешный → `"stale_processing": len(stale_processing), "stale_enqueued": len(stale_enqueued)`, ошибочный (строка 97) → нулями.

- [ ] **Step 7: Прогнать оба сьюта**

Run: `cd worker && python -m pytest tests/test_watchdog_processing_stale.py tests/ -v && cd ../backend && python -m pytest tests/`
Expected: PASS все.

- [ ] **Step 8: Commit**

```bash
git add backend/alembic/versions/ backend/app/models.py backend/app/routes/sessions.py \
        backend/tests/test_processing_started_reset.py \
        worker/tasks/pipeline.py worker/tasks/session_watchdog.py \
        worker/tests/test_watchdog_processing_stale.py
git commit -m "feat(watchdog): processing_started_at + правила 3a/3b — зависшие processing без ложных убийств reprocess"
```

---

### Task 3: Разрез `pipeline.py`: стадия 1 (CPU) + `analyze_session` (IO) + ретраи + слоты

**Files:**
- Modify: `worker/tasks/pipeline.py` (крупная реструктуризация, детали ниже)
- Modify: `worker/tasks/celery_app.py:41-44` (очередь `analysis`)
- Test: `worker/tests/test_pipeline_stages.py` (создать)

**Interfaces:**
- Produces:
  - Таска `pipeline.analyze_session(self, session_id, audio_path, config=None, tenant_schema=None)`, `queue="analysis"` — KB-нормализация → extraction-ветка ИЛИ sentiment→quality→card→plan→AmoCRM → `completed`. Fail-fast на пустой `tenant_schema` (как `process_session`).
  - `_run_gates(...) -> dict | None` — вынесенные гейты (short-call/broken-recording, бывшие строки 554-594): прежний гейт-результат или `None`; зовётся и из `_run_stage1`, и из `_run_pipeline`.
  - `_run_stage1(...)` (сигнатура = `_run_pipeline`) — гейт-результат или ASR+диаризация+merge+`save_results(..., "transcript", ...)` + handoff: возвращает `{"handed_off": True}`.
  - `_run_pipeline(...)` — **СОХРАНЯЕТСЯ** (имя/сигнатура/контракт — API `amocrm_poll.py:473`): гейт-результат или синхронный полный прогон `_transcribe_and_merge` → `save_results` → `lead_lock` → `_analyze_inner`, возврат как сегодня.
  - `analyze_session` — с `acks_late=True, reject_on_worker_lost=True` (идемпотентная IO-стадия, at-least-once безопасен).
  - `_transcribe_and_merge(task, session_id, audio_path, company_config) -> list` — бывшие шаги 2-4.
  - `_analyze_inner(...)` — бывший `_run_pipeline_inner` без шагов 2-4.
  - `_is_transient(exc) -> bool` — транзиентность по имени класса исключения.
  - Слот-семафор в начале `_process_session_body`/`_process_session_from_file_body`; `TENANT_SLOT_RETRY_SEC` (env, default 60).
- Consumes: `try_acquire`/`release` из Task 1; `get_tenant_schema` из `tenancy.context` (для kwargs handoff'а).
- Constraint: `_run_pipeline_inner` вне pipeline.py не используется — свободен к переименованию в `_analyze_inner`. **`_run_pipeline` — НЕ свободен**: его зовёт `amocrm_poll.py:465/473` (читает `quality_report`/`use_extended` из результата) и стабит `test_amocrm_inbound_and_push.py:71` — сохраняется как синхронная полная обёртка (см. 4c). Список «неломаемых» имён — в Global Constraints.

- [ ] **Step 1: Написать падающие тесты**

Создать `worker/tests/test_pipeline_stages.py`:

```python
"""Разрез пайплайна: stage1 → handoff в analysis; гейты завершают сами; ретраи."""
import json

import pytest

import tasks.pipeline as pl
from tenancy.context import reset_tenant_schema, set_tenant_schema


class RetryCalled(Exception):
    def __init__(self, countdown=None):
        self.countdown = countdown


class StubTask:
    def __init__(self, retries=0):
        self.request = type("R", (), {"retries": retries})()

    def update_state(self, state=None, meta=None):
        pass

    def retry(self, countdown=None, exc=None, max_retries=None):
        raise RetryCalled(countdown)


@pytest.fixture
def tenant_ctx(tmp_path, monkeypatch):
    token = set_tenant_schema("t_acme")
    monkeypatch.setattr(pl, "RESULTS_PATH", str(tmp_path / "results"))
    monkeypatch.setattr(pl, "AUDIO_PATH", str(tmp_path / "audio"))
    statuses = []
    monkeypatch.setattr(pl, "update_session_status",
                        lambda sid, st, **kw: statuses.append(st))
    monkeypatch.setattr(pl, "_get_session_metadata", lambda sid: {})
    monkeypatch.setattr(pl, "try_acquire", lambda slug: ("k", "t"))
    monkeypatch.setattr(pl, "release", lambda key, token: None)
    monkeypatch.setattr(pl, "tenant_company_config_id", lambda: "default")
    monkeypatch.setattr(pl, "load_company_config", lambda cid: {"id": cid})
    monkeypatch.setattr(pl, "get_scenario", lambda cfg, sid: None)
    monkeypatch.setattr(pl, "get_default_scenario_id", lambda cfg: None)
    yield statuses
    reset_tenant_schema(token)


def _spy_send_task(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pl.app, "send_task",
        lambda name, kwargs=None, queue=None, **kw: calls.append(
            {"name": name, "kwargs": kwargs, "queue": queue}))
    return calls


def _results_dir(sid):
    return pl.tenant_results_dir(pl.RESULTS_PATH, "acme", sid)


def test_short_call_completes_without_handoff(tenant_ctx, monkeypatch, tmp_path):
    statuses = tenant_ctx
    calls = _spy_send_task(monkeypatch)
    monkeypatch.setattr(pl, "merge_chunks", lambda sid: str(tmp_path / "full.wav"))
    monkeypatch.setattr(pl, "_get_audio_duration", lambda p: 12.0)  # < 20с

    result = pl._process_session_body(StubTask(), "sid-1", None)

    assert result["status"] == "completed"
    assert statuses == ["processing", "completed"]
    assert calls == []                                   # analyze НЕ ставился
    q = json.loads((_results_dir("sid-1") / "quality.json").read_text())
    assert q["skip_reason"] == "too_short"


def test_full_call_hands_off_to_analysis(tenant_ctx, monkeypatch, tmp_path):
    statuses = tenant_ctx
    calls = _spy_send_task(monkeypatch)
    monkeypatch.setattr(pl, "merge_chunks", lambda sid: str(tmp_path / "full.wav"))
    monkeypatch.setattr(pl, "_get_audio_duration", lambda p: 300.0)
    monkeypatch.setattr(pl, "_detect_broken_recording", lambda p, d: None)
    monkeypatch.setattr(
        pl, "_transcribe_and_merge",
        lambda task, sid, path, cfg: [{"start": 0, "end": 1, "text": "х", "speaker": "S1"}])

    result = pl._process_session_body(StubTask(), "sid-2", None)

    assert result["status"] == "analyzing"
    assert statuses == ["processing"]                    # completed поставит analyze
    assert len(calls) == 1
    assert calls[0]["name"] == "pipeline.analyze_session"
    assert calls[0]["queue"] == "analysis"
    assert calls[0]["kwargs"]["tenant_schema"] == "t_acme"
    assert calls[0]["kwargs"]["session_id"] == "sid-2"
    assert (_results_dir("sid-2") / "transcript.json").exists()


def test_no_slot_retries(tenant_ctx, monkeypatch):
    monkeypatch.setattr(pl, "try_acquire", lambda slug: None)
    with pytest.raises(RetryCalled):
        pl._process_session_body(StubTask(), "sid-3", None)
    assert tenant_ctx == []                              # статус не трогали


def _stub_analyze_deps(monkeypatch):
    import tasks.card as card_mod
    import tasks.knowledge_base as kb
    monkeypatch.setattr(kb, "kb_build_matcher", lambda: None)
    monkeypatch.setattr(kb, "normalize_transcript", lambda t, m: (t, []))
    monkeypatch.setattr(kb, "kb_record_mentions", lambda sid, hits: None)
    monkeypatch.setattr(pl, "_kb_glossary_safe", lambda: "")
    monkeypatch.setattr(pl, "_load_template_kind", lambda t: (None, "evaluation"))
    monkeypatch.setattr(pl, "get_protocol", lambda c: None)
    monkeypatch.setattr(pl, "get_custom_prompt", lambda c: None)
    monkeypatch.setattr(pl, "analyze_sentiment", lambda t: [])
    monkeypatch.setattr(pl, "assess_quality", lambda *a, **kw: {"overall_score": 7})
    monkeypatch.setattr(card_mod, "run_card_extraction", lambda *a: None)
    monkeypatch.setattr(card_mod, "has_card_extraction", lambda *a: False)
    monkeypatch.setattr(pl, "tenant_amocrm_enabled", lambda slug: False)
    monkeypatch.setattr(pl, "_get_audio_duration", lambda p: 300.0)
    monkeypatch.setattr(pl, "_get_session_created_at", lambda sid: None)


def _write_transcript(sid):
    rd = _results_dir(sid)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "transcript.json").write_text(
        '[{"start": 0, "end": 1, "text": "х", "speaker": "S1"}]', encoding="utf-8")
    return rd


def test_analyze_body_completes(tenant_ctx, monkeypatch, tmp_path):
    statuses = tenant_ctx
    rd = _write_transcript("sid-4")
    _stub_analyze_deps(monkeypatch)

    result = pl._analyze_session_body(StubTask(), "sid-4", str(tmp_path / "full.wav"), None)

    assert result["status"] == "completed"
    assert statuses[-1] == "completed"
    assert json.loads((rd / "quality.json").read_text())["overall_score"] == 7


def test_analyze_transient_error_retries(tenant_ctx, monkeypatch, tmp_path):
    statuses = tenant_ctx
    _write_transcript("sid-5")
    _stub_analyze_deps(monkeypatch)

    class APITimeoutError(Exception):
        pass

    def boom(t):
        raise APITimeoutError("connect timeout")

    monkeypatch.setattr(pl, "analyze_sentiment", boom)
    with pytest.raises(RetryCalled):
        pl._analyze_session_body(StubTask(retries=0), "sid-5", str(tmp_path / "f.wav"), None)
    assert "failed" not in statuses                      # ретрай ≠ провал


def test_analyze_deterministic_error_fails(tenant_ctx, monkeypatch, tmp_path):
    statuses = tenant_ctx
    _write_transcript("sid-6")
    _stub_analyze_deps(monkeypatch)

    def boom(t):
        raise ValueError("bad schema")

    monkeypatch.setattr(pl, "analyze_sentiment", boom)
    with pytest.raises(ValueError):
        pl._analyze_session_body(StubTask(), "sid-6", str(tmp_path / "f.wav"), None)
    assert statuses[-1] == "failed"


def test_is_transient_classification():
    class APITimeoutError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    class ValidationError(Exception):
        pass

    assert pl._is_transient(APITimeoutError())
    assert pl._is_transient(RateLimitError())
    assert pl._is_transient(APIConnectionError())
    assert pl._is_transient(TimeoutError())
    assert pl._is_transient(ConnectionError())
    assert not pl._is_transient(ValidationError())
    assert not pl._is_transient(ValueError())


def test_run_pipeline_compat_for_amocrm(tenant_ctx, monkeypatch, tmp_path):
    # amocrm_poll.py:473 зовёт _run_pipeline напрямую и читает
    # quality_report/use_extended — синхронный контракт сохранён, handoff'а нет.
    import contextlib
    calls = _spy_send_task(monkeypatch)
    _stub_analyze_deps(monkeypatch)
    monkeypatch.setattr(pl, "_detect_broken_recording", lambda p, d: None)
    monkeypatch.setattr(pl, "lead_lock", lambda _lid: contextlib.nullcontext())
    monkeypatch.setattr(
        pl, "_transcribe_and_merge",
        lambda task, sid, path, cfg: [{"start": 0, "end": 1, "text": "х", "speaker": "S1"}])

    # сигнатуру вызова сверить с фактической _run_pipeline (планом не меняется)
    result = pl._run_pipeline(StubTask(), "sid-8", str(tmp_path / "f.wav"), {},
                              {"id": "default"}, None, {})

    assert calls == []                                    # БЕЗ handoff
    assert "quality_report" in result and "use_extended" in result
    assert "transcript_with_speakers" in result
```

- [ ] **Step 2: Прогнать — падает**

Run: `cd worker && python -m pytest tests/test_pipeline_stages.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'try_acquire'` (и далее `_transcribe_and_merge`, `_analyze_session_body`, `_is_transient`).

- [ ] **Step 3: celery_app — очередь `analysis`**

В `worker/tasks/celery_app.py` в `task_queues` (строки 41-44) добавить:

```python
        "analysis": {"exchange": "analysis", "routing_key": "analysis"},
```

- [ ] **Step 4: pipeline.py — реструктуризация**

4a. Импорты: к строке 18 (`from tenancy.context import ...`) добавить `get_tenant_schema`; после строки 37 (`from tasks.lead_lock import lead_lock`) добавить:

```python
from tasks.tenant_slots import try_acquire, release
```

После `SHORT_CALL_THRESHOLD_SEC = 20` (строка 41) добавить:

```python
# Нет свободного слота тенанта → retry с этим countdown (слот воркера
# освобождается для других тенантов).
TENANT_SLOT_RETRY_SEC = int(os.getenv("TENANT_SLOT_RETRY_SEC", "60"))

# Транзиентные ошибки (сеть/лимиты OpenAI, AmoCRM, httpx) — кандидаты на
# retry analyze-стадии. Матчим по имени класса, чтобы не тащить импорты
# всех клиентских SDK.
_TRANSIENT_MARKERS = ("Timeout", "Connection", "RateLimit",
                      "ServiceUnavailable", "InternalServerError", "TryAgain")


def _is_transient(exc: BaseException) -> bool:
    name = type(exc).__name__
    return any(m in name for m in _TRANSIENT_MARKERS)
```

4b. Выделить шаги 2-4 в `_transcribe_and_merge`. Новая функция — тело составляют **строки 604-636 без изменений** (от `transcript_path = tenant_results_dir(...)` до `transcript_with_speakers = merge_transcript_with_speakers(transcript, diarization)` включительно, вместе с веткой кэша и веткой `has_speakers`):

```python
def _transcribe_and_merge(task, session_id: str, audio_path: str, company_config: dict) -> list:
    """Шаги 2-4 (CPU): ASR (с кэшем transcript.json при reprocess) + диаризация + merge."""
    # <-- сюда переносятся строки 604-636 текущего _run_pipeline_inner без правок -->
    return transcript_with_speakers
```

4c. `_run_pipeline` (строки 549-599) разрезать на ТРИ функции (имя `_run_pipeline` сохраняется — это API amocrm_poll):

- `_run_gates(...)` — вынести гейты (строки 554-594) как есть, сигнатура та же, что у `_run_pipeline`; возврат — прежний гейт-результат или `None`.
- `_run_stage1(...)` — сигнатура та же: `gate = _run_gates(...)`; если `gate is not None` — вернуть его; иначе:

```python
    # === 2-4 (CPU): транскрипция + диаризация + merge ===
    transcript_with_speakers = _transcribe_and_merge(task, session_id, audio_path, company_config)
    # transcript.json ДО KB-нормализации: analyze нормализует идемпотентно
    # (KB layer-1, см. комментарий в _analyze_inner) и пересохранит.
    save_results(session_id, "transcript", transcript_with_speakers)

    # Handoff: CPU-часть кончилась; LLM/AmoCRM уходят на IO-очередь analysis,
    # не занимая слот тяжёлого воркера.
    app.send_task(
        "pipeline.analyze_session",
        kwargs={
            "session_id": session_id,
            "audio_path": audio_path,
            "config": config,
            "tenant_schema": get_tenant_schema(),
        },
        queue="analysis",
    )
    return {"handed_off": True}
```

- `_run_pipeline(...)` — прежние имя и сигнатура (сверить 1-в-1 по строке 549; если `audio_duration` приходит параметром — прокинуть его в `_run_gates`), тело:

```python
def _run_pipeline(task, session_id, audio_path, config, company_config, scenario, session_meta):
    """Синхронный полный прогон БЕЗ handoff — путь AmoCRM-поллинга
    (amocrm_poll.py:473 читает quality_report/use_extended из результата).
    Контракт возврата НЕ менять."""
    gate = _run_gates(task, session_id, audio_path, config, company_config, scenario, session_meta)
    if gate is not None:
        return gate
    transcript_with_speakers = _transcribe_and_merge(task, session_id, audio_path, company_config)
    save_results(session_id, "transcript", transcript_with_speakers)
    with lead_lock(session_meta.get("lead_id")):
        return _analyze_inner(task, session_id, audio_path, config, company_config,
                              scenario, session_meta, transcript_with_speakers)
```

4d. `_run_pipeline_inner` (строки 602-832) переименовать в `_analyze_inner`, сигнатура:

```python
def _analyze_inner(task, session_id: str, audio_path: str, config: dict, company_config: dict,
                   scenario: dict | None, session_meta: dict, transcript_with_speakers: list):
    """KB → (extraction | sentiment → quality → card → plan → AmoCRM). Вызывается под lead_lock."""
```

Из тела УДАЛИТЬ строки 604-636 (ушли в `_transcribe_and_merge`; параметр `audio_duration` тоже удаляется — в оставшемся коде не используется). Тело начинается сразу с KB-нормализации (бывшая строка 638, `# KB layer-1: ...`); всё остальное (строки 638-832) — без изменений.

4e. Новая analyze-таска и её тело (добавить после `_analyze_inner`):

```python
def _analyze_session_body(task, session_id: str, audio_path: str, config: dict | None = None):
    """IO-стадия: грузит transcript.json со стадии 1 и доводит сессию до completed."""
    config = config or {}
    logger.info(f"[{session_id}] Analyze stage starting...")
    start_time = datetime.now(timezone.utc)

    session_meta = _get_session_metadata(session_id)
    company_id = config.get("company_id") or tenant_company_config_id()
    scenario_id = config.get("scenario_id")
    company_config = load_company_config(company_id)
    scenario = get_scenario(company_config, scenario_id or get_default_scenario_id(company_config))

    try:
        transcript_path = tenant_results_dir(RESULTS_PATH, require_tenant_slug(), session_id) / "transcript.json"
        with transcript_path.open(encoding="utf-8") as f:
            transcript_with_speakers = json.load(f)

        # lead_lock здесь, а не в стадии 1: только analyze трогает
        # prior_context/метаданные лида/AmoCRM.
        with lead_lock(session_meta.get("lead_id")):
            result = _analyze_inner(task, session_id, audio_path, config, company_config,
                                    scenario, session_meta, transcript_with_speakers)

        finished_at = datetime.now(timezone.utc)
        audio_duration = _get_audio_duration(audio_path)
        processing_duration = (finished_at - start_time).total_seconds()
        update_session_status(
            session_id, "completed",
            finished_at=finished_at,
            duration_seconds=audio_duration or processing_duration,
        )
        logger.info(f"[{session_id}] Analyze stage completed!")

        tws = result["transcript_with_speakers"]
        return {
            "session_id": session_id,
            "status": "completed",
            "segments_count": len(tws),
            "speakers_count": len(set(s.get("speaker", "") for s in tws)),
            "quality_score": result["quality_report"].get("overall_score"),
        }

    except Exception as e:
        if _is_transient(e) and task.request.retries < 2:
            logger.warning(f"[{session_id}] Analyze transient failure (retry {task.request.retries + 1}/2): {e}")
            raise task.retry(countdown=120, exc=e)
        update_session_status(session_id, "failed", finished_at=datetime.now(timezone.utc))
        logger.exception(f"[{session_id}] Analyze stage failed: {e}")
        raise


# acks_late: analyze идемпотентна (вход — transcript.json с диска) → at-least-once
# безопасен; убитый io-воркер не теряет задачу (ре-доставка после
# visibility_timeout Redis-брокера, по умолчанию 1 час).
@app.task(bind=True, queue="analysis", name="pipeline.analyze_session",
          acks_late=True, reject_on_worker_lost=True)
def analyze_session(self, session_id: str, audio_path: str, config: dict | None = None,
                    tenant_schema: str | None = None):
    if not tenant_schema:
        raise ValueError("tenant_schema is required (fail fast: a task without "
                         "tenant context would read/write the wrong schema)")
    token = set_tenant_schema(tenant_schema)
    try:
        return _analyze_session_body(self, session_id, audio_path, config)
    finally:
        reset_tenant_schema(token)
```

4f. `_process_session_body` (строки 835-885): обернуть в слот и поменять хвост. Новое тело:

```python
def _process_session_body(task, session_id: str, config: dict | None = None):
    """Стадия 1 для browser-записей: merge чанков + гейты + ASR + handoff."""
    config = config or {}
    # Fairness: не больше TENANT_MAX_CONCURRENT тяжёлых задач на тенанта.
    slot = try_acquire(require_tenant_slug())
    if slot is None:
        raise task.retry(countdown=TENANT_SLOT_RETRY_SEC, max_retries=None)
    try:
        logger.info(f"[{session_id}] Starting processing pipeline...")
        start_time = datetime.now(timezone.utc)
        update_session_status(session_id, "processing")

        session_meta = _get_session_metadata(session_id)
        # company/scenario — server-owned (спека 5.6): из клиентских метаданных
        # сессии НЕ читаются. company — из shared.tenants, scenario — только из
        # явного config (ops-скрипты/reprocess).
        company_id = config.get("company_id") or tenant_company_config_id()
        scenario_id = config.get("scenario_id")
        company_config = load_company_config(company_id)
        scenario = get_scenario(company_config, scenario_id or get_default_scenario_id(company_config))
        logger.info(f"[{session_id}] Company: {company_config.get('name', company_id)}, "
                    f"Scenario: {scenario.get('name') if scenario else 'default'}")

        try:
            task.update_state(state="PROGRESS", meta={"step": "merging", "progress": 5})
            audio_path = merge_chunks(session_id)

            result = _run_stage1(task, session_id, audio_path, config, company_config, scenario, session_meta)

            if result.get("handed_off"):
                logger.info(f"[{session_id}] Stage 1 done — analyze queued")
                return {"session_id": session_id, "status": "analyzing"}

            # Гейт (short-call / broken recording) уже сохранил quality.json —
            # завершаем сессию прямо здесь, analyze не нужен.
            finished_at = datetime.now(timezone.utc)
            audio_duration = _get_audio_duration(audio_path)
            processing_duration = (finished_at - start_time).total_seconds()
            update_session_status(
                session_id, "completed",
                finished_at=finished_at,
                duration_seconds=audio_duration or processing_duration,
            )
            return {
                "session_id": session_id,
                "status": "completed",
                "segments_count": 0,
                "speakers_count": 0,
                "quality_score": result["quality_report"].get("overall_score"),
            }
        except Exception as e:
            update_session_status(session_id, "failed", finished_at=datetime.now(timezone.utc))
            logger.exception(f"[{session_id}] Pipeline stage 1 failed: {e}")
            raise
    finally:
        if slot is not None:
            release(*slot)
```

4g. `_process_session_from_file_body` (строки 901-943) — та же трансформация: слот в начале, `_run_stage1` вместо `_run_pipeline`, ветка `handed_off` → return `{"session_id": ..., "status": "analyzing"}`, гейт-ветка завершает как в 4f (без `merge_chunks` — `audio_path` приходит параметром).

Таск-обёртки `process_session` (888-898) и `process_session_from_file` (946-956) — БЕЗ изменений.

- [ ] **Step 5: Прогнать — зелёный + весь worker-сьют**

Run: `cd worker && python -m pytest tests/test_pipeline_stages.py -v && python -m pytest`
Expected: PASS все, включая нетронутые `test_short_call.py`, `test_broken_recording_detection.py`, `test_tenant_propagation.py`, `test_pipeline_card.py`, `test_push_tenant_gate.py`, `test_amocrm_inbound_and_push.py` (контракт `_run_pipeline`).

- [ ] **Step 6: Commit**

```bash
git add worker/tasks/pipeline.py worker/tasks/celery_app.py worker/tests/test_pipeline_stages.py
git commit -m "feat(pipeline): разрез на CPU-стадию и analyze_session (очередь analysis) + слоты + ретраи"
```

---

### Task 4: Ops — воркер под `analysis`, run.sh, деплой

**Files:**
- Modify: `run.sh` (queues дев-воркера)
- Modify: `scripts/deploy_prod.sh` (рестарт нового юнита; создан Планом 1 Task 5)
- Create (на боксе): `/etc/systemd/system/silentqa-worker-io.service` (+ staging-аналог)

**Interfaces:**
- Produces: прод: `silentqa-worker` (без изменений: `-Q default,transcription -B --concurrency=2`) + НОВЫЙ `silentqa-worker-io` (`-Q analysis --concurrency=4`). Дев: один воркер на все очереди. Стейджинг: `silentqa-staging-worker-io` по образцу.
- Осознанный компромисс (зафиксировать): ASR-стадия остаётся на очереди `transcription` даже для облачного AssemblyAI — CPU-слот занят на время облачного polling'а. Разводка ASR по движкам — отдельная итерация (после ElevenLabs-адаптера, роадмап 2.6).

- [ ] **Step 1: run.sh — дев-воркер слушает все очереди**

В `run.sh` в обеих celery-командах (`start_worker` и `start_all`) заменить `-Q default,transcription` на `-Q default,transcription,analysis`.

Run: `bash -n run.sh`
Expected: exit 0.

- [ ] **Step 2: Прод-юнит `/etc/systemd/system/silentqa-worker-io.service`**

```ini
[Unit]
Description=SilentQA Worker IO (celery, analysis queue)
After=network.target postgresql.service redis-server.service
Requires=postgresql.service redis-server.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/projects/silentqa/worker
EnvironmentFile=/root/projects/silentqa/.env
Environment=PATH=/root/projects/silentqa/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=VIRTUAL_ENV=/root/projects/silentqa/.venv
# IO-стадия: LLM/AmoCRM/KB — без тяжёлых моделей, поэтому concurrency 4
# и щадящий max-tasks-per-child
ExecStart=/root/projects/silentqa/.venv/bin/celery -A tasks.celery_app worker --loglevel=info --concurrency=4 --max-tasks-per-child=50 -Q analysis
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now silentqa-worker-io
journalctl -u silentqa-worker-io -n 10   # celery ready, queues: analysis
```

Аналогичный `/etc/systemd/system/silentqa-staging-worker-io.service` — WorkingDirectory/EnvironmentFile/PATH/VIRTUAL_ENV → `/root/projects/silentqa-dev` + `.env.staging`, `--concurrency=2`.

- [ ] **Step 3: deploy_prod.sh — рестартовать оба воркера**

В `scripts/deploy_prod.sh` строку `systemctl restart silentqa-backend silentqa-worker` заменить на:

```bash
systemctl restart silentqa-backend silentqa-worker silentqa-worker-io
```

- [ ] **Step 4: Commit**

```bash
git add run.sh scripts/deploy_prod.sh
git commit -m "ops(worker): очередь analysis — отдельный io-воркер, дев-воркер слушает все очереди"
```

---

### Task 5: E2E-смоук на стейджинге

**Interfaces:**
- Consumes: стейджинг из Плана 1 Task 7 (`:8008`, тенант `stagetest`), юниты из Task 4.

- [ ] **Step 1: Живой прогон**

1. Загрузить аудио-файл >20 сек через дашборд stagetest (`#upload`) или `POST /api/sessions` + chunks + finish с API-ключом.
2. `journalctl -u silentqa-staging-worker -f` — видно стадию 1: merge → transcribe → «Stage 1 done — analyze queued».
3. `journalctl -u silentqa-staging-worker-io -f` — видно analyze: sentiment → quality → «Analyze stage completed».
4. В дашборде сессия `completed`, транскрипт и оценка на месте; на диске `transcript.json`, `sentiment.json`, `quality.json`.

- [ ] **Step 2: Смоук отказоустойчивости**

1. Запустить обработку файла и в момент analyze-стадии `systemctl kill -s KILL silentqa-staging-worker-io` → `systemctl start` — у `analyze_session` `acks_late=True`: задача ре-доставится (при рестарте воркера или после visibility_timeout брокера, деф. 1ч) и, будучи идемпотентной, добежит до `completed`; страховка сверху — watchdog-правило 3a. Фактическое время ре-доставки зафиксировать в ранбуке.
2. Короткий файл (<20 сек) → сессия завершается стадией 1 со `skip_reason=too_short`, в analysis-очередь ничего не падает (проверить `journalctl -u silentqa-staging-worker-io`).
3. Залповая загрузка 5 файлов → в `journalctl` видно слот-ретраи (`Retry in 60s`) и то, что одновременно в работе не больше `TENANT_MAX_CONCURRENT=2` стадий-1 этого тенанта.
4. Переоценка старого звонка: сессия с `created_at` старше 6ч (подправить в БД при необходимости) → reprocess из дашборда → доходит до `completed`, watchdog её НЕ пометил `failed` (3a ключуется по `processing_started_at`, 3b не сработал: сброс в NULL + быстрый старт стадии).

- [ ] **Step 3: Прод-выкат**

`scripts/deploy_prod.sh` (уже рестартует все три юнита) + контроль: обработать один реальный звонок целиком, `journalctl -u silentqa-worker-io` показывает analyze-стадии.

---

## Порядок и зависимости

1 → 3 (pipeline импортирует tenant_slots); 2 — ДО 3 (правила 3a/3b страхуют потерянные analyze-задачи; его миграция едет деплой-шагом ДО рестарта воркеров — `deploy_prod.sh` так устроен); 4 — после 3; 5 — последним. Прод-выкат: сначала юнит Task 4 Step 2 (io-воркер должен слушать `analysis` ДО того, как туда полетят первые handoff'ы), затем `deploy_prod.sh`.

## Verification (итоговая)

- `cd worker && python -m pytest` и `cd backend && python -m pytest tests/` — зелёные целиком (в т.ч. `test_amocrm_inbound_and_push.py` — контракт `_run_pipeline` сохранён, и `test_tenant_propagation.py`).
- Смоук Task 5: полный звонок проходит обе стадии на двух разных воркерах; короткий — завершается в одной; залп — упирается в слоты, чужие тенанты не ждут; reprocess старого звонка НЕ убивается watchdog'ом.
- `psql`: нет сессий с `processing_started_at` старше 6ч в `processing` (3a) и NULL-ждунов старше 24ч (3b).
