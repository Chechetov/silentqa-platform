# Дашборд руководителя (quality_results + аналитика) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Результаты качества пишутся в тенант-таблицу `quality_results` (двойная запись: файл остаётся истиной для drill-down), поверх — `/api/stats/*`, страница `#dashboard` с трендами/возражениями/рисковыми звонками, talk-ratio, Telegram-алерт «рисковый звонок», починка мёртвого `avg_score` и бэкфилл истории.

**Architecture:** Worker после сохранения `quality.json` (и в двух гейтах) зовёт одну best-effort функцию `record_quality_result(...)` (`worker/tasks/results_db.py`): чистые функции считают objections/talk-metrics/risk-flags, upsert `ON CONFLICT (session_id) DO UPDATE` через `tenancy.db.tenant_connect()`, при рисковых флагах — Telegram-алерт (`worker/tasks/alerts.py`). Backend читает таблицу `text()`-запросами в новом роутере `routes/stats.py` (идиома: monkeypatch-able `_rows_*` хелперы + чистые shaping-функции); `routes/managers.py` переводится с in-memory скана на SQL. Фронт: `static/charts.js` (чистые функции → SVG-строки, node-тестируемые) + страница `#dashboard`; talk-ratio в карточке звонка считается клиентски из уже загруженного транскрипта.

**Tech Stack:** Alembic (tenant-дерево, raw SQL), psycopg2 через `tenancy.db`, FastAPI + async SQLAlchemy `text()`, vanilla JS без сборки, `node --test`, pytest без Postgres/Redis (monkeypatch/стабы).

**Спек:** `docs/superpowers/specs/2026-07-05-manager-dashboard-design.md` — при расхождении спек первичен.

## Global Constraints

- Тесты: `cd /root/projects/silentqa-dev/backend && ../.venv/bin/python -m pytest tests/` и `cd /root/projects/silentqa-dev/worker && ../.venv/bin/python -m pytest` — БЕЗ Postgres/Redis (FakeRedis, monkeypatch, стабы). Фронт: `node --check backend/static/app.js backend/static/charts.js` + `node --test backend/static/tests/`.
- **Запись в БД из worker — best-effort:** любой сбой аналитической записи/алерта → `logger.warning`/`logger.exception`, НИКОГДА не исключение наружу (пайплайн не фейлится из-за аналитики).
- Идемпотентность: upsert `ON CONFLICT (session_id) DO UPDATE` — редоставка `analyze_session` безвредна.
- Только `tenancy.db.tenant_connect()`/`shared_connect()` для сырых коннектов (хаус-рул, enforced `worker/tests/test_no_raw_connects.py`). Схемные имена в SQL не интерполируем — таблица без префикса схемы (search_path решает).
- **Замороженные имена НЕ ломать** (на них тесты/прод): `_run_pipeline` (имя+сигнатура+контракт `{transcript_with_speakers, quality_report, use_extended}`), `_run_gates`, `_analyze_inner` сигнатура, `save_results`, `_process_session_body`, `process_session(self, session_id, config=None, tenant_schema=None)`.
- Форма ответа `GET /api/managers` НЕ меняется: `[{name, total_calls, avg_score, last_call_date}]`.
- Новый фронт-код: все строки через `escapeHtml`, числа в SVG через `Number()`; НИКАКИХ `JSON.stringify` внутри `onclick`-атрибутов.
- Комментарии/UI-строки — на русском; коммиты — conventional commits по-русски (`feat(scope): …`). Каждая таска = отдельный коммит.
- Миграция — ТОЛЬКО тенант-дерево (`backend/alembic/versions/`), стиль raw-SQL как `014_knowledge_base.py`.
- `charts.js` подключается в `index.html` ДО `app.js`; функции глобальные + guard `if (typeof module !== 'undefined') module.exports = {...}` (паттерн `desktop-app/src/renderer/mode.js`).

---

### Task 1: Миграция 018 — таблица `quality_results`

**Files:**
- Create: `backend/alembic/versions/018_quality_results.py`
- Test: `backend/tests/test_migration_018.py`

**Interfaces:**
- Produces: тенант-таблица `quality_results` (колонки см. DDL ниже) — её читают Task 5/6, пишет Task 3.

- [ ] **Step 1: Посмотреть образец стиля**

Прочитай `backend/alembic/versions/014_knowledge_base.py` и `017_sessions_enqueued_at.py` (down_revision-цепочка, `op.execute` raw SQL). `down_revision` для 018 = ревизия головы `017` (возьми её `revision`-идентификатор из файла 017).

- [ ] **Step 2: Написать падающий тест**

`backend/tests/test_migration_018.py` (идиома source-level как `test_migration_014.py` — если тот другой, адаптируй, суть сохрани):

```python
"""Миграция 018: таблица quality_results — структурные гарантии по исходнику.

Тесты без БД: читаем файл миграции и проверяем DDL-инварианты,
на которые опираются worker (upsert) и backend (/api/stats).
"""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1]
       / "alembic" / "versions" / "018_quality_results.py").read_text(encoding="utf-8")


def test_creates_table_with_key_columns():
    assert "CREATE TABLE quality_results" in SRC
    for col in ("session_id", "overall_score", "version", "scenario_id",
                "employee", "session_created_at", "duration_seconds",
                "skip_reason", "criteria", "objections", "talk_metrics",
                "sentiment_counts", "risk_flags", "updated_at"):
        assert col in SRC, f"нет колонки {col}"


def test_pk_fk_and_indexes():
    assert "PRIMARY KEY" in SRC
    assert "REFERENCES sessions(id) ON DELETE CASCADE" in SRC
    assert "ix_qr_created" in SRC
    assert "ix_qr_employee_created" in SRC


def test_downgrade_drops_table():
    assert "DROP TABLE IF EXISTS quality_results" in SRC
```

- [ ] **Step 3: Прогнать — убедиться, что падает**

Run: `cd /root/projects/silentqa-dev/backend && ../.venv/bin/python -m pytest tests/test_migration_018.py -v`
Expected: FAIL (FileNotFoundError — файла миграции нет).

- [ ] **Step 4: Написать миграцию**

`backend/alembic/versions/018_quality_results.py`:

```python
"""quality_results: денормализованная проекция отчётов качества для аналитики.

Файлы quality.json остаются источником истины для drill-down; эта таблица —
для SQL-агрегаций дашборда руководителя (тренды/лидерборд/возражения/риски).
Пишет worker (results_db.record_quality_result), читает backend (/api/stats).

Revision ID: 018_quality_results
Revises: <ID ревизии 017 — подставить фактический>
"""
from alembic import op

revision = "018_quality_results"
down_revision = "<ID ревизии 017>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE quality_results (
            session_id UUID PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            overall_score SMALLINT,
            version SMALLINT,
            scenario_id TEXT,
            employee TEXT,
            session_created_at TIMESTAMPTZ NOT NULL,
            duration_seconds REAL,
            skip_reason TEXT,
            criteria JSONB,
            objections JSONB,
            talk_metrics JSONB,
            sentiment_counts JSONB,
            risk_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_qr_created ON quality_results (session_created_at)")
    op.execute("CREATE INDEX ix_qr_employee_created ON quality_results (employee, session_created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quality_results")
```

- [ ] **Step 5: Тест зелёный + весь backend-сьют не сломан**

Run: `cd /root/projects/silentqa-dev/backend && ../.venv/bin/python -m pytest tests/ -q`
Expected: все зелёные (266+3).

- [ ] **Step 6: Commit**

```bash
git add backend/alembic/versions/018_quality_results.py backend/tests/test_migration_018.py
git commit -m "feat(db): миграция 018 — тенант-таблица quality_results под аналитику дашборда"
```

---

### Task 2: Чистые функции аналитики (`results_db`: objections / talk / risk / sentiment)

**Files:**
- Create: `worker/tasks/results_db.py` (пока только чистые функции)
- Test: `worker/tests/test_results_db.py`

**Interfaces:**
- Produces (используют Task 3/4/8):
  - `extract_objections(quality_report: dict | None, card: dict | None) -> list[dict]` → `[{"text": str, "category": str, "resolved": bool | None, "handling_quality": int | None}]`
  - `compute_talk_metrics(transcript: list[dict], speaker_roles: dict | None) -> dict | None` → `{"manager_sec": float, "client_sec": float, "other_sec": float, "talk_ratio": float | None, "longest_monologue_sec": float, "unattributed": bool}`; `None` при пустом транскрипте
  - `sentiment_counts_from(sentiment_results: list[dict] | None) -> dict | None` → `{"positive": int, "neutral": int, "negative": int}`
  - `compute_risk_flags(overall_score, sentiment_counts, objections, thresholds: dict | None) -> list[str]` — подмножество `["low_score", "negative_sentiment", "unresolved_objections"]`
  - `RISK_DEFAULTS = {"risk_score_below": 4, "negative_sentiment_ratio": 0.4}`

- [ ] **Step 1: Написать падающие тесты**

`worker/tests/test_results_db.py`:

```python
"""Чистые функции results_db: возражения (2 формата), talk-метрики, риск-флаги."""
from tasks.results_db import (
    RISK_DEFAULTS, compute_risk_flags, compute_talk_metrics,
    extract_objections, sentiment_counts_from,
)

# --- extract_objections ---

def test_objections_v4_quality_shape():
    qr = {"objections": [{"text": "Дорого", "category": "too_expensive",
                          "broker_response": "Рассрочка", "handling_quality": 7,
                          "resolved": True}]}
    out = extract_objections(qr, None)
    assert out == [{"text": "Дорого", "category": "too_expensive",
                    "resolved": True, "handling_quality": 7}]


def test_objections_card_shape_used_when_quality_empty():
    card = {"objections": [{"objection": "Подумаю", "raised_by": "клиент",
                            "handled": False, "handling": None, "improvement": "..."}]}
    out = extract_objections({"objections": []}, card)
    assert out == [{"text": "Подумаю", "category": "other",
                    "resolved": False, "handling_quality": None}]


def test_objections_quality_wins_over_card():
    qr = {"objections": [{"text": "A", "category": "no_time",
                          "broker_response": "", "handling_quality": 5, "resolved": False}]}
    card = {"objections": [{"objection": "B", "handled": True}]}
    assert [o["text"] for o in extract_objections(qr, card)] == ["A"]


def test_objections_empty_inputs():
    assert extract_objections(None, None) == []
    assert extract_objections({}, {}) == []


def test_objections_malformed_items_skipped():
    qr = {"objections": ["мусор", {"category": "other"}]}  # без text — скип
    assert extract_objections(qr, None) == []


# --- compute_talk_metrics ---

SEGS = [
    {"speaker": "A", "start": 0.0, "end": 10.0, "text": "…"},
    {"speaker": "B", "start": 10.0, "end": 14.0, "text": "…"},
    {"speaker": "A", "start": 14.0, "end": 20.0, "text": "…"},
    {"speaker": "A", "start": 20.0, "end": 25.0, "text": "…"},
]


def test_talk_with_roles():
    m = compute_talk_metrics(SEGS, {"A": "manager", "B": "client"})
    assert m["manager_sec"] == 21.0 and m["client_sec"] == 4.0
    assert abs(m["talk_ratio"] - 21.0 / 25.0) < 1e-9
    assert m["longest_monologue_sec"] == 11.0  # A: 14→25 непрерывно
    assert m["unattributed"] is False


def test_talk_without_roles_top2_heuristic():
    m = compute_talk_metrics(SEGS, None)
    # топ-2 по времени: A(21с) → manager-слот, B(4с) → client-слот, пометка unattributed
    assert m["manager_sec"] == 21.0 and m["client_sec"] == 4.0
    assert m["unattributed"] is True


def test_talk_third_speaker_goes_other():
    segs = SEGS + [{"speaker": "C", "start": 25.0, "end": 27.0, "text": "…"}]
    m = compute_talk_metrics(segs, {"A": "manager", "B": "client", "C": "other"})
    assert m["other_sec"] == 2.0


def test_talk_empty():
    assert compute_talk_metrics([], None) is None
    assert compute_talk_metrics(None, None) is None


# --- sentiment_counts_from ---

def test_sentiment_counts():
    res = [{"sentiment": "positive"}, {"sentiment": "negative"},
           {"sentiment": "negative"}, {"sentiment": "neutral"}]
    assert sentiment_counts_from(res) == {"positive": 1, "neutral": 1, "negative": 2}
    assert sentiment_counts_from(None) is None
    assert sentiment_counts_from([]) == {"positive": 0, "neutral": 0, "negative": 0}


# --- compute_risk_flags ---

def test_risk_low_score_default_threshold():
    assert "low_score" in compute_risk_flags(3, None, [], None)
    assert "low_score" not in compute_risk_flags(4, None, [], None)
    assert compute_risk_flags(None, None, [], None) == []  # нет скора — нет low_score


def test_risk_negative_sentiment_ratio():
    counts = {"positive": 1, "neutral": 1, "negative": 3}  # 0.6 > 0.4
    assert "negative_sentiment" in compute_risk_flags(8, counts, [], None)
    counts_ok = {"positive": 4, "neutral": 4, "negative": 2}  # 0.2
    assert "negative_sentiment" not in compute_risk_flags(8, counts_ok, [], None)


def test_risk_unresolved_objections():
    objs = [{"text": "Дорого", "category": "too_expensive",
             "resolved": False, "handling_quality": None}]
    assert "unresolved_objections" in compute_risk_flags(8, None, objs, None)
    assert "unresolved_objections" not in compute_risk_flags(
        8, None, [{**objs[0], "resolved": True}], None)


def test_risk_custom_thresholds():
    assert "low_score" in compute_risk_flags(6, None, [], {"risk_score_below": 7})
    assert RISK_DEFAULTS["risk_score_below"] == 4
```

