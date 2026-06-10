# Extraction Templates and Complex DB — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a structured-extraction processing path (parallel to existing call quality assessment) that pulls structured facts from complex-presentation recordings, auto-aggregates multiple recordings about the same complex into a knowledge base, and exposes a Templates editor + «База ЖК» page in the dashboard.

**Architecture:** Three new tables (`extraction_templates`, `complex_extractions`, `complexes`) drive a second branch in the existing Celery pipeline. Templates are stored in DB and edited in UI. Branching happens in `pipeline.process_session` based on the template's `kind`. Aggregation uses normalized `(name, developer)` matching across extractions to one complex. Mutations protected by the existing `DELETE_PASSWORD`.

**Tech Stack:** FastAPI, SQLAlchemy 2 async + Alembic, Celery, OpenAI Python SDK (`gpt-5.4` with Responses API + `response_format=json_schema`), vanilla JS SPA dashboard, pytest.

**Spec:** [`docs/superpowers/specs/2026-04-27-extraction-templates-and-complex-db-design.md`](../specs/2026-04-27-extraction-templates-and-complex-db-design.md)

---

## File Structure

**Create:**
- `backend/alembic/versions/004_extraction_templates_and_complexes.py` — migration + seed
- `backend/app/routes/templates.py` — Templates CRUD
- `backend/app/routes/complexes.py` — Complexes API + extraction relink
- `backend/app/schemas_templates.py` — Pydantic schemas for templates/complexes
- `worker/tasks/extract.py` — OpenAI extraction task
- `worker/tasks/complex_match.py` — normalization + match-or-create + aggregation
- `worker/tasks/_text_normalize.py` — `normalize_complex_name`, `normalize_developer`
- `worker/tests/test_text_normalize.py` — unit tests for normalization
- `worker/tests/test_complex_match.py` — unit tests for match-or-create + aggregation
- `worker/tests/test_extraction_aggregation.py` — unit tests for `recompute_aggregate`

**Modify:**
- `backend/app/models.py` — add `ExtractionTemplate`, `ComplexExtraction`, `Complex`
- `backend/app/main.py` — include new routers
- `backend/app/routes/sessions.py` — `template_id` on upload, `/reprocess` endpoint, extension auth
- `backend/app/routes/chunks.py` — accept `template_id` in `upload-audio` form
- `backend/app/main.py` — guard `DELETE/POST/PATCH` on `/api/templates/*` and `/api/complexes/*` with `X-Delete-Password`
- `worker/tasks/pipeline.py` — branch on `template.kind` (extraction vs evaluation)
- `worker/tasks/celery_app.py` — register `extract` task
- `backend/static/app.js` — Templates page, upload dropdown, session detail extraction view + reprocess, complexes list + detail
- `backend/static/styles.css` — minor styles for new pages
- `backend/static/index.html` — add sidebar links for «Шаблоны» and «База ЖК»

---

## Task 1: DB migration with seeded «Презентация ЖК» template

**Files:**
- Create: `backend/alembic/versions/004_extraction_templates_and_complexes.py`

- [ ] **Step 1: Create the migration file**

```python
"""Add extraction_templates, complex_extractions, complexes tables

Revision ID: 004
Revises: 003
Create Date: 2026-04-27
"""
import json
import uuid
from typing import Sequence, Union

from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PRESENTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "name":      {"type": ["string", "null"], "description": "Название ЖК"},
        "developer": {"type": ["string", "null"], "description": "Название застройщика"},
        "class":     {"type": ["string", "null"], "description": "эконом / комфорт / комфорт+ / бизнес / премиум / элит"},
        "location": {
            "type": "object",
            "properties": {
                "district":     {"type": ["string", "null"]},
                "address":      {"type": ["string", "null"]},
                "parks":        {"type": "array", "items": {"type": "string"}},
                "embankments":  {"type": "array", "items": {"type": "string"}},
                "transport":    {"type": "array", "items": {"type": "string"}, "description": "Метро/автобусы/дороги, время до точек"},
                "malls":        {"type": "array", "items": {"type": "string"}},
                "venues":       {"type": "array", "items": {"type": "string"}, "description": "Театры, рестораны и т.п."},
                "future_plans": {"type": ["string", "null"], "description": "Что будет в районе в будущем"}
            }
        },
        "architecture": {
            "type": "object",
            "properties": {
                "style":        {"type": ["string", "null"], "description": "модернизм/неоклассика/ар-деко/бионический и т.п."},
                "materials":    {"type": "array", "items": {"type": "string"}, "description": "натуральный камень, клинкер, медные панели и т.п."},
                "phases":       {"type": ["integer", "null"], "description": "Количество очередей"},
                "buildings":    {"type": ["integer", "null"], "description": "Количество корпусов"},
                "floors":       {"type": "array", "items": {"type": "string"}, "description": "Этажности корпусов с пояснениями"},
                "layouts_note": {"type": ["string", "null"], "description": "Описание выбора планировок"}
            }
        },
        "amenities": {
            "type": "object",
            "properties": {
                "lobby":            {"type": ["boolean", "null"]},
                "concierge":        {"type": ["boolean", "null"]},
                "meeting_rooms":    {"type": ["boolean", "null"]},
                "coworking":        {"type": ["boolean", "null"]},
                "guest_entrance":   {"type": ["boolean", "null"]},
                "observation_deck": {"type": ["boolean", "null"]},
                "fitness":          {"type": ["boolean", "null"]},
                "parking":          {"type": ["string", "null"]},
                "engineering":      {"type": "array", "items": {"type": "string"}, "description": "VRV, фанкойл, очистка воздуха/воды и т.п."},
                "yard": {
                    "type": "object",
                    "properties": {
                        "area_ha": {"type": ["number", "null"]},
                        "zones":   {"type": "array", "items": {"type": "string"}}
                    }
                }
            }
        },
        "delivery": {
            "type": "object",
            "properties": {
                "overall_year":    {"type": ["integer", "null"]},
                "overall_quarter": {"type": ["integer", "null"]},
                "by_phase": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "phase":   {"type": ["string", "null"]},
                            "year":    {"type": ["integer", "null"]},
                            "quarter": {"type": ["integer", "null"]}
                        }
                    }
                }
            }
        },
        "finishes": {
            "type": "object",
            "properties": {
                "options":       {"type": "array", "items": {"type": "string"}, "description": "без / white box / чистовая / дизайнерская"},
                "design_styles": {"type": "array", "items": {"type": "string"}}
            }
        },
        "pricing": {
            "type": "object",
            "properties": {
                "cash":        {"type": ["string", "null"], "description": "Цена при 100% оплате"},
                "mortgage":    {"type": ["string", "null"], "description": "Условия по ипотеке"},
                "installment": {"type": ["string", "null"], "description": "Условия по рассрочке"},
                "min_price":   {"type": ["number", "null"]},
                "currency":    {"type": ["string", "null"]}
            }
        },
        "additional_info": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Любые факты, не вошедшие в стандартные поля выше"
        }
    },
    "required": ["name", "developer", "additional_info"]
}

PRESENTATION_PROMPT = (
    "Ты извлекаешь структурированные факты о жилом комплексе из транскрипта презентации. "
    "Презентует обычно представитель застройщика или брокер. "
    "Заполни поля JSON Schema. Если факт не упомянут — оставь null (для скаляров) или [] (для массивов). "
    "НЕ выдумывай и не додумывай факты, которых нет в транскрипте. "
    "В additional_info собери всё значимое, что не попало в стандартные поля."
)


def upgrade() -> None:
    op.execute("""CREATE TABLE extraction_templates (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name VARCHAR(200) NOT NULL UNIQUE,
        description TEXT,
        kind VARCHAR(20) NOT NULL,
        prompt TEXT NOT NULL,
        json_schema JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")

    op.execute("""CREATE TABLE complexes (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name VARCHAR(300) NOT NULL,
        name_normalized VARCHAR(300) NOT NULL,
        developer VARCHAR(200),
        developer_normalized VARCHAR(200) NOT NULL DEFAULT '',
        class VARCHAR(50),
        district VARCHAR(200),
        aggregated_data JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE UNIQUE INDEX idx_complexes_norm ON complexes(name_normalized, developer_normalized)")
    op.execute("CREATE INDEX idx_complexes_developer ON complexes(developer)")
    op.execute("CREATE INDEX idx_complexes_class ON complexes(class)")

    op.execute("""CREATE TABLE complex_extractions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        template_id UUID NOT NULL REFERENCES extraction_templates(id) ON DELETE RESTRICT,
        complex_id UUID REFERENCES complexes(id) ON DELETE SET NULL,
        raw_data JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX idx_extractions_session ON complex_extractions(session_id)")
    op.execute("CREATE INDEX idx_extractions_complex ON complex_extractions(complex_id)")

    # Seed «Презентация ЖК»
    op.execute(f"""INSERT INTO extraction_templates (name, description, kind, prompt, json_schema)
        VALUES (
            'Презентация ЖК',
            'Извлечение структурированных данных из презентации жилого комплекса (застройщик или брокер презентует ЖК).',
            'extraction',
            $${PRESENTATION_PROMPT}$$,
            $${json.dumps(PRESENTATION_SCHEMA, ensure_ascii=False)}$$::jsonb
        )""")


def downgrade() -> None:
    op.drop_table("complex_extractions")
    op.drop_index("idx_complexes_class")
    op.drop_index("idx_complexes_developer")
    op.drop_index("idx_complexes_norm")
    op.drop_table("complexes")
    op.drop_table("extraction_templates")
```

- [ ] **Step 2: Apply migration to dev DB**

Run:
```bash
cd /root/projects/realestate/backend && alembic upgrade head
```

Expected output: `INFO  [alembic.runtime.migration] Running upgrade 003 -> 004, Add extraction_templates...`.

- [ ] **Step 3: Verify schema and seed**

Run:
```bash
PGPASSWORD=$(grep ^POSTGRES_PASSWORD /root/projects/realestate/.env | cut -d= -f2) psql -h localhost -U realestate -d realestate -c "\d extraction_templates" -c "\d complexes" -c "\d complex_extractions" -c "SELECT name, kind FROM extraction_templates"
```

Expected: three tables present with the columns above; one row `Презентация ЖК | extraction`.

- [ ] **Step 4: Verify rollback works (then re-apply)**

Run:
```bash
cd /root/projects/realestate/backend && alembic downgrade 003 && alembic upgrade head
```

