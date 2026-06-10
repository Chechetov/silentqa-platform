# Multi-call Context — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the Phase 1 multi-call pipeline with follow-through tracking, an updated-in-place deal-summary note, stage-aware recommendations, offline-gap awareness, and Redis-based per-lead serialisation.

**Architecture:** Builds on Phase 1 primitives (`prior_context`, V4 schema, `plan_next_call`, two-note AmoCRM push). Extensions are additive: V4 gains `previous_recommendations_follow_through`; `prior_context` gains `last_call_plan`, `current_deal_stage`, and non-call events in `interactions_history`; a third AmoCRM note (deal summary) is upserted via a new `amocrm_deal_summaries` table; a Redis-lock wrapper serialises concurrent processing on the same lead.

**Tech Stack:** Python 3.11 (Celery, FastAPI), OpenAI Responses API, PostgreSQL (alembic), Redis (lock + Celery broker), pytest (existing infra).

**Spec:** `docs/superpowers/specs/2026-04-17-short-call-filter-and-reprocess-design.md` (Phase 2 section).

**Restore point if needed:** tag `pre-multicall-2026-04-17`, commit `4f8c75a`.

---

## File Structure Overview

**New files:**
- `worker/tasks/lead_lock.py` — Redis-backed per-lead context manager.
- `worker/tasks/deal_summary.py` — pure builder + persistence for the rolling deal-summary note.
- `backend/alembic/versions/003_amocrm_deal_summaries.py` — migration for the new table.
- `worker/tests/test_lead_lock.py`, `worker/tests/test_deal_summary.py` — unit tests.

**Modified files:**
- `worker/tasks/prior_context.py` — `last_call_plan`, `current_deal_stage`, and event-based `interactions_history`; add loaders + merge logic.
- `worker/tasks/quality.py` — V4 schema gains `previous_recommendations_follow_through`; `CONTEXT_AWARE_INSTRUCTIONS` gains a follow-through section; `PLAN_SYSTEM_PROMPT` gains stage-aware rules; `plan_next_call` wires `deal_stage` from context.
- `worker/tasks/amocrm_sync.py` — new helpers `get_pipelines_cached`, `get_lead_stage`, `fetch_lead_events`; `format_enriched_note` renders follow-through block.
- `worker/tasks/pipeline.py` — wrap `_run_pipeline` body in lead-lock, fetch stage, push deal-summary note after plan, thread `deal_stage` through.
- `worker/tests/test_prior_context.py` — extend with new fields/behaviour.
- `backend/static/app.js` — follow-through card in call detail view.

---

## Task 1: Redis-lock context manager

**Files:**
- Create: `worker/tasks/lead_lock.py`
- Create: `worker/tests/test_lead_lock.py`

**Rationale:** Two calls on the same lead (or a poll + a manual reprocess) can race on `prior_context` construction and metadata updates. A Redis `SET NX` lock keyed on `lead_id` prevents this cheaply. Redis is already running for Celery broker.

- [ ] **Step 1: Write failing tests**

Write `worker/tests/test_lead_lock.py`:

```python
"""Tests for the per-lead Redis lock context manager."""
import threading
import time
from unittest.mock import MagicMock

import pytest

from tasks.lead_lock import lead_lock, LOCK_KEY_PREFIX


def test_no_lock_when_lead_id_falsy():
    """With no lead_id, lock is a no-op — yields immediately."""
    client = MagicMock()
    with lead_lock(0, client=client):
        pass
    with lead_lock(None, client=client):
        pass
    client.set.assert_not_called()


def test_acquires_on_first_try():
    client = MagicMock()
    # Simulate successful acquire
    client.set.return_value = True
    with lead_lock(42, client=client, poll_interval=0.01):
        pass
    # first call: set key NX EX
    args, kwargs = client.set.call_args_list[0]
    assert args[0] == f"{LOCK_KEY_PREFIX}42"
    assert kwargs.get("nx") is True
    assert kwargs.get("ex") == 600  # default timeout


def test_releases_only_own_token():
    """The finally block must eval the Lua script with the same token used to acquire."""
    client = MagicMock()
    client.set.return_value = True
    with lead_lock(42, client=client, poll_interval=0.01):
        pass
    assert client.eval.called
    eval_args = client.eval.call_args[0]
    # eval(script, 1, key, token) — token stored on acquire must equal the one passed to eval
    assert eval_args[2] == f"{LOCK_KEY_PREFIX}42"
    # token argv[1] is the 4th positional arg
    set_token = client.set.call_args[0][1]
    eval_token = eval_args[3]
    assert set_token == eval_token


def test_waits_and_retries_until_acquired():
    """If first acquire fails, poll until it succeeds."""
    client = MagicMock()
    # 2 failures, then success
    client.set.side_effect = [False, False, True]
    start = time.time()
    with lead_lock(42, client=client, poll_interval=0.01):
        pass
    elapsed = time.time() - start
    assert client.set.call_count == 3
    assert elapsed >= 0.02  # at least two sleeps


def test_custom_timeout_respected():
    client = MagicMock()
    client.set.return_value = True
    with lead_lock(42, client=client, timeout=1200, poll_interval=0.01):
        pass
    assert client.set.call_args[1]["ex"] == 1200
```

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_lead_lock.py -v`
Expected: ImportError on `tasks.lead_lock`.

- [ ] **Step 2: Implement `lead_lock.py`**

Write `worker/tasks/lead_lock.py`:

```python
"""
Per-lead mutual exclusion via Redis.

Wraps pipeline stages so concurrent calls on the same lead_id serialise,
preventing races between prior_context reads and subsequent metadata
writes.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager
from typing import Any

import redis

logger = logging.getLogger(__name__)

LOCK_KEY_PREFIX = "lead_lock:"
DEFAULT_TIMEOUT_SEC = 600

_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

_default_client: redis.Redis | None = None


def _get_default_client() -> redis.Redis:
    global _default_client
    if _default_client is None:
        url = os.getenv("REDIS_URL", "redis://localhost:6379/1")
        _default_client = redis.Redis.from_url(url)
    return _default_client


@contextmanager
def lead_lock(
    lead_id: int | None,
    *,
    client: Any | None = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    poll_interval: float = 1.0,
):
    """Acquire a lock scoped to lead_id; no-op when lead_id is falsy."""
    if not lead_id:
        yield
        return

    redis_client = client or _get_default_client()
    key = f"{LOCK_KEY_PREFIX}{lead_id}"
    token = str(uuid.uuid4())

    acquired = redis_client.set(key, token, nx=True, ex=timeout)
    waited = 0.0
    while not acquired:
        time.sleep(poll_interval)
        waited += poll_interval
        if waited >= timeout:
            logger.warning(
                f"lead_lock: waited {waited:.0f}s for lead {lead_id}; "
                f"proceeding without lock (upstream lock may be stale)"
            )
            # Proceed without lock — better than stalling forever.
            yield
            return
        acquired = redis_client.set(key, token, nx=True, ex=timeout)

    logger.debug(f"lead_lock: acquired lead {lead_id}")
    try:
        yield
    finally:
        try:
            redis_client.eval(_RELEASE_LUA, 1, key, token)
            logger.debug(f"lead_lock: released lead {lead_id}")
        except Exception:
            logger.exception(f"lead_lock: failed to release lead {lead_id}")
```

- [ ] **Step 3: Run tests**

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_lead_lock.py -v`
Expected: all 5 tests pass.

- [ ] **Step 4: Wire lock into `_run_pipeline` via inner helper**

In `worker/tasks/pipeline.py`, near the other `tasks.*` imports (line ~20), add:

```python
from tasks.lead_lock import lead_lock
```

To avoid a massive re-indentation of `_run_pipeline`, extract the post-short-call body into a private helper `_run_pipeline_inner(task, session_id, audio_path, config, company_config, scenario, session_meta, audio_duration)` that takes the already-measured audio duration and performs steps 2–8. Then `_run_pipeline` becomes a thin wrapper:

1. Run the short-call gate (unchanged, returns early if audio < 20s).
2. Acquire the per-lead Redis lock.
3. Delegate to `_run_pipeline_inner`.

Concretely, after the short-call early-return block and BEFORE `# === 2. Transcription ===`, insert:

```python
    # Serialise concurrent pipelines on the same lead (prevents prior_context
    # races when a manual reprocess overlaps with polling, etc.)
    with lead_lock(session_meta.get("lead_id")):
        return _run_pipeline_inner(task, session_id, audio_path, config, company_config, scenario, session_meta, audio_duration)
```

Then MOVE the existing body (everything from `# === 2. Transcription ===` down to the final `return {...}`) into a new function defined immediately after `_run_pipeline`:

```python
def _run_pipeline_inner(task, session_id: str, audio_path: str, config: dict, company_config: dict, scenario: dict | None, session_meta: dict, audio_duration: float):
    """Steps 2–8 of the pipeline, wrapped by a per-lead lock in the caller."""
    # === 2. Transcription ===
    ...  # existing body, indentation unchanged
```

IMPORTANT: do not indent the short-call gate (that path runs before the lock and early-returns). Only steps 2–8 go inside `_run_pipeline_inner`.

Verify by running existing tests (no functional change should be observable):
```bash
cd worker && source ../.venv/bin/activate && python -c "from tasks.pipeline import _run_pipeline, _run_pipeline_inner; print('ok')"
```
Expected: `ok`.

- [ ] **Step 5: Smoke-test imports and run unit tests**

Run:
```bash
cd worker && source ../.venv/bin/activate && \
  python -c "from tasks.pipeline import _run_pipeline; from tasks.lead_lock import lead_lock; print('ok')" && \
  pytest -v
```
Expected: `ok`, 19 tests pass (14 existing + 5 new).

- [ ] **Step 6: Commit**

```bash
git add worker/tasks/lead_lock.py worker/tests/test_lead_lock.py worker/tasks/pipeline.py
git commit -m "feat(pipeline): per-lead Redis lock to serialise concurrent processing"
```

---

## Task 2: Follow-through — V4 schema + prompt

**Files:**
- Modify: `worker/tasks/quality.py`
- Modify: `worker/tests/test_quality_schemas.py`

- [ ] **Step 1: Extend the V4 schema test**

In `worker/tests/test_quality_schemas.py`, append after the existing tests:

```python
def test_v4_has_follow_through_field():
    props = QUALITY_JSON_SCHEMA_V4["schema"]["properties"]
    assert "previous_recommendations_follow_through" in props
    ft = props["previous_recommendations_follow_through"]["properties"]
    assert "total_recommendations" in ft
    assert "executed_count" in ft
    assert "items" in ft
    item_props = ft["items"]["items"]["properties"]
    for field in ("recommendation_text", "executed", "evidence", "effectiveness"):
        assert field in item_props


def test_v4_follow_through_in_required():
    req = set(QUALITY_JSON_SCHEMA_V4["schema"]["required"])
    assert "previous_recommendations_follow_through" in req
```

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_quality_schemas.py -v`
Expected: 2 new tests fail ("previous_recommendations_follow_through" missing).

- [ ] **Step 2: Extend V4 schema**

In `worker/tasks/quality.py`, locate `QUALITY_JSON_SCHEMA_V4` (added in Phase 1). Add the new property to its `properties` dict and to the `required` list. The full schema after the edit should include this block inside `properties`:

```python
            "previous_recommendations_follow_through": {
                "type": "object",
                "description": "Как брокер выполнил рекомендации из плана ПРЕДЫДУЩЕГО звонка",
                "properties": {
                    "total_recommendations": {"type": "integer"},
                    "executed_count": {"type": "integer"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "recommendation_text": {"type": "string"},
                                "executed": {
                                    "type": "string",
                                    "description": "yes | no | partial",
                                },
                                "evidence": {"type": ["string", "null"]},
                                "effectiveness": {"type": ["string", "null"]},
                            },
                            "required": ["recommendation_text", "executed", "evidence", "effectiveness"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["total_recommendations", "executed_count", "items"],
                "additionalProperties": False,
            },
```

And add `"previous_recommendations_follow_through"` to the `required` list of `QUALITY_JSON_SCHEMA_V4["schema"]`:

```python
        "required": QUALITY_JSON_SCHEMA_V3["schema"]["required"] + [
            "stage_progression", "applicable_checklist_items",
            "previous_recommendations_follow_through",
        ],
```

- [ ] **Step 3: Extend `CONTEXT_AWARE_INSTRUCTIONS`**

In `worker/tasks/quality.py`, extend the existing `CONTEXT_AWARE_INSTRUCTIONS` constant by appending this block before its final triple-quote:

```python
## Follow-through по плану прошлого звонка

В prior_context может быть поле `last_call_plan` — рекомендации, которые
были даны после ПРЕДЫДУЩЕГО звонка. Для КАЖДОЙ рекомендации из
`last_call_plan.goals` и `last_call_plan.talking_points` определи:

- `executed`: "yes" | "no" | "partial"
- `evidence`: короткая цитата из транскрипта текущего звонка,
  подтверждающая выполнение; `null` если не выполнено.
- `effectiveness`: если выполнено — как это сработало; `null` если не выполнено.

Заполни `previous_recommendations_follow_through`:
- `total_recommendations` = сумма `len(goals) + len(talking_points)` прошлого плана.
- `executed_count` = сколько из них "yes".
- `items` — перечень с полями выше.

Если `last_call_plan` отсутствует (первый звонок или план не был
сгенерирован) — верни пустой блок: `total_recommendations=0,
executed_count=0, items=[]`.
```

- [ ] **Step 4: Run schema tests, verify pass**

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_quality_schemas.py -v`
Expected: all 6 tests pass (4 existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/quality.py worker/tests/test_quality_schemas.py
git commit -m "feat(quality): V4 gains previous_recommendations_follow_through block"
```

---

## Task 3: Follow-through — load past plan into prior_context

**Files:**
- Modify: `worker/tasks/prior_context.py`
- Modify: `worker/tests/test_prior_context.py`

**Design note:** the eval LLM can only assess follow-through if it sees the plan that was generated after the immediately-previous call. We load that plan from disk (`<RESULTS_PATH>/<prev_session_id>/next_call_plan.json`) during `load_past_sessions_for_lead`, pass it through, and surface it as `prior_context["last_call_plan"]`.

- [ ] **Step 1: Write failing test**

In `worker/tests/test_prior_context.py`, append:

```python
def test_build_prior_context_surfaces_last_call_plan():
    r1 = {
        "created_at_iso": "2026-04-01T10:00:00Z",
        "duration": 900,
        "direction": "out",
        "report": {"brief_summary": "первый", "client_info": {}},
        "plan": None,
    }
    plan = {
        "goals": [{"priority": 1, "text": "Назначить показ"}],
        "talking_points": [{"topic": "Виды", "argument": "хорошие", "personalized_hook": ""}],
        "unresolved_objections": [],
        "information_gaps": [],
        "recommended_properties": [],
        "risks": [],
        "suggested_opener": "Иван, добрый день!",
    }
    r2 = {
        "created_at_iso": "2026-04-10T10:00:00Z",
        "duration": 1200,
        "direction": "out",
        "report": {"brief_summary": "второй", "client_info": {}},
        "plan": plan,
    }
    ctx = build_prior_context_dict(past_reports=[r1, r2], current_created_at="2026-04-17T10:00:00Z")
    # last_call_plan must reflect the plan from the most-recent prior call that has one
    assert ctx["last_call_plan"] == plan


def test_build_prior_context_last_call_plan_none_when_missing():
    r1 = {
        "created_at_iso": "2026-04-10T10:00:00Z",
        "duration": 1200,
        "direction": "out",
        "report": {"brief_summary": "первый", "client_info": {}},
        "plan": None,
    }
    ctx = build_prior_context_dict(past_reports=[r1], current_created_at="2026-04-17T10:00:00Z")
    assert ctx.get("last_call_plan") is None
```

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_prior_context.py -v`
Expected: 2 new tests fail (last_call_plan not populated).

- [ ] **Step 2: Extend `build_prior_context_dict`**

In `worker/tasks/prior_context.py`, modify `build_prior_context_dict`. The function signature stays the same, but inside the loop, capture the most-recent `plan` field; after the loop, assign it to `last_call_plan`:

Find the existing `return {...}` block and modify it. The updated tail of the function should read:

```python
    last_call_plan = None
    for item in past_reports:
        # existing body that appends to interactions_history, etc.
        ...
        if item.get("plan"):
            last_call_plan = item["plan"]

    client_profile = merge_client_profiles(profile_entries)

    return {
        "call_number": call_number,
        "previous_calls_count": previous_count,
        "client_profile": client_profile,
        "interactions_history": interactions_history,
        "open_objections": open_objections,
        "resolved_objections": resolved_objections,
        "last_call_plan": last_call_plan,
    }
```

Note: the `plan` key in each past-report item is populated by the orchestrator (next step). In tests it's set directly.

- [ ] **Step 3: Load plan from disk in the orchestrator**

In `worker/tasks/prior_context.py`, extend `_load_quality_report` with a sibling helper, or modify `load_past_sessions_for_lead` to additionally load the plan. Add this helper next to `_load_quality_report`:

```python
def _load_plan(session_id: str) -> dict | None:
    path = Path(RESULTS_PATH) / session_id / "next_call_plan.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception(f"Failed to read {path}")
        return None
```

Then in `load_past_sessions_for_lead`, after the existing `report = _load_quality_report(session_id)` block, add plan loading before `entries.append(...)`:

```python
        plan = _load_plan(session_id)
        ...
        entries.append({
            "created_at_iso": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
            "duration": duration,
            "direction": direction,
            "report": report,
            "plan": plan,
        })
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_prior_context.py -v`
Expected: all 9 tests pass (7 existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/prior_context.py worker/tests/test_prior_context.py
git commit -m "feat(prior-context): surface last_call_plan for follow-through tracking"
```

---

## Task 4: Follow-through — UI card + AmoCRM note block

**Files:**
- Modify: `worker/tasks/amocrm_sync.py` (render follow-through inside `format_enriched_note`)
- Modify: `backend/static/app.js` (follow-through card in call detail)

- [ ] **Step 1: Render follow-through in the AmoCRM eval note**

In `worker/tasks/amocrm_sync.py`, inside `format_enriched_note`, after the "Возражения клиента" block and before the "Рекомендации" block (search for `suggestions = quality_report.get("improvement_suggestions", [])`), insert:

```python
    # Follow-through на предыдущий план
    ft = quality_report.get("previous_recommendations_follow_through") or {}
    if ft.get("total_recommendations", 0) > 0:
        executed = ft.get("executed_count", 0)
        total = ft["total_recommendations"]
        parts.append(f"✅ Выполнено {executed} из {total} рекомендаций прошлого звонка:")
        for item in ft.get("items", []):
            status = item.get("executed", "no")
            icon = {"yes": "✅", "partial": "~", "no": "❌"}.get(status, "—")
            parts.append(f"{icon} {item.get('recommendation_text', '')}")
            if item.get("evidence"):
                parts.append(f"  → {item['evidence']}")
        parts.append("")
```

- [ ] **Step 2: Smoke-check the rendered text**

Run:
```bash
cd worker && source ../.venv/bin/activate && python -c "
from tasks.amocrm_sync import format_enriched_note
q = {
    'overall_score': 8,
    'call_classification': {'type': 'productive'},
    'brief_summary': 'Test',
    'client_info': {},
    'protocol_checklist': [],
    'objections': [],
    'conversation_outcome': {},
    'previous_recommendations_follow_through': {
        'total_recommendations': 3,
        'executed_count': 2,
        'items': [
            {'recommendation_text': 'Уточнить бюджет', 'executed': 'yes', 'evidence': 'Брокер спросил \"какая верхняя планка\"', 'effectiveness': 'узнали 20-25'},
            {'recommendation_text': 'Показать рассрочку', 'executed': 'no', 'evidence': None, 'effectiveness': None},
            {'recommendation_text': 'Предложить встречу', 'executed': 'partial', 'evidence': 'Упомянул вскользь', 'effectiveness': 'не назначили'},
        ],
    },
    'general_checks': [],
    'improvement_suggestions': [],
    'summary': '',
}
print(format_enriched_note(q, 'sess-1'))
" | grep -A 8 "Выполнено"
```
Expected: output shows `Выполнено 2 из 3 рекомендаций прошлого звонка:` followed by 3 items with ✅/~/❌ icons.

- [ ] **Step 3: Add follow-through card to admin UI**

In `backend/static/app.js`, locate the `renderCallDetail` function and the existing V3 blocks (e.g., `callClassification`, `briefSummary`). After the existing classification card, add:

```javascript
    // Follow-through (V4 Phase 2)
    const followThrough = analysis && analysis.previous_recommendations_follow_through;
    if (followThrough && followThrough.total_recommendations > 0) {
      const icons = {yes: '✅', partial: '~', no: '❌'};
      const colors = {yes: '#4CAF50', partial: '#d29922', no: '#f85149'};
      const items = (followThrough.items || []).map(it => {
        const icon = icons[it.executed] || '—';
        const color = colors[it.executed] || 'var(--text-muted)';
        return `
          <li style="margin:6px 0">
            <span style="color:${color};font-weight:600">${icon}</span>
            ${escapeHtml(it.recommendation_text || '')}
            ${it.evidence ? `<div style="margin-left:24px;color:var(--text-secondary);font-size:13px">${escapeHtml(it.evidence)}</div>` : ''}
          </li>
        `;
      }).join('');
      html += `
        <div class="card">
          <h3>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
            Выполнение рекомендаций прошлого звонка
          </h3>
          <p style="margin-bottom:8px"><strong>${followThrough.executed_count}</strong> из <strong>${followThrough.total_recommendations}</strong></p>
          <ul style="list-style:none;padding:0;margin:0">${items}</ul>
        </div>
      `;
    }
```

Insert after the `callClassification` card block (search for `Классификация звонка` heading).

- [ ] **Step 4: Commit**

```bash
git add worker/tasks/amocrm_sync.py backend/static/app.js
git commit -m "feat(ui): render follow-through block in AmoCRM note and admin UI"
```

---

## Task 5: Deal-summary migration

**Files:**
- Create: `backend/alembic/versions/003_amocrm_deal_summaries.py`

- [ ] **Step 1: Write migration**

Write `backend/alembic/versions/003_amocrm_deal_summaries.py`:

```python
"""Add amocrm_deal_summaries table for the rolling per-lead summary note