- [ ] **Step 2: Прогнать — падает**

Run: `cd /root/projects/silentqa-dev/worker && ../.venv/bin/python -m pytest tests/test_results_db.py -v`
Expected: FAIL — `ModuleNotFoundError: tasks.results_db`.

- [ ] **Step 3: Реализация**

`worker/tasks/results_db.py`:

```python
"""Аналитическая проекция отчётов качества → тенант-таблица quality_results.

Файл quality.json остаётся источником истины; здесь — денормализованная
строка для SQL-агрегаций дашборда (/api/stats). Все внешние вызовы
best-effort: сбой записи НИКОГДА не валит пайплайн.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

RISK_DEFAULTS = {"risk_score_below": 4, "negative_sentiment_ratio": 0.4}


def extract_objections(quality_report: dict | None, card: dict | None) -> list[dict]:
    """Нормализует возражения из v4-quality и card-extraction к единому виду.

    Приоритет — quality (там category-enum); карточные берём, только если
    quality-список пуст (у card нет category → "other")."""
    out: list[dict] = []
    for raw in (quality_report or {}).get("objections") or []:
        if not isinstance(raw, dict) or not raw.get("text"):
            continue
        out.append({
            "text": str(raw["text"]),
            "category": str(raw.get("category") or "other"),
            "resolved": raw.get("resolved") if isinstance(raw.get("resolved"), bool) else None,
            "handling_quality": raw.get("handling_quality")
            if isinstance(raw.get("handling_quality"), int) else None,
        })
    if out:
        return out
    for raw in (card or {}).get("objections") or []:
        if not isinstance(raw, dict) or not raw.get("objection"):
            continue
        out.append({
            "text": str(raw["objection"]),
            "category": "other",
            "resolved": raw.get("handled") if isinstance(raw.get("handled"), bool) else None,
            "handling_quality": None,
        })
    return out


def compute_talk_metrics(transcript: list | None, speaker_roles: dict | None) -> dict | None:
    """Суммы говорения по ролям из сегментов {speaker,start,end}.

    Роли — из speaker_roles (LLM: manager/client/...); без ролей — топ-2
    спикера по времени занимают слоты manager/client с пометкой unattributed."""
    segs = [s for s in (transcript or [])
            if isinstance(s, dict) and isinstance(s.get("start"), (int, float))
            and isinstance(s.get("end"), (int, float)) and s["end"] > s["start"]]
    if not segs:
        return None

    per_speaker: dict[str, float] = {}
    for s in segs:
        sp = str(s.get("speaker") or "UNKNOWN")
        per_speaker[sp] = per_speaker.get(sp, 0.0) + (s["end"] - s["start"])

    roles = {str(k): str(v) for k, v in (speaker_roles or {}).items()}
    unattributed = not any(v == "manager" for v in roles.values())
    if unattributed:
        ranked = sorted(per_speaker, key=per_speaker.get, reverse=True)
        roles = {}
        if ranked:
            roles[ranked[0]] = "manager"
        if len(ranked) > 1:
            roles[ranked[1]] = "client"

    manager_sec = client_sec = other_sec = 0.0
    for sp, sec in per_speaker.items():
        role = roles.get(sp)
        if role == "manager":
            manager_sec += sec
        elif role == "client":
            client_sec += sec
        else:
            other_sec += sec

    talked = manager_sec + client_sec
    longest, cur, cur_sp = 0.0, 0.0, None
    for s in segs:  # сегменты идут в хронологии транскрипта
        sp = str(s.get("speaker") or "UNKNOWN")
        cur = cur + (s["end"] - s["start"]) if sp == cur_sp else (s["end"] - s["start"])
        cur_sp = sp
        longest = max(longest, cur)

    return {
        "manager_sec": round(manager_sec, 2),
        "client_sec": round(client_sec, 2),
        "other_sec": round(other_sec, 2),
        "talk_ratio": round(manager_sec / talked, 4) if talked > 0 else None,
        "longest_monologue_sec": round(longest, 2),
        "unattributed": unattributed,
    }


def sentiment_counts_from(sentiment_results: list | None) -> dict | None:
    if sentiment_results is None:
        return None
    counts = {"positive": 0, "neutral": 0, "negative": 0}
    for r in sentiment_results:
        label = (r.get("sentiment") or "").lower() if isinstance(r, dict) else ""
        if label in counts:
            counts[label] += 1
    return counts


def compute_risk_flags(overall_score, sentiment_counts, objections, thresholds: dict | None) -> list[str]:
    th = {**RISK_DEFAULTS, **(thresholds or {})}
    flags: list[str] = []
    if isinstance(overall_score, (int, float)) and overall_score < th["risk_score_below"]:
        flags.append("low_score")
    if sentiment_counts:
        total = sum(sentiment_counts.values())
        if total and sentiment_counts.get("negative", 0) / total > th["negative_sentiment_ratio"]:
            flags.append("negative_sentiment")
    if any(o.get("resolved") is False for o in (objections or [])):
        flags.append("unresolved_objections")
    return flags
```

- [ ] **Step 4: Тесты зелёные**

Run: `cd /root/projects/silentqa-dev/worker && ../.venv/bin/python -m pytest tests/test_results_db.py -v`
Expected: PASS (все).

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/results_db.py worker/tests/test_results_db.py
git commit -m "feat(worker): чистые функции аналитики — возражения/talk-метрики/риск-флаги (results_db)"
```

---

### Task 3: `upsert_quality_result` + оркестратор `record_quality_result`

**Files:**
- Modify: `worker/tasks/results_db.py` (дописать)
- Test: `worker/tests/test_results_db_upsert.py`

**Interfaces:**
- Consumes: чистые функции Task 2; `tenancy.db.tenant_connect`, `tenancy.context.get_tenant_slug`.
- Produces (используют Task 4-wiring и Task 8-бэкфилл):
  - `upsert_quality_result(session_id: str, *, overall_score, version, scenario_id, employee, session_created_at, duration_seconds, skip_reason, criteria, objections, talk_metrics, sentiment_counts, risk_flags) -> bool` — голый upsert, кидает исключения наружу (ловит вызывающий).
  - `record_quality_result(session_id: str, quality_report: dict | None, *, card: dict | None = None, transcript: list | None = None, sentiment_results: list | None = None, company_config: dict | None = None, skip_reason: str | None = None, duration_seconds: float | None = None) -> list[str]` — best-effort оркестратор: читает сессию (metadata → employee/scenario_id, created_at, duration fallback), считает всё чистыми функциями, upsert'ит, при непустых risk_flags и НЕ-skip зовёт `alerts.send_risk_alert` (алерт появится в Task 4 — здесь вызов через локальный импорт в try/except с заглушкой «модуля нет — молчим»). Возвращает risk_flags (для тестов/логов); при ЛЮБОЙ ошибке — `logger.warning`/`exception` и `[]`.

- [ ] **Step 1: Падающие тесты**

`worker/tests/test_results_db_upsert.py`:

```python
"""upsert/record: SQL-параметры, best-effort, идемпотентный ON CONFLICT."""
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import tasks.results_db as rdb


class FakeCursor:
    def __init__(self, session_row=None):
        self.executed = []
        self._session_row = session_row

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._session_row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


SESSION_ROW = (
    {"employee": "Иванов", "scenario_id": "general"},   # metadata
    datetime(2026, 7, 1, tzinfo=timezone.utc),           # created_at
    123.0,                                               # duration_seconds
)


def _wire(monkeypatch, cursor):
    monkeypatch.setattr(rdb, "tenant_connect", lambda: FakeConn(cursor))
    monkeypatch.setattr(rdb, "get_tenant_slug", lambda: "acme")


def test_upsert_sql_has_on_conflict(monkeypatch):
    cur = FakeCursor()
    _wire(monkeypatch, cur)
    ok = rdb.upsert_quality_result(
        "sid-1", overall_score=7, version=4, scenario_id="general",
        employee="Иванов", session_created_at=SESSION_ROW[1],
        duration_seconds=123.0, skip_reason=None,
        criteria=[{"name": "politeness", "score": 8, "comment": ""}],
        objections=[], talk_metrics=None, sentiment_counts=None, risk_flags=[])
    assert ok is True
    sql, params = cur.executed[-1]
    assert "INSERT INTO quality_results" in sql
    assert "ON CONFLICT (session_id) DO UPDATE" in sql
    assert params["overall_score"] == 7
    assert json.loads(params["criteria"])[0]["name"] == "politeness"


def test_record_reads_session_and_upserts(monkeypatch):
    cur = FakeCursor(session_row=SESSION_ROW)
    _wire(monkeypatch, cur)
    sent = [{"sentiment": "negative"}] * 3 + [{"sentiment": "positive"}]
    flags = rdb.record_quality_result(
        "sid-1", {"overall_score": 3, "version": 4, "objections": []},
        transcript=[{"speaker": "A", "start": 0, "end": 5, "text": "х"}],
        sentiment_results=sent)
    assert "low_score" in flags and "negative_sentiment" in flags
    ins = [p for s, p in cur.executed if "INSERT INTO quality_results" in s]
    assert ins and ins[0]["employee"] == "Иванов"
    assert ins[0]["skip_reason"] is None


def test_record_skip_reason_no_risk_flags(monkeypatch):
    cur = FakeCursor(session_row=SESSION_ROW)
    _wire(monkeypatch, cur)
    flags = rdb.record_quality_result(
        "sid-1", {"overall_score": None, "skip_reason": "too_short"},
        skip_reason="too_short")
    assert flags == []
    ins = [p for s, p in cur.executed if "INSERT INTO quality_results" in s]
    assert ins[0]["skip_reason"] == "too_short"


