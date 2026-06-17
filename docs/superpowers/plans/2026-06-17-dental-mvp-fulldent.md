# Dental MVP (fulldent tenant) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the SilentQA multi-tenant platform, let the **fulldent** tenant turn a recorded dental appointment into a transcript + a structured **«Карта приёма»** (patient / anamnesis / dental chart / diagnosis / treatment plan) + a dental communication-QA score — all via **generic, config-driven** engine features so other tenants (realestate) are provably unaffected.

**Architecture:** Generic features parameterized per tenant through `companies/<id>.json`. New generic `card_extraction` config block → a new `worker/tasks/card.py` step (clinical card only; QA stays in `quality.py`). New ElevenLabs ASR adapter selected per-tenant via `asr.engine`. Dental specifics (schema with tooth chart, criteria) live only in `companies/dental.json`. Every engine change is gated by config presence + covered by a "realestate-unaffected" regression test.

**Tech Stack:** Python 3.12, FastAPI, Celery, OpenAI `gpt-5.4` (structured output), ElevenLabs Scribe v2 STT (REST via `httpx`), AssemblyAI (fallback), vanilla-JS SPA. Tests: pytest (no Postgres/Redis — `FakeRedis` + monkeypatched registry).

**Spec:** `docs/superpowers/specs/2026-06-17-dental-mvp-fulldent-design.md`. **Branch:** `dental-mvp-fulldent`.

**Pre-flight (read once):**
- Interpreter: `python` is not on PATH — use **`/root/projects/silentqa/.venv/bin/python`** (has openai/assemblyai/httpx/faster_whisper).
- Worker tests run from `worker/`; backend tests from `backend/`. Example:
  `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_X.py -v`
- Known drift (CLAUDE.md): `openai` is missing from `worker/requirements.txt` though `quality.py`/`extract.py` use it; `httpx==0.28.1` and `assemblyai==0.58.0` ARE declared. Task 9 adds `openai`.

---

### Task 1: ElevenLabs ASR adapter + 3-engine dispatch

**Files:**
- Modify: `worker/tasks/transcribe.py` (top imports; add `_transcribe_elevenlabs`; restructure `transcribe_audio` dispatch)
- Test: `worker/tests/test_transcribe_elevenlabs.py` (create)

Context: today `transcribe_audio` has a hardcoded 2-engine closure (`_primary`/`_fallback` branch only on `assemblyai`). AssemblyAI segments look like `{"speaker","start","end","text","words"}`. The ElevenLabs adapter must emit the same `speaker`-bearing shape so the pipeline skips diarization. ElevenLabs STT returns word-level `words[]` with `speaker_id` (+ `type` of `word`/`spacing`); merge consecutive same-speaker words into turns.

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_transcribe_elevenlabs.py`:
```python
import tasks.transcribe as tr


class _FakeResp:
    status_code = 200
    text = ""
    def __init__(self, payload): self._p = payload
    def json(self): return self._p


class _FakeClient:
    def __init__(self, payload): self._p = payload
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, *a, **k): return _FakeResp(self._p)


def test_elevenlabs_merges_words_into_speaker_turns(monkeypatch, tmp_path):
    payload = {"text": "x", "audio_duration_secs": 1.2, "words": [
        {"type": "word", "text": "Здравствуйте", "start": 0.0, "end": 0.5, "speaker_id": "speaker_0"},
        {"type": "spacing", "text": " "},
        {"type": "word", "text": "доктор", "start": 0.5, "end": 0.9, "speaker_id": "speaker_0"},
        {"type": "word", "text": "Да", "start": 1.0, "end": 1.2, "speaker_id": "speaker_1"},
    ]}
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(tr.httpx, "Client", lambda *a, **k: _FakeClient(payload))
    audio = tmp_path / "a.wav"; audio.write_bytes(b"RIFF")
    segs = tr._transcribe_elevenlabs(str(audio))
    assert segs == [
        {"speaker": "speaker_0", "start": 0.0, "end": 0.9, "text": "Здравствуйте доктор"},
        {"speaker": "speaker_1", "start": 1.0, "end": 1.2, "text": "Да"},
    ]


def test_elevenlabs_requires_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    audio = tmp_path / "a.wav"; audio.write_bytes(b"x")
    import pytest
    with pytest.raises(RuntimeError):
        tr._transcribe_elevenlabs(str(audio))


def test_dispatch_falls_back_to_next_engine(monkeypatch):
    calls = []
    monkeypatch.setattr(tr, "_transcribe_elevenlabs", lambda p, **k: (_ for _ in ()).throw(RuntimeError("no key")))
    monkeypatch.setattr(tr, "_transcribe_assemblyai", lambda p, **k: calls.append("aai") or [{"speaker": "A", "start": 0, "end": 1, "text": "ok"}])
    out = tr.transcribe_audio("/x.wav", engine_override="elevenlabs")
    assert calls == ["aai"] and out[0]["text"] == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_transcribe_elevenlabs.py -v`
Expected: FAIL — `AttributeError: module 'tasks.transcribe' has no attribute 'httpx'` / `_transcribe_elevenlabs`.

- [ ] **Step 3: Implement — move `httpx` import to module top + add adapter**

In `worker/tasks/transcribe.py`, change the top imports block (currently `import logging` / `import os`) to also import httpx at module level (so tests can monkeypatch `tr.httpx`):
```python
import logging
import os