Expected: both commands succeed, final schema state matches Step 3.

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/004_extraction_templates_and_complexes.py
git commit -m "feat(db): add extraction_templates, complexes, complex_extractions tables"
```

---

## Task 2: Normalization functions (TDD)

**Files:**
- Create: `worker/tasks/_text_normalize.py`
- Create: `worker/tests/test_text_normalize.py`

- [ ] **Step 1: Write the failing tests**

```python
# worker/tests/test_text_normalize.py
import pytest

from tasks._text_normalize import normalize_complex_name, normalize_developer


@pytest.mark.parametrize("raw,expected", [
    ("Шагал", "шагал"),
    ("ЖК Шагал", "шагал"),
    ("ЖК «Шагал»", "шагал"),
    ('ЖК "Шагал"', "шагал"),
    ("жилой комплекс Шагал", "шагал"),
    ("Жилой Комплекс Шагал-2", "шагал 2"),
    ("МФК Шагалёв", "шагалев"),
    ("  Шагал   ", "шагал"),
    ("Апарт-комплекс Шагал", "шагал"),
    ("ЖК «Шагал».", "шагал"),
    ("Жилой район «Шагал»", "шагал"),
    ("", ""),
    (None, ""),
])
def test_normalize_complex_name(raw, expected):
    assert normalize_complex_name(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Эталон", "эталон"),
    ("ГК Эталон", "эталон"),
    ('Группа компаний "Эталон"', "эталон"),
    ("ООО Эталон", "эталон"),
    ("PIK", "pik"),
    ("Самолёт", "самолет"),
    ("", ""),
    (None, ""),
])
def test_normalize_developer(raw, expected):
    assert normalize_developer(raw) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/projects/realestate/worker && ../.venv/bin/pytest tests/test_text_normalize.py -v
```

Expected: `ModuleNotFoundError: No module named 'tasks._text_normalize'`.

- [ ] **Step 3: Implement normalization**

```python
# worker/tasks/_text_normalize.py
"""Normalization helpers for matching complex/developer names across extractions."""
from __future__ import annotations

import re

# Order matters: longest prefix first so "жилой комплекс" matches before "жилой"
_COMPLEX_PREFIXES = (
    "апарт-комплекс",
    "жилой комплекс",
    "жилой район",
    "мфк",
    "жк",
)
_DEVELOPER_PREFIXES = (
    "группа компаний",
    "гк",
    "ооо",
    "оао",
    "зао",
    "ао",
    "пао",
)
_QUOTES = '«»""“”‘’‚'