def test_record_best_effort_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(rdb, "tenant_connect", boom)
    monkeypatch.setattr(rdb, "get_tenant_slug", lambda: "acme")
    assert rdb.record_quality_result("sid-1", {"overall_score": 5}) == []


def test_record_missing_session_row_warns_and_skips(monkeypatch, caplog):
    cur = FakeCursor(session_row=None)
    _wire(monkeypatch, cur)
    assert rdb.record_quality_result("sid-nope", {"overall_score": 5}) == []
    assert not [p for s, p in cur.executed if "INSERT INTO quality_results" in s]


def test_record_calls_alert_on_risk(monkeypatch):
    cur = FakeCursor(session_row=SESSION_ROW)
    _wire(monkeypatch, cur)
    called = {}
    monkeypatch.setattr(rdb, "_send_risk_alert_safe",
                        lambda **kw: called.update(kw))
    rdb.record_quality_result("sid-1", {"overall_score": 1, "version": 4})
    assert called["session_id"] == "sid-1" and "low_score" in called["flags"]
```

- [ ] **Step 2: Падает**

Run: `cd /root/projects/silentqa-dev/worker && ../.venv/bin/python -m pytest tests/test_results_db_upsert.py -v`
Expected: FAIL — нет `upsert_quality_result`.

- [ ] **Step 3: Реализация (дописать в `worker/tasks/results_db.py`)**

```python
import json
from datetime import datetime, timezone

from tenancy.context import get_tenant_slug
from tenancy.db import tenant_connect

_UPSERT_SQL = """
INSERT INTO quality_results (
    session_id, overall_score, version, scenario_id, employee,
    session_created_at, duration_seconds, skip_reason,
    criteria, objections, talk_metrics, sentiment_counts, risk_flags, updated_at
) VALUES (
    %(session_id)s, %(overall_score)s, %(version)s, %(scenario_id)s, %(employee)s,
    %(session_created_at)s, %(duration_seconds)s, %(skip_reason)s,
    %(criteria)s, %(objections)s, %(talk_metrics)s, %(sentiment_counts)s,
    %(risk_flags)s, now()
)
ON CONFLICT (session_id) DO UPDATE SET
    overall_score = EXCLUDED.overall_score,
    version = EXCLUDED.version,
    scenario_id = EXCLUDED.scenario_id,
    employee = EXCLUDED.employee,
    session_created_at = EXCLUDED.session_created_at,
    duration_seconds = EXCLUDED.duration_seconds,
    skip_reason = EXCLUDED.skip_reason,
    criteria = EXCLUDED.criteria,
    objections = EXCLUDED.objections,
    talk_metrics = EXCLUDED.talk_metrics,
    sentiment_counts = EXCLUDED.sentiment_counts,
    risk_flags = EXCLUDED.risk_flags,
    updated_at = now()
"""


def _jsonb(value):
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def upsert_quality_result(session_id: str, *, overall_score, version, scenario_id,
                          employee, session_created_at, duration_seconds, skip_reason,
                          criteria, objections, talk_metrics, sentiment_counts,
                          risk_flags) -> bool:
    """Голый идемпотентный upsert. Исключения НЕ глотает — ловит вызывающий."""
    conn = tenant_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(_UPSERT_SQL, {
                    "session_id": session_id,
                    "overall_score": overall_score,
                    "version": version,
                    "scenario_id": scenario_id,
                    "employee": employee,
                    "session_created_at": session_created_at,
                    "duration_seconds": duration_seconds,
                    "skip_reason": skip_reason,
                    "criteria": _jsonb(criteria),
                    "objections": _jsonb(objections),
                    "talk_metrics": _jsonb(talk_metrics),
                    "sentiment_counts": _jsonb(sentiment_counts),
                    "risk_flags": _jsonb(risk_flags or []),
                })
    finally:
        conn.close()
    return True


def _fetch_session_row(session_id: str):
    conn = tenant_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metadata, created_at, duration_seconds FROM sessions WHERE id = %s",
                (session_id,))
            return cur.fetchone()
    finally:
        conn.close()


def _send_risk_alert_safe(**kwargs) -> None:
    """Локальный импорт: alerts появляется в соседней таске; без него — молчим."""
    try:
        from tasks.alerts import send_risk_alert
        send_risk_alert(**kwargs)
    except ImportError:
        pass
    except Exception:
        logger.exception("risk alert failed (continuing)")


def record_quality_result(session_id: str, quality_report: dict | None, *,
                          card: dict | None = None, transcript: list | None = None,
                          sentiment_results: list | None = None,
                          company_config: dict | None = None,
                          skip_reason: str | None = None,
                          duration_seconds: float | None = None) -> list[str]:
    """Единая best-effort точка записи аналитики (пайплайн зовёт только её)."""
    try:
        row = _fetch_session_row(session_id)
        if row is None:
            logger.warning(f"[{session_id}] quality_results: сессия не найдена — скип")
            return []
        meta = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
        report = quality_report or {}

        objections = extract_objections(report, card)
        talk = compute_talk_metrics(transcript, report.get("speaker_roles"))
        counts = sentiment_counts_from(sentiment_results)
        thresholds = (company_config or {}).get("alerts")
        flags = [] if skip_reason else compute_risk_flags(
            report.get("overall_score"), counts, objections, thresholds)

        upsert_quality_result(
            session_id,
            overall_score=report.get("overall_score"),
            version=report.get("version"),
            scenario_id=meta.get("scenario_id"),
            employee=meta.get("employee"),
            session_created_at=row[1] or datetime.now(timezone.utc),
            duration_seconds=duration_seconds if duration_seconds is not None else row[2],
            skip_reason=skip_reason,
            criteria=report.get("criteria"),
            objections=objections,
            talk_metrics=talk,
            sentiment_counts=counts,
            risk_flags=flags,
        )
        if flags:
            _send_risk_alert_safe(
                slug=get_tenant_slug(), session_id=session_id,
                employee=meta.get("employee"),
                score=report.get("overall_score"), flags=flags,
                company_config=company_config)
        return flags
    except Exception:
        logger.exception(f"[{session_id}] quality_results запись не удалась (пайплайн продолжает)")
        return []
```

- [ ] **Step 4: Зелёные + no_raw_connects не сломан**

Run: `cd /root/projects/silentqa-dev/worker && ../.venv/bin/python -m pytest tests/test_results_db_upsert.py tests/test_no_raw_connects.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/results_db.py worker/tests/test_results_db_upsert.py
git commit -m "feat(worker): upsert quality_results + best-effort record_quality_result"
```

---

### Task 4: Telegram-алерт «рисковый звонок» + вайринг в пайплайн

**Files:**
- Create: `worker/tasks/alerts.py`
- Modify: `worker/tasks/pipeline.py` (3 точки: `_analyze_inner` хвост, 2 гейта в `_run_gates`)
- Test: `worker/tests/test_alerts.py`, `worker/tests/test_results_db_wiring.py`

**Interfaces:**
- Consumes: `record_quality_result` (Task 3).
- Produces: `alerts.send_risk_alert(slug, session_id, employee, score, flags, company_config=None) -> bool`.

- [ ] **Step 1: Падающие тесты алертов**

`worker/tests/test_alerts.py` (образец — как устроен `amocrm_alerts`; env читаем на вызове, НЕ на импорте, чтобы тестировалось monkeypatch.setenv):

```python
"""Риск-алерты: деградация без env, порядок chat_id, формат сообщения."""
import tasks.alerts as alerts


def test_no_env_degrades_to_log(monkeypatch, caplog):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert alerts.send_risk_alert(
        slug="acme", session_id="sid", employee="Иванов",
        score=2, flags=["low_score"]) is False


def test_chat_id_priority_config_over_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("RISK_ALERT_CHAT_ID", "env-chat")
    sent = {}
    monkeypatch.setattr(alerts, "_post_telegram",
                        lambda token, chat_id, text: sent.update(chat=chat_id, text=text) or True)
    alerts.send_risk_alert(slug="acme", session_id="sid", employee=None, score=3,
                           flags=["low_score"],
                           company_config={"alerts": {"telegram_chat_id": "cfg-chat"}})
    assert sent["chat"] == "cfg-chat"


def test_message_format_russian(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    sent = {}
    monkeypatch.setattr(alerts, "_post_telegram",
                        lambda token, chat_id, text: sent.update(text=text) or True)
    alerts.send_risk_alert(slug="acme", session_id="sid-9", employee="Иванов",
                           score=2, flags=["low_score", "unresolved_objections"])
    assert "рисковый звонок" in sent["text"].lower()
    assert "Иванов" in sent["text"] and "2/10" in sent["text"]
    assert "https://acme.silentqa.com/#call/sid-9" in sent["text"]
    assert "низкая оценка" in sent["text"] and "неотработанные возражения" in sent["text"]


def test_post_failure_never_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    def boom(*a):
        raise RuntimeError("net")
    monkeypatch.setattr(alerts, "_post_telegram", boom)
    assert alerts.send_risk_alert(slug="a", session_id="s", employee=None,
                                  score=None, flags=["negative_sentiment"]) is False
```

- [ ] **Step 2: Падает** — `ModuleNotFoundError: tasks.alerts`.

- [ ] **Step 3: Реализация `worker/tasks/alerts.py`**

```python
"""Риск-алерты дашборда (Telegram). Деградирует в WARNING-лог без env; никогда не raise.

Отличие от amocrm_alerts: env читается на вызове (тестируемость), chat_id
пер-тенантный — company-config alerts.telegram_chat_id → RISK_ALERT_CHAT_ID
→ TELEGRAM_CHAT_ID."""
import logging
import os

import requests

logger = logging.getLogger(__name__)

FLAG_RU = {
    "low_score": "низкая оценка",
    "negative_sentiment": "негативная тональность",
    "unresolved_objections": "неотработанные возражения",
}


def _post_telegram(token: str, chat_id: str, text: str) -> bool:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text}, timeout=10)
    return resp.status_code == 200


def send_risk_alert(*, slug: str | None, session_id: str, employee: str | None,
                    score, flags: list[str], company_config: dict | None = None) -> bool:
    reasons = ", ".join(FLAG_RU.get(f, f) for f in flags)
    who = employee or "менеджер не указан"
    score_txt = f"{score}/10" if score is not None else "без оценки"
    url = f"https://{slug}.silentqa.com/#call/{session_id}" if slug else f"#call/{session_id}"
    msg = f"⚠️ SilentQA [{slug or '?'}]: рисковый звонок — {who}, оценка {score_txt} ({reasons})\n{url}"

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = (((company_config or {}).get("alerts") or {}).get("telegram_chat_id")
               or os.getenv("RISK_ALERT_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", ""))
    if not (token and chat_id):
        logger.warning(f"[alert] {msg}")
        return False
    try:
        if _post_telegram(token, chat_id, msg):
            return True
        logger.warning(f"[alert] telegram не 200: {msg}")
        return False
    except Exception:
        logger.exception(f"[alert] telegram send failed (continuing): {msg}")
        return False
```

- [ ] **Step 4: Падающие wiring-тесты**

`worker/tests/test_results_db_wiring.py` (идиома `test_kb_pipeline_wiring.py` — monkeypatch и вызов внутренних функций pipeline; посмотри тот файл и повтори манеру стабов соседних шагов):

```python
"""Пайплайн зовёт record_quality_result: analyze-хвост и оба гейта."""
from unittest.mock import MagicMock

import tasks.pipeline as pipeline