Revision ID: 003
Revises: 002
Create Date: 2026-04-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""CREATE TABLE amocrm_deal_summaries (
        lead_id BIGINT PRIMARY KEY,
        amo_note_id BIGINT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        content_json JSONB NOT NULL
    )""")
    op.execute("CREATE INDEX idx_deal_summaries_updated ON amocrm_deal_summaries(updated_at)")


def downgrade() -> None:
    op.drop_index("idx_deal_summaries_updated")
    op.drop_table("amocrm_deal_summaries")
```

- [ ] **Step 2: Run the migration**

Run:
```bash
cd backend && source ../.venv/bin/activate && \
  set -a && source ../.env && set +a && \
  alembic upgrade head
```
Expected: `Running upgrade 002 -> 003, Add amocrm_deal_summaries table...`.

Verify table exists:
```bash
PGPASSWORD="$(grep ^DATABASE_URL= ../.env | sed -E 's/.*:([^:@]+)@.*/\1/')" \
  psql -h localhost -U realestate -d realestate -c "\d+ amocrm_deal_summaries"
```
Expected: columns `lead_id`, `amo_note_id`, `updated_at`, `content_json` listed.

- [ ] **Step 3: Commit**

```bash
git add backend/alembic/versions/003_amocrm_deal_summaries.py
git commit -m "feat(db): amocrm_deal_summaries table for rolling per-lead note"
```

---

## Task 6: Deal-summary builder (pure)

**Files:**
- Create: `worker/tasks/deal_summary.py` (pure section — build + format)
- Create: `worker/tests/test_deal_summary.py`

- [ ] **Step 1: Write failing tests**

Write `worker/tests/test_deal_summary.py`:

```python
"""Tests for the deal-summary builder and formatter."""
from tasks.deal_summary import build_deal_summary, format_deal_summary


def _ctx(**overrides):
    base = {
        "call_number": 3,
        "previous_calls_count": 2,
        "client_profile": {
            "budget": "20-25 млн (ранее: 18 млн, пересмотрено 2026-04-10)",
            "locations": "Хамовники",
            "purchase_goal": "жильё",
        },
        "interactions_history": [
            {"date": "2026-04-03", "type": "call_out", "duration": 840,
             "classification": "partial", "outcome": "info_provided", "brief": "Первый контакт"},
            {"date": "2026-04-10", "type": "call_out", "duration": 1200,
             "classification": "productive", "outcome": "appointment", "brief": "Назначили встречу"},
        ],
        "open_objections": [{"text": "Хочет южную сторону", "raised_at": "2026-04-10", "category": "other"}],
        "resolved_objections": [{"text": "Дорого", "resolved_at": "2026-04-10", "how": "показали рассрочку"}],
        "last_call_plan": None,
    }
    base.update(overrides)
    return base


def test_build_summary_counts_interactions_and_current_call():
    ctx = _ctx()
    current = {"brief_summary": "обсуждали впечатления", "audio_duration": 1020}
    s = build_deal_summary(ctx, current, stage_name="Квалификация", current_created_at="2026-04-17T14:23:00Z")
    assert s["stage"] == "Квалификация"
    assert s["total_interactions"] == 3  # 2 prior + 1 current
    assert s["open_objections_count"] == 1
    assert s["resolved_objections_count"] == 1


def test_format_summary_contains_expected_sections():
    ctx = _ctx()
    current = {"brief_summary": "обсуждали впечатления", "audio_duration": 1020}
    s = build_deal_summary(ctx, current, stage_name="Квалификация", current_created_at="2026-04-17T14:23:00Z")
    text = format_deal_summary(s)
    assert "🏠 Сводка по сделке" in text
    assert "Квалификация" in text
    assert "Хамовники" in text
    assert "Дорого" in text  # resolved
    assert "Хочет южную сторону" in text  # open
    assert "2026-04-17" in text  # updated_at


def test_format_summary_without_last_plan():
    ctx = _ctx(last_call_plan=None)
    current = {"brief_summary": "X", "audio_duration": 100}
    s = build_deal_summary(ctx, current, stage_name=None, current_created_at="2026-04-17T14:23:00Z")
    text = format_deal_summary(s)
    assert "Следующие шаги" not in text  # section omitted when no plan


def test_format_summary_with_last_plan():
    plan = {
        "goals": [{"priority": 1, "text": "Показ ЖК X в четверг"}],
        "talking_points": [], "unresolved_objections": [], "information_gaps": [],
        "recommended_properties": [], "risks": [], "suggested_opener": "",
    }
    ctx = _ctx(last_call_plan=plan)
    current = {"brief_summary": "X", "audio_duration": 100}
    s = build_deal_summary(ctx, current, stage_name=None, current_created_at="2026-04-17T14:23:00Z")
    text = format_deal_summary(s)
    assert "Следующие шаги" in text
    assert "Показ ЖК X" in text
```

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_deal_summary.py -v`
Expected: ImportError on `tasks.deal_summary`.

- [ ] **Step 2: Implement the pure builder + formatter**

Write `worker/tasks/deal_summary.py`:

```python
"""
Build and render the rolling deal-summary note for a lead.