def _basic_normalize(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower()
    s = s.replace("ё", "е")
    # strip quotes
    for q in _QUOTES:
        s = s.replace(q, "")
    # punctuation/separators -> space
    s = re.sub(r"[\.,;:!\?\-_/\\]+", " ", s)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _strip_prefix(s: str, prefixes: tuple[str, ...]) -> str:
    for p in prefixes:
        if s == p:
            return ""
        if s.startswith(p + " "):
            return s[len(p) + 1:].strip()
    return s


def normalize_complex_name(raw: str | None) -> str:
    s = _basic_normalize(raw)
    return _strip_prefix(s, _COMPLEX_PREFIXES)


def normalize_developer(raw: str | None) -> str:
    s = _basic_normalize(raw)
    return _strip_prefix(s, _DEVELOPER_PREFIXES)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
cd /root/projects/realestate/worker && ../.venv/bin/pytest tests/test_text_normalize.py -v
```

Expected: 21 passed.

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/_text_normalize.py worker/tests/test_text_normalize.py
git commit -m "feat(worker): name+developer normalization for complex matching"
```

---

## Task 3: Aggregation logic (TDD, pure functions)

**Files:**
- Create: `worker/tasks/complex_match.py` (will grow in Task 4 — for now only `_merge_aggregate`)
- Create: `worker/tests/test_extraction_aggregation.py`

- [ ] **Step 1: Write the failing tests**

```python
# worker/tests/test_extraction_aggregation.py
"""Tests for incrementally merging an extraction into the aggregated profile.

Rules (per spec):
- Scalar: latest non-null wins (newest extraction first in input list).
- List of strings: union + dedup (case-insensitive after normalize).
- Dict: recursive.
- additional_info[]: each element wrapped with {note, session_id, recorded_at} — never deduped.
"""
from datetime import datetime, timezone

import pytest

from tasks.complex_match import merge_extractions


def _ext(raw, when, session_id="sess1"):
    return {"raw_data": raw, "created_at": when, "session_id": session_id}


def test_merge_single_extraction_returns_same_data_with_attribution():
    ext = _ext({"name": "Шагал", "additional_info": ["новая школа рядом"]},
               datetime(2026, 4, 1, tzinfo=timezone.utc), "s1")
    agg = merge_extractions([ext])
    assert agg["name"] == "Шагал"
    assert agg["additional_info"] == [
        {"note": "новая школа рядом", "session_id": "s1",
         "recorded_at": "2026-04-01T00:00:00+00:00"}
    ]


def test_scalar_latest_wins():
    new = _ext({"name": "Шагал-2"}, datetime(2026, 4, 10, tzinfo=timezone.utc), "s2")
    old = _ext({"name": "Шагал"}, datetime(2026, 4, 1, tzinfo=timezone.utc), "s1")
    agg = merge_extractions([new, old])  # newer first
    assert agg["name"] == "Шагал-2"


def test_scalar_falls_back_when_newest_is_null():
    new = _ext({"name": None, "developer": "Эталон"}, datetime(2026, 4, 10, tzinfo=timezone.utc))
    old = _ext({"name": "Шагал"}, datetime(2026, 4, 1, tzinfo=timezone.utc))
    agg = merge_extractions([new, old])
    assert agg["name"] == "Шагал"
    assert agg["developer"] == "Эталон"


def test_list_union_dedup_case_insensitive():
    e1 = _ext({"location": {"parks": ["Парк Победы", "Воробьёвы горы"]}},
              datetime(2026, 4, 1, tzinfo=timezone.utc))
    e2 = _ext({"location": {"parks": ["парк победы", "Парк Горького"]}},
              datetime(2026, 4, 5, tzinfo=timezone.utc))
    agg = merge_extractions([e2, e1])  # newer first
    parks = agg["location"]["parks"]
    assert len(parks) == 3
    assert "Парк Победы" in parks  # originals preserved
    assert "Воробьёвы горы" in parks
    assert "Парк Горького" in parks


def test_nested_dicts_merge_recursively():
    e1 = _ext({"pricing": {"cash": "от 25 млн"}}, datetime(2026, 4, 1, tzinfo=timezone.utc))
    e2 = _ext({"pricing": {"mortgage": "5% на 30 лет"}}, datetime(2026, 4, 5, tzinfo=timezone.utc))
    agg = merge_extractions([e2, e1])
    assert agg["pricing"]["cash"] == "от 25 млн"
    assert agg["pricing"]["mortgage"] == "5% на 30 лет"


def test_additional_info_collected_with_attribution_no_dedup():
    e1 = _ext({"additional_info": ["скидка 5% по промо", "офис продаж в ТЦ"]},
              datetime(2026, 4, 1, tzinfo=timezone.utc), "s1")
    e2 = _ext({"additional_info": ["скидка 5% по промо"]},  # same string, kept twice
              datetime(2026, 4, 5, tzinfo=timezone.utc), "s2")
    agg = merge_extractions([e2, e1])
    notes = agg["additional_info"]
    assert len(notes) == 3
    assert {n["session_id"] for n in notes} == {"s1", "s2"}


def test_empty_input_returns_empty_dict():
    assert merge_extractions([]) == {}


def test_empty_strings_treated_as_null_for_scalars():
    new = _ext({"name": "", "developer": "Эталон"}, datetime(2026, 4, 10, tzinfo=timezone.utc))
    old = _ext({"name": "Шагал"}, datetime(2026, 4, 1, tzinfo=timezone.utc))
    agg = merge_extractions([new, old])
    assert agg["name"] == "Шагал"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/projects/realestate/worker && ../.venv/bin/pytest tests/test_extraction_aggregation.py -v
```

Expected: `ModuleNotFoundError: No module named 'tasks.complex_match'`.

- [ ] **Step 3: Implement `merge_extractions`**

```python
# worker/tasks/complex_match.py
"""Match-or-create complex profile + aggregation.

Phase 1 of this module is the pure aggregation logic (this commit).
Phase 2 (next task) adds DB-bound match_or_create_complex.
"""
from __future__ import annotations

from typing import Any

from tasks._text_normalize import _basic_normalize


def _is_empty_scalar(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _dedup_strings(values: list[str]) -> list[str]:
    """Order-preserving dedup with case-insensitive normalized comparison."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if not isinstance(v, str):
            continue
        key = _basic_normalize(v)
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def _merge_field(newest_first: list[Any]) -> Any:
    """Merge a single field's values from extractions (newest first).

    Scalars: first non-empty wins.
    Lists of strings: union + dedup, preserving the newest extraction's order.
    Dicts: recursive merge by key.
    Lists of dicts: union without dedup (dedup is only meaningful for strings).
    """
    if not newest_first:
        return None

    sample = next((v for v in newest_first if v is not None), None)
    if sample is None:
        return None

    if isinstance(sample, dict):
        keys: list[str] = []
        for v in newest_first:
            if isinstance(v, dict):
                for k in v.keys():
                    if k not in keys:
                        keys.append(k)
        return {k: _merge_field([v.get(k) for v in newest_first if isinstance(v, dict)]) for k in keys}

    if isinstance(sample, list):
        # decide: list of strings vs list of dicts
        flat: list[Any] = []
        for v in newest_first:
            if isinstance(v, list):
                flat.extend(v)
        if all(isinstance(x, str) for x in flat):
            return _dedup_strings(flat)
        return flat  # list of dicts: keep all without dedup

    # scalar
    for v in newest_first:
        if not _is_empty_scalar(v):
            return v
    return None


def merge_extractions(extractions_newest_first: list[dict]) -> dict:
    """Build aggregated profile from a list of extractions sorted newest first.

    Each extraction is a dict with keys: raw_data, created_at, session_id.
    Special handling for `additional_info` — wrapped with attribution, no dedup.
    """
    if not extractions_newest_first:
        return {}

    # All keys across all raw_data
    all_keys: list[str] = []
    for e in extractions_newest_first:
        for k in (e.get("raw_data") or {}).keys():
            if k not in all_keys:
                all_keys.append(k)

    out: dict = {}
    for k in all_keys:
        if k == "additional_info":
            notes: list[dict] = []
            # collect oldest-first so reading order is chronological
            for e in reversed(extractions_newest_first):
                values = (e.get("raw_data") or {}).get("additional_info") or []
                ts = e.get("created_at")
                ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts) if ts is not None else None
                for note in values:
                    if not isinstance(note, str) or not note.strip():
                        continue
                    notes.append({
                        "note": note,
                        "session_id": e.get("session_id"),
                        "recorded_at": ts_str,
                    })
            out["additional_info"] = notes
        else:
            values = [(e.get("raw_data") or {}).get(k) for e in extractions_newest_first]
            out[k] = _merge_field(values)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
cd /root/projects/realestate/worker && ../.venv/bin/pytest tests/test_extraction_aggregation.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/complex_match.py worker/tests/test_extraction_aggregation.py
git commit -m "feat(worker): pure-function aggregation for complex profiles"
```

---

## Task 4: ORM models for new tables

**Files:**
- Modify: `backend/app/models.py`

- [ ] **Step 1: Append model definitions**

At the end of `backend/app/models.py`, after the `Chunk` class, add:

```python
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB


class ExtractionTemplate(Base):
    __tablename__ = "extraction_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # 'extraction' | 'evaluation'
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    json_schema: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Complex(Base):
    __tablename__ = "complexes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(300), nullable=False)
    developer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    developer_normalized: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    class_: Mapped[str | None] = mapped_column("class", String(50), nullable=True)
    district: Mapped[str | None] = mapped_column(String(200), nullable=True)
    aggregated_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("name_normalized", "developer_normalized", name="idx_complexes_norm"),)


class ComplexExtraction(Base):
    __tablename__ = "complex_extractions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction_templates.id", ondelete="RESTRICT"), nullable=False)
    complex_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("complexes.id", ondelete="SET NULL"), nullable=True)
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 2: Verify models load without errors**

Run:
```bash
cd /root/projects/realestate/backend && ../.venv/bin/python -c "from app.models import ExtractionTemplate, Complex, ComplexExtraction; print('OK')"
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/models.py
git commit -m "feat(models): add ExtractionTemplate, Complex, ComplexExtraction"
```

---

## Task 5: Match-or-create complex (sync, used by Celery worker)

**Files:**
- Modify: `worker/tasks/complex_match.py`
- Create: `worker/tests/test_complex_match.py`

- [ ] **Step 1: Write the failing tests**

```python
# worker/tests/test_complex_match.py
"""Integration tests for match_or_create_complex against a real PostgreSQL DB.

Requires DATABASE_URL_SYNC pointing to the dev DB. Tests create their own data
inside a transaction that's rolled back at the end.
"""
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session as DbSession

from tasks.complex_match import match_or_create_complex


@pytest.fixture
def db():
    url = os.environ["DATABASE_URL_SYNC"]
    eng = create_engine(url, future=True)
    with eng.connect() as conn:
        with conn.begin() as trans:
            with DbSession(bind=conn, expire_on_commit=False) as s:
                yield s
            trans.rollback()


@pytest.fixture
def template_id(db):
    row = db.execute(text("SELECT id FROM extraction_templates WHERE name='Презентация ЖК'")).first()
    assert row, "seed template missing — run alembic upgrade head first"
    return row[0]


@pytest.fixture
def session_id(db):
    sid = uuid.uuid4()
    db.execute(text("INSERT INTO sessions (id, status) VALUES (:id, 'completed')"), {"id": sid})
    return sid


def _insert_extraction(db, session_id, template_id, raw):
    eid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO complex_extractions (id, session_id, template_id, raw_data)
        VALUES (:id, :sid, :tid, CAST(:raw AS jsonb))
    """), {"id": eid, "sid": session_id, "tid": template_id, "raw": __import__("json").dumps(raw)})
    return eid


def test_creates_new_complex_when_none_exists(db, template_id, session_id):
    eid = _insert_extraction(db, session_id, template_id, {
        "name": "Шагал", "developer": "Эталон", "class": "бизнес", "additional_info": []
    })
    cid = match_or_create_complex(db, eid)
    assert cid is not None
    row = db.execute(text("SELECT name, name_normalized, developer FROM complexes WHERE id=:id"), {"id": cid}).first()
    assert row.name == "Шагал"
    assert row.name_normalized == "шагал"
    assert row.developer == "Эталон"


def test_matches_existing_by_normalized_name(db, template_id, session_id):
    e1 = _insert_extraction(db, session_id, template_id,
                            {"name": "ЖК «Шагал»", "developer": "Эталон", "additional_info": []})
    cid1 = match_or_create_complex(db, e1)

    sid2 = uuid.uuid4()
    db.execute(text("INSERT INTO sessions (id, status) VALUES (:id, 'completed')"), {"id": sid2})
    e2 = _insert_extraction(db, sid2, template_id,
                            {"name": "Шагал", "developer": "ГК Эталон", "additional_info": ["скидка"]})
    cid2 = match_or_create_complex(db, e2)

    assert cid1 == cid2  # matched, not created twice
    count = db.execute(text("SELECT COUNT(*) FROM complexes WHERE name_normalized='шагал'")).scalar()
    assert count == 1


def test_aggregate_is_recomputed_on_match(db, template_id, session_id):
    e1 = _insert_extraction(db, session_id, template_id,
                            {"name": "Шагал", "developer": "Эталон",
                             "location": {"parks": ["Парк А"]}, "additional_info": []})
    match_or_create_complex(db, e1)

    sid2 = uuid.uuid4()
    db.execute(text("INSERT INTO sessions (id, status) VALUES (:id, 'completed')"), {"id": sid2})
    e2 = _insert_extraction(db, sid2, template_id,
                            {"name": "Шагал", "developer": "Эталон",
                             "location": {"parks": ["Парк Б"]}, "class": "бизнес", "additional_info": []})
    cid = match_or_create_complex(db, e2)

    row = db.execute(text("SELECT aggregated_data, class FROM complexes WHERE id=:id"), {"id": cid}).first()
    parks = row.aggregated_data["location"]["parks"]
    assert set(parks) == {"Парк А", "Парк Б"}
    assert row[1] == "бизнес"  # class column populated from latest extraction


def test_skips_when_name_or_developer_missing(db, template_id, session_id):
    e1 = _insert_extraction(db, session_id, template_id,
                            {"name": None, "developer": "Эталон", "additional_info": []})
    cid = match_or_create_complex(db, e1)
    assert cid is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/projects/realestate/worker && DATABASE_URL_SYNC=$(grep ^DATABASE_URL_SYNC /root/projects/realestate/.env | cut -d= -f2) ../.venv/bin/pytest tests/test_complex_match.py -v
```

Expected: `ImportError: cannot import name 'match_or_create_complex' from 'tasks.complex_match'`.

- [ ] **Step 3: Implement `match_or_create_complex`**

Append to `worker/tasks/complex_match.py`:

```python
import json
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session as DbSession

from tasks._text_normalize import normalize_complex_name, normalize_developer


def match_or_create_complex(db: DbSession, extraction_id: uuid.UUID | str) -> uuid.UUID | None:
    """Find or create a complex profile for the given extraction; recompute its aggregate.

    Returns the complex_id, or None if the extraction lacks a usable name or developer.
    Mutates: complex_extractions.complex_id, complexes.aggregated_data + indexed columns.
    """
    row = db.execute(text("SELECT raw_data FROM complex_extractions WHERE id=:id"),
                     {"id": extraction_id}).first()
    if not row:
        raise ValueError(f"extraction {extraction_id} not found")
    raw = row[0]
    name_raw = (raw or {}).get("name")
    developer_raw = (raw or {}).get("developer")
    name_norm = normalize_complex_name(name_raw)
    developer_norm = normalize_developer(developer_raw)
    if not name_norm or not developer_norm:
        return None

    existing = db.execute(text("""
        SELECT id FROM complexes
        WHERE name_normalized=:n AND developer_normalized=:d
    """), {"n": name_norm, "d": developer_norm}).first()

    if existing:
        complex_id = existing[0]
    else:
        complex_id = uuid.uuid4()
        db.execute(text("""
            INSERT INTO complexes (id, name, name_normalized, developer, developer_normalized,
                                   class, district, aggregated_data)
            VALUES (:id, :name, :nn, :dev, :dn, :cls, :dist, '{}'::jsonb)
        """), {
            "id": complex_id,
            "name": name_raw,
            "nn": name_norm,
            "dev": developer_raw,
            "dn": developer_norm,
            "cls": (raw or {}).get("class"),
            "dist": ((raw or {}).get("location") or {}).get("district"),
        })

    db.execute(text("UPDATE complex_extractions SET complex_id=:cid WHERE id=:id"),
               {"cid": complex_id, "id": extraction_id})

    _recompute_aggregate(db, complex_id)
    return complex_id


def _recompute_aggregate(db: DbSession, complex_id: uuid.UUID) -> None:
    """Rebuild aggregated_data + indexed columns from all extractions of this complex."""
    rows = db.execute(text("""
        SELECT raw_data, created_at, session_id
        FROM complex_extractions
        WHERE complex_id=:cid
        ORDER BY created_at DESC
    """), {"cid": complex_id}).all()

    extractions = [
        {"raw_data": r[0], "created_at": r[1], "session_id": str(r[2])}
        for r in rows
    ]
    aggregated = merge_extractions(extractions)

    # Indexed columns mirror the freshest non-null scalar from the aggregate
    name = aggregated.get("name")
    developer = aggregated.get("developer")
    cls = aggregated.get("class")
    district = (aggregated.get("location") or {}).get("district") if isinstance(aggregated.get("location"), dict) else None

    db.execute(text("""
        UPDATE complexes SET
            aggregated_data = CAST(:agg AS jsonb),
            name = COALESCE(:name, name),
            name_normalized = COALESCE(:nn, name_normalized),
            developer = :dev,
            developer_normalized = COALESCE(:dn, developer_normalized),
            class = :cls,
            district = :dist,
            updated_at = now()
        WHERE id = :cid
    """), {
        "agg": json.dumps(aggregated, ensure_ascii=False),
        "name": name,
        "nn": normalize_complex_name(name) if name else None,
        "dev": developer,
        "dn": normalize_developer(developer) if developer else None,
        "cls": cls,
        "dist": district,
        "cid": complex_id,
    })
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
cd /root/projects/realestate/worker && DATABASE_URL_SYNC=$(grep ^DATABASE_URL_SYNC /root/projects/realestate/.env | cut -d= -f2) ../.venv/bin/pytest tests/test_complex_match.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/complex_match.py worker/tests/test_complex_match.py
git commit -m "feat(worker): match-or-create complex with aggregate recompute"
```

---

## Task 6: Extraction task (OpenAI structured output)

**Files:**
- Create: `worker/tasks/extract.py`

- [ ] **Step 1: Implement the extraction task**

```python
# worker/tasks/extract.py
"""Extract structured facts from a transcript using a template's JSON Schema.

Designed to be called from `pipeline.process_session` when the session's template
has kind='extraction'. Saves results/{session_id}/extraction.json on disk and
inserts a row into complex_extractions, then runs match_or_create_complex.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from openai import OpenAI
from sqlalchemy import text
from sqlalchemy.orm import Session as DbSession

from tasks.complex_match import match_or_create_complex

logger = logging.getLogger(__name__)

MAX_TRANSCRIPT_TOKENS = 500_000  # safety cap; gpt-5.4 context is 1.05M
RESULTS_PATH = Path(os.getenv("RESULTS_STORAGE_PATH", "./data/results"))


def _approx_tokens(text_str: str) -> int:
    return len(text_str) // 4


def _flatten_transcript(transcript: dict | list) -> str:
    """Build a plain-text transcript with speakers for the LLM."""
    if isinstance(transcript, list):
        segments = transcript
    else:
        segments = transcript.get("segments") or transcript.get("utterances") or []
    parts: list[str] = []
    for seg in segments:
        speaker = seg.get("speaker") or "UNKNOWN"
        content = seg.get("text") or seg.get("content") or ""
        if content.strip():
            parts.append(f"[{speaker}] {content.strip()}")
    return "\n".join(parts)


def run_extraction(db: DbSession, session_id: uuid.UUID | str, template_id: uuid.UUID | str) -> dict:
    """Run the LLM extraction step. Returns the extracted dict.

    Raises RuntimeError on safety-cap breach or LLM error — caller marks session as failed.
    """
    template = db.execute(text(
        "SELECT id, name, prompt, json_schema FROM extraction_templates WHERE id=:id"
    ), {"id": template_id}).first()
    if not template:
        raise RuntimeError(f"template {template_id} not found")

    transcript_path = RESULTS_PATH / str(session_id) / "transcript.json"
    if not transcript_path.exists():
        raise RuntimeError(f"transcript not found at {transcript_path}")
    with transcript_path.open() as f:
        transcript = json.load(f)
    flat = _flatten_transcript(transcript)
    if not flat.strip():
        raise RuntimeError("transcript is empty after flattening")

    if _approx_tokens(flat) > MAX_TRANSCRIPT_TOKENS:
        raise RuntimeError(
            f"transcript exceeds safety cap ({_approx_tokens(flat)} > {MAX_TRANSCRIPT_TOKENS} tokens) — "
            "looks corrupted"
        )

    client = OpenAI()
    logger.info("Running extraction with template '%s' for session %s", template.name, session_id)
    resp = client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": template.prompt},
            {"role": "user", "content": f"Транскрипт:\n\n{flat}"},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "extraction",
                "schema": template.json_schema,
                "strict": True,
            },
        },
    )
    content = resp.choices[0].message.content
    extracted = json.loads(content)

    # Save raw extraction to disk
    out_dir = RESULTS_PATH / str(session_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "extraction.json").open("w") as f:
        json.dump(extracted, f, ensure_ascii=False, indent=2)

    # Insert DB row
    extraction_id = uuid.uuid4()
    db.execute(text("""
        INSERT INTO complex_extractions (id, session_id, template_id, raw_data)
        VALUES (:id, :sid, :tid, CAST(:raw AS jsonb))
    """), {
        "id": extraction_id,
        "sid": session_id,
        "tid": template_id,
        "raw": json.dumps(extracted, ensure_ascii=False),
    })

    # Match or create complex; on failure log + continue (extraction is still saved)
    try:
        match_or_create_complex(db, extraction_id)
    except Exception:
        logger.exception("match_or_create_complex failed for extraction %s", extraction_id)

    db.commit()
    return extracted