import httpx

logger = logging.getLogger(__name__)
```

Add this function above `# ── Public API` :
```python
# ── ElevenLabs Scribe v2 (cloud, diarization built-in) ──────────

def _transcribe_elevenlabs(audio_path: str) -> list[dict]:
    """ElevenLabs Scribe v2: ru + speaker diarization. Returns assemblyai-shaped
    segments {speaker,start,end,text} (so the pipeline skips diarization)."""
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY not set")
    model = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")
    language = os.getenv("ELEVENLABS_LANGUAGE", "ru")
    logger.info(f"Sending {audio_path} to ElevenLabs ({model}, {language})...")
    with open(audio_path, "rb") as fh:
        files = {
            "file": (os.path.basename(audio_path), fh, "application/octet-stream"),
            "model_id": (None, model),
            "language_code": (None, language),
            "diarize": (None, "true"),
        }
        with httpx.Client(timeout=900) as client:
            resp = client.post("https://api.elevenlabs.io/v1/speech-to-text",
                               headers={"xi-api-key": api_key}, files=files)
    if resp.status_code != 200:
        raise RuntimeError(f"ElevenLabs STT failed: HTTP {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    segments: list[dict] = []
    for w in (data.get("words") or []):
        text = w.get("text", "")
        if w.get("type", "word") != "word":          # spacing / audio_event -> append
            if segments:
                segments[-1]["text"] += text
            continue
        spk = w.get("speaker_id") or "speaker_0"
        start, end = round(w.get("start", 0.0), 2), round(w.get("end", 0.0), 2)
        if segments and segments[-1]["speaker"] == spk:
            segments[-1]["text"] += text
            segments[-1]["end"] = end
        else:
            segments.append({"speaker": spk, "start": start, "end": end, "text": text})
    for s in segments:
        s["text"] = " ".join(s["text"].split())
    segments = [s for s in segments if s["text"]]
    if not segments and data.get("text"):
        segments = [{"speaker": "speaker_0", "start": 0.0,
                     "end": round(data.get("audio_duration_secs", 0.0) or 0.0, 2),
                     "text": data["text"].strip()}]
    logger.info(f"ElevenLabs: {len(segments)} segments")
    return segments
```