Deterministic aggregation of prior_context + the current call — no LLM.
Persistence (DB / AmoCRM note upsert) lives in the companion module entries
below; the builder/formatter here are pure so they can be unit-tested.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def build_deal_summary(
    prior_context: dict,
    current_quality_report: dict,
    stage_name: str | None,
    current_created_at: str,
) -> dict:
    """Aggregate prior_context + current call into a structured summary dict."""
    profile = dict(prior_context.get("client_profile") or {})

    # Count current call as part of history total
    total = len(prior_context.get("interactions_history") or []) + 1

    return {
        "stage": stage_name or "—",
        "total_interactions": total,
        "call_number": prior_context.get("call_number", 1),
        "client_profile": profile,
        "history": list(prior_context.get("interactions_history") or []),
        "open_objections": list(prior_context.get("open_objections") or []),
        "resolved_objections": list(prior_context.get("resolved_objections") or []),
        "last_call_plan": prior_context.get("last_call_plan"),
        "current_brief": current_quality_report.get("brief_summary") or "",
        "current_duration": int(current_quality_report.get("audio_duration") or 0),
        "updated_at": current_created_at,
    }


_PROFILE_LABELS = {
    "purchase_goal": "Цель",
    "locations": "Локация",
    "apartment_format": "Формат",
    "timeline": "Сроки",
    "budget": "Бюджет",
    "payment_form": "Оплата",
    "important_factors": "Важно",
    "what_viewed": "Смотрели",
    "current_situation": "Ситуация",
    "other": "Прочее",
}