```

- [ ] **Step 2: Smoke-import the module**

Run:
```bash
cd /root/projects/realestate/worker && ../.venv/bin/python -c "from tasks.extract import run_extraction; print('OK')"
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add worker/tasks/extract.py
git commit -m "feat(worker): extraction task using OpenAI structured outputs (gpt-5.4)"
```

---

## Task 7: Pipeline branching by template kind

**Files:**
- Modify: `worker/tasks/pipeline.py`

- [ ] **Step 1: Locate the analysis section in `process_session`**

Read `worker/tasks/pipeline.py` lines 616–665 (the `process_session` task) plus the section that runs `quality.py` + `sentiment.py`. Identify where after diarization the "analysis" begins. We will branch there.

- [ ] **Step 2: Add helper to load template kind**

Near the top of `worker/tasks/pipeline.py` (after existing imports), add:

```python
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session as DbSession


def _load_template_kind(template_id: str | None) -> tuple[str | None, str]:
    """Return (template_id, kind) given an optional id stored in session metadata.

    Falls back to ('', 'evaluation') when no template is set.
    """
    if not template_id:
        return None, "evaluation"
    db_url = os.environ.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL", "").replace("+asyncpg", "+psycopg2")
    eng = create_engine(db_url, future=True)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT id, kind FROM extraction_templates WHERE id=:id"),
                           {"id": template_id}).first()
    if not row:
        return None, "evaluation"
    return str(row[0]), row[1]
```

- [ ] **Step 3: Branch in `process_session` (and `process_session_from_file` if it shares the same analysis path)**

Locate the part of `process_session` that runs after diarization, before `quality.assess_quality(...)` is called. Replace that quality+sentiment block with:

```python
        # ---- Analysis branching by template ----
        meta = session_metadata or {}
        template_id, kind = _load_template_kind(meta.get("template_id"))

        if kind == "extraction":
            from tasks.extract import run_extraction
            db_url = os.environ.get("DATABASE_URL_SYNC") or os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
            eng = create_engine(db_url, future=True)
            with eng.connect() as conn:
                with DbSession(bind=conn, expire_on_commit=False) as db:
                    run_extraction(db, session_id, template_id)
            logger.info("Extraction completed for session %s", session_id)
        else:
            # Existing evaluation path: quality + sentiment + amocrm sync — UNCHANGED
            <leave the existing quality/sentiment/amocrm-sync calls here as-is>
```

The key invariant: the **existing evaluation path code is left untouched** — we only wrap it in an `else:` branch and prepend the `if kind == "extraction"` arm.

- [ ] **Step 4: Run worker test suite to check nothing else broke**

Run:
```bash
cd /root/projects/realestate/worker && ../.venv/bin/pytest tests/ -v --ignore=tests/test_complex_match.py
```

Expected: all existing tests pass (we excluded the DB-bound test which needs explicit env).

- [ ] **Step 5: Restart Celery worker**

Run:
```bash
systemctl restart realestate-worker.service && sleep 2 && systemctl is-active realestate-worker.service
```

Expected: `active`.

- [ ] **Step 6: Commit**

```bash
git add worker/tasks/pipeline.py
git commit -m "feat(pipeline): branch on template.kind (extraction vs evaluation)"
```

---

## Task 8: Backend Templates CRUD

**Files:**
- Create: `backend/app/schemas_templates.py`
- Create: `backend/app/routes/templates.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Create Pydantic schemas**

```python
# backend/app/schemas_templates.py
from datetime import datetime
import uuid

from pydantic import BaseModel, Field


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    kind: str = Field(pattern="^(extraction|evaluation)$")
    prompt: str = Field(min_length=1)
    json_schema: dict


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    prompt: str | None = Field(default=None, min_length=1)
    json_schema: dict | None = None


class TemplateListItem(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    kind: str
    updated_at: datetime


class TemplateDetail(TemplateListItem):
    prompt: str
    json_schema: dict
    created_at: datetime


class ComplexListItem(BaseModel):
    id: uuid.UUID
    name: str
    developer: str | None
    class_: str | None = Field(alias="class")
    district: str | None
    sources_count: int
    updated_at: datetime

    model_config = {"populate_by_name": True}


class ComplexDetail(ComplexListItem):
    aggregated_data: dict
    sources: list[dict]


class ComplexRename(BaseModel):
    name: str = Field(min_length=1, max_length=300)


class ComplexMerge(BaseModel):
    target_complex_id: uuid.UUID


class ExtractionRelink(BaseModel):
    complex_id: uuid.UUID | None
```

- [ ] **Step 2: Create the templates router**

```python
# backend/app/routes/templates.py
"""CRUD for extraction templates. Mutations require X-Delete-Password."""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from jsonschema import Draft202012Validator, SchemaError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas_templates import (
    TemplateCreate, TemplateDetail, TemplateListItem, TemplateUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/templates", tags=["templates"])


def _validate_schema(schema: dict) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        raise HTTPException(status_code=422, detail=f"json_schema is not a valid JSON Schema: {e.message}")


@router.get("", response_model=list[TemplateListItem])
async def list_templates(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(text(
        "SELECT id, name, description, kind, updated_at FROM extraction_templates ORDER BY name"
    ))).all()
    return [TemplateListItem(id=r[0], name=r[1], description=r[2], kind=r[3], updated_at=r[4]) for r in rows]


@router.get("/{template_id}", response_model=TemplateDetail)
async def get_template(template_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(text("""
        SELECT id, name, description, kind, prompt, json_schema, created_at, updated_at
        FROM extraction_templates WHERE id=:id
    """), {"id": template_id})).first()
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    return TemplateDetail(
        id=row[0], name=row[1], description=row[2], kind=row[3],
        prompt=row[4], json_schema=row[5], created_at=row[6], updated_at=row[7],
    )


@router.post("", response_model=TemplateDetail, status_code=201)
async def create_template(body: TemplateCreate, db: AsyncSession = Depends(get_db)):
    _validate_schema(body.json_schema)
    new_id = uuid.uuid4()
    try:
        await db.execute(text("""
            INSERT INTO extraction_templates (id, name, description, kind, prompt, json_schema)
            VALUES (:id, :name, :desc, :kind, :prompt, CAST(:schema AS jsonb))
        """), {
            "id": new_id, "name": body.name, "desc": body.description, "kind": body.kind,
            "prompt": body.prompt,
            "schema": __import__("json").dumps(body.json_schema, ensure_ascii=False),
        })
        await db.commit()
    except Exception as e:  # unique violation, etc.
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Cannot create template: {e}")
    return await get_template(new_id, db)


@router.patch("/{template_id}", response_model=TemplateDetail)
async def update_template(template_id: uuid.UUID, body: TemplateUpdate, db: AsyncSession = Depends(get_db)):
    if body.json_schema is not None:
        _validate_schema(body.json_schema)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if not fields:
        return await get_template(template_id, db)
    sets = []
    params: dict = {"id": template_id}
    for k, v in fields.items():
        if k == "json_schema":
            sets.append("json_schema = CAST(:schema AS jsonb)")
            params["schema"] = __import__("json").dumps(v, ensure_ascii=False)
        else:
            sets.append(f"{k} = :{k}")
            params[k] = v
    sets.append("updated_at = now()")
    res = await db.execute(text(f"UPDATE extraction_templates SET {', '.join(sets)} WHERE id=:id RETURNING id"), params)
    if not res.first():
        raise HTTPException(status_code=404, detail="Template not found")
    await db.commit()
    return await get_template(template_id, db)


@router.delete("/{template_id}", status_code=204)
async def delete_template(template_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    used = (await db.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM complex_extractions WHERE template_id=:id) AS exts,
          (SELECT COUNT(*) FROM sessions WHERE metadata->>'template_id' = :id_str) AS sess
    """), {"id": template_id, "id_str": str(template_id)})).first()
    if used and (used[0] > 0 or used[1] > 0):
        raise HTTPException(
            status_code=409,
            detail=f"Template is in use (extractions={used[0]}, sessions={used[1]}); cannot delete",
        )
    res = await db.execute(text("DELETE FROM extraction_templates WHERE id=:id RETURNING id"), {"id": template_id})
    if not res.first():
        raise HTTPException(status_code=404, detail="Template not found")
    await db.commit()
```