def test_run_gates_short_call_records_skip(monkeypatch):
    calls = {}
    monkeypatch.setattr(pipeline, "record_quality_result",
                        lambda sid, report, **kw: calls.update(sid=sid, kw=kw) or [])
    monkeypatch.setattr(pipeline, "_get_audio_duration", lambda p: 5.0)
    monkeypatch.setattr(pipeline, "save_results", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_push_to_amocrm", lambda *a, **k: None)
    task = MagicMock()
    res = pipeline._run_gates(task, "sid-1", "/tmp/a.wav", {}, {}, None, {})
    assert res is not None
    assert calls["sid"] == "sid-1"
    assert calls["kw"]["skip_reason"] == "too_short"
    assert calls["kw"]["duration_seconds"] == 5.0


def test_run_gates_broken_recording_records_skip(monkeypatch):
    calls = {}
    monkeypatch.setattr(pipeline, "record_quality_result",
                        lambda sid, report, **kw: calls.update(kw=kw) or [])
    monkeypatch.setattr(pipeline, "_get_audio_duration", lambda p: 300.0)
    monkeypatch.setattr(pipeline, "_detect_broken_recording",
                        lambda p, d: {"silent_windows": 3, "total_windows": 4,
                                      "opening_db": -20.0, "tail_db": [-80.0]})
    monkeypatch.setattr(pipeline, "save_results", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_push_to_amocrm", lambda *a, **k: None)
    res = pipeline._run_gates(MagicMock(), "sid-2", "/tmp/a.wav", {}, {}, None, {})
    assert res is not None
    assert calls["kw"]["skip_reason"] == "broken_recording"


def test_run_gates_passed_no_record(monkeypatch):
    called = []
    monkeypatch.setattr(pipeline, "record_quality_result",
                        lambda *a, **k: called.append(1) or [])
    monkeypatch.setattr(pipeline, "_get_audio_duration", lambda p: 300.0)
    monkeypatch.setattr(pipeline, "_detect_broken_recording", lambda p, d: None)
    assert pipeline._run_gates(MagicMock(), "sid-3", "/tmp/a.wav", {}, {}, None, {}) is None
    assert not called
```

Для analyze-хвоста — тест на уровне `_analyze_inner` громоздок (много стабов); вместо этого grep-инвариант в том же файле:

```python
def test_analyze_inner_source_calls_record():
    import inspect
    src = inspect.getsource(pipeline._analyze_inner)
    assert "record_quality_result(" in src
    for kwarg in ("card=", "transcript=", "sentiment_results=", "company_config="):
        assert kwarg in src, f"_analyze_inner должен передавать {kwarg}"
```

- [ ] **Step 5: Падает** (нет импорта `record_quality_result` в pipeline).

- [ ] **Step 6: Вайринг в `worker/tasks/pipeline.py`**

Импорт рядом с остальными `from tasks...`:

```python
from tasks.results_db import record_quality_result
```

В `_run_gates` — после КАЖДОГО из двух `save_results(session_id, "quality", quality_report)` (short-call ветка и broken-recording ветка), ДО `use_extended = ...`:

```python
        record_quality_result(session_id, quality_report,
                              skip_reason="too_short",  # во 2-й ветке: "broken_recording"
                              duration_seconds=audio_duration)
```

В `_analyze_inner` — после блока `=== 7. Auto-save speaker roles ===`, ПЕРЕД `=== 7.5 Next-call plan ===` (новый блок `=== 7.6 Аналитика дашборда ===`):

```python
    # === 7.6 Аналитика дашборда (best-effort, не влияет на пайплайн) ===
    risk_flags = record_quality_result(
        session_id, quality_report,
        card=card,
        transcript=transcript_with_speakers,
        sentiment_results=sentiment_results,
        company_config=company_config,
        duration_seconds=_get_audio_duration(audio_path),
    )
    if risk_flags:
        logger.info(f"[{session_id}] Рисковые флаги: {risk_flags}")
```

Проверь фактические имена локальных переменных в `_analyze_inner` (`card`, `sentiment_results`, `transcript_with_speakers`, `company_config`, `audio_path` — все существуют, см. `pipeline.py:756-906`).

- [ ] **Step 7: Всё зелёное**

Run: `cd /root/projects/silentqa-dev/worker && ../.venv/bin/python -m pytest -q`
Expected: 212+нов. passed, 5 skipped, 0 failed.

- [ ] **Step 8: Commit**

```bash
git add worker/tasks/alerts.py worker/tasks/pipeline.py worker/tests/test_alerts.py worker/tests/test_results_db_wiring.py
git commit -m "feat(worker): запись quality_results из пайплайна (analyze + гейты) + Telegram-алерт рискового звонка"
```

---

### Task 5: Backend `/api/stats/*`

**Files:**
- Create: `backend/app/routes/stats.py`
- Modify: `backend/app/main.py` (импорт + `app.include_router(stats.router)`)
- Test: `backend/tests/test_stats_api.py`

**Interfaces:**
- Consumes: таблица `quality_results` (Task 1), `require_viewer`/`employee_scope` из `app.auth_user`, `get_db`.
- Produces (контракт для фронта Task 7):
  - `GET /api/stats/overview?days=30&granularity=day|week` → `{"kpi": {"calls": int, "scored_calls": int, "avg_score": float|null, "risk_calls": int, "avg_talk_ratio": float|null}, "prev_kpi": {…то же}, "series": [{"bucket": "2026-07-01", "calls": int, "avg_score": float|null}]}`
  - `GET /api/stats/managers?days=30` → `[{"name": str, "calls": int, "avg_score": float|null, "risk_calls": int, "avg_talk_ratio": float|null, "last_call_date": iso|null, "spark": [float|null,…]}]`
  - `GET /api/stats/objections?days=30` → `[{"category": str, "count": int, "resolved_rate": float|null, "avg_handling_quality": float|null, "examples": [str,…≤3]}]`
  - `GET /api/stats/risk-calls?days=30&limit=20` → `[{"session_id": str, "employee": str|null, "score": int|null, "risk_flags": [str], "created_at": iso}]`
  - Внутренние monkeypatch-точки: `_kpi_row(db, scope, start, end)`, `_series_rows(db, scope, start, end, granularity)`, `_manager_rows(db, scope, start)`, `_spark_rows(db, scope, start)`, `_objection_rows(db, scope, start)`, `_risk_rows(db, scope, start, limit)` — каждая `async`, возвращает список/словарь примитивов.

- [ ] **Step 1: Падающие тесты**

`backend/tests/test_stats_api.py` (клиент/куки — идиома `test_manager_scope.py`: monkeypatch `TenantRegistry.all_tenants`, `_cookie` через `auth_sessions.create_session`; скопируй фикстуры оттуда):

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


def _cookie(fake_redis, role="viewer", employee=None, email="v@x.io"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u1", email, role,
                                                      employee_name=employee)
        finally:
            reset_tenant_schema(token)
    return {SESSION_COOKIE: asyncio.run(seed())}


KPI = {"calls": 10, "scored_calls": 8, "avg_score": 6.5,
       "risk_calls": 2, "avg_talk_ratio": 0.61}


@pytest.fixture
def stub_stats(monkeypatch):
    from app.routes import stats as st
    captured = {}

    async def kpi(db, scope, start, end):
        captured.setdefault("kpi_scopes", []).append(scope)
        return dict(KPI)

    async def series(db, scope, start, end, granularity):
        captured["granularity"] = granularity
        return [{"bucket": "2026-07-01", "calls": 3, "avg_score": 7.0}]

    monkeypatch.setattr(st, "_kpi_row", kpi)
    monkeypatch.setattr(st, "_series_rows", series)
    return captured


def test_overview_shape_and_prev(client, fake_redis, stub_stats):
    r = client.get("/api/stats/overview?days=30", cookies=_cookie(fake_redis))
    assert r.status_code == 200
    body = r.json()
    assert body["kpi"] == KPI and body["prev_kpi"] == KPI
    assert body["series"][0]["bucket"] == "2026-07-01"
    assert len(stub_stats["kpi_scopes"]) == 2  # текущее + предыдущее окно


def test_overview_granularity_validation(client, fake_redis, stub_stats):
    assert client.get("/api/stats/overview?granularity=week",
                      cookies=_cookie(fake_redis)).status_code == 200
    assert client.get("/api/stats/overview?granularity=hour",
                      cookies=_cookie(fake_redis)).status_code == 422


def test_manager_scope_passed_to_sql(client, fake_redis, stub_stats):
    client.get("/api/stats/overview",
               cookies=_cookie(fake_redis, role="manager", employee="Иванов"))
    assert stub_stats["kpi_scopes"][0] == "Иванов"


def test_unauthenticated_401(client, fake_redis):
    assert client.get("/api/stats/overview").status_code == 401


def test_managers_endpoint_shape(client, fake_redis, monkeypatch):
    from app.routes import stats as st

    async def rows(db, scope, start):
        return [{"name": "Иванов", "calls": 5, "avg_score": 7.2,
                 "risk_calls": 1, "avg_talk_ratio": 0.55,
                 "last_call_date": "2026-07-01T00:00:00+00:00"}]

    async def spark(db, scope, start):
        return [{"name": "Иванов", "bucket": "2026-06-29", "avg_score": 7.0}]

    monkeypatch.setattr(st, "_manager_rows", rows)
    monkeypatch.setattr(st, "_spark_rows", spark)
    r = client.get("/api/stats/managers", cookies=_cookie(fake_redis))
    assert r.status_code == 200
    m = r.json()[0]
    assert m["name"] == "Иванов" and m["spark"] == [7.0]


def test_objections_and_risk_calls(client, fake_redis, monkeypatch):
    from app.routes import stats as st

    async def obj(db, scope, start):
        return [{"category": "too_expensive", "count": 4, "resolved_rate": 0.5,
                 "avg_handling_quality": 6.0, "examples": ["Дорого"]}]

    async def risk(db, scope, start, limit):
        return [{"session_id": "s1", "employee": None, "score": 2,
                 "risk_flags": ["low_score"], "created_at": "2026-07-01T00:00:00+00:00"}]

    monkeypatch.setattr(st, "_objection_rows", obj)
    monkeypatch.setattr(st, "_risk_rows", risk)
    assert client.get("/api/stats/objections",
                      cookies=_cookie(fake_redis)).json()[0]["category"] == "too_expensive"
    assert client.get("/api/stats/risk-calls",
                      cookies=_cookie(fake_redis)).json()[0]["risk_flags"] == ["low_score"]
```

- [ ] **Step 2: Падает** — `ModuleNotFoundError: app.routes.stats`.

- [ ] **Step 3: Реализация `backend/app/routes/stats.py`**

```python
"""Агрегаты дашборда руководителя поверх quality_results (миграция 018).

Идиома модуля: _*_rows-хелперы (text()-SQL, monkeypatch в тестах) + тонкие
хендлеры. avg_score везде взвешенный (AVG по строкам с overall_score),
calls — все строки (включая skip-гейты, чтобы биться со списком звонков).
Менеджерский скоуп (employee_scope) сужает ВСЕ запросы до своего employee."""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_user import employee_scope, require_viewer
from app.database import get_db

router = APIRouter(prefix="/api/stats", tags=["stats"],
                   dependencies=[Depends(require_viewer)])

_SCOPE_SQL = "AND (CAST(:scope AS TEXT) IS NULL OR employee = :scope)"


async def _kpi_row(db: AsyncSession, scope, start, end) -> dict:
    row = (await db.execute(text(f"""
        SELECT COUNT(*) AS calls,
               COUNT(overall_score) AS scored_calls,
               ROUND(AVG(overall_score)::numeric, 2) AS avg_score,
               COUNT(*) FILTER (WHERE jsonb_array_length(risk_flags) > 0) AS risk_calls,
               ROUND(AVG((talk_metrics->>'talk_ratio')::numeric), 3) AS avg_talk_ratio
        FROM quality_results
        WHERE session_created_at >= :start AND session_created_at < :end {_SCOPE_SQL}
    """), {"scope": scope, "start": start, "end": end})).mappings().one()
    return {k: (float(v) if k in ("avg_score", "avg_talk_ratio") and v is not None else v)
            for k, v in dict(row).items()}


async def _series_rows(db: AsyncSession, scope, start, end, granularity) -> list[dict]:
    rows = (await db.execute(text(f"""
        SELECT to_char(date_trunc(:g, session_created_at), 'YYYY-MM-DD') AS bucket,
               COUNT(*) AS calls,
               ROUND(AVG(overall_score)::numeric, 2) AS avg_score
        FROM quality_results
        WHERE session_created_at >= :start AND session_created_at < :end {_SCOPE_SQL}
        GROUP BY 1 ORDER BY 1
    """), {"g": granularity, "scope": scope, "start": start, "end": end})).mappings().all()
    return [{"bucket": r["bucket"], "calls": r["calls"],
             "avg_score": float(r["avg_score"]) if r["avg_score"] is not None else None}
            for r in rows]


async def _manager_rows(db: AsyncSession, scope, start) -> list[dict]:
    rows = (await db.execute(text(f"""
        SELECT employee AS name, COUNT(*) AS calls,
               ROUND(AVG(overall_score)::numeric, 2) AS avg_score,
               COUNT(*) FILTER (WHERE jsonb_array_length(risk_flags) > 0) AS risk_calls,
               ROUND(AVG((talk_metrics->>'talk_ratio')::numeric), 3) AS avg_talk_ratio,
               MAX(session_created_at) AS last_call_date
        FROM quality_results
        WHERE session_created_at >= :start AND employee IS NOT NULL {_SCOPE_SQL}
        GROUP BY employee ORDER BY calls DESC
    """), {"scope": scope, "start": start})).mappings().all()
    return [{**dict(r),
             "avg_score": float(r["avg_score"]) if r["avg_score"] is not None else None,
             "avg_talk_ratio": float(r["avg_talk_ratio"]) if r["avg_talk_ratio"] is not None else None,
             "last_call_date": r["last_call_date"].isoformat() if r["last_call_date"] else None}
            for r in rows]


async def _spark_rows(db: AsyncSession, scope, start) -> list[dict]:
    rows = (await db.execute(text(f"""
        SELECT employee AS name,
               to_char(date_trunc('week', session_created_at), 'YYYY-MM-DD') AS bucket,
               ROUND(AVG(overall_score)::numeric, 2) AS avg_score
        FROM quality_results
        WHERE session_created_at >= :start AND employee IS NOT NULL {_SCOPE_SQL}
        GROUP BY 1, 2 ORDER BY 1, 2
    """), {"scope": scope, "start": start})).mappings().all()
    return [{"name": r["name"], "bucket": r["bucket"],
             "avg_score": float(r["avg_score"]) if r["avg_score"] is not None else None}
            for r in rows]


async def _objection_rows(db: AsyncSession, scope, start) -> list[dict]:
    rows = (await db.execute(text(f"""
        SELECT o->>'category' AS category, COUNT(*) AS count,
               ROUND(AVG(CASE WHEN (o->>'resolved')::boolean THEN 1.0
                              WHEN (o->>'resolved') IS NOT NULL THEN 0.0 END)::numeric, 2)
                   AS resolved_rate,
               ROUND(AVG((o->>'handling_quality')::numeric), 1) AS avg_handling_quality,
               (ARRAY_AGG(o->>'text'))[1:3] AS examples
        FROM quality_results qr
        CROSS JOIN LATERAL jsonb_array_elements(qr.objections) AS o
        WHERE qr.session_created_at >= :start {_SCOPE_SQL.replace('employee', 'qr.employee')}
        GROUP BY 1 ORDER BY count DESC
    """), {"scope": scope, "start": start})).mappings().all()
    return [{**dict(r),
             "resolved_rate": float(r["resolved_rate"]) if r["resolved_rate"] is not None else None,
             "avg_handling_quality": float(r["avg_handling_quality"])
             if r["avg_handling_quality"] is not None else None,
             "examples": list(r["examples"] or [])}
            for r in rows]


async def _risk_rows(db: AsyncSession, scope, start, limit) -> list[dict]:
    rows = (await db.execute(text(f"""
        SELECT session_id::text, employee, overall_score AS score,
               risk_flags, session_created_at AS created_at
        FROM quality_results
        WHERE jsonb_array_length(risk_flags) > 0
          AND session_created_at >= :start {_SCOPE_SQL}
        ORDER BY session_created_at DESC LIMIT :limit
    """), {"scope": scope, "start": start, "limit": limit})).mappings().all()
    return [{"session_id": r["session_id"], "employee": r["employee"],
             "score": r["score"], "risk_flags": list(r["risk_flags"] or []),
             "created_at": r["created_at"].isoformat()}
            for r in rows]


def _window(days: int):
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days), now


@router.get("/overview")
async def stats_overview(
    days: int = Query(30, ge=1, le=365),
    granularity: Literal["day", "week"] = "day",
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    start, end = _window(days)
    prev_start = start - timedelta(days=days)
    return {
        "kpi": await _kpi_row(db, scope, start, end),
        "prev_kpi": await _kpi_row(db, scope, prev_start, start),
        "series": await _series_rows(db, scope, start, end, granularity),
    }


@router.get("/managers")
async def stats_managers(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    start, _ = _window(days)
    managers = await _manager_rows(db, scope, start)
    sparks: dict[str, list] = {}
    for r in await _spark_rows(db, scope, start):
        sparks.setdefault(r["name"], []).append(r["avg_score"])
    return [{**m, "spark": sparks.get(m["name"], [])} for m in managers]


@router.get("/objections")
async def stats_objections(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    start, _ = _window(days)
    return await _objection_rows(db, scope, start)


@router.get("/risk-calls")
async def stats_risk_calls(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    start, _ = _window(days)
    return await _risk_rows(db, scope, start, limit)
```

В `backend/app/main.py`: добавить `stats` в импорт из `app.routes` и `app.include_router(stats.router)` рядом с `managers`.

- [ ] **Step 4: Зелёные + весь backend-сьют**

Run: `cd /root/projects/silentqa-dev/backend && ../.venv/bin/python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/stats.py backend/app/main.py backend/tests/test_stats_api.py
git commit -m "feat(api): /api/stats — overview/managers/objections/risk-calls поверх quality_results"
```

---

### Task 6: Починка `/api/managers` (E-2 + N+1)

**Files:**
- Modify: `backend/app/routes/managers.py` (полная замена реализации, форма ответов прежняя)
- Test: `backend/tests/test_managers_sql.py`
- Не трогать: `backend/tests/test_manager_scope.py` должен остаться зелёным (он стабит `managers_mod._all_sessions` — эта функция УДАЛЯЕТСЯ, поэтому тот тест НАДО обновить: стабить новые хелперы; правь его минимально, семантика та же).

**Interfaces:**
- Produces: `GET /api/managers` → прежняя форма `[{name, total_calls, avg_score, last_call_date}]`, но `avg_score` — реальный `AVG(quality_results.overall_score)`; `GET /api/managers/{name}/sessions` — прежняя форма, без N+1.
- Внутренние monkeypatch-точки: `_manager_agg_rows(db, scope)`, `_sessions_for(db, name)`.

- [ ] **Step 1: Падающие тесты**

`backend/tests/test_managers_sql.py` (фикстуры client/_cookie — как в test_stats_api.py):

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


def _cookie(fake_redis, role="viewer", employee=None):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u1", "v@x.io", role,
                                                      employee_name=employee)
        finally:
            reset_tenant_schema(token)
    return {SESSION_COOKIE: asyncio.run(seed())}


def test_list_managers_shape_from_sql(client, fake_redis, monkeypatch):
    from app.routes import managers as m

    async def agg(db, scope):
        return [{"name": "Иванов", "total_calls": 7, "avg_score": 6.43,
                 "last_call_date": "2026-07-01T00:00:00+00:00"}]
    monkeypatch.setattr(m, "_manager_agg_rows", agg)
    r = client.get("/api/managers", cookies=_cookie(fake_redis))
    assert r.status_code == 200
    assert r.json() == [{"name": "Иванов", "total_calls": 7, "avg_score": 6.43,
                         "last_call_date": "2026-07-01T00:00:00+00:00"}]


def test_manager_scope_filters(client, fake_redis, monkeypatch):
    from app.routes import managers as m
    seen = {}

    async def agg(db, scope):
        seen["scope"] = scope
        return []
    monkeypatch.setattr(m, "_manager_agg_rows", agg)
    client.get("/api/managers", cookies=_cookie(fake_redis, role="manager",
                                                employee="Иванов"))
    assert seen["scope"] == "Иванов"


def test_manager_sessions_404_when_empty(client, fake_redis, monkeypatch):
    from app.routes import managers as m

    async def none_rows(db, name):
        return []
    monkeypatch.setattr(m, "_sessions_for", none_rows)
    r = client.get("/api/managers/Иванов/sessions", cookies=_cookie(fake_redis))
    assert r.status_code == 404
```

- [ ] **Step 2: Падает** (нет `_manager_agg_rows`).

- [ ] **Step 3: Реализация — заменить тело `backend/app/routes/managers.py`**

```python
"""Менеджеры: SQL-агрегаты вместо in-memory скана всех сессий.

avg_score — из quality_results (реальный AVG по overall_score); раньше
читалось метаполе metadata->>'score', которое никто не писал (мёртвый ноль).
total_calls — ВСЕ сессии менеджера (как раньше), не только оценённые."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_user import employee_scope, require_viewer
from app.database import get_db
from app.schemas import SessionResponse

router = APIRouter(prefix="/api/managers", tags=["managers"])

_SCOPE_SQL = "AND (CAST(:scope AS TEXT) IS NULL OR s.metadata->>'employee' = :scope)"


async def _manager_agg_rows(db: AsyncSession, scope) -> list[dict]:
    rows = (await db.execute(text(f"""
        SELECT s.metadata->>'employee' AS name,
               COUNT(*) AS total_calls,
               ROUND(AVG(qr.overall_score)::numeric, 2) AS avg_score,
               MAX(s.created_at) AS last_call_date
        FROM sessions s
        LEFT JOIN quality_results qr ON qr.session_id = s.id
        WHERE s.metadata->>'employee' IS NOT NULL {_SCOPE_SQL}
        GROUP BY 1 ORDER BY total_calls DESC
    """), {"scope": scope})).mappings().all()
    return [{"name": r["name"], "total_calls": r["total_calls"],
             "avg_score": float(r["avg_score"]) if r["avg_score"] is not None else 0.0,
             "last_call_date": r["last_call_date"].isoformat()
             if r["last_call_date"] else None}
            for r in rows]


async def _sessions_for(db: AsyncSession, name: str) -> list[dict]:
    rows = (await db.execute(text("""
        SELECT s.id, s.status, s.created_at, s.finished_at, s.duration_seconds,
               s.file_size_bytes, s.metadata, COALESCE(c.cnt, 0) AS chunks_count
        FROM sessions s
        LEFT JOIN (SELECT session_id, COUNT(*) AS cnt FROM chunks GROUP BY 1) c
               ON c.session_id = s.id
        WHERE s.metadata->>'employee' = :name
        ORDER BY s.created_at DESC
    """), {"name": name})).mappings().all()
    return [dict(r) for r in rows]


@router.get("", dependencies=[Depends(require_viewer)])
async def list_managers(
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    """Уникальные менеджеры с агрегатами (взвешенный avg_score из quality_results)."""
    return await _manager_agg_rows(db, scope)


@router.get("/{name}/sessions", response_model=list[SessionResponse],
            dependencies=[Depends(require_viewer)])
async def get_manager_sessions(
    name: str,
    db: AsyncSession = Depends(get_db),
    scope: str | None = Depends(employee_scope),
):
    if scope is not None and name != scope:
        raise HTTPException(status_code=403, detail="foreign_manager")
    rows = await _sessions_for(db, name)
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"No sessions found for manager '{name}'")
    return [SessionResponse(
        id=r["id"] if isinstance(r["id"], uuid.UUID) else uuid.UUID(str(r["id"])),
        status=r["status"], created_at=r["created_at"], finished_at=r["finished_at"],
        duration_seconds=r["duration_seconds"], file_size_bytes=r["file_size_bytes"],
        metadata=r["metadata"], chunks_count=r["chunks_count"],
    ) for r in rows]
```

Обнови `backend/tests/test_manager_scope.py`: стабы `managers_mod._all_sessions` замени на `managers_mod._sessions_for` (тот же смысл — «данных нет»/«данные есть»), утверждения не меняй.

- [ ] **Step 4: Зелёные (в т.ч. старый test_manager_scope)**

Run: `cd /root/projects/silentqa-dev/backend && ../.venv/bin/python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/managers.py backend/tests/test_managers_sql.py backend/tests/test_manager_scope.py
git commit -m "fix(api): /api/managers — SQL-агрегаты, реальный avg_score из quality_results (был мёртвый metadata.score), без N+1"
```

---

### Task 7: Фронт — `charts.js` (SVG + computeTalkMetrics) с node-тестами

**Files:**
- Create: `backend/static/charts.js`
- Create: `backend/static/tests/charts.test.mjs`
- Modify: `backend/static/index.html` (`<script src="charts.js"></script>` СТРОГО перед `<script src="app.js">`)

**Interfaces:**
- Produces (использует Task 8): глобальные функции
  - `svgLineChart(points, opts)` — `points: [{label: str, value: number|null}]`, `opts: {width=560, height=160, yMax=10}` → SVG-строка (line + точки + подписи оси X первая/последняя).
  - `svgSparkline(values, opts)` — `values: (number|null)[]`, `opts: {width=90, height=24, yMax=10}` → SVG-строка.
  - `svgBarRow(value, max, opts)` — горизонтальный бар (для resolved-rate) → SVG-строка.
  - `computeTalkMetrics(segments, speakerMap)` — та же семантика, что worker-функция Task 2 (manager_sec/client_sec/other_sec/talk_ratio/longest_monologue_sec/unattributed), `null` при пустых сегментах.
- Все функции чистые (вход → строка/объект), без DOM; в конце файла guard `if (typeof module !== 'undefined') { module.exports = { svgLineChart, svgSparkline, svgBarRow, computeTalkMetrics }; }`.

- [ ] **Step 1: Падающие node-тесты**

`backend/static/tests/charts.test.mjs`:

```js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { svgLineChart, svgSparkline, svgBarRow, computeTalkMetrics } = require('../charts.js');

test('svgLineChart возвращает svg с polyline и пропускает null-точки', () => {
  const svg = svgLineChart([
    { label: '2026-07-01', value: 5 },
    { label: '2026-07-02', value: null },
    { label: '2026-07-03', value: 8 },
  ], { yMax: 10 });
  assert.ok(svg.startsWith('<svg'));
  assert.ok(svg.includes('<polyline'));
  assert.ok(svg.includes('2026-07-01') && svg.includes('2026-07-03'));
});

test('svgLineChart пустые данные → заглушка без polyline', () => {
  const svg = svgLineChart([], {});
  assert.ok(svg.startsWith('<svg') && !svg.includes('<polyline'));
});

test('svgSparkline рисует и терпит null', () => {
  const svg = svgSparkline([7, null, 8, 6]);
  assert.ok(svg.startsWith('<svg') && svg.includes('<polyline'));
});

test('svgBarRow ширина пропорциональна', () => {
  const half = svgBarRow(0.5, 1);
  const full = svgBarRow(1, 1);
  assert.ok(half.startsWith('<svg') && full.startsWith('<svg'));
  assert.notEqual(half, full);
});

test('computeTalkMetrics — паритет с worker-версией', () => {
  const segs = [
    { speaker: 'A', start: 0, end: 10 },
    { speaker: 'B', start: 10, end: 14 },
    { speaker: 'A', start: 14, end: 20 },
    { speaker: 'A', start: 20, end: 25 },
  ];
  const m = computeTalkMetrics(segs, { A: 'manager', B: 'client' });
  assert.equal(m.manager_sec, 21);
  assert.equal(m.client_sec, 4);
  assert.ok(Math.abs(m.talk_ratio - 21 / 25) < 1e-9);
  assert.equal(m.longest_monologue_sec, 11);
  assert.equal(m.unattributed, false);
  assert.equal(computeTalkMetrics([], null), null);
  assert.equal(computeTalkMetrics(segs, null).unattributed, true);
});
```

- [ ] **Step 2: Падает**

Run: `node --test backend/static/tests/`
Expected: FAIL — cannot find `../charts.js`.

- [ ] **Step 3: Реализация `backend/static/charts.js`**

Чистые функции, только числа в разметке (инъекции невозможны: все динамические значения проходят `Number()`, label'ы — через `_esc`):

```js
/* ============================================
   charts.js — SVG-графики дашборда (без зависимостей).
   Чистые функции: данные → SVG-строка. Node-тестируемо.
   Подключать в index.html ДО app.js.
   ============================================ */

function _esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _pts(values, w, h, yMax, pad) {
  // индексы с null пропускаем; x равномерно, y инвертирован
  const n = values.length;
  const out = [];
  values.forEach((v, i) => {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return;
    const x = n === 1 ? w / 2 : pad + (i * (w - 2 * pad)) / (n - 1);
    const y = h - pad - (Math.min(Number(v), yMax) / yMax) * (h - 2 * pad);
    out.push([Math.round(x * 10) / 10, Math.round(y * 10) / 10]);
  });
  return out;
}

function svgLineChart(points, opts) {
  const o = Object.assign({ width: 560, height: 160, yMax: 10 }, opts || {});
  const w = Number(o.width), h = Number(o.height), yMax = Number(o.yMax) || 10;
  const head = `<svg class="chart-line" viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img">`;
  if (!points || !points.length) {
    return head + `<text x="${w / 2}" y="${h / 2}" text-anchor="middle" class="chart-empty">нет данных</text></svg>`;
  }
  const vals = points.map(p => (p && p.value !== undefined ? p.value : null));
  const pts = _pts(vals, w, h, yMax, 18);
  const poly = pts.length
    ? `<polyline fill="none" stroke="var(--accent, #b45309)" stroke-width="2" points="${pts.map(p => p.join(',')).join(' ')}"/>`
    : '';
  const dots = pts.map(p => `<circle cx="${p[0]}" cy="${p[1]}" r="2.5" fill="var(--accent, #b45309)"/>`).join('');
  const first = points[0], last = points[points.length - 1];
  const labels = `<text x="18" y="${h - 4}" class="chart-axis">${_esc(first.label || '')}</text>` +
    `<text x="${w - 18}" y="${h - 4}" text-anchor="end" class="chart-axis">${_esc(last.label || '')}</text>`;
  const grid = [0.25, 0.5, 0.75].map(f => {
    const y = Math.round((h - 18 - f * (h - 36)) * 10) / 10;
    return `<line x1="18" x2="${w - 18}" y1="${y}" y2="${y}" class="chart-grid"/>`;
  }).join('');
  return head + grid + poly + dots + labels + '</svg>';
}

function svgSparkline(values, opts) {
  const o = Object.assign({ width: 90, height: 24, yMax: 10 }, opts || {});
  const w = Number(o.width), h = Number(o.height);
  const pts = _pts(values || [], w, h, Number(o.yMax) || 10, 2);
  const poly = pts.length > 1
    ? `<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${pts.map(p => p.join(',')).join(' ')}"/>`
    : (pts.length === 1 ? `<circle cx="${pts[0][0]}" cy="${pts[0][1]}" r="2" fill="currentColor"/>` : '');
  return `<svg class="sparkline" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">${poly}</svg>`;
}

function svgBarRow(value, max, opts) {
  const o = Object.assign({ width: 120, height: 10 }, opts || {});
  const w = Number(o.width), h = Number(o.height);
  const frac = max > 0 ? Math.max(0, Math.min(1, Number(value) / Number(max))) : 0;
  const fw = Math.round(frac * w * 10) / 10;
  return `<svg class="bar-row" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">` +
    `<rect x="0" y="0" width="${w}" height="${h}" rx="3" class="bar-bg"/>` +
    `<rect x="0" y="0" width="${fw}" height="${h}" rx="3" class="bar-fill"/></svg>`;
}

function computeTalkMetrics(segments, speakerMap) {
  const segs = (segments || []).filter(s => s && typeof s.start === 'number'
    && typeof s.end === 'number' && s.end > s.start);
  if (!segs.length) return null;

  const perSpeaker = {};
  segs.forEach(s => {
    const sp = String(s.speaker || 'UNKNOWN');
    perSpeaker[sp] = (perSpeaker[sp] || 0) + (s.end - s.start);
  });

  let roles = {};
  Object.entries(speakerMap || {}).forEach(([k, v]) => { roles[k] = String(v); });
  const unattributed = !Object.values(roles).some(v => v === 'manager');
  if (unattributed) {
    const ranked = Object.keys(perSpeaker).sort((a, b) => perSpeaker[b] - perSpeaker[a]);
    roles = {};
    if (ranked[0]) roles[ranked[0]] = 'manager';
    if (ranked[1]) roles[ranked[1]] = 'client';
  }

  let manager_sec = 0, client_sec = 0, other_sec = 0;
  Object.entries(perSpeaker).forEach(([sp, sec]) => {
    if (roles[sp] === 'manager') manager_sec += sec;
    else if (roles[sp] === 'client') client_sec += sec;
    else other_sec += sec;
  });

  let longest = 0, cur = 0, curSp = null;
  segs.forEach(s => {
    const sp = String(s.speaker || 'UNKNOWN');
    cur = sp === curSp ? cur + (s.end - s.start) : (s.end - s.start);
    curSp = sp;
    if (cur > longest) longest = cur;
  });

  const talked = manager_sec + client_sec;
  const r2 = x => Math.round(x * 100) / 100;
  return {
    manager_sec: r2(manager_sec), client_sec: r2(client_sec), other_sec: r2(other_sec),
    talk_ratio: talked > 0 ? Math.round((manager_sec / talked) * 10000) / 10000 : null,
    longest_monologue_sec: r2(longest),
    unattributed,
  };
}

if (typeof module !== 'undefined') {
  module.exports = { svgLineChart, svgSparkline, svgBarRow, computeTalkMetrics };
}
```

В `backend/static/index.html` перед строкой подключения `app.js` добавить: `<script src="charts.js"></script>`.

- [ ] **Step 4: Зелёные**

Run: `node --test backend/static/tests/ && node --check backend/static/charts.js`
Expected: PASS, exit 0.

- [ ] **Step 5: Commit**

```bash
git add backend/static/charts.js backend/static/tests/charts.test.mjs backend/static/index.html
git commit -m "feat(front): charts.js — SVG line/sparkline/bar + computeTalkMetrics (node-тесты)"
```

---

### Task 8: Фронт — страница `#dashboard`, вес в `#managers`, talk-ratio в карточке

**Files:**
- Modify: `backend/static/index.html` (nav-пункт «Дашборд» первым в `.nav-links`)
- Modify: `backend/static/app.js` (router-ветка, `renderDashboard`, правка стат-строки `renderManagers`, talk-блок в `renderCallDetail`)
- Modify: `backend/static/styles.css` (стили KPI/чартов — компактно, в духе существующих `.stat-*`)

**Interfaces:**
- Consumes: эндпоинты Task 5 (контракты из его блока Interfaces), функции Task 7, существующие `api()`, `escapeHtml()`, `showLoading()`, `moduleOn`/`isAdmin`.

- [ ] **Step 1: nav + router**

`index.html` — первым `<li>` в `.nav-links` (иконку возьми в стиле соседних, например 📊-глиф из существующего набора svg или текстовый):

```html
<li><a href="#dashboard" data-route="dashboard" class="nav-link">
  <span class="nav-icon">▤</span><span>Дашборд</span>
</a></li>
```

`app.js` `router()` — первой веткой:

```js
  if (route === 'dashboard') {
    await renderDashboard();
  } else if (route === 'calls') {
```

- [ ] **Step 2: `renderDashboard` (добавить в app.js рядом с renderManagers)**

```js
// ---- Дашборд руководителя ----
let _dashDays = 30;

const OBJECTION_RU = {
  already_contacted: 'Уже общались', no_time: 'Нет времени',
  not_interested: 'Не интересно', too_expensive: 'Дорого',
  has_broker: 'Есть свой брокер', just_looking: 'Просто смотрю',
  send_info: 'Пришлите информацию', other: 'Другое',
};

function _delta(cur, prev, invert = false) {
  if (cur == null || prev == null || prev === 0) return '';
  const d = cur - prev;
  if (Math.abs(d) < 1e-9) return '';
  const up = d > 0;
  const good = invert ? !up : up;
  const cls = good ? 'delta-good' : 'delta-bad';
  return `<span class="kpi-delta ${cls}">${up ? '↑' : '↓'} ${Math.abs(Math.round(d * 100) / 100)}</span>`;
}

async function renderDashboard() {
  showLoading();
  try {
    const granularity = _dashDays >= 90 ? 'week' : 'day';
    const [ov, managers, objections, risks] = await Promise.all([
      api(`/api/stats/overview?days=${_dashDays}&granularity=${granularity}`),
      api(`/api/stats/managers?days=${_dashDays}`),
      api(`/api/stats/objections?days=${_dashDays}`),
      api(`/api/stats/risk-calls?days=${_dashDays}&limit=10`),
    ]);
    const k = ov.kpi, p = ov.prev_kpi;
    const hasData = k.calls > 0;

    const kpiRow = `
      <div class="stats-row">
        <div class="stat-card"><div class="stat-value">${k.calls}${_delta(k.calls, p.calls)}</div><div class="stat-label">Звонков за период</div></div>
        <div class="stat-card"><div class="stat-value">${k.avg_score != null ? k.avg_score : '--'}${_delta(k.avg_score, p.avg_score)}</div><div class="stat-label">Средняя оценка</div></div>
        <div class="stat-card"><div class="stat-value">${k.risk_calls}${_delta(k.risk_calls, p.risk_calls, true)}</div><div class="stat-label">Рисковых звонков</div></div>
        <div class="stat-card"><div class="stat-value">${k.avg_talk_ratio != null ? Math.round(k.avg_talk_ratio * 100) + '%' : '--'}</div><div class="stat-label">Доля речи менеджера</div></div>
      </div>`;

    const series = (ov.series || []).map(b => ({ label: b.bucket, value: b.avg_score }));
    const trend = `
      <div class="card">
        <div class="card-header"><h3>Тренд средней оценки</h3></div>
        ${svgLineChart(series, { yMax: 10 })}
      </div>`;

    const mgrRows = managers.map(m => `
      <tr>
        <td>${escapeHtml(m.name)}</td>
        <td>${m.calls}</td>
        <td>${m.avg_score != null ? `<span style="color:${m.avg_score >= 7 ? 'var(--good)' : m.avg_score >= 4 ? 'var(--warning)' : 'var(--danger)'};font-weight:600">${m.avg_score}</span>` : '--'}</td>
        <td>${svgSparkline(m.spark)}</td>
        <td>${m.risk_calls || 0}</td>
        <td>${m.avg_talk_ratio != null ? Math.round(m.avg_talk_ratio * 100) + '%' : '--'}</td>
      </tr>`).join('');
    const mgrTable = `
      <div class="card">
        <div class="card-header"><h3>Менеджеры</h3></div>
        <table class="data-table"><thead><tr>
          <th>Менеджер</th><th>Звонки</th><th>Ср. оценка</th><th>Динамика</th><th>Риск</th><th>Речь</th>
        </tr></thead><tbody>${mgrRows || '<tr><td colspan="6">Нет данных</td></tr>'}</tbody></table>
      </div>`;

    const maxObj = Math.max(1, ...objections.map(o => o.count));
    const objRows = objections.map(o => `
      <tr>
        <td>${escapeHtml(OBJECTION_RU[o.category] || o.category)}</td>
        <td>${o.count} ${svgBarRow(o.count, maxObj)}</td>
        <td>${o.resolved_rate != null ? Math.round(o.resolved_rate * 100) + '%' : '--'}</td>
        <td class="obj-examples">${(o.examples || []).map(e => escapeHtml(e)).join(' · ')}</td>
      </tr>`).join('');
    const objBlock = `
      <div class="card">
        <div class="card-header"><h3>Возражения</h3></div>
        <table class="data-table"><thead><tr>
          <th>Категория</th><th>Сколько</th><th>Отработано</th><th>Примеры</th>
        </tr></thead><tbody>${objRows || '<tr><td colspan="4">Возражений не зафиксировано</td></tr>'}</tbody></table>
      </div>`;

    const riskRows = risks.map(r => `
      <tr class="clickable" data-sid="${escapeHtml(r.session_id)}">
        <td>${new Date(r.created_at).toLocaleDateString('ru-RU')}</td>
        <td>${escapeHtml(r.employee || '—')}</td>
        <td>${r.score != null ? r.score + '/10' : '--'}</td>
        <td>${(r.risk_flags || []).map(f => escapeHtml({low_score: 'низкая оценка', negative_sentiment: 'негатив', unresolved_objections: 'возражения'}[f] || f)).join(', ')}</td>
      </tr>`).join('');
    const riskBlock = `
      <div class="card">
        <div class="card-header"><h3>Рисковые звонки</h3></div>
        <table class="data-table" id="riskTable"><thead><tr>
          <th>Дата</th><th>Менеджер</th><th>Оценка</th><th>Причина</th>
        </tr></thead><tbody>${riskRows || '<tr><td colspan="4">Рисковых звонков нет 🎉</td></tr>'}</tbody></table>
      </div>`;

    app.innerHTML = `
      <div class="page-header">
        <h2>Дашборд</h2>
        <div class="dash-period">
          ${[7, 30, 90].map(d => `<button class="btn btn-sm ${d === _dashDays ? 'btn-primary' : ''}" data-days="${d}">${d} дн</button>`).join('')}
        </div>
      </div>
      ${hasData ? kpiRow + trend + mgrTable + objBlock + riskBlock
        : '<div class="empty-state"><p>Нет данных за период — обработайте звонки или запустите бэкфилл (worker/scripts/backfill_quality_results.py)</p></div>'}`;

    $$('.dash-period button').forEach(b => b.addEventListener('click', () => {
      _dashDays = parseInt(b.dataset.days, 10);
      renderDashboard();
    }));
    $$('#riskTable tr.clickable').forEach(tr => tr.addEventListener('click', () => {
      navigate('#call/' + tr.dataset.sid);
    }));
  } catch (err) {
    app.innerHTML = `<div class="empty-state"><p>Ошибка загрузки дашборда: ${escapeHtml(err.message)}</p></div>`;
  }
}
```

Все динамические строки — через `escapeHtml`; переходы — `data-sid` + `addEventListener`, НЕ inline-onclick со строками.

- [ ] **Step 3: Взвешенный средний в `renderManagers` (app.js:1071)**

Заменить строку `…managers.reduce((s, m) => s + m.avg_score, 0) / managers.length…` на взвешенную:

```js
const _totCalls = managers.reduce((s, m) => s + (m.total_calls || 0), 0);
const _wAvg = _totCalls ? managers.reduce((s, m) => s + (m.avg_score || 0) * (m.total_calls || 0), 0) / _totCalls : null;
```

и в разметке: `${_wAvg != null && _wAvg > 0 ? _wAvg.toFixed(1) : '--'}`.

- [ ] **Step 4: Talk-блок в `renderCallDetail`**

Сразу после блока сентимент-таймлайна (`app.js` ~625-642, ориентируйся по комментарию/разметке `sentiment-bar`) вставить (переменная транскрипта в этой функции — проверь фактическое имя, сегменты содержат `speaker/start/end`; `speaker_map` — из `session.metadata.speaker_map`, если есть):

```js
    // Кто говорил (клиентский расчёт из транскрипта; паритет с worker-метриками)
    const _tm = (typeof computeTalkMetrics === 'function' && Array.isArray(transcriptSegments))
      ? computeTalkMetrics(transcriptSegments, (session.metadata || {}).speaker_map) : null;
    const talkBlock = _tm ? `
      <div class="card">
        <div class="card-header"><h3>Кто говорил</h3></div>
        <div class="talk-split">
          <div class="talk-bar">
            <div class="talk-manager" style="width:${Math.round((_tm.talk_ratio || 0) * 100)}%"></div>
          </div>
          <div class="talk-legend">
            Менеджер${_tm.unattributed ? '*' : ''}: ${Math.round((_tm.talk_ratio || 0) * 100)}% ·
            Клиент: ${Math.round((1 - (_tm.talk_ratio || 0)) * 100)}% ·
            Длиннейший монолог: ${Math.round(_tm.longest_monologue_sec)}с
            ${_tm.unattributed ? '<div class="talk-note">* роли определены эвристикой (кто говорил больше)</div>' : ''}
          </div>
        </div>
      </div>` : '';
```

и включить `${talkBlock}` в собираемый HTML детального вида после сентимент-блока. `transcriptSegments` — замени на фактическое имя массива сегментов в `renderCallDetail` (см. как рендерится транскрипт там же ниже).

- [ ] **Step 5: Стили (styles.css, в конец)**

```css
/* --- Дашборд руководителя --- */
.dash-period { display: flex; gap: 6px; }
.kpi-delta { font-size: 0.65em; margin-left: 6px; vertical-align: middle; }
.delta-good { color: var(--good); }
.delta-bad { color: var(--danger); }
.chart-line .chart-grid { stroke: var(--border, #44403c); stroke-width: 0.5; opacity: 0.4; }
.chart-line .chart-axis, .chart-line .chart-empty { fill: var(--text-secondary, #a8a29e); font-size: 10px; }
.sparkline { color: var(--accent, #b45309); }
.bar-row .bar-bg { fill: var(--border, #44403c); opacity: 0.35; }
.bar-row .bar-fill { fill: var(--accent, #b45309); }
.obj-examples { font-size: 0.85em; color: var(--text-secondary, #a8a29e); max-width: 380px; }
.talk-split { padding: 12px 16px; }
.talk-bar { height: 14px; border-radius: 7px; background: var(--border, #44403c); overflow: hidden; }
.talk-manager { height: 100%; background: var(--accent, #b45309); }
.talk-legend { margin-top: 8px; font-size: 0.9em; color: var(--text-secondary, #a8a29e); }
.talk-note { font-size: 0.8em; opacity: 0.7; }
```

Проверь фактические имена CSS-переменных в `styles.css` (`--good/--warning/--danger` уже используются в app.js:1098; для accent/border/text-secondary найди реальные из темы «Claude-warm» и подставь).

- [ ] **Step 6: Синтаксис-проверка**

Run: `node --check backend/static/app.js && node --check backend/static/charts.js && node --test backend/static/tests/`
Expected: exit 0, тесты зелёные.

- [ ] **Step 7: Commit**

```bash
git add backend/static/app.js backend/static/index.html backend/static/styles.css
git commit -m "feat(front): страница #dashboard (KPI+тренд+менеджеры+возражения+риски), взвешенный avg в #managers, talk-ratio в карточке звонка"
```

---

### Task 9: Бэкфилл истории

**Files:**
- Create: `worker/scripts/backfill_quality_results.py`
- Test: `worker/tests/test_backfill_quality_results.py`

**Interfaces:**
- Consumes: `results_db.extract_objections/compute_talk_metrics/sentiment_counts_from/compute_risk_flags/upsert_quality_result` (Task 2/3), `tenancy.registry.iter_active_tenants`, `tenancy.context.set/reset_tenant_schema`, `tenancy.paths.tenant_results_dir`.
- Produces: CLI `python scripts/backfill_quality_results.py --tenant <slug>|--all [--apply]` (dry-run по умолчанию, конвенция `cleanup_zoom_template.py`). Тестируемая функция `backfill_tenant(slug, results_root, apply=False) -> dict` → `{"found": int, "upserted": int, "skipped": int}`.

- [ ] **Step 1: Падающие тесты**

`worker/tests/test_backfill_quality_results.py`:

```python
"""Бэкфилл: сборка строки из файлов + dry-run/apply + скип без сессии."""
import json

import scripts.backfill_quality_results as bf


def _mk_session_dir(tmp_path, slug, sid, quality, transcript=None, sentiment=None, card=None):
    d = tmp_path / slug / sid
    d.mkdir(parents=True)
    (d / "quality.json").write_text(json.dumps(quality, ensure_ascii=False), encoding="utf-8")
    if transcript is not None:
        (d / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")
    if sentiment is not None:
        (d / "sentiment.json").write_text(json.dumps(sentiment), encoding="utf-8")
    if card is not None:
        (d / "card.json").write_text(json.dumps(card), encoding="utf-8")
    return d


def test_dry_run_counts_but_does_not_upsert(tmp_path, monkeypatch):
    _mk_session_dir(tmp_path, "acme", "sid-1", {"overall_score": 7, "version": 4})
    upserts = []
    monkeypatch.setattr(bf, "_upsert_from_files", lambda *a, **k: upserts.append(1) or True)
    monkeypatch.setattr(bf, "_session_exists", lambda sid: True)
    stats = bf.backfill_tenant("acme", tmp_path, apply=False)
    assert stats == {"found": 1, "upserted": 0, "skipped": 0}
    assert not upserts


def test_apply_upserts(tmp_path, monkeypatch):
    _mk_session_dir(tmp_path, "acme", "sid-1", {"overall_score": 7, "version": 4},
                    transcript=[{"speaker": "A", "start": 0, "end": 5, "text": "х"}],
                    sentiment=[{"sentiment": "positive"}])
    seen = {}
    monkeypatch.setattr(bf, "_upsert_from_files",
                        lambda sid, quality, transcript, sentiment, card: seen.update(
                            sid=sid, q=quality) or True)
    monkeypatch.setattr(bf, "_session_exists", lambda sid: True)
    stats = bf.backfill_tenant("acme", tmp_path, apply=True)
    assert stats["upserted"] == 1 and seen["sid"] == "sid-1"


def test_missing_session_row_skipped(tmp_path, monkeypatch):
    _mk_session_dir(tmp_path, "acme", "sid-ghost", {"overall_score": 5})
    monkeypatch.setattr(bf, "_session_exists", lambda sid: False)
    stats = bf.backfill_tenant("acme", tmp_path, apply=True)
    assert stats == {"found": 1, "upserted": 0, "skipped": 1}


def test_dir_without_quality_json_ignored(tmp_path, monkeypatch):
    (tmp_path / "acme" / "sid-noq").mkdir(parents=True)
    monkeypatch.setattr(bf, "_session_exists", lambda sid: True)
    stats = bf.backfill_tenant("acme", tmp_path, apply=True)
    assert stats == {"found": 0, "upserted": 0, "skipped": 0}
```

- [ ] **Step 2: Падает** — модуля нет. Проверь, что `worker/pytest.ini` `pythonpath=.` покрывает импорт `scripts.` (в `worker/scripts/` должен быть `__init__.py` — посмотри, как импортируются существующие скрипты в тестах; если пакета нет — добавь пустой `worker/scripts/__init__.py`).

- [ ] **Step 3: Реализация `worker/scripts/backfill_quality_results.py`**

```python
"""Бэкфилл quality_results из исторических JSON-файлов результатов.

Dry-run по умолчанию; --apply для записи. Идемпотентен (upsert) — безопасно
гонять повторно, в т.ч. после reassess_quality.py (тот пишет только файлы,
эта команда доносит изменения до таблицы).

  python scripts/backfill_quality_results.py --tenant acme          # dry-run
  python scripts/backfill_quality_results.py --all --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # worker/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))          # repo root

from tenancy.context import reset_tenant_schema, set_tenant_schema
from tenancy.db import tenant_connect
from tenancy.identifiers import schema_for_slug
from tenancy.registry import iter_active_tenants

from tasks.results_db import record_quality_result  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill")

RESULTS_ROOT = os.getenv("RESULTS_STORAGE_PATH", "./data/results")


def _load(d: Path, name: str):
    f = d / name
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        logger.warning(f"  битый {f} — пропускаю файл")
        return None


def _session_exists(session_id: str) -> bool:
    conn = tenant_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM sessions WHERE id = %s", (session_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def _upsert_from_files(session_id: str, quality, transcript, sentiment, card) -> bool:
    # record_quality_result сам читает сессию, считает метрики и upsert'ит;
    # skip_reason достаём из самого отчёта (гейтовые отчёты его содержат).
    flags = record_quality_result(
        session_id, quality, card=card, transcript=transcript,
        sentiment_results=sentiment,
        skip_reason=(quality or {}).get("skip_reason"))
    return True  # best-effort: ошибки уже залогированы внутри


def backfill_tenant(slug: str, results_root, apply: bool = False) -> dict:
    stats = {"found": 0, "upserted": 0, "skipped": 0}
    tenant_dir = Path(results_root) / slug
    if not tenant_dir.is_dir():
        logger.info(f"[{slug}] нет каталога результатов: {tenant_dir}")
        return stats
    for session_dir in sorted(p for p in tenant_dir.iterdir() if p.is_dir()):
        quality = _load(session_dir, "quality.json")
        if quality is None:
            continue
        stats["found"] += 1
        sid = session_dir.name
        if not _session_exists(sid):
            logger.warning(f"[{slug}] {sid}: нет строки sessions — скип")
            stats["skipped"] += 1
            continue
        if not apply:
            continue
        _upsert_from_files(sid, quality,
                           _load(session_dir, "transcript.json"),
                           _load(session_dir, "sentiment.json"),
                           _load(session_dir, "card.json"))
        stats["upserted"] += 1
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tenant", help="слаг одного тенанта")
    g.add_argument("--all", action="store_true", help="все активные тенанты")
    ap.add_argument("--apply", action="store_true",
                    help="реально писать (без флага — dry-run)")
    args = ap.parse_args()

    slugs = ([args.tenant] if args.tenant
             else [t["slug"] for t in iter_active_tenants()])
    mode = "APPLY" if args.apply else "DRY-RUN"
    total = {"found": 0, "upserted": 0, "skipped": 0}
    for slug in slugs:
        token = set_tenant_schema(schema_for_slug(slug))
        try:
            stats = backfill_tenant(slug, RESULTS_ROOT, apply=args.apply)
        finally:
            reset_tenant_schema(token)
        logger.info(f"[{mode}] {slug}: найдено {stats['found']}, "
                    f"записано {stats['upserted']}, скип {stats['skipped']}")
        for k in total:
            total[k] += stats[k]
    logger.info(f"[{mode}] ИТОГО: {total}")
    if not args.apply and total["found"]:
        logger.info("Повтори с --apply для записи.")


if __name__ == "__main__":
    main()
```

Также допиши в docstring `worker/scripts/reassess_quality.py` одну строку: «После массовой переоценки прогони backfill_quality_results.py --apply — таблица quality_results обновится из файлов.»

- [ ] **Step 4: Зелёные + no_raw_connects**

Run: `cd /root/projects/silentqa-dev/worker && ../.venv/bin/python -m pytest -q`
Expected: PASS (test_no_raw_connects сканирует `worker/tasks` — скрипт использует `tenant_connect`, нарушений нет).

- [ ] **Step 5: Commit**

```bash
git add worker/scripts/backfill_quality_results.py worker/tests/test_backfill_quality_results.py worker/scripts/reassess_quality.py
git commit -m "feat(worker): бэкфилл quality_results из исторических JSON (dry-run/--apply, идемпотентен)"
```

---

### Task 10: Интеграция — полные сьюты, CLAUDE.md, финальная сверка со спеком

**Files:**
- Modify: `CLAUDE.md` (три точечных упоминания)
- Проверка: все сьюты, `node --check`, спек-чеклист

- [ ] **Step 1: Полные прогоны**

```bash
cd /root/projects/silentqa-dev/backend && ../.venv/bin/python -m pytest tests/ -q
cd /root/projects/silentqa-dev/worker && ../.venv/bin/python -m pytest -q
node --check /root/projects/silentqa-dev/backend/static/app.js
node --check /root/projects/silentqa-dev/backend/static/charts.js
node --test /root/projects/silentqa-dev/backend/static/tests/
```
Expected: всё зелёное. Любой красный — чинить ДО коммита.

- [ ] **Step 2: CLAUDE.md — дописать факты (кратко, в существующие разделы)**

1. В раздел про пайплайн (после описания стадий): «Стадия 2 и оба гейта пишут денормализованную строку в тенант-таблицу `quality_results` (`worker/tasks/results_db.py`, best-effort — сбой записи не фейлит пайплайн); рисковые звонки шлют Telegram-алерт (`worker/tasks/alerts.py`, env `TELEGRAM_BOT_TOKEN` + `RISK_ALERT_CHAT_ID`/company-config `alerts.telegram_chat_id`).»
2. В раздел про дашборд/клиентские поверхности: «`#dashboard` — дашборд руководителя (`/api/stats/*` поверх `quality_results`); графики — `static/charts.js` (рукописный SVG, node-тестируется).»
3. В раздел ops-скриптов worker: добавить `backfill_quality_results.py` в перечень.

- [ ] **Step 3: Сверка со спеком** — открой `docs/superpowers/specs/2026-07-05-manager-dashboard-design.md`, пройди §4-§9: каждая позиция реализована? Особо: best-effort инвариант (§2), формы ответов §6, empty-state §7, dry-run §8.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): quality_results, /api/stats, #dashboard, алерты, бэкфилл"
```

---

## Раскатка (после мерджа, вне плана — руками владельца)

1. `scripts/deploy_prod.sh` (миграция 018 применится ко всем тенантам до рестарта).
2. `cd /root/projects/silentqa/worker && ../.venv/bin/python scripts/backfill_quality_results.py --all` → проверить dry-run → `--apply`.
3. Задать `RISK_ALERT_CHAT_ID` в прод `.env` (иначе алерты в лог — сознательная деградация).