def format_deal_summary(summary: dict) -> str:
    """Render the structured summary dict as the AmoCRM note markdown."""
    updated = (summary.get("updated_at") or "")[:10]
    lines: list[str] = [f"🏠 Сводка по сделке (AI, обновлена {updated})", ""]

    lines.append(f"📊 Этап: {summary['stage']}")
    lines.append(f"📞 Взаимодействий: {summary['total_interactions']}")
    lines.append("")

    profile = summary.get("client_profile") or {}
    if profile:
        lines.append("👤 Профиль клиента:")
        for key, label in _PROFILE_LABELS.items():
            val = profile.get(key)
            if val:
                lines.append(f"• {label}: {val}")
        lines.append("")

    history = summary.get("history") or []
    if history:
        lines.append("📅 История:")
        for h in history:
            mins = (h.get("duration") or 0) // 60
            type_label = {"call_in": "входящий звонок", "call_out": "исходящий звонок"}.get(h.get("type", ""), h.get("type", ""))
            lines.append(
                f"• {h.get('date', '')} ({type_label}, {mins} мин) — {h.get('brief', '')}"
            )
        # Add current call
        cur_mins = (summary.get("current_duration") or 0) // 60
        lines.append(
            f"• {updated} (текущий звонок, {cur_mins} мин) — {summary.get('current_brief', '')}"
        )
        lines.append("")

    open_obj = summary.get("open_objections") or []
    if open_obj:
        lines.append("❗ Открытые возражения:")
        for o in open_obj:
            lines.append(f"• «{o.get('text', '')}»")
        lines.append("")

    resolved = summary.get("resolved_objections") or []
    if resolved:
        lines.append("✅ Закрытые возражения:")
        for o in resolved:
            how = o.get("how") or ""
            when = o.get("resolved_at") or ""
            suffix = f" — {how}" if how else ""
            date_suffix = f" ({when})" if when else ""
            lines.append(f"• «{o.get('text', '')}»{suffix}{date_suffix}")
        lines.append("")

    plan = summary.get("last_call_plan")
    if plan:
        goals = sorted(plan.get("goals") or [], key=lambda g: g.get("priority", 99))
        if goals:
            lines.append("➡️ Следующие шаги (из последнего плана):")
            for g in goals:
                lines.append(f"• {g.get('text', '')}")
            lines.append("")

    return "\n".join(lines).strip()
```

- [ ] **Step 3: Run tests, verify pass**

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_deal_summary.py -v`
Expected: all 4 tests pass.

- [ ] **Step 4: Commit**

```bash
git add worker/tasks/deal_summary.py worker/tests/test_deal_summary.py
git commit -m "feat(deal-summary): pure builder and markdown formatter"
```

---

## Task 7: Deal-summary persistence + AmoCRM upsert

**Files:**
- Modify: `worker/tasks/deal_summary.py` (add DB + AmoCRM upsert functions)
- Modify: `worker/tasks/pipeline.py` (call the upserter after plan note)

- [ ] **Step 1: Append DB and AmoCRM helpers to `deal_summary.py`**

At the end of `worker/tasks/deal_summary.py`, append:

```python
# === Persistence ==================================================================

import json
import os
from datetime import datetime, timezone

import psycopg2


def _get_sync_db_url() -> str:
    url = os.getenv("DATABASE_URL_SYNC", "") or os.getenv("DATABASE_URL", "")
    return url.replace("postgresql+psycopg2://", "postgresql://").replace("postgresql+asyncpg://", "postgresql://")


def get_existing_summary_note_id(lead_id: int) -> int | None:
    db_url = _get_sync_db_url()
    if not db_url or not lead_id:
        return None
    try:
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT amo_note_id FROM amocrm_deal_summaries WHERE lead_id=%s", (lead_id,))
                row = cur.fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:
        logger.exception(f"Failed to read deal summary for lead {lead_id}")
        return None


def upsert_summary_record(lead_id: int, amo_note_id: int, content: dict) -> None:
    db_url = _get_sync_db_url()
    if not db_url or not lead_id:
        return
    try:
        conn = psycopg2.connect(db_url)
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO amocrm_deal_summaries (lead_id, amo_note_id, updated_at, content_json)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (lead_id) DO UPDATE
                           SET amo_note_id = EXCLUDED.amo_note_id,
                               updated_at  = EXCLUDED.updated_at,
                               content_json = EXCLUDED.content_json
                        """,
                        (lead_id, amo_note_id, datetime.now(timezone.utc), json.dumps(content, ensure_ascii=False)),
                    )
        finally:
            conn.close()
    except Exception:
        logger.exception(f"Failed to upsert deal summary for lead {lead_id}")


def push_deal_summary(lead_id: int, summary: dict) -> int | None:
    """Format + create-or-update the AmoCRM summary note. Returns the note_id."""
    from tasks.amocrm_sync import create_plain_note, update_note
    text = format_deal_summary(summary)
    existing = get_existing_summary_note_id(lead_id)
    if existing:
        result = update_note(lead_id, existing, text)
        if result.get("ok"):
            upsert_summary_record(lead_id, existing, summary)
            logger.info(f"Updated deal summary note {existing} for lead {lead_id}")
            return existing
        logger.warning(f"Failed to update summary note {existing}, will try create: {result}")

    result = create_plain_note(lead_id, text)
    if result.get("ok"):
        note_id = result["note_id"]
        upsert_summary_record(lead_id, note_id, summary)
        logger.info(f"Created deal summary note {note_id} for lead {lead_id}")
        return note_id
    logger.warning(f"Failed to create summary note for lead {lead_id}: {result}")
    return None
```

- [ ] **Step 2: Call the upserter after the plan note in `_push_to_amocrm`**

In `worker/tasks/pipeline.py`, extend the imports (line ~27) to include summary helpers:

```python
from tasks.deal_summary import build_deal_summary, push_deal_summary
```

In `_push_to_amocrm`, after the plan-note creation block (last lines of the function), append:

```python
    # Third note (singleton per lead): deal summary — update-in-place
    if quality_report.get("skip_reason") != "too_short":
        stage_name = (prior_context_for_summary or {}).get("current_deal_stage", {}).get("stage_name")
        summary = build_deal_summary(
            prior_context_for_summary or {},
            quality_report,
            stage_name=stage_name,
            current_created_at=session_meta.get("_current_created_at_iso", ""),
        )
        push_deal_summary(lead_id, summary)
```

This requires `prior_context_for_summary` and the ISO timestamp to be threaded into `_push_to_amocrm`. Extend its signature:

```python
def _push_to_amocrm(
    session_id: str,
    quality_report: dict,
    session_meta: dict,
    audio_path: str,
    next_call_plan: dict | None = None,
    prior_context_for_summary: dict | None = None,
):
```

And update the caller in `_run_pipeline` step 8 to pass it:

```python
        _push_to_amocrm(
            session_id, quality_report, session_meta, audio_path,
            next_call_plan=next_call_plan,
            prior_context_for_summary=prior_context,
        )
```

Ensure `session_meta["_current_created_at_iso"]` is populated earlier in `_run_pipeline`, right after computing `current_created_at`:

```python
    current_created_at = _get_session_created_at(session_id)
    if current_created_at is not None:
        session_meta["_current_created_at_iso"] = (
            current_created_at.isoformat() if hasattr(current_created_at, "isoformat") else str(current_created_at)
        )
```

- [ ] **Step 3: Smoke-test imports and DB helper**

Run:
```bash
cd worker && source ../.venv/bin/activate && \
  set -a && source ../.env && set +a && \
  python -c "
from tasks.deal_summary import get_existing_summary_note_id, push_deal_summary, build_deal_summary
print('existing for 0:', get_existing_summary_note_id(0))  # None
print('existing for 11425275:', get_existing_summary_note_id(11425275))  # None at first
print('ok')
"
```
Expected: prints `None, None, ok`.

- [ ] **Step 4: Commit**

```bash
git add worker/tasks/deal_summary.py worker/tasks/pipeline.py
git commit -m "feat(deal-summary): persist and push third AmoCRM note per lead"
```

---

## Task 8: Stage-aware — AmoCRM helpers + prior_context + plan prompt

**Files:**
- Modify: `worker/tasks/amocrm_sync.py` (pipelines cache + lead-stage lookup)
- Modify: `worker/tasks/prior_context.py` (wire stage into prior_context)
- Modify: `worker/tasks/quality.py` (plan prompt stage addendum + thread deal_stage)
- Modify: `worker/tasks/pipeline.py` (pass stage_name into plan)

- [ ] **Step 1: Add pipelines cache + `get_lead_stage` to `amocrm_sync.py`**

At the end of `worker/tasks/amocrm_sync.py`, append:

```python
# === Pipelines / stage resolver ==========================================

import time as _time

_PIPELINES_CACHE: dict | None = None
_PIPELINES_CACHE_TS: float = 0.0
_PIPELINES_TTL_SEC = 3600


def _get_pipelines_cached() -> dict:
    """Return {pipeline_id: {"name": ..., "stages": {status_id: {"name": ..., "id": ...}}}}."""
    global _PIPELINES_CACHE, _PIPELINES_CACHE_TS
    now = _time.time()
    if _PIPELINES_CACHE and now - _PIPELINES_CACHE_TS < _PIPELINES_TTL_SEC:
        return _PIPELINES_CACHE
    if not _get_access_token():
        return _PIPELINES_CACHE or {}
    try:
        resp = requests.get(
            f"{AMOCRM_BASE_URL}/api/v4/leads/pipelines",
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"Failed to fetch pipelines: {resp.status_code}")
            return _PIPELINES_CACHE or {}
        pipelines: dict = {}
        for p in resp.json().get("_embedded", {}).get("pipelines", []):
            stages = {}
            for s in p.get("_embedded", {}).get("statuses", []):
                stages[s["id"]] = {"name": s.get("name", ""), "id": s["id"]}
            pipelines[p["id"]] = {"name": p.get("name", ""), "stages": stages}
        _PIPELINES_CACHE = pipelines
        _PIPELINES_CACHE_TS = now
        return pipelines
    except Exception:
        logger.exception("Failed to fetch AmoCRM pipelines")
        return _PIPELINES_CACHE or {}


def get_lead_stage(lead_id: int) -> dict | None:
    """Return {"pipeline_name", "stage_name", "stage_id"} for a lead, or None on failure."""
    if not _get_access_token() or not lead_id:
        return None
    try:
        resp = requests.get(
            f"{AMOCRM_BASE_URL}/api/v4/leads/{lead_id}",
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        pipeline_id = data.get("pipeline_id")
        status_id = data.get("status_id")
        if pipeline_id is None or status_id is None:
            return None
        pipelines = _get_pipelines_cached()
        pipeline = pipelines.get(pipeline_id, {})
        stage = (pipeline.get("stages") or {}).get(status_id, {})
        return {
            "pipeline_name": pipeline.get("name", ""),
            "stage_name": stage.get("name", ""),
            "stage_id": status_id,
        }
    except Exception:
        logger.exception(f"Failed to get stage for lead {lead_id}")
        return None
```

- [ ] **Step 2: Thread `current_deal_stage` into `prior_context`**

In `worker/tasks/prior_context.py`, extend `build_prior_context_for_session` to accept an optional `deal_stage` dict and surface it:

```python
def build_prior_context_for_session(lead_id: int, current_created_at: datetime | str, deal_stage: dict | None = None) -> dict | None:
    if not lead_id:
        return None
    past = load_past_sessions_for_lead(lead_id, current_created_at)
    iso_now = current_created_at.isoformat() if hasattr(current_created_at, "isoformat") else str(current_created_at)
    ctx = build_prior_context_dict(past, iso_now)
    if deal_stage:
        ctx["current_deal_stage"] = deal_stage
    return ctx
```

- [ ] **Step 3: Fetch stage + thread through pipeline**

In `worker/tasks/pipeline.py`, near the existing `lead_id = session_meta.get("lead_id")` block (inside `_run_pipeline`), extend:

```python
    lead_id = session_meta.get("lead_id")
    phone = session_meta.get("phone", "")
    if not lead_id and phone:
        lead_id = find_lead_by_phone(phone)
        if lead_id:
            _update_session_metadata(session_id, {"lead_id": lead_id})
            session_meta["lead_id"] = lead_id

    current_created_at = _get_session_created_at(session_id)
    if current_created_at is not None:
        session_meta["_current_created_at_iso"] = (
            current_created_at.isoformat() if hasattr(current_created_at, "isoformat") else str(current_created_at)
        )

    # Fetch deal stage for stage-aware recommendations (Phase 2)
    from tasks.amocrm_sync import get_lead_stage
    deal_stage = get_lead_stage(lead_id) if lead_id else None

    prior_context = (
        build_prior_context_for_session(lead_id, current_created_at, deal_stage=deal_stage)
        if lead_id else None
    )
```

In the plan-call block (step 7.5), pass `stage_name` into `plan_next_call`:

```python
            stage_name = (deal_stage or {}).get("stage_name")
            next_call_plan = plan_next_call(prior_context, quality_report, deal_stage=stage_name)
```

- [ ] **Step 4: Extend `PLAN_SYSTEM_PROMPT` with stage guide**

In `worker/tasks/quality.py`, locate `PLAN_SYSTEM_PROMPT` (added in Phase 1, Task 8). Append before the closing triple-quote:

```python
## Стадия сделки

Если в user-payload передан `deal_stage`, рекомендации должны быть уместны
для этого этапа:
- «Квалификация»: фокус на discovery-вопросах; глубже узнавать потребности;
  не форсировать встречу.
- «Показы»: аргументы по конкретным объектам; отработка «посмотрю ещё»;
  договорённость о следующем показе.
- «Сделка» / «Договор»: closing-сценарии; детали оформления; снятие
  финальных возражений.
- Для нераспознанных стадий — опирайся на здравый смысл.
```

- [ ] **Step 5: Smoke-check**