- [ ] **Step 3: Wire the router into `main.py`**

Edit `backend/app/main.py`:

Replace the import line:
```python
from app.routes import chunks, sessions, companies, transcripts, analysis, managers, webhooks, amocrm
```
with:
```python
from app.routes import (
    chunks, sessions, companies, transcripts, analysis, managers,
    webhooks, amocrm, templates,
)
```

Add after the existing `app.include_router(amocrm.router)` line:
```python
app.include_router(templates.router)
```

- [ ] **Step 4: Restart backend and smoke-test**

Run:
```bash
systemctl restart realestate-backend.service && sleep 2 && systemctl is-active realestate-backend.service
curl -s http://127.0.0.1:8002/api/templates | head -200
```

Expected: JSON array with at least one entry — the seeded `"Презентация ЖК"` template.

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas_templates.py backend/app/routes/templates.py backend/app/main.py
git commit -m "feat(api): templates CRUD with JSON Schema validation"
```

---

## Task 9: Backend Sessions extension (template_id on upload + reprocess)

**Files:**
- Modify: `backend/app/routes/chunks.py`
- Modify: `backend/app/routes/sessions.py`

- [ ] **Step 1: Accept `template_id` in `/upload-audio`**

In `backend/app/routes/chunks.py`, change the `upload_audio_file` signature:

```python
@router.post("/{session_id}/upload-audio")
async def upload_audio_file(
    session_id: uuid.UUID,
    file: UploadFile,
    template_id: uuid.UUID | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
):
```

Add at top of file (with other imports):
```python
from fastapi import Form
```

After the existing `session.status = SessionStatus.uploading` line, add:
```python
    if template_id is not None:
        meta = dict(session.metadata_ or {})
        meta["template_id"] = str(template_id)
        session.metadata_ = meta
```

- [ ] **Step 2: Add `/reprocess` endpoint**

In `backend/app/routes/sessions.py`, after the existing `delete_session` endpoint, add:

```python
class ReprocessBody(BaseModel):
    template_id: uuid.UUID


@router.post("/{session_id}/reprocess", response_model=SessionResponse)
async def reprocess_session(
    session_id: uuid.UUID,
    body: ReprocessBody,
    db: AsyncSession = Depends(get_db),
):
    """Reprocess an existing session with a different template (skip transcription)."""
    sess = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one_or_none()
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    if sess.status in (SessionStatus.processing, SessionStatus.uploading):
        raise HTTPException(status_code=409, detail=f"Session is currently {sess.status.value}")

    tpl = (await db.execute(text("SELECT id FROM extraction_templates WHERE id=:id"),
                            {"id": body.template_id})).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    meta = dict(sess.metadata_ or {})
    meta["template_id"] = str(body.template_id)
    sess.metadata_ = meta
    sess.status = SessionStatus.processing
    await db.commit()
    await db.refresh(sess)

    # Re-run pipeline starting after transcription. We use the same task — pipeline.py
    # detects existing transcript.json and skips re-transcription.
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: celery_app.send_task("pipeline.process_session", args=[str(session_id)], queue="transcription"),
    )

    chunks_count = await db.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.session_id == session_id)
    )
    return _to_response(sess, chunks_count or 0)
```

Add at top (with existing imports):
```python
from pydantic import BaseModel
from sqlalchemy import text
```

- [ ] **Step 3: Verify pipeline.py skips transcription when transcript.json exists**

Read `worker/tasks/pipeline.py` around `process_session`. If transcription is unconditional, add a guard:

```python
        transcript_path = RESULTS_PATH / str(session_id) / "transcript.json"
        if transcript_path.exists():
            logger.info("Transcript already exists for %s — skipping transcription", session_id)
            transcript = json.loads(transcript_path.read_text())
        else:
            <existing transcription call>
```

(Adjust to the local variable name and structure used in `pipeline.py`.)

- [ ] **Step 4: Restart and smoke-test**

Run:
```bash
systemctl restart realestate-backend.service realestate-worker.service && sleep 3
curl -s http://127.0.0.1:8002/api/sessions?limit=1 | python3 -c "import sys,json; print(list(json.load(sys.stdin).keys()))"
```

Expected: `['items', 'total']`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/chunks.py backend/app/routes/sessions.py worker/tasks/pipeline.py
git commit -m "feat(api): template_id on upload + /reprocess endpoint"
```

---

## Task 10: Backend Complexes API + extraction relink

**Files:**
- Create: `backend/app/routes/complexes.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Implement the router**

```python
# backend/app/routes/complexes.py
"""Complexes browse + detail + manual merge/relink."""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas_templates import (
    ComplexDetail, ComplexListItem, ComplexMerge, ComplexRename, ExtractionRelink,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["complexes"])