Replace the body of `transcribe_audio` (the `engine = ...` line onward, i.e. the nested `_primary`/`_fallback`/try-except) with an ordered fallback chain:
```python
    engine = engine_override or os.getenv("ASR_ENGINE", "whisper")

    def _run(name: str) -> list[dict]:
        if name == "elevenlabs":
            return _transcribe_elevenlabs(audio_path)
        if name == "assemblyai":
            return _transcribe_assemblyai(audio_path, word_boost=word_boost)
        return _transcribe_whisper(audio_path)

    default_chain = ["elevenlabs", "assemblyai", "whisper"]
    chain = [engine] + [e for e in default_chain if e != engine]
    last_err: Exception | None = None
    for i, name in enumerate(chain):
        try:
            return _run(name)
        except Exception as e:
            last_err = e
            nxt = chain[i + 1] if i + 1 < len(chain) else None
            logger.warning(f"ASR engine '{name}' failed: {e}." +
                           (f" Trying '{nxt}'..." if nxt else " No more engines."))
    raise RuntimeError(f"All ASR engines failed. Last error: {last_err}") from last_err
```
(Keep the `transcribe_audio` signature and docstring as-is.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_transcribe_elevenlabs.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**
```bash
cd /root/projects/silentqa-dev
git add worker/tasks/transcribe.py worker/tests/test_transcribe_elevenlabs.py
git commit -m "feat(asr): ElevenLabs Scribe v2 adapter + ordered engine-fallback chain"
```

---

### Task 2: company_config accessors (`card_extraction`, `default_scenario_id`)

**Files:**
- Modify: `worker/tasks/company_config.py` (add two accessors at end)
- Test: `worker/tests/test_card_config.py` (create)

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_card_config.py`:
```python
import tasks.company_config as cc


def test_get_card_extraction_present():
    cfg = {"card_extraction": {"label": "Карта приёма", "prompt": "p", "json_schema": {"type": "object"}}}
    assert cc.get_card_extraction(cfg)["label"] == "Карта приёма"


def test_get_card_extraction_absent():
    assert cc.get_card_extraction({"id": "realestate"}) is None


def test_default_scenario_id():
    assert cc.get_default_scenario_id({"default_scenario_id": "consultation"}) == "consultation"
    assert cc.get_default_scenario_id({}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_card_config.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'get_card_extraction'`.

- [ ] **Step 3: Implement — append to `worker/tasks/company_config.py`**
```python
def get_card_extraction(company_config: dict) -> dict | None:
    """Generic structured-card config block ({label, prompt, json_schema}) or None."""
    return company_config.get("card_extraction")


def get_default_scenario_id(company_config: dict) -> str | None:
    """Tenant's default evaluation scenario id (used when the session has none)."""
    return company_config.get("default_scenario_id")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_card_config.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**
```bash
cd /root/projects/silentqa-dev
git add worker/tasks/company_config.py worker/tests/test_card_config.py
git commit -m "feat(config): get_card_extraction + get_default_scenario_id accessors"
```

---

### Task 3: `worker/tasks/card.py` — generic clinical-card extraction

**Files:**
- Create: `worker/tasks/card.py`
- Test: `worker/tests/test_card.py` (create)

Note: clinical card ONLY (no QA — QA stays in `quality.py`). No DB, no complex matching. Takes the in-memory transcript (in scope at the pipeline insertion point), so it needs no disk read → trivially testable.

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_card.py`:
```python
from unittest import mock

import tasks.card as card


CFG = {"card_extraction": {"label": "Карта приёма", "prompt": "извлеки карту",
                           "json_schema": {"type": "object"}}}
TRANSCRIPT = [{"speaker": "B", "text": "Жалобы на сухость во рту"},
              {"speaker": "A", "text": "Да, давно"}]


def test_no_op_without_config():
    assert card.run_card_extraction(TRANSCRIPT, {"id": "realestate"}) is None


def test_extracts_with_config(monkeypatch):
    fake_resp = mock.MagicMock()
    fake_resp.output_text = '{"summary": "сбор анамнеза"}'
    fake_client = mock.MagicMock()
    fake_client.responses.create.return_value = fake_resp
    monkeypatch.setattr(card, "OpenAI", lambda: fake_client)
    out = card.run_card_extraction(TRANSCRIPT, CFG)
    assert out == {"summary": "сбор анамнеза"}
    # schema + prompt from config were used
    _, kwargs = fake_client.responses.create.call_args
    assert kwargs["text"]["format"]["schema"] == {"type": "object"}
    assert kwargs["input"][0]["content"] == "извлеки карту"


def test_empty_transcript_returns_none():
    assert card.run_card_extraction([{"speaker": "A", "text": "  "}], CFG) is None


def test_llm_error_degrades_to_none(monkeypatch):
    fake_client = mock.MagicMock()
    fake_client.responses.create.side_effect = RuntimeError("boom")
    monkeypatch.setattr(card, "OpenAI", lambda: fake_client)
    assert card.run_card_extraction(TRANSCRIPT, CFG) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_card.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tasks.card'`.

- [ ] **Step 3: Implement — create `worker/tasks/card.py`**
```python
"""Generic structured-card extraction (config-driven).

If the tenant's company config has a `card_extraction` block ({label, prompt,
json_schema}), run one gpt-5.4 strict structured-output call over the transcript
and return the card dict. Domain-agnostic: the schema/prompt come from config,
NOT from a DB template, and there is NO complex matching. Degrades to None on any
error so it never fails the pipeline. QA scoring is handled separately in quality.py."""
from __future__ import annotations

import logging

from openai import OpenAI

logger = logging.getLogger(__name__)


def _flatten_transcript(transcript: list[dict]) -> str:
    parts = []
    for seg in transcript or []:
        speaker = seg.get("speaker") or "UNKNOWN"
        text = (seg.get("text") or seg.get("content") or "").strip()
        if text:
            parts.append(f"[{speaker}] {text}")
    return "\n".join(parts)


def run_card_extraction(transcript: list[dict], company_config: dict) -> dict | None:
    cfg = company_config.get("card_extraction")
    if not cfg:
        return None
    flat = _flatten_transcript(transcript)
    if not flat.strip():
        return None
    try:
        client = OpenAI()
        resp = client.responses.create(
            model="gpt-5.4",
            input=[{"role": "system", "content": cfg["prompt"]},
                   {"role": "user", "content": "Транскрипт приёма:\n\n" + flat}],
            text={"format": {"type": "json_schema", "name": "card",
                             "strict": True, "schema": cfg["json_schema"]}},
        )
        import json
        return json.loads(resp.output_text)
    except Exception:
        logger.exception("card extraction failed; skipping card")
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_card.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**
```bash
cd /root/projects/silentqa-dev
git add worker/tasks/card.py worker/tests/test_card.py
git commit -m "feat(card): generic config-driven clinical-card extraction step"
```

---

### Task 4: QA — allow N/A criteria (V2 schema only; V4/realestate untouched)

**Files:**
- Modify: `worker/tasks/quality.py` (V2 `QUALITY_JSON_SCHEMA` score type + V2 `SYSTEM_PROMPT` N/A instruction)
- Test: `worker/tests/test_quality_na.py` (create)

Why V2 only: dental scenarios carry no `prompt` → `use_extended=False` → V2 path. realestate scenarios carry a `prompt` → V4 (inherits V3). Editing only V2 leaves realestate's scoring unchanged (regression guard below).

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_quality_na.py`:
```python
from tasks.quality import QUALITY_JSON_SCHEMA, QUALITY_JSON_SCHEMA_V3


def _score_type(schema):
    return schema["schema"]["properties"]["criteria"]["items"]["properties"]["score"]["type"]


def test_v2_score_allows_null():
    assert _score_type(QUALITY_JSON_SCHEMA) == ["integer", "null"]


def test_v4_path_score_stays_integer_only():
    # realestate uses V4 (inherits V3 criteria) — must NOT gain null (regression guard)
    assert _score_type(QUALITY_JSON_SCHEMA_V3) == "integer"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_quality_na.py -v`
Expected: FAIL on `test_v2_score_allows_null` (current type is `"integer"`).

- [ ] **Step 3: Implement**

In `worker/tasks/quality.py`, inside `QUALITY_JSON_SCHEMA` (the V2 constant, ~line 53), change the criteria item `score`:
```python
                        "score": {"type": ["integer", "null"], "description": "0-10, либо null если критерий неприменим (N/A)"},
```
Then in the V2 `SYSTEM_PROMPT` (~line 706), append to its instructions (inside the prompt string, before the closing triple-quote) a sentence:
```
Если критерий неприменим к этому разговору — поставь "score": null и поясни в comment; такие критерии НЕ учитывай в overall_score (усредняй только применимые).
```
Leave `QUALITY_JSON_SCHEMA_V3`, `QUALITY_JSON_SCHEMA_V4`, and all V3/V4 prompts unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_quality_na.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**
```bash
cd /root/projects/silentqa-dev
git add worker/tasks/quality.py worker/tests/test_quality_na.py
git commit -m "feat(qa): allow N/A criteria in V2 schema/prompt (V4/realestate untouched)"
```

---

### Task 5: Pipeline — default scenario + config-gated card step

**Files:**
- Modify: `worker/tasks/pipeline.py` (scenario default in both body fns ~805-808 and ~870-873; card call after `save_results(... "quality" ...)` ~line 749)
- Modify: `worker/tasks/company_config.py` import line in pipeline (already imports from company_config — add `get_default_scenario_id`)
- Test: `worker/tests/test_pipeline_card.py` (create)

- [ ] **Step 1: Write the failing test** (tests the gating logic in isolation via `card.run_card_extraction`, mirroring `test_kb_pipeline_wiring.py`)

Create `worker/tests/test_pipeline_card.py`:
```python
from unittest import mock
import tasks.card as card

DENTAL = {"card_extraction": {"label": "Карта приёма", "prompt": "p", "json_schema": {"type": "object"}}}
REALESTATE = {"id": "realestate"}


def test_card_runs_for_dental_config(monkeypatch):
    fake_resp = mock.MagicMock(); fake_resp.output_text = '{"summary": "ok"}'
    fc = mock.MagicMock(); fc.responses.create.return_value = fake_resp
    monkeypatch.setattr(card, "OpenAI", lambda: fc)
    assert card.run_card_extraction([{"speaker": "A", "text": "жалоба"}], DENTAL) == {"summary": "ok"}


def test_card_skipped_for_realestate(monkeypatch):
    # realestate config has no card_extraction -> no OpenAI call, returns None
    monkeypatch.setattr(card, "OpenAI", lambda: (_ for _ in ()).throw(AssertionError("must not call LLM")))
    assert card.run_card_extraction([{"speaker": "A", "text": "x"}], REALESTATE) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_pipeline_card.py -v`
Expected: PASS already for these two (they exercise `card.py`). This task's real change is wiring; the regression guard is `test_card_skipped_for_realestate`. (If it fails, Task 3 is incomplete.)

- [ ] **Step 3: Implement the wiring in `worker/tasks/pipeline.py`**

(a) Extend the company_config import (~line 33) to include `get_default_scenario_id`:
```python
from tasks.company_config import load_company_config, get_word_boost, get_protocol, get_custom_prompt, get_asr_engine, get_scenario, get_default_scenario_id, tenant_company_config_id
```

(b) In BOTH body functions, change the scenario resolution (currently `scenario = get_scenario(company_config, scenario_id)` at ~808 and ~872) to fall back to the tenant default:
```python
    scenario = get_scenario(company_config, scenario_id or get_default_scenario_id(company_config))
```

(c) Immediately AFTER `save_results(session_id, "quality", quality_report)` (~line 749), insert the config-gated card step:
```python
    # === 6b. Structured card (config-gated, generic; clinical only — QA above) ===
    from tasks.card import run_card_extraction
    card = run_card_extraction(transcript_with_speakers, company_config)
    if card is not None:
        save_results(session_id, "card", card)
        logger.info(f"[{session_id}] Card extraction saved")
```

- [ ] **Step 4: Run tests (new + full worker suite for regressions)**

Run: `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_pipeline_card.py tests/test_card.py tests/test_quality_na.py tests/test_transcribe_elevenlabs.py tests/test_card_config.py -v`
Then the full worker suite (excluding the Postgres-dependent test): `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest --ignore=tests/test_complex_match.py -q`
Expected: all pass (no regressions).

- [ ] **Step 5: Commit**
```bash
cd /root/projects/silentqa-dev
git add worker/tasks/pipeline.py worker/tests/test_pipeline_card.py
git commit -m "feat(pipeline): config-gated card step after quality + per-tenant default scenario"
```

---

### Task 6: `companies/dental.json` — dental tenant config

**Files:**
- Create: `companies/dental.json`
- Test: `worker/tests/test_dental_config.py` (create)

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_dental_config.py`:
```python
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dental_config_valid_and_wired():
    cfg = json.loads((ROOT / "companies" / "dental.json").read_text())
    assert cfg["asr"]["engine"] == "elevenlabs"
    assert cfg["default_scenario_id"] == "consultation"
    ce = cfg["card_extraction"]
    assert ce["label"] == "Карта приёма"
    sch = ce["json_schema"]
    assert sch["type"] == "object" and sch.get("additionalProperties") is False
    # dental tooth chart present
    assert "dental_status" in sch["properties"]
    # consultation scenario has criteria and NO prompt (keeps QA on the lean V2 path)
    cons = next(s for s in cfg["scenarios"] if s["id"] == "consultation")
    assert cons.get("prompt") in (None, "") and len(cons["criteria"]) >= 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_dental_config.py -v`
Expected: FAIL — file not found.

- [ ] **Step 3: Implement — create `companies/dental.json`**

```json
{
  "id": "dental",
  "name": "FullDent",
  "language": "ru",
  "default_scenario_id": "consultation",
  "asr": {
    "engine": "elevenlabs",
    "word_boost": [
      "имплант", "имплантация", "коронка", "винир", "протез", "мост", "пульпит",
      "периодонтит", "кариес", "пародонтит", "пародонтоз", "гингивит", "эндодонтия",
      "чистка", "гигиена", "Air Flow", "отбеливание", "реставрация", "удаление",
      "зуб мудрости", "ретинированный", "снимок", "КТ", "ОПТГ", "рентген", "анестезия",
      "брекеты", "элайнеры", "ортодонт", "прикус", "десна", "слизистая", "синус-лифтинг",
      "абатмент", "формирователь", "пломба", "композит", "цирконий", "металлокерамика",
      "E-max", "план лечения", "консультация", "осмотр", "рассрочка", "стоимость",
      "гарантия", "доктор", "стоматолог", "ортопед", "хирург", "терапевт", "слюнокаменная"
    ]
  },
  "quality": {
    "protocol": "Регламент очной консультации стоматолога:\n1. Приветствие, контакт\n2. Сбор анамнеза (жалобы, история, противопоказания)\n3. Осмотр и диагностика\n4. Объяснение диагноза понятным языком\n5. Презентация вариантов лечения с плюсами/минусами\n6. Прозрачность стоимости и вариантов оплаты\n7. Ответы на вопросы, без давления\n8. Фиксация следующего шага",
    "prompt": null
  },
  "scenarios": [
    {
      "id": "consultation",
      "name": "Очная консультация",
      "type": "in_person",
      "description": "Первичная/повторная консультация врача с пациентом в кабинете",
      "protocol": "1. Приветствие и контакт\n2. Сбор анамнеза: жалобы, история, аллергии, хронические заболевания\n3. Осмотр и диагностика\n4. Объяснение диагноза понятным языком\n5. Варианты лечения с плюсами/минусами\n6. Прозрачность стоимости, варианты оплаты\n7. Ответы на вопросы, без давления\n8. Фиксация следующего шага",
      "criteria": [
        {"id": "greeting", "name": "Приветствие и контакт", "description": "Врач представился, создал комфортную атмосферу"},
        {"id": "anamnesis", "name": "Сбор анамнеза", "description": "Подробно расспросил о жалобах, истории, противопоказаниях"},
        {"id": "diagnosis_explanation", "name": "Объяснение диагноза", "description": "Понятно объяснил проблему без избыточной терминологии"},
        {"id": "treatment_options", "name": "Варианты лечения", "description": "Представил варианты с объяснением плюсов/минусов"},
        {"id": "cost_transparency", "name": "Прозрачность стоимости", "description": "Честно озвучил стоимость, предложил варианты оплаты/рассрочки"},
        {"id": "patient_questions", "name": "Работа с вопросами", "description": "Терпеливо ответил на вопросы, не торопил пациента"},
        {"id": "consent_and_next", "name": "Согласие и следующий шаг", "description": "Зафиксировал решение пациента, записал на следующий этап"}
      ]
    },
    {
      "id": "treatment_plan",
      "name": "Презентация плана лечения",
      "type": "in_person",
      "description": "Врач/координатор представляет пациенту план лечения",
      "protocol": "1. Контекст и результаты диагностики\n2. Общая картина: что и зачем\n3. Поэтапный план\n4. Приоритезация (срочно/можно отложить)\n5. Полная стоимость и по этапам\n6. Варианты оплаты\n7. Работа с сомнениями без давления\n8. Фиксация решения",
      "criteria": [
        {"id": "context_setting", "name": "Контекст и мотивация", "description": "Напомнил результаты диагностики, объяснил зачем лечение"},
        {"id": "plan_clarity", "name": "Понятность плана", "description": "Объяснил этапы простым языком"},
        {"id": "prioritization", "name": "Приоритезация", "description": "Объяснил что срочно, что можно отложить"},
        {"id": "cost_transparency", "name": "Прозрачность стоимости", "description": "Озвучил полную стоимость и по этапам"},
        {"id": "payment_options", "name": "Варианты оплаты", "description": "Предложил рассрочку/поэтапную оплату"},
        {"id": "objection_handling", "name": "Работа с сомнениями", "description": "Не давил, дал время, ответил на вопросы"},
        {"id": "closing", "name": "Фиксация решения", "description": "Зафиксировал решение, записал на следующий шаг"}
      ]
    }
  ],
  "card_extraction": {
    "label": "Карта приёма",
    "prompt": "Ты — ассистент стоматолога. На входе транскрипт приёма со спикерами. Извлеки структурную КАРТУ ПРИЁМА строго по схеме: пациент (имя/возраст/пол/телефон, если прозвучали); повод обращения; анамнез (жалобы, история, аллергии, хронические заболевания, препараты); данные осмотра; зубная формула dental_status (номер зуба в той нотации, что звучит — FDI/универсальная — + статус: здоров/кариес/пломба/коронка/имплант/удалён/в лечении и т.п. + заметка); диагноз(ы); план лечения (этапы: процедура, зубы, приоритет, срок, цена; итоговая стоимость; варианты оплаты); рекомендации; итог визита. ВАЖНО: ничего не выдумывай — если данных нет, ставь null или пустой список. Всё на русском.",
    "json_schema": {
      "type": "object",
      "additionalProperties": false,
      "required": ["patient", "chief_complaint", "anamnesis", "examination", "dental_status", "diagnosis", "treatment_plan", "recommendations", "visit_outcome", "summary"],
      "properties": {
        "patient": {"type": "object", "additionalProperties": false, "required": ["name", "age", "gender", "phone"],
          "properties": {"name": {"type": ["string", "null"]}, "age": {"type": ["string", "null"]}, "gender": {"type": ["string", "null"]}, "phone": {"type": ["string", "null"]}}},
        "chief_complaint": {"type": ["string", "null"]},
        "anamnesis": {"type": "object", "additionalProperties": false, "required": ["complaints", "history", "allergies", "chronic_conditions", "medications"],
          "properties": {"complaints": {"type": "array", "items": {"type": "string"}}, "history": {"type": ["string", "null"]}, "allergies": {"type": "array", "items": {"type": "string"}}, "chronic_conditions": {"type": "array", "items": {"type": "string"}}, "medications": {"type": "array", "items": {"type": "string"}}}},
        "examination": {"type": "array", "items": {"type": "string"}},
        "dental_status": {"type": "array", "items": {"type": "object", "additionalProperties": false, "required": ["tooth", "status", "note"],
          "properties": {"tooth": {"type": "string"}, "status": {"type": "string"}, "note": {"type": ["string", "null"]}}}},
        "diagnosis": {"type": "array", "items": {"type": "string"}},
        "treatment_plan": {"type": "object", "additionalProperties": false, "required": ["stages", "total_cost", "payment_options"],
          "properties": {"stages": {"type": "array", "items": {"type": "object", "additionalProperties": false, "required": ["procedure", "teeth", "priority", "timeline", "price"],
            "properties": {"procedure": {"type": "string"}, "teeth": {"type": ["string", "null"]}, "priority": {"type": "string", "enum": ["срочно", "планово", "по желанию", "не указано"]}, "timeline": {"type": ["string", "null"]}, "price": {"type": ["string", "null"]}}}},
            "total_cost": {"type": ["string", "null"]}, "payment_options": {"type": "array", "items": {"type": "string"}}}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "visit_outcome": {"type": "object", "additionalProperties": false, "required": ["result", "next_step"],
          "properties": {"result": {"type": "string", "enum": ["согласие на лечение", "думает", "повторный приём", "направление", "отказ", "не указано"]}, "next_step": {"type": ["string", "null"]}}},
        "summary": {"type": "string"}
      }
    }
  }
}
```

- [ ] **Step 4: Run test + validate JSON**

Run: `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_dental_config.py -v`
Run: `/root/projects/silentqa/.venv/bin/python -c "import json; json.load(open('/root/projects/silentqa-dev/companies/dental.json')); print('valid json')"`
Expected: test passes; `valid json`.

- [ ] **Step 5: Commit**
```bash
cd /root/projects/silentqa-dev
git add companies/dental.json worker/tests/test_dental_config.py
git commit -m "feat(dental): companies/dental.json — ElevenLabs ASR, dental QA scenarios, card_extraction with tooth chart"
```

---

### Task 7: Backend `GET /api/sessions/{id}/card`

**Files:**
- Modify: `backend/app/routes/analysis.py` (add endpoint — file already has `_read_result_file`, `require_session_access`)
- Test: `backend/tests/test_session_card.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_session_card.py`:
```python
import asyncio
from starlette.testclient import TestClient
from app.auth_sessions import SESSION_COOKIE


def _client(monkeypatch, fake_redis):
    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "fulldent", "schema_name": "t_fulldent", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True, "modules": {}}]

    async def fake_all(self):
        return rows
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    return TestClient(app, base_url="https://fulldent.silentqa.com")


def _viewer():
    from app import auth_sessions
    from tenancy.context import reset_tenant_schema, set_tenant_schema

    async def seed():
        t = set_tenant_schema("t_fulldent")
        try:
            return await auth_sessions.create_session("u", "v@t.io", "viewer")
        finally:
            reset_tenant_schema(t)
    return {SESSION_COOKIE: asyncio.run(seed())}


def test_card_requires_session(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    r = c.get("/api/sessions/00000000-0000-0000-0000-000000000001/card")
    assert r.status_code == 401


def test_card_404_when_no_file(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis)
    r = c.get("/api/sessions/00000000-0000-0000-0000-000000000001/card", cookies=_viewer())
    assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/projects/silentqa-dev/backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_session_card.py -v`
Expected: FAIL — route 404 vs expected, or `test_card_requires_session` returns 404 not 401 (route missing).

- [ ] **Step 3: Implement — add to `backend/app/routes/analysis.py`** (next to `get_quality`):
```python
@router.get("/{session_id}/card", dependencies=[Depends(require_session_access)])
async def get_card(session_id: uuid.UUID):
    """Structured «Карта приёма» (card.json). 404 if this tenant produces none."""
    return _read_result_file(session_id, "card.json")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/projects/silentqa-dev/backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_session_card.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**
```bash
cd /root/projects/silentqa-dev
git add backend/app/routes/analysis.py backend/tests/test_session_card.py
git commit -m "feat(api): GET /api/sessions/{id}/card serves card.json"
```

---

### Task 8: Dashboard — render «Карта приёма»

**Files:**
- Modify: `backend/static/app.js` (fetch `card` in `renderCallDetail`; add a render block + a `renderCard(card)` helper)
- Verify: `node --check`

The dental tenant has `complexes` off → `extraction` is null → the `if (!extraction)` branch renders (transcript + QA). Render the card INSIDE that branch, near the top, so card + QA + transcript all show.

- [ ] **Step 1: Add the fetch** in `renderCallDetail` data block (after the `kbTags` fetch, ~line 477):
```javascript
    let card = null;
    try { card = await api(`/api/sessions/${id}/card`); } catch {}
```

- [ ] **Step 2: Add a render helper** near the other render helpers (e.g. after `renderComplexProfile`). Generic-ish but tuned to the dental card shape:
```javascript
function renderCard(card, label) {
  if (!card) return '';
  const esc = escapeHtml;
  const list = (a) => (a && a.length) ? a.map(esc).join(', ') : '—';
  const cl = card; // card.json IS the clinical object
  const p = cl.patient || {};
  const an = cl.anamnesis || {};
  const tp = cl.treatment_plan || {};
  const teeth = (cl.dental_status || []).map(t =>
    `<tr><td>${esc(t.tooth)}</td><td>${esc(t.status)}</td><td>${esc(t.note || '—')}</td></tr>`).join('');
  const stages = (tp.stages || []).map(s =>
    `<tr><td>${esc(s.procedure)}</td><td>${esc(s.teeth || '—')}</td><td>${esc(s.priority)}</td><td>${esc(s.timeline || '—')}</td><td>${esc(s.price || '—')}</td></tr>`).join('');
  const row = (k, v) => `<dt>${k}</dt><dd>${v}</dd>`;
  return `
    <div class="card">
      <h3>🦷 ${esc(label || 'Карта приёма')}</h3>
      <dl class="card-dl">
        ${row('Пациент', `${esc(p.gender || '—')}; имя ${esc(p.name || '—')}; возраст ${esc(p.age || '—')}; тел. ${esc(p.phone || '—')}`)}
        ${row('Повод', esc(cl.chief_complaint || '—'))}
        ${row('Жалобы', list(an.complaints))}
        ${row('Анамнез', esc(an.history || '—'))}
        ${row('Хронические', list(an.chronic_conditions))}
        ${row('Аллергии', list(an.allergies))}
        ${row('Препараты', list(an.medications))}
        ${row('Осмотр', list(cl.examination))}
        ${row('Диагноз', list(cl.diagnosis))}
      </dl>
      ${teeth ? `<h4>Зубная формула</h4><table class="card-table"><tr><th>Зуб</th><th>Статус</th><th>Заметка</th></tr>${teeth}</table>` : ''}
      ${stages ? `<h4>План лечения</h4><table class="card-table"><tr><th>Процедура</th><th>Зубы</th><th>Приоритет</th><th>Срок</th><th>Цена</th></tr>${stages}</table>
        <p><b>Итого:</b> ${esc(tp.total_cost || '—')} · <b>Оплата:</b> ${list(tp.payment_options)}</p>` : '<h4>План лечения</h4><p>—</p>'}
      ${(cl.recommendations && cl.recommendations.length) ? `<h4>Рекомендации</h4><ul>${cl.recommendations.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}
      ${cl.visit_outcome ? `<p><b>Итог визита:</b> ${esc(cl.visit_outcome.result)} — ${esc(cl.visit_outcome.next_step || '—')}</p>` : ''}
      ${cl.summary ? `<blockquote>${esc(cl.summary)}</blockquote>` : ''}
    </div>`;
}
```

- [ ] **Step 3: Insert the card** at the top of the `if (!extraction) {` branch (right after it opens, ~line 580), before the existing analysis blocks:
```javascript
      html += renderCard(card, _currentCardLabel);
```
Where `_currentCardLabel` defaults to `'Карта приёма'` (the label is the same for the dental tenant; reading it from the API is a later nicety). Add near the top of `renderCallDetail`:
```javascript
  const _currentCardLabel = 'Карта приёма';
```

- [ ] **Step 4: Verify JS parses**

Run: `cd /root/projects/silentqa-dev && node --check backend/static/app.js`
Expected: no output (exit 0).

- [ ] **Step 5: Commit**
```bash
cd /root/projects/silentqa-dev
git add backend/static/app.js
git commit -m "feat(dashboard): render «Карта приёма» card on the call-detail page"
```

---

### Task 9: Dependency fix + deployment checklist

**Files:**
- Modify: `worker/requirements.txt` (add `openai`)
- Create: `docs/superpowers/plans/2026-06-17-dental-mvp-DEPLOY.md` (ops checklist; not code)

- [ ] **Step 1: Add `openai` to `worker/requirements.txt`** (it's imported by `quality.py`/`extract.py`/`card.py` but undeclared — CLAUDE.md drift). Add under the LLM section:
```
openai>=2.26.0
```

- [ ] **Step 2: Verify the worker imports resolve in the venv**

Run: `/root/projects/silentqa/.venv/bin/python -c "import openai, httpx, assemblyai; print(openai.__version__, httpx.__version__, assemblyai.__version__)"`
Expected: prints versions (2.26.0 / 0.28.1 / 0.58.0).

- [ ] **Step 3: Write the deploy checklist** `docs/superpowers/plans/2026-06-17-dental-mvp-DEPLOY.md`:
```markdown
# Dental MVP — deploy to prod SilentQA (fulldent)

Prod platform: `/root/projects/silentqa` (:8007). Do these in order.

1. Merge `dental-mvp-fulldent` → `multi-tenant-core-phase1`; deploy code to `/root/projects/silentqa` (pull/rsync per existing process). Includes `companies/dental.json`.
2. Add to prod worker `.env`: `ELEVENLABS_API_KEY=<rotated key>` (env only — never commit). Optional: `ELEVENLABS_STT_MODEL=scribe_v2`, `ELEVENLABS_LANGUAGE=ru`.
3. Point the tenant: `UPDATE shared.tenants SET company_config_id='dental' WHERE slug='fulldent';` (or via platform admin). Then invalidate the registry cache / restart backend.
4. Restart worker + backend (so company_config + ASR engine reload; `_TENANT_COMPANY_CACHE`/`_cache` are process-level).
5. E2E as a fulldent user: upload a dental appointment via `#upload`, wait for processing, open the call → verify transcript (ElevenLabs speakers), QA (dental criteria, N/A where inapplicable), and «Карта приёма» (incl. зубная формула).
6. Rotate the ElevenLabs key used during testing (it was pasted in chat).

Rollback: set `company_config_id` back to previous; remove `ELEVENLABS_API_KEY`. realestate is unaffected throughout (no `card_extraction`, scenarios keep their `prompt` → V4 unchanged).
```

- [ ] **Step 4: Final full-suite check (both services)**

Run: `cd /root/projects/silentqa-dev/worker && /root/projects/silentqa/.venv/bin/python -m pytest --ignore=tests/test_complex_match.py -q`
Run: `cd /root/projects/silentqa-dev/backend && /root/projects/silentqa/.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**
```bash
cd /root/projects/silentqa-dev
git add worker/requirements.txt docs/superpowers/plans/2026-06-17-dental-mvp-DEPLOY.md
git commit -m "chore(worker): declare openai dep + dental MVP deploy checklist"
```

---

## Notes on safety (realestate / other tenants)

Every engine change is inert for tenants without the dental config:
- **ASR:** `transcribe_audio` still defaults to env/whisper; elevenlabs only runs when a tenant's `asr.engine="elevenlabs"`. The new fallback chain is a superset of the old behavior.
- **Card step:** runs only when `company_config["card_extraction"]` exists (realestate has none → `run_card_extraction` returns None, no LLM call — guarded by `test_card_skipped_for_realestate`).
- **QA N/A:** only the V2 schema/prompt changed; realestate uses V4 (scenario has `prompt`) which is untouched — guarded by `test_v4_path_score_stays_integer_only`.
- **Default scenario:** `get_default_scenario_id` returns None for realestate → scenario resolution unchanged.
- **Endpoint:** `/card` 404s for any tenant with no `card.json`.

## Self-Review

**1. Spec coverage:**
- ElevenLabs ASR + fallback → Task 1. ✅
- ASR per-tenant via config → already supported (Task 6 sets `asr.engine`); Task 1 teaches dispatch `elevenlabs`. ✅
- Generic `card_extraction` (label/prompt/schema) → Tasks 2,3,6. ✅
- Pipeline врезка config-gated → Task 5. ✅
- QA scenario-aware + N/A → Task 4 (N/A) + Task 5 (default scenario) + Task 6 (dental criteria). ✅
- Dental schema incl. tooth chart (`dental_status`) → Task 6. ✅
- Backend `/card` + dashboard card → Tasks 7,8. ✅
- realestate-unaffected tests → Tasks 3/4/5 guards + Notes. ✅
- Deploy (ELEVENLABS_API_KEY, fulldent→dental, restart, rotate key) → Task 9. ✅
- Registry/patient cross-session = OUT of scope (separate dental-CRM) → not in plan, by design. ✅

**2. Placeholder scan:** No TBD/"handle errors"/"similar to". Every code step shows real code. `_currentCardLabel` hardcoded to 'Карта приёма' (explicit, not a placeholder; reading from API is noted as a later nicety).

**3. Type consistency:** `run_card_extraction(transcript, company_config)` signature consistent across Tasks 3/5/test. `get_card_extraction`/`get_default_scenario_id` names consistent (Tasks 2/5/6). card.json = the clinical object directly (no `qa`/`speaker_roles` — those stay in quality.json), and `renderCard` reads it as the clinical object (Task 8) — consistent with the schema in Task 6.