Run:
```bash
cd worker && source ../.venv/bin/activate && \
  set -a && source ../.env && set +a && \
  python -c "
from tasks.amocrm_sync import _get_pipelines_cached, get_lead_stage
pipelines = _get_pipelines_cached()
print(f'pipelines count: {len(pipelines)}')
if pipelines:
    first_id = next(iter(pipelines))
    print(f'first pipeline: {pipelines[first_id][\"name\"]}, stages: {len(pipelines[first_id][\"stages\"])}')
stage = get_lead_stage(11425275)
print(f'lead 11425275 stage: {stage}')
"
```
Expected: prints a non-zero pipeline count and a stage dict for lead 11425275.

- [ ] **Step 6: Commit**

```bash
git add worker/tasks/amocrm_sync.py worker/tasks/prior_context.py worker/tasks/pipeline.py worker/tasks/quality.py
git commit -m "feat(stage): plumb AmoCRM deal stage into prior_context and plan prompt"
```

---

## Task 9: Offline-gap awareness — AmoCRM events + prior_context

**Files:**
- Modify: `worker/tasks/amocrm_sync.py` (fetch_lead_events helper)
- Modify: `worker/tasks/prior_context.py` (inject non-call interactions + inferred gaps)
- Modify: `worker/tasks/quality.py` (add offline-gap paragraph to `CONTEXT_AWARE_INSTRUCTIONS` and `PLAN_SYSTEM_PROMPT`)

- [ ] **Step 1: Add `fetch_lead_events` to `amocrm_sync.py`**

At the end of `worker/tasks/amocrm_sync.py`, append:

```python
def fetch_lead_events(lead_id: int, since_ts: int = 0) -> list[dict]:
    """Fetch AmoCRM events (status changes, notes, calls) for a lead since given timestamp."""
    if not _get_access_token() or not lead_id:
        return []
    try:
        resp = requests.get(
            f"{AMOCRM_BASE_URL}/api/v4/events",
            params={
                "filter[entity]": "lead",
                "filter[entity_id][]": [lead_id],
                "filter[created_at][from]": since_ts,
                "limit": 100,
            },
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("_embedded", {}).get("events", [])
    except Exception:
        logger.exception(f"Failed to fetch events for lead {lead_id}")
        return []
```

- [ ] **Step 2: Inject non-call interactions + infer gaps in prior_context**

In `worker/tasks/prior_context.py`, add a new pure helper after `summarise_interaction`:

```python
def extend_history_with_events(
    interactions: list[dict],
    events: list[dict],
    before_iso: str,
) -> list[dict]:
    """
    Merge AmoCRM events (status changes, external notes) into the interactions list
    and mark status-change-without-call as offline_gap_inferred.
    Returns a new sorted-by-date list.
    """
    call_dates = {h.get("date") for h in interactions if h.get("type", "").startswith("call_")}
    before_date = before_iso.split("T")[0] if "T" in before_iso else before_iso[:10]

    extra: list[dict] = []
    for ev in events:
        ev_type = ev.get("type", "")
        created = ev.get("created_at", 0)
        if not isinstance(created, (int, float)):
            continue
        # Unix ts → YYYY-MM-DD (UTC)
        import datetime as _dt
        d = _dt.datetime.fromtimestamp(int(created), tz=_dt.timezone.utc).date().isoformat()
        if d >= before_date:
            continue  # skip events after the current call

        if ev_type == "lead_status_changed":
            note = {
                "date": d,
                "type": "offline_gap_inferred" if d not in call_dates else "stage_change",
                "has_data": d in call_dates,
                "detail": "Этап сменился",
            }
            extra.append(note)

    merged = sorted(interactions + extra, key=lambda x: x.get("date", ""))
    return merged
```

In `load_past_sessions_for_lead` (or in `build_prior_context_for_session`), call `fetch_lead_events` and pass events through to `extend_history_with_events` once `interactions_history` is assembled. The simplest wiring is inside `build_prior_context_for_session`:

```python
def build_prior_context_for_session(
    lead_id: int,
    current_created_at,
    deal_stage: dict | None = None,
    events: list[dict] | None = None,
) -> dict | None:
    if not lead_id:
        return None
    past = load_past_sessions_for_lead(lead_id, current_created_at)
    iso_now = current_created_at.isoformat() if hasattr(current_created_at, "isoformat") else str(current_created_at)
    ctx = build_prior_context_dict(past, iso_now)
    if events:
        ctx["interactions_history"] = extend_history_with_events(
            ctx["interactions_history"], events, iso_now
        )
    if deal_stage:
        ctx["current_deal_stage"] = deal_stage
    return ctx
```

Then in `worker/tasks/pipeline.py`, fetch events and pass them:

```python
    from tasks.amocrm_sync import get_lead_stage, fetch_lead_events
    events = fetch_lead_events(lead_id) if lead_id else []
    deal_stage = get_lead_stage(lead_id) if lead_id else None
    prior_context = (
        build_prior_context_for_session(lead_id, current_created_at, deal_stage=deal_stage, events=events)
        if lead_id else None
    )
```

- [ ] **Step 3: Write a test for `extend_history_with_events`**

In `worker/tests/test_prior_context.py`, append:

```python
def test_extend_history_with_events_marks_offline_gap():
    from tasks.prior_context import extend_history_with_events
    import datetime as _dt
    call_ts = _dt.datetime(2026, 4, 3, tzinfo=_dt.timezone.utc)
    gap_ts = _dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc)
    interactions = [{"date": "2026-04-03", "type": "call_out"}]
    events = [
        {"type": "lead_status_changed", "created_at": int(gap_ts.timestamp())},
        {"type": "lead_status_changed", "created_at": int(call_ts.timestamp())},  # same day as call → not a gap
    ]
    merged = extend_history_with_events(interactions, events, before_iso="2026-04-17T00:00:00Z")
    gap_entries = [e for e in merged if e.get("type") == "offline_gap_inferred"]
    assert len(gap_entries) == 1
    assert gap_entries[0]["date"] == "2026-04-10"
    assert gap_entries[0]["has_data"] is False
```

Run: `cd worker && source ../.venv/bin/activate && pytest tests/test_prior_context.py -v`
Expected: test passes.

- [ ] **Step 4: Extend prompts with offline-gap guidance**

In `worker/tasks/quality.py`, inside `CONTEXT_AWARE_INSTRUCTIONS`, append before the closing triple-quote:

```python
## Офлайн-пробелы (gaps)

В interactions_history могут быть элементы с type="offline_gap_inferred"
или has_data=false — это периоды, когда что-то произошло в офлайне
(встреча, мессенджер) и у нас нет записей. Не делай выводов из их
отсутствия. Если клиент в текущем звонке ссылается на обсуждение,
которое не видно в истории — предположи, что оно было в офлайне.
```

And extend `PLAN_SYSTEM_PROMPT` with the same paragraph.

- [ ] **Step 5: Smoke-test**

Run:
```bash
cd worker && source ../.venv/bin/activate && \
  set -a && source ../.env && set +a && \
  python -c "
from tasks.amocrm_sync import fetch_lead_events
evs = fetch_lead_events(11425275)
print(f'total events: {len(evs)}')
status_changes = [e for e in evs if e.get('type') == 'lead_status_changed']
print(f'status changes: {len(status_changes)}')
"
```
Expected: prints total events and status-change count.