@router.get("/complexes", response_model=list[ComplexListItem])
async def list_complexes(
    developer: str | None = None,
    class_: str | None = None,
    district: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    where = ["1=1"]
    params: dict = {"limit": limit, "offset": offset}
    if developer:
        where.append("c.developer ILIKE :dev")
        params["dev"] = f"%{developer}%"
    if class_:
        where.append("c.class = :cls")
        params["cls"] = class_
    if district:
        where.append("c.district ILIKE :dist")
        params["dist"] = f"%{district}%"
    if q:
        where.append("c.name ILIKE :q")
        params["q"] = f"%{q}%"
    rows = (await db.execute(text(f"""
        SELECT c.id, c.name, c.developer, c.class, c.district, c.updated_at,
               (SELECT COUNT(*) FROM complex_extractions ce WHERE ce.complex_id = c.id) AS sources
        FROM complexes c
        WHERE {' AND '.join(where)}
        ORDER BY c.updated_at DESC
        LIMIT :limit OFFSET :offset
    """), params)).all()
    return [
        ComplexListItem(
            id=r[0], name=r[1], developer=r[2], class_=r[3], district=r[4],
            updated_at=r[5], sources_count=r[6],
        ) for r in rows
    ]


@router.get("/complexes/{complex_id}", response_model=ComplexDetail)
async def get_complex(complex_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    c = (await db.execute(text("""
        SELECT id, name, developer, class, district, updated_at, aggregated_data
        FROM complexes WHERE id=:id
    """), {"id": complex_id})).first()
    if not c:
        raise HTTPException(status_code=404, detail="Complex not found")
    sources = (await db.execute(text("""
        SELECT ce.id, ce.session_id, ce.created_at
        FROM complex_extractions ce WHERE ce.complex_id=:id
        ORDER BY ce.created_at DESC
    """), {"id": complex_id})).all()
    return ComplexDetail(
        id=c[0], name=c[1], developer=c[2], class_=c[3], district=c[4],
        updated_at=c[5], aggregated_data=c[6],
        sources=[{"extraction_id": str(s[0]), "session_id": str(s[1]), "created_at": s[2].isoformat()} for s in sources],
        sources_count=len(sources),
    )


@router.patch("/complexes/{complex_id}", response_model=ComplexDetail)
async def rename_complex(complex_id: uuid.UUID, body: ComplexRename, db: AsyncSession = Depends(get_db)):
    from worker_helpers import normalize_complex_name  # see Step 2
    res = await db.execute(text("""
        UPDATE complexes SET name=:name, name_normalized=:nn, updated_at=now()
        WHERE id=:id RETURNING id
    """), {"id": complex_id, "name": body.name, "nn": normalize_complex_name(body.name)})
    if not res.first():
        raise HTTPException(status_code=404, detail="Complex not found")
    await db.commit()
    return await get_complex(complex_id, db)


@router.delete("/complexes/{complex_id}", status_code=204)
async def delete_complex(complex_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    res = await db.execute(text("DELETE FROM complexes WHERE id=:id RETURNING id"), {"id": complex_id})
    if not res.first():
        raise HTTPException(status_code=404, detail="Complex not found")
    await db.commit()


@router.post("/complexes/{complex_id}/merge", status_code=204)
async def merge_complex(complex_id: uuid.UUID, body: ComplexMerge, db: AsyncSession = Depends(get_db)):
    if complex_id == body.target_complex_id:
        raise HTTPException(status_code=400, detail="Cannot merge complex into itself")
    target = (await db.execute(text("SELECT id FROM complexes WHERE id=:id"),
                               {"id": body.target_complex_id})).first()
    if not target:
        raise HTTPException(status_code=404, detail="Target complex not found")
    src = (await db.execute(text("SELECT id FROM complexes WHERE id=:id"), {"id": complex_id})).first()
    if not src:
        raise HTTPException(status_code=404, detail="Source complex not found")

    await db.execute(text(
        "UPDATE complex_extractions SET complex_id=:tgt WHERE complex_id=:src"
    ), {"src": complex_id, "tgt": body.target_complex_id})
    await db.execute(text("DELETE FROM complexes WHERE id=:id"), {"id": complex_id})
    await db.commit()

    # Recompute target aggregate via worker helper called synchronously
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as DbSession
    import os
    db_url = os.environ.get("DATABASE_URL_SYNC") or os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    eng = create_engine(db_url, future=True)
    with eng.connect() as conn:
        with DbSession(bind=conn, expire_on_commit=False) as sdb:
            from tasks.complex_match import _recompute_aggregate
            _recompute_aggregate(sdb, body.target_complex_id)
            sdb.commit()


@router.post("/extractions/{extraction_id}/relink", status_code=204)
async def relink_extraction(extraction_id: uuid.UUID, body: ExtractionRelink, db: AsyncSession = Depends(get_db)):
    if body.complex_id is not None:
        target = (await db.execute(text("SELECT id FROM complexes WHERE id=:id"),
                                   {"id": body.complex_id})).first()
        if not target:
            raise HTTPException(status_code=404, detail="Target complex not found")

    old = (await db.execute(text(
        "UPDATE complex_extractions SET complex_id=:cid WHERE id=:id RETURNING (SELECT complex_id FROM complex_extractions WHERE id=:id2)"
    ), {"cid": body.complex_id, "id": extraction_id, "id2": extraction_id})).first()
    if not old:
        raise HTTPException(status_code=404, detail="Extraction not found")
    await db.commit()

    # Recompute aggregates for the two affected complexes (old + new), if any
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as DbSession
    import os
    db_url = os.environ.get("DATABASE_URL_SYNC") or os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    eng = create_engine(db_url, future=True)
    with eng.connect() as conn:
        with DbSession(bind=conn, expire_on_commit=False) as sdb:
            from tasks.complex_match import _recompute_aggregate
            for cid in {old[0], body.complex_id}:
                if cid:
                    _recompute_aggregate(sdb, cid)
            sdb.commit()
```

- [ ] **Step 2: Make worker helpers importable from backend**

The `complexes.py` route imports `tasks.complex_match._recompute_aggregate` and `tasks._text_normalize.normalize_complex_name`. Both live in the `worker/tasks/` package. The backend already imports from `tasks.amocrm_poll` (see `backend/app/routes/amocrm.py`), so the import path works.

Replace the placeholder line `from worker_helpers import normalize_complex_name` (in `rename_complex`) with the real import:
```python
from tasks._text_normalize import normalize_complex_name
```

- [ ] **Step 3: Wire router into `main.py`**

Edit `backend/app/main.py` imports to include `complexes`, and add the include line:
```python
app.include_router(complexes.router)
```

- [ ] **Step 4: Restart and smoke-test**

Run:
```bash
systemctl restart realestate-backend.service && sleep 2
curl -s http://127.0.0.1:8002/api/complexes | python3 -m json.tool
```

Expected: empty array `[]` (no complexes yet).

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/complexes.py backend/app/main.py
git commit -m "feat(api): complexes browse/detail/merge + extraction relink"
```

---

## Task 11: Auth gating on mutations (X-Delete-Password)

**Files:**
- Modify: `backend/app/main.py`

- [ ] **Step 1: Extend the BasicAuthMiddleware to gate destructive `/api/templates` and `/api/complexes` calls**

Replace the `BasicAuthMiddleware.dispatch` method in `backend/app/main.py` with:

```python
    PROTECTED_PREFIXES = ("/api/templates", "/api/complexes", "/api/extractions")
    PROTECTED_METHODS = {"POST", "PATCH", "DELETE"}

    async def dispatch(self, request: Request, call_next):
        # Destructive calls on templates/complexes/extractions need the delete password
        if (request.method in self.PROTECTED_METHODS
                and any(request.url.path.startswith(p) for p in self.PROTECTED_PREFIXES)):
            expected = settings.DELETE_PASSWORD
            if not expected:
                return Response(status_code=503, content="Mutations disabled: DELETE_PASSWORD not configured")
            provided = request.headers.get("X-Delete-Password")
            if not provided or not secrets.compare_digest(provided, expected):
                return Response(status_code=401, content='{"detail":"Invalid delete password"}',
                                media_type="application/json")
            return await call_next(request)

        if request.url.path.startswith(self.OPEN_PREFIXES):
            return await call_next(request)
        # ...rest of existing dispatch...
```

Keep the existing Basic Auth checks below (for the UI). Note: the existing `OPEN_PREFIXES` includes `/api/`, but our new check runs before it — so destructive `/api/templates*` calls are gated even though `/api/` itself stays open for non-destructive calls and other endpoints (chunks, finish, etc.).

- [ ] **Step 2: Restart and verify**

Run:
```bash
systemctl restart realestate-backend.service && sleep 2
echo "--- without password (expect 401) ---"
curl -s -X POST -w "\n%{http_code}\n" -H 'Content-Type: application/json' \
     -d '{"name":"t","kind":"extraction","prompt":"p","json_schema":{"type":"object"}}' \
     http://127.0.0.1:8002/api/templates
echo "--- with password (expect 201) ---"
PWD=$(grep ^DELETE_PASSWORD /root/projects/realestate/.env | cut -d= -f2-)
curl -s -X POST -w "\n%{http_code}\n" -H 'Content-Type: application/json' \
     -H "X-Delete-Password: $PWD" \
     -d '{"name":"_smoke_test","kind":"extraction","prompt":"p","json_schema":{"type":"object"}}' \
     http://127.0.0.1:8002/api/templates
echo "--- cleanup ---"
TID=$(curl -s http://127.0.0.1:8002/api/templates | python3 -c "import sys,json; [print(t['id']) for t in json.load(sys.stdin) if t['name']=='_smoke_test']")
curl -s -X DELETE -H "X-Delete-Password: $PWD" "http://127.0.0.1:8002/api/templates/$TID"
unset PWD
```

Expected: first call → 401, second → 201, cleanup → 204 silently.

- [ ] **Step 3: Commit**

```bash
git add backend/app/main.py
git commit -m "feat(api): protect templates/complexes mutations with DELETE_PASSWORD"
```

---

## Task 12: Frontend — Templates page

**Files:**
- Modify: `backend/static/app.js`
- Modify: `backend/static/index.html`
- Modify: `backend/static/styles.css`

- [ ] **Step 1: Add sidebar link**

In `backend/static/index.html`, locate the sidebar `<nav>` block (search for `href="#calls"`). Add after the «База ЖК»-adjacent links:

```html
<a href="#templates" class="nav-link" data-route="templates">
  <span class="nav-icon">📋</span> Шаблоны
</a>
```

(Keep emoji even though the rest of the codebase usually doesn't — the existing nav-links use emoji icons.)

- [ ] **Step 2: Wire route in `app.js`**

In `backend/static/app.js`, locate the `router()` function (around line 33). Add a new branch:

```javascript
  } else if (route === 'templates') {
    await renderTemplates();
  } else if (route.startsWith('template/')) {
    await renderTemplateEdit(route.split('/')[1]);
  } else if (route === 'template/new') {
    await renderTemplateEdit(null);
```

- [ ] **Step 3: Implement `renderTemplates` (list page)**

Append to `backend/static/app.js` (near other render functions):

```javascript
async function renderTemplates() {
  showLoading();
  try {
    const items = await api('/api/templates');
    let html = `
      <div class="page-header">
        <h1>Шаблоны</h1>
        <p>Шаблоны извлечения структурированных данных и оценки</p>
      </div>
      <div style="margin-bottom:16px">
        <button class="btn btn-secondary" onclick="navigate('template/new')">+ Новый шаблон</button>
      </div>
      <div class="table-container">
        <table>
          <thead><tr><th>Имя</th><th>Тип</th><th>Описание</th><th>Изменён</th><th></th></tr></thead>
          <tbody>
            ${items.length === 0
              ? '<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:40px">Нет шаблонов</td></tr>'
              : items.map(t => `
                <tr>
                  <td><a href="#template/${t.id}">${escapeHtml(t.name)}</a></td>
                  <td><span class="badge badge-source-${t.kind === 'extraction' ? 'desktop' : 'amocrm'}">${escapeHtml(t.kind)}</span></td>
                  <td>${escapeHtml(t.description || '')}</td>
                  <td>${formatDate(t.updated_at)}</td>
                  <td><button class="btn btn-danger btn-sm" onclick="deleteTemplate('${t.id}','${escapeHtml(t.name).replace(/'/g, "\\'")}')">Удалить</button></td>
                </tr>
              `).join('')}
          </tbody>
        </table>
      </div>
    `;
    app.innerHTML = html;
  } catch (err) {
    app.innerHTML = `<div class="empty-state"><p>Ошибка: ${escapeHtml(err.message)}</p></div>`;
  }
}
```

- [ ] **Step 4: Implement `renderTemplateEdit` (create/edit form)**

Append to `app.js`:

```javascript
async function renderTemplateEdit(id) {
  showLoading();
  let tpl = { name: '', description: '', kind: 'extraction', prompt: '', json_schema: {} };
  try {
    if (id) tpl = await api(`/api/templates/${id}`);
    app.innerHTML = `
      <a href="#templates" class="back-link">← Назад к шаблонам</a>
      <div class="page-header"><h1>${id ? 'Редактирование шаблона' : 'Новый шаблон'}</h1></div>
      <form id="tplForm" class="settings-form" onsubmit="event.preventDefault(); saveTemplate('${id || ''}')">
        <label>Имя<input type="text" id="tplName" value="${escapeHtml(tpl.name)}" required></label>
        <label>Описание<textarea id="tplDesc" rows="2">${escapeHtml(tpl.description || '')}</textarea></label>
        <label>Тип
          <select id="tplKind"${id ? ' disabled' : ''}>
            <option value="extraction"${tpl.kind === 'extraction' ? ' selected' : ''}>extraction (извлечение фактов)</option>
            <option value="evaluation"${tpl.kind === 'evaluation' ? ' selected' : ''}>evaluation (оценка звонка)</option>
          </select>
        </label>
        <label>System prompt<textarea id="tplPrompt" rows="8" required>${escapeHtml(tpl.prompt)}</textarea></label>
        <label>JSON Schema
          <textarea id="tplSchema" rows="20" style="font-family:monospace;font-size:12px" required>${escapeHtml(JSON.stringify(tpl.json_schema, null, 2))}</textarea>
        </label>
        <div style="display:flex;gap:8px">
          <button type="submit" class="btn btn-primary">Сохранить</button>
          <button type="button" class="btn btn-secondary" onclick="validateSchemaInline()">Validate JSON Schema</button>
        </div>
      </form>
    `;
  } catch (err) {
    app.innerHTML = `<div class="empty-state"><p>Ошибка: ${escapeHtml(err.message)}</p></div>`;
  }
}

async function saveTemplate(id) {
  let schema;
  try { schema = JSON.parse($('#tplSchema').value); }
  catch (e) { return showToast('JSON Schema is not valid JSON: ' + e.message, 'error'); }
  const body = {
    name: $('#tplName').value.trim(),
    description: $('#tplDesc').value.trim() || null,
    prompt: $('#tplPrompt').value,
    json_schema: schema,
  };
  if (!id) body.kind = $('#tplKind').value;

  const password = await ensureDeletePassword(); if (!password) return;
  try {
    if (id) {
      await api(`/api/templates/${id}`, { method: 'PATCH', headers: { 'X-Delete-Password': password }, body: JSON.stringify(body) });
    } else {
      await api('/api/templates', { method: 'POST', headers: { 'X-Delete-Password': password }, body: JSON.stringify(body) });
    }
    showToast('Шаблон сохранён');
    navigate('templates');
  } catch (err) {
    showToast('Не удалось сохранить: ' + err.message, 'error');
  }
}

function validateSchemaInline() {
  try {
    JSON.parse($('#tplSchema').value);
    showToast('JSON синтаксически корректен (серверная валидация JSON Schema на сохранении)');
  } catch (e) {
    showToast('Ошибка JSON: ' + e.message, 'error');
  }
}

async function deleteTemplate(id, name) {
  if (!confirm(`Удалить шаблон "${name}"?`)) return;
  const password = await ensureDeletePassword(); if (!password) return;
  try {
    await api(`/api/templates/${id}`, { method: 'DELETE', headers: { 'X-Delete-Password': password } });
    showToast('Шаблон удалён');
    renderTemplates();
  } catch (err) {
    showToast('Не удалось удалить: ' + err.message, 'error');
  }
}

// Shared password helper — caches in sessionStorage; clears on 401
async function ensureDeletePassword() {
  let p = sessionStorage.getItem('deletePassword');
  if (!p) {
    p = prompt('Пароль для опасных действий:');
    if (!p) return null;
    sessionStorage.setItem('deletePassword', p);
  }
  return p;
}
```

- [ ] **Step 5: Refactor existing `deleteSession` to use the shared helper**

In `app.js` find the existing `deleteSession` function (added in earlier work). Replace its inline `prompt()` for password with:
```javascript
  const password = await ensureDeletePassword();
  if (!password) return;
```
Keep the typed-phrase confirmation step before that.

- [ ] **Step 6: Smoke-test in the browser**

Open `http://localhost:8002/#templates`. Expected:
- Sidebar shows «Шаблоны»
- The list shows one row: «Презентация ЖК», kind `extraction`, with «Удалить» button
- Click on the name → edit form pre-filled with the seeded prompt and schema
- The «+ Новый шаблон» button leads to a blank form

- [ ] **Step 7: Commit**

```bash
git add backend/static/app.js backend/static/index.html backend/static/styles.css
git commit -m "feat(ui): templates page with editor and JSON Schema validation"
```

---

## Task 13: Frontend — template picker on upload

**Files:**
- Modify: `backend/static/app.js`

- [ ] **Step 1: Locate the upload page render function**

In `app.js`, find `renderUpload()` (around line 1227 from earlier inspection). Locate the form element where the file input lives.

- [ ] **Step 2: Add the dropdown + load templates**

At the top of `renderUpload()`, after the existing initial state, fetch templates:
```javascript
  let templates = [];
  try { templates = await api('/api/templates'); } catch {}
```

In the form HTML, before the existing `<input type="file" ...>` line, insert:
```html
<label>Шаблон обработки
  <select id="uploadTemplateId">
    <option value="">Звонок брокера (по умолчанию)</option>
    ${templates.map(t => `<option value="${t.id}">${escapeHtml(t.name)} (${escapeHtml(t.kind)})</option>`).join('')}
  </select>
</label>
```

- [ ] **Step 3: Pass `template_id` in the upload request**

Locate `startUpload()` (around line 1368). The function builds a `FormData`. Before the `fetch(...)` call, add:
```javascript
  const tplId = $('#uploadTemplateId') && $('#uploadTemplateId').value;
  if (tplId) formData.append('template_id', tplId);
```

- [ ] **Step 4: Smoke-test upload (without actually uploading)**

Open `http://localhost:8002/#upload`. Expected: dropdown with "Звонок брокера" + "Презентация ЖК (extraction)".

- [ ] **Step 5: Commit**

```bash
git add backend/static/app.js
git commit -m "feat(ui): template picker on the upload page"
```

---

## Task 14: Frontend — session detail extraction view + reprocess + relink

**Files:**
- Modify: `backend/static/app.js`

- [ ] **Step 1: Fetch extraction in `renderCallDetail`**

In `renderCallDetail(id)` (around line 321), after the existing `try { sentiment = await api(...) } catch {}` line, add:
```javascript
    let extraction = null;
    try { extraction = await api(`/api/sessions/${id}/extraction`); } catch {}
```

The endpoint `GET /api/sessions/{id}/extraction` doesn't exist yet — add it now in the **backend** before continuing this task: in `backend/app/routes/sessions.py`, append:

```python
@router.get("/{session_id}/extraction")
async def get_session_extraction(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(text("""
        SELECT ce.id, ce.raw_data, ce.complex_id, t.id, t.name, t.json_schema
        FROM complex_extractions ce
        JOIN extraction_templates t ON t.id = ce.template_id
        WHERE ce.session_id = :sid
        ORDER BY ce.created_at DESC LIMIT 1
    """), {"sid": session_id})).first()
    if not row:
        raise HTTPException(status_code=404, detail="No extraction for this session")
    return {
        "extraction_id": str(row[0]),
        "raw_data": row[1],
        "complex_id": str(row[2]) if row[2] else None,
        "template": {"id": str(row[3]), "name": row[4], "json_schema": row[5]},
    }
```

Then restart the backend.

- [ ] **Step 2: Conditionally render «Профиль ЖК» section**

In `renderCallDetail`, where the existing analysis HTML is built, wrap the existing analysis sections in:
```javascript
    const showExtraction = !!extraction;
```
At the appropriate place in the rendered HTML (after the call header), insert:
```html
${showExtraction ? `
  <div class="extraction-section">
    <h2>Профиль ЖК — ${escapeHtml(extraction.template.name)}</h2>
    ${renderExtractionFields(extraction.raw_data)}
    ${extraction.complex_id ? `
      <a class="btn btn-secondary btn-sm" href="#complex/${extraction.complex_id}">Открыть профиль ЖК</a>
      <button class="btn btn-secondary btn-sm" onclick="relinkExtraction('${extraction.extraction_id}')">Перепривязать к другому ЖК…</button>
    ` : `
      <button class="btn btn-secondary btn-sm" onclick="relinkExtraction('${extraction.extraction_id}')">Привязать к ЖК…</button>
    `}
  </div>
` : ''}
```
Hide the existing analysis/sentiment sections when `showExtraction` is true.

- [ ] **Step 3: Implement `renderExtractionFields`**

Append to `app.js`:

```javascript
function renderExtractionFields(data, depth = 0) {
  if (data === null || data === undefined) return '<em class="muted">—</em>';
  if (Array.isArray(data)) {
    if (data.length === 0) return '<em class="muted">пусто</em>';
    if (data.every(v => typeof v === 'string')) {
      return '<ul>' + data.map(v => `<li>${escapeHtml(v)}</li>`).join('') + '</ul>';
    }
    // list of objects (e.g. additional_info attribution or by_phase)
    return '<ul>' + data.map(v => `<li>${typeof v === 'object' ? renderExtractionFields(v, depth + 1) : escapeHtml(String(v))}</li>`).join('') + '</ul>';
  }
  if (typeof data === 'object') {
    return Object.entries(data).map(([k, v]) =>
      `<div class="ext-field"><div class="ext-key">${escapeHtml(k)}</div><div class="ext-val">${renderExtractionFields(v, depth + 1)}</div></div>`
    ).join('');
  }
  if (typeof data === 'boolean') return data ? '✓ да' : '✗ нет';
  return escapeHtml(String(data));
}
```

Add styles in `styles.css`:
```css
.extraction-section { background: var(--bg-card); border-radius: var(--radius); padding: 20px; margin-bottom: 20px; }
.ext-field { display: flex; gap: 12px; padding: 6px 0; border-bottom: 1px solid var(--border); }
.ext-key { width: 200px; color: var(--text-muted); font-size: 13px; }
.ext-val { flex: 1; }
.muted { color: var(--text-muted); }
```

- [ ] **Step 4: «Перепрогнать с шаблоном…» button**

In the same `renderCallDetail` HTML (next to Export/Delete buttons), add:
```html
<button class="btn btn-secondary btn-sm" onclick="reprocessSession('${id}')">Перепрогнать с шаблоном…</button>
```

Append to `app.js`:
```javascript
async function reprocessSession(sessionId) {
  let templates = [];
  try { templates = await api('/api/templates'); } catch (e) { return showToast('Не удалось загрузить шаблоны', 'error'); }
  const choice = prompt(
    'Перепрогнать с шаблоном — введите его имя:\n\n' + templates.map(t => `• ${t.name}`).join('\n'),
    templates[0] && templates[0].name
  );
  if (!choice) return;
  const tpl = templates.find(t => t.name === choice.trim());
  if (!tpl) return showToast('Шаблон не найден', 'error');
  if (!confirm(`Запустить перепрогон сессии с шаблоном "${tpl.name}"?`)) return;
  try {
    await api(`/api/sessions/${sessionId}/reprocess`, {
      method: 'POST',
      body: JSON.stringify({ template_id: tpl.id }),
    });
    showToast('Перепрогон запущен — обновляйте страницу через минуту');
  } catch (err) {
    showToast('Ошибка: ' + err.message, 'error');
  }
}
```

- [ ] **Step 5: «Перепривязать к другому ЖК» action**

Append to `app.js`:
```javascript
async function relinkExtraction(extractionId) {
  const items = await api('/api/complexes?limit=200');
  const choice = prompt('Привязать к ЖК — введи имя:\n\n' + items.map(c => `• ${c.name} (${c.developer})`).join('\n'));
  if (!choice) return;
  const target = items.find(c => c.name === choice.trim());
  if (!target) return showToast('ЖК не найден', 'error');
  const password = await ensureDeletePassword(); if (!password) return;
  try {
    await api(`/api/extractions/${extractionId}/relink`, {
      method: 'POST',
      headers: { 'X-Delete-Password': password },
      body: JSON.stringify({ complex_id: target.id }),
    });
    showToast('Перепривязано');
    location.reload();
  } catch (err) {
    showToast('Ошибка: ' + err.message, 'error');
  }
}
```

- [ ] **Step 6: Smoke-test**

Open `http://localhost:8002/#calls` → click any session. Expected: page renders without errors. The extraction section appears only if there is one (yet — there isn't until Task 16).

- [ ] **Step 7: Commit**

```bash
git add backend/app/routes/sessions.py backend/static/app.js backend/static/styles.css
git commit -m "feat(ui): extraction view + reprocess + relink on session detail"
```

---

## Task 15: Frontend — «База ЖК» list and detail pages

**Files:**
- Modify: `backend/static/app.js`
- Modify: `backend/static/index.html`

- [ ] **Step 1: Sidebar link**

In `index.html`, near the «Шаблоны» link, add:
```html
<a href="#complexes" class="nav-link" data-route="complexes">
  <span class="nav-icon">🏙️</span> База ЖК
</a>
```

- [ ] **Step 2: Wire routes**

In `app.js` `router()`:
```javascript
  } else if (route === 'complexes') {
    await renderComplexes();
  } else if (route.startsWith('complex/')) {
    await renderComplexDetail(route.split('/')[1]);
```

- [ ] **Step 3: Implement list page**

Append:
```javascript
let complexFilters = { developer: '', class_: '', district: '', q: '' };

async function renderComplexes() {
  showLoading();
  try {
    const params = new URLSearchParams();
    if (complexFilters.developer) params.set('developer', complexFilters.developer);
    if (complexFilters.class_) params.set('class_', complexFilters.class_);
    if (complexFilters.district) params.set('district', complexFilters.district);
    if (complexFilters.q) params.set('q', complexFilters.q);
    const items = await api('/api/complexes?' + params.toString());

    app.innerHTML = `
      <div class="page-header"><h1>База ЖК</h1><p>${items.length} профилей</p></div>
      <div class="table-filters" style="margin-bottom:16px">
        <input class="table-filter" id="cxQ" placeholder="Поиск по имени" value="${escapeHtml(complexFilters.q)}">
        <input class="table-filter" id="cxDev" placeholder="Застройщик" value="${escapeHtml(complexFilters.developer)}">
        <input class="table-filter" id="cxCls" placeholder="Класс (бизнес/комфорт/...)" value="${escapeHtml(complexFilters.class_)}">
        <input class="table-filter" id="cxDist" placeholder="Район" value="${escapeHtml(complexFilters.district)}">
        <button class="btn btn-secondary btn-sm" onclick="applyComplexFilters()">Применить</button>
      </div>
      ${items.length === 0
        ? '<div class="empty-state"><p>Профилей пока нет — обработай презентацию через шаблон «Презентация ЖК».</p></div>'
        : '<div class="cards-grid">' + items.map(c => `
          <a class="complex-card" href="#complex/${c.id}">
            <h3>${escapeHtml(c.name)}</h3>
            <div class="complex-card-sub">
              ${c.class ? `<span class="badge badge-source-amocrm">${escapeHtml(c.class)}</span>` : ''}
              <span class="muted">${escapeHtml(c.developer || '')}</span>
            </div>
            <div class="muted">${escapeHtml(c.district || '')}</div>
            <div class="muted" style="margin-top:8px">${c.sources_count} источник${c.sources_count === 1 ? '' : 'ов'}</div>
          </a>
        `).join('') + '</div>'}
    `;
  } catch (err) {
    app.innerHTML = `<div class="empty-state"><p>Ошибка: ${escapeHtml(err.message)}</p></div>`;
  }
}

function applyComplexFilters() {
  complexFilters = {
    q:        $('#cxQ').value.trim(),
    developer: $('#cxDev').value.trim(),
    class_:   $('#cxCls').value.trim(),
    district: $('#cxDist').value.trim(),
  };
  renderComplexes();
}
```

Styles:
```css
.cards-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
.complex-card { display: block; background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; text-decoration: none; color: inherit; transition: border-color var(--transition); }
.complex-card:hover { border-color: var(--accent); }
.complex-card h3 { margin: 0 0 8px 0; font-size: 16px; }
.complex-card-sub { display: flex; gap: 8px; align-items: center; margin-bottom: 4px; }
```

- [ ] **Step 4: Implement detail page**

Append:
```javascript
async function renderComplexDetail(id) {
  showLoading();
  try {
    const c = await api(`/api/complexes/${id}`);
    app.innerHTML = `
      <a class="back-link" href="#complexes">← К списку ЖК</a>
      <div class="page-header">
        <h1>${escapeHtml(c.name)}</h1>
        <p>${escapeHtml(c.developer || '')} ${c.class ? '· ' + escapeHtml(c.class) : ''} ${c.district ? '· ' + escapeHtml(c.district) : ''}</p>
      </div>
      <div style="margin-bottom:16px;display:flex;gap:8px">
        <button class="btn btn-secondary btn-sm" onclick="renameComplex('${c.id}','${escapeHtml(c.name).replace(/'/g, "\\'")}')">Переименовать</button>
        <button class="btn btn-danger btn-sm" onclick="deleteComplex('${c.id}','${escapeHtml(c.name).replace(/'/g, "\\'")}')">Удалить профиль</button>
      </div>
      <div class="extraction-section">
        ${renderExtractionFields(c.aggregated_data)}
      </div>
      <h2>Источники</h2>
      <ul>
        ${c.sources.map(s => `<li><a href="#call/${s.session_id}">Запись от ${formatDate(s.created_at)}</a></li>`).join('')}
      </ul>
    `;
  } catch (err) {
    app.innerHTML = `<div class="empty-state"><p>Ошибка: ${escapeHtml(err.message)}</p></div>`;
  }
}

async function renameComplex(id, currentName) {
  const newName = prompt('Новое имя ЖК:', currentName);
  if (!newName || newName === currentName) return;
  const password = await ensureDeletePassword(); if (!password) return;
  try {
    await api(`/api/complexes/${id}`, {
      method: 'PATCH',
      headers: { 'X-Delete-Password': password },
      body: JSON.stringify({ name: newName }),
    });
    showToast('Переименовано');
    renderComplexDetail(id);
  } catch (err) { showToast('Ошибка: ' + err.message, 'error'); }
}

async function deleteComplex(id, name) {
  if (!confirm(`Удалить профиль "${name}"? Сами записи останутся, но будут отвязаны.`)) return;
  const password = await ensureDeletePassword(); if (!password) return;
  try {
    await api(`/api/complexes/${id}`, { method: 'DELETE', headers: { 'X-Delete-Password': password } });
    showToast('Удалено');
    navigate('complexes');
  } catch (err) { showToast('Ошибка: ' + err.message, 'error'); }
}
```

- [ ] **Step 5: Smoke-test**

Open `http://localhost:8002/#complexes`. Expected: empty state «Профилей пока нет…».

- [ ] **Step 6: Commit**

```bash
git add backend/static/app.js backend/static/styles.css backend/static/index.html
git commit -m "feat(ui): complex database list and detail pages"
```

---

## Task 16: End-to-end manual test on user's 3 files

**Files:** none (verification task).

- [ ] **Step 1: Confirm the 3 files are accessible**

Ask the user to upload the 3 presentation files via `http://localhost:8002/#upload` with template **«Презентация ЖК»** selected.

- [ ] **Step 2: Wait for processing**

Each upload should appear in `#calls` with template badge «Презентация ЖК», status going `uploading → processing → completed`.

For long files (60+ minutes), processing takes 5-15 minutes. Monitor the worker:
```bash
journalctl -u realestate-worker.service -f | grep -E "session|extraction"
```

- [ ] **Step 3: Verify each session has an extraction**

```bash
PGPASSWORD=$(grep ^POSTGRES_PASSWORD /root/projects/realestate/.env | cut -d= -f2) psql -h localhost -U realestate -d realestate -c "
SELECT s.id, s.status, s.metadata->>'template_id' AS tpl,
       (SELECT COUNT(*) FROM complex_extractions ce WHERE ce.session_id = s.id) AS exts
FROM sessions s
WHERE s.metadata->>'template_id' IS NOT NULL
ORDER BY s.created_at DESC LIMIT 10
"
```

Expected: each of the 3 sessions has `exts = 1` and `status = completed`.

- [ ] **Step 4: Verify the «База ЖК» page**

Open `http://localhost:8002/#complexes`. Expected: 1-3 cards (depends on whether the files are about the same ЖК or different). Click a card — fields are populated.

- [ ] **Step 5: Verify session detail shows extraction**

Open any of the 3 sessions in `#calls/{id}`. Expected: «Профиль ЖК» section with rendered fields, «Открыть профиль ЖК» button works.

- [ ] **Step 6: Try reprocess**

On one of the sessions, click «Перепрогнать с шаблоном…» → choose «Презентация ЖК» again → confirm. Expected: status goes to `processing`, then back to `completed` within 1-3 minutes (no transcription, only extraction). Page shows updated extraction.

- [ ] **Step 7: Try relink**

If the auto-match created two profiles for what should be one ЖК, use «Перепривязать к другому ЖК…» on one of the sessions to merge them.

- [ ] **Step 8: Cleanup test files (only if desired)**

Use the existing «Удалить сессию» button in session detail — it cascades through the new tables.

- [ ] **Step 9: Final commit (no code, just a tag)**

```bash
git tag -a feat-extraction-templates -m "Extraction templates and complex DB shipped"
```

---

## Self-Review Notes

**Spec coverage:** all 7 spec sections (data model, pipeline, matching/aggregation, API endpoints, frontend pages, pre-seeded template, risks) have at least one task. The «Out of scope» items are explicitly NOT in any task.

**Type consistency:** `match_or_create_complex(db, extraction_id) → uuid | None`, `_recompute_aggregate(db, complex_id) → None`, `merge_extractions(list[dict]) → dict` — used consistently across Tasks 3, 5, 7, 10.

**Placeholder check:** the only intentionally-deferred item is the `<leave the existing quality/sentiment/amocrm-sync calls here as-is>` marker in Task 7 Step 3 — that's literally instructing the implementer to keep the existing block untouched, not a TBD.

**Risks not in tasks:** the "shared `_basic_normalize` between text-normalize and aggregation" import lands in Task 3 Step 3 (`from tasks._text_normalize import _basic_normalize`). This relies on Task 2 being merged first — task order is enforced.

**Frontend `renderExtractionFields`** is reused by both the session detail (Task 14) and the complex detail (Task 15). The function is defined once in Task 14 (Step 3) and referenced in Task 15. Task order: 14 must be merged before 15 attempts to use it.