- [ ] **Step 6: Commit**

```bash
git add worker/tasks/amocrm_sync.py worker/tasks/prior_context.py worker/tasks/quality.py worker/tasks/pipeline.py worker/tests/test_prior_context.py
git commit -m "feat(offline-gap): surface non-call AmoCRM events and inferred gaps"
```

---

## Task 10: End-to-end smoke test

No code changes — verification only.

- [ ] **Step 1: Restart worker and backend to pick up all Phase 2 code**

Note: worker caches AmoCRM tokens in module globals (memory gotcha). Restart is mandatory.

Run:
```bash
systemctl restart realestate-worker.service realestate-backend.service
sleep 5
systemctl is-active realestate-worker.service realestate-backend.service
```
Expected: both `active`.

Check beat poll succeeds (no 401):
```bash
sleep 60  # wait for first post-restart poll
journalctl -u realestate-worker.service --since "2 minutes ago" --no-pager | grep -E "Poll complete|token refresh" | tail -5
```
Expected: successful poll, token refreshed if needed.

- [ ] **Step 2: Reprocess lead 11425275 with force**

```bash
source .venv/bin/activate && curl -s -X POST http://127.0.0.1:8002/api/amocrm/reprocess \
  -H 'Content-Type: application/json' \
  -d '{"lead_id": 11425275, "force": true}' | python -m json.tool
```
Expected: `queued` contains 2 calls (5s short, 1050s long).

- [ ] **Step 3: Watch pipeline**

Use Monitor tool (or `tail -f` in a separate shell) to watch:
```bash
journalctl -u realestate-worker.service -f --no-pager --since "now" | \
  grep --line-buffered -E "Prior context|Step 7.5|Created plain note|Created deal summary|Updated deal summary|Created AmoCRM|ERROR|Failed|Traceback"
```
Expected sequence for the 1050s call:
- `Prior context: call #N, ≥1 prior, ...`
- `Step 7.5: Next-call plan...`
- `Created AmoCRM note ...` (eval)
- `Created plain note ...` (plan)
- `Created deal summary note ...` OR `Updated deal summary note ...`

- [ ] **Step 4: Verify quality.json has V4 + follow-through**

```bash
SID=$(ls -t /data/realestate/results/ | head -1)
python3 -c "
import json
with open(f'/data/realestate/results/$SID/quality.json') as f:
    d = json.load(f)
print('score_version:', d.get('score_version'))
print('has stage_progression:', bool(d.get('stage_progression')))
ft = d.get('previous_recommendations_follow_through') or {}
print(f'follow_through: {ft.get(\"executed_count\")}/{ft.get(\"total_recommendations\")}')
print('items:', len(ft.get('items', [])))
"
```
Expected: `score_version: 4`, `stage_progression: True`, follow-through with nonzero total.

- [ ] **Step 5: Verify AmoCRM has 3 notes for the lead**

```bash
cd worker && source ../.venv/bin/activate && \
  set -a && source ../.env && set +a && \
  python -c "
import requests
from tasks.amocrm_sync import _headers, AMOCRM_BASE_URL
resp = requests.get(f'{AMOCRM_BASE_URL}/api/v4/leads/11425275/notes', params={'limit': 20, 'order[created_at]': 'desc'}, headers=_headers(), timeout=10)
notes = resp.json().get('_embedded', {}).get('notes', []) if resp.status_code == 200 else []
for n in notes[:10]:
    text = (n.get('params') or {}).get('text', '')
    first = text.split(chr(10))[0] if text else '(no text)'
    print(n['id'], n.get('note_type'), '->', first[:60])
"
```
Expected: at least one note starting with «🏠 Сводка по сделке», one with «📋 План следующего звонка», and enriched eval note.

- [ ] **Step 6: Verify DB row exists for the summary**

```bash
PGPASSWORD='nVyrZu-rJSaPgYs6o3AcYM97yQb_ruZh' psql -h localhost -U realestate -d realestate \
  -c "SELECT lead_id, amo_note_id, updated_at FROM amocrm_deal_summaries WHERE lead_id=11425275;"
```
Expected: one row with the note_id.

- [ ] **Step 7: Second reprocess (verify update-in-place)**

Rerun the reprocess with `force: true` as in Step 2. Verify:
- Logs show `Updated deal summary note ...` (not "Created").
- `amo_note_id` in DB stays the same; only `updated_at` advances.

No commit for this task — it's verification only.

---

## Task 11: Update documentation

**Files:**
- Modify: `docs/superpowers/specs/2026-04-17-short-call-filter-and-reprocess-design.md` (mark Phase 2 complete).

- [ ] **Step 1: Append Phase 2 status**

Find the `## Implementation status` section (added at the end of the spec during Phase 1). Change `**Фаза 2 — pending.**` to:

```markdown
**Фаза 2 — ✅ Завершена 2026-04-17.** Реализация по плану
`docs/superpowers/plans/2026-04-17-multi-call-phase2.md`.

Итоговые изменения: Redis-lock по lead_id, V4
`previous_recommendations_follow_through`, deal-summary note
(update-in-place через таблицу `amocrm_deal_summaries`), stage-aware
рекомендации через `GET /api/v4/leads/pipelines` с кэшем, offline-gap
inferred из `lead_status_changed` событий.

End-to-end смоук: на сделке 11425275 после второго прогона в AmoCRM
присутствуют три заметки (оценка + план + сводка), сводка обновляется
in-place, follow-through показывает выполнение рекомендаций из первого
прогона.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-04-17-short-call-filter-and-reprocess-design.md
git commit -m "docs: mark phase 2 complete"
```

---

## Self-review notes (for the executing agent)

- **Schema stability:** V4 now has three extra top-level fields vs. V3 (`stage_progression`, `applicable_checklist_items`, `previous_recommendations_follow_through`). First-call routing still falls back to V3 — this keeps Phase 2 backward-compatible with existing saved reports.
- **Lock scope:** the Redis-lock wraps the entire post-short-call pipeline. Short-call early-exit runs *before* the lock by design — a 5s call doesn't need to block a concurrent productive call on the same lead.
- **Deal-summary race:** the Redis-lock prevents concurrent writes to the same summary note. If two calls land on the same lead simultaneously, the second waits.
- **Stage cache:** 1-hour TTL is a trade-off — stages rarely change at the pipeline level, but if the ops team adds/renames a stage, the worker won't see it until restart or TTL expiry. Acceptable for v1; can be shortened later.
- **Offline-gap conservatism:** we infer a gap only when `lead_status_changed` happens on a day *without* our own call. Other signals (common_note_added, custom field changes) are ignored in v1 to avoid false positives.
- **Backward compatibility for prior_context consumers:** `last_call_plan`, `current_deal_stage`, and the new `offline_gap_inferred` entries are additive. Existing Phase 1 code paths that consume prior_context (Phase 1 only read a few fields) keep working unchanged.
- **Token gotcha:** Task 10 step 1 mandates the worker restart. Phase 2 adds DB reads (new table), AmoCRM helpers (pipelines, events), and schema changes — all are module-level imports, so stale processes must be restarted. This matches `project_amocrm_token_refresh_gotcha.md`.
