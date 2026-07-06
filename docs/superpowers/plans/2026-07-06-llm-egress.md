# Пакет 2 «LLM-egress + экономика» — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Единый LLM-egress-адаптер (шов под стоимость сейчас и 152-ФЗ потом), ликвидация legacy-дубля quality (тихий V4→V2-даунгрейд + двойной спенд), card/plan на gpt-5.4-mini через env, гейт «нет диалога» после ASR, кап prior_context. Оценка: extended-звонок $0.10-0.13 → $0.06-0.08, мусорные звонки → $0.

**Architecture:** Новый shared-пакет `llm/` в корне репо (паттерн `tenancy/` — на sys.path обоих сервисов): `structured_completion()` владеет клиентом, ретраями транзиентных, обработкой усечения, выбором модели. Все 5 structured-вызовов переводятся на него; legacy-путь quality удаляется (транзиентные ошибки всплывают в существующий task-retry, детерминированные → честный `skip_reason="llm_error"`). Волны: W1=[T1 адаптер, T2 гейт, T3 prior-кап] → W2=[T4 quality, T5 card/extract/eval].

**Tech Stack:** OpenAI Responses API (SDK ≥2.44), Celery task-retry (существующий), pure-unit тесты.

## Global Constraints

- Русский в комментариях/докстрингах. Тесты pure-unit без сети (`monkeypatch` клиента). cwd: worker-тесты из `worker/`, backend из `backend/`; python `../.venv/bin/python`.
- **Матрица моделей (env, чтение в момент вызова):** `SQA_LLM_MODEL_QUALITY`→`gpt-5.4`, `SQA_LLM_MODEL_PLAN`→`gpt-5.4-mini`, `SQA_LLM_MODEL_CARD`→`gpt-5.4-mini`, `SQA_LLM_MODEL_EXTRACT`→`gpt-5.4`; eval-rewrite сохраняет существующий `QUALITY_OPENAI_MODEL`.
- Константы `*_MAX_OUTPUT_TOKENS` и cache-ключи (`sqa-quality-v{2|4}[-tpl]`, `sqa-plan`, `sqa-card`, `sqa-extract-{tid}`, `sqa-eval-rewrite`) сохраняют имена/значения — на них завязаны тесты.
- Публичная сигнатура `assess_quality(...)`, `run_card_extraction(...)`, `plan_next_call(...)`, `rewrite_eval_prompt(...)` НЕ меняется. Внутренняя `_assess_with_structured_output` теряет параметр `client` (владение клиентом уходит в адаптер).
- Контракт возврата `_run_pipeline`/`_analyze_inner` НЕ меняется (AmoCRM-поллер его читает).
- **Агенты НЕ делают git**; `.env.example` правит контроллер на границе волны (три задачи хотят его — единый владелец).

---

### Task 1: shared-пакет `llm/` — egress-адаптер

**Files:**
- Create: `llm/__init__.py` (пустой с докстрингом), `llm/egress.py`
- Test: `worker/tests/test_llm_egress.py` (новый; worker-conftest уже кладёт корень репо на sys.path — проверь, иначе добавь insert в тесте)

**Interfaces (Produces — на них завязаны T4/T5):**
```python
from llm.egress import structured_completion, LLMEgressError, LLMTruncated, LLMBadOutput

def structured_completion(*, system: str, user: str, schema: dict, schema_name: str,
                          max_output_tokens: int, cache_key: str,
                          model: str | None = None, retries: int = 2,
                          api_key: str | None = None, backoff_base: float = 2.0) -> dict
```
Семантика: транзиентные (`openai.APITimeoutError|APIConnectionError|RateLimitError|InternalServerError`) — ретрай внутри с бэкофом 2с→8с, при исчерпании оригинальное исключение всплывает (его ловит существующий task-retry пайплайна); `response.status == "incomplete"` → `LLMTruncated`; невалидный JSON → `LLMBadOutput`; успех → распарсенный dict. `model=None` → `os.getenv("SQA_LLM_MODEL_DEFAULT", "gpt-5.4")`.

- [ ] **Step 1: тест** — `worker/tests/test_llm_egress.py`, monkeypatch `llm.egress.OpenAI` фейком (идиома FakeResponses/FakeClient из `worker/tests/test_llm_call_params.py`) и `llm.egress.time.sleep` (капчерить задержки). Кейсы: (1) happy-path — kwargs вызова содержат model/max_output_tokens/prompt_cache_key/text.format.strict; (2) 2 транзиентных → успех на 3-й, слипы [2.0, 8.0]; (3) 3 транзиентных при retries=2 → исходное исключение всплывает; (4) status="incomplete" → LLMTruncated; (5) мусорный output_text → LLMBadOutput; (6) model=None → SQA_LLM_MODEL_DEFAULT из env.
- [ ] **Step 2: красный прогон** `cd worker && ../.venv/bin/python -m pytest tests/test_llm_egress.py -v` (ModuleNotFoundError).
- [ ] **Step 3: реализация** `llm/egress.py`:

```python
"""Единая точка выхода на облачный LLM (ревью D-1/C-4-задел).

Все structured-вызовы проекта идут сюда: модель/потолок/кэш-ключ/ретраи
живут в одном месте, и это же — будущий шов псевдонимизации (152-ФЗ)
или локальной модели: замена провайдера = правка одного модуля.
"""
import json
import logging
import os
import time

import openai as _openai
from openai import OpenAI

logger = logging.getLogger(__name__)

# Транзиентные классы SDK: ретраим сами (поверх встроенных ретраев SDK),
# при исчерпании отдаём исключение наверх — там его ловит task-retry Celery.
_TRANSIENT = (_openai.APITimeoutError, _openai.APIConnectionError,
              _openai.RateLimitError, _openai.InternalServerError)


class LLMEgressError(Exception):
    """База детерминированных ошибок LLM-выхода (ретрай не поможет)."""


class LLMTruncated(LLMEgressError):
    """Выход усечён потолком max_output_tokens."""


class LLMBadOutput(LLMEgressError):
    """Модель вернула невалидный JSON при strict-схеме."""


def structured_completion(*, system, user, schema, schema_name,
                          max_output_tokens, cache_key,
                          model=None, retries=2, api_key=None,
                          backoff_base=2.0) -> dict:
    client = OpenAI(api_key=api_key) if api_key else OpenAI()
    attempt = 0
    while True:
        try:
            resp = client.responses.create(
                model=model or os.getenv("SQA_LLM_MODEL_DEFAULT", "gpt-5.4"),
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_output_tokens=max_output_tokens,
                prompt_cache_key=cache_key,
                text={"format": {"type": "json_schema", "name": schema_name,
                                 "strict": True, "schema": schema}},
            )
            break
        except _TRANSIENT as e:
            attempt += 1
            if attempt > retries:
                raise
            delay = backoff_base * (4 ** (attempt - 1))  # 2с, 8с
            logger.warning("LLM transient %s (%s, попытка %d/%d), повтор через %.0fс",
                           type(e).__name__, schema_name, attempt, retries, delay)
            time.sleep(delay)
    if getattr(resp, "status", None) == "incomplete":
        raise LLMTruncated(f"{schema_name}: выход усечён на {max_output_tokens} токенов")
    try:
        return json.loads(resp.output_text)
    except (json.JSONDecodeError, TypeError) as e:
        raise LLMBadOutput(f"{schema_name}: невалидный JSON от модели: {e}") from e
```

`llm/__init__.py`: `"""Shared LLM-egress пакет (импортируют worker и backend)."""`
- [ ] **Step 4: зелёный прогон** тех же тестов; smoke-импорт `PYTHONPATH=/root/projects/silentqa-dev ../.venv/bin/python -c "import llm.egress; print('ok')"`.
- [ ] **Step 5: коммит (контроллер)** — `feat(llm): egress-адаптер structured_completion (единый шов моделей/ретраев/кэша)`

---

### Task 2: гейт «нет диалога» после ASR (D-2)

**Files:**
- Modify: `worker/tasks/pipeline.py`
- Test: `worker/tests/test_no_dialogue_gate.py` (новый)

**Interfaces:** Produces: `_no_dialogue_gate(task, session_id, audio_path, scenario, session_meta, transcript_with_speakers) -> dict | None` (контракт возврата тот же, что у `_run_gates`); env-ручки `NO_DIALOGUE_GATE=1|0` (дефолт 1), `NO_DIALOGUE_MIN_SEGMENTS=4`, `NO_DIALOGUE_MIN_CHARS=200` (читаются в момент вызова).

- [ ] **Step 1: тест** — матрица условий на чистой функции с monkeypatch `pipeline.save_results`, `pipeline.record_quality_result`, `pipeline._push_to_amocrm`, `pipeline._get_audio_duration` (idiom смотри в существующих тестах pipeline, напр. `test_pipeline_card.py`): один спикер → отчёт со skip_reason="no_dialogue"; 3 непустых сегмента (<4) → гейт; длинный двусторонний диалог → None; `NO_DIALOGUE_GATE=0` → None всегда; вернувшийся dict несёт исходный transcript_with_speakers и use_extended по сценарию; record_quality_result вызван со skip_reason="no_dialogue".
- [ ] **Step 2: красный прогон.**
- [ ] **Step 3: реализация.** Рядом с `_build_short_call_report` (pipeline.py:~68) добавь `_build_no_dialogue_report(stats: dict) -> dict` по тому же образцу: `overall_score: None`, `skip_reason: "no_dialogue"`, `brief_summary` вида `"⚠️ Диалог не состоялся ({speakers} спикер(а), {segments} реплик). Оценка не проводилась — вероятно, гудки/автоответчик/монолог."`, `summary`, `dialogue_stats: stats`, `score_version: "no_dialogue_v1"`. Затем функция-гейт (место — после extraction-ветки `_analyze_inner`, ПЕРЕД `=== 5. Sentiment ===`):

```python
def _no_dialogue_gate(task, session_id, audio_path, scenario, session_meta,
                      transcript_with_speakers) -> dict | None:
    """Пост-ASR гейт (ревью D-2): гудки/автоответчик/монолог не жгут LLM.

    Дешёвая эвристика по готовому транскрипту: <2 спикеров с речью, либо
    слишком мало реплик/символов. Персист — зеркально гейтам _run_gates."""
    if os.getenv("NO_DIALOGUE_GATE", "1") == "0":
        return None
    min_segments = int(os.getenv("NO_DIALOGUE_MIN_SEGMENTS", "4"))
    min_chars = int(os.getenv("NO_DIALOGUE_MIN_CHARS", "200"))
    voiced = [s for s in (transcript_with_speakers or [])
              if (s.get("text") or "").strip()]
    speakers = {s.get("speaker") for s in voiced}
    total_chars = sum(len((s.get("text") or "").strip()) for s in voiced)
    if len(speakers) >= 2 and len(voiced) >= min_segments and total_chars >= min_chars:
        return None
    stats = {"speakers": len(speakers), "segments": len(voiced), "chars": total_chars}
    logger.info(f"[{session_id}] No-dialogue gate: {stats} — пропускаем LLM-оценку")
    quality_report = _build_no_dialogue_report(stats)
    save_results(session_id, "quality", quality_report)
    record_quality_result(session_id, quality_report, skip_reason="no_dialogue",
                          duration_seconds=_get_audio_duration(audio_path) or None)
    use_extended = bool(scenario and scenario.get("prompt"))
    if use_extended:
        task.update_state(state="PROGRESS", meta={"step": "amocrm_sync", "progress": 95})
        _push_to_amocrm(session_id, quality_report, session_meta, audio_path)
    return {
        "transcript_with_speakers": transcript_with_speakers,
        "quality_report": quality_report,
        "use_extended": use_extended,
    }
```

Вызов в `_analyze_inner` сразу после extraction-ветки:

```python
    gate = _no_dialogue_gate(task, session_id, audio_path, scenario, session_meta,
                             transcript_with_speakers)
    if gate is not None:
        return gate
```

ОБЯЗАТЕЛЬНО: прочитай вызывающие `_analyze_inner` (`analyze_session`-таска и `_run_pipeline`) и подтверди в отчёте, что ранний return доводит сессию до `completed` тем же путём, что extraction-ветка (она уже так возвращается).
- [ ] **Step 4: зелёный прогон** нового файла + `cd worker && ../.venv/bin/python -m pytest -q` (весь worker-сьют, гейт не должен зацепить существующие пайплайн-тесты — если зацепил, значит их фикстуры дают «недиалоговый» транскрипт: выставь в таких тестах env NO_DIALOGUE_GATE=0 через monkeypatch, это легитимно).
- [ ] **Step 5: коммит (контроллер)** — `feat(worker): гейт «нет диалога» после ASR — не жжём LLM на гудках (D-2)`

---

### Task 3: кап prior_context (D-1.5)

**Files:**
- Modify: `worker/tasks/prior_context.py` (SQL `load_past_sessions_for_lead` :213-240; `build_prior_context_for_session`)
- Test: существующий сьют prior_context (`grep -l prior_context worker/tests/`) — дополни; новых файлов не создавай без нужды.

**Interfaces:** env `PRIOR_CONTEXT_MAX_CALLS=8` (LIMIT в SQL), `PRIOR_CONTEXT_MAX_CHARS=16000` (кап сериализованного контекста), `PRIOR_CONTEXT_MAX_OBJECTIONS=10`.

- [ ] **Step 1: тест** — (а) SQL-запрос содержит `ORDER BY` + `LIMIT` (source-тест по тексту функции); (б) `build_prior_context_for_session`-уровень: при раздутом входе (сгенерируй 30 интеракций по ~2КБ) итоговый `json.dumps(ctx, ensure_ascii=False)` ≤ PRIOR_CONTEXT_MAX_CHARS и выкинуты СТАРЕЙШИЕ интеракции (первые), новейшие целы; (в) open_objections капится до последних 10; (г) маленький контекст не трогается.
- [ ] **Step 2: красный прогон.**
- [ ] **Step 3: реализация.** В SQL: `ORDER BY s.created_at DESC LIMIT %s` с `int(os.getenv("PRIOR_CONTEXT_MAX_CALLS", "8"))`, после выборки развернуть в хронологический порядок (`entries.reverse()`), СОХРАНИВ текущую сортировку-семантику для build_prior_context_dict (прочитай, в каком порядке она ждёт вход, и не сломай). В `build_prior_context_for_session` после сборки ctx:

```python
    max_chars = int(os.getenv("PRIOR_CONTEXT_MAX_CHARS", "16000"))
    max_obj = int(os.getenv("PRIOR_CONTEXT_MAX_OBJECTIONS", "10"))
    if len(ctx.get("open_objections") or []) > max_obj:
        ctx["open_objections"] = ctx["open_objections"][-max_obj:]
    # Токен-бюджет: выкидываем старейшие интеракции, пока не влезем
    # (client_profile уже агрегирует ВСЮ историю — факты не теряются).
    while (len(json.dumps(ctx, ensure_ascii=False)) > max_chars
           and len(ctx.get("interactions_history") or []) > 1):
        ctx["interactions_history"].pop(0)
```
- [ ] **Step 4: зелёный прогон** всего worker-сьюта.
- [ ] **Step 5: коммит (контроллер)** — `fix(worker): кап prior_context — LIMIT в SQL + токен-бюджет (D-1.5)`

---

### Task 4 (W2): quality.py на адаптер, legacy-путь удалён

**Files:**
- Modify: `worker/tasks/quality.py`
- Test: `worker/tests/test_llm_call_params.py` (переписать под адаптер), `worker/tests/test_kb_glossary.py` (тест legacy-пути :58 заменить на structured-эквивалент)

**Interfaces:**
- Consumes: `llm.egress.structured_completion / LLMTruncated / LLMBadOutput` (Task 1).
- Produces: `_assess_with_structured_output(transcript_text, sentiment_json, protocol, custom_instructions, criteria_instructions="", use_extended_schema=False, prior_context=None, template_driven=False, glossary=None)` — БЕЗ параметра client; при детерминированной LLM-ошибке `assess_quality` возвращает `{"overall_score": None, "skip_reason": "llm_error", "error": <строка>, "score_version": <2|4>}`.

- [ ] **Step 1: тесты.** Перепиши `test_llm_call_params.py`: monkeypatch `tasks.quality.structured_completion` капчер-фейком; проверь для v2/v4-tpl/plan: `cache_key`, `max_output_tokens` (константы), `model` (env-дефолты: quality "gpt-5.4", plan "gpt-5.4-mini"; при `SQA_LLM_MODEL_PLAN=x` через monkeypatch.setenv — "x"). Card/extract-кейсы этого файла НЕ трогай (их обновит Task 5 — если гоняешь весь файл, эти два упадут до T5: гоняй свои тесты поимённо). Новые кейсы: `structured_completion` кидает LLMBadOutput → `assess_quality` возвращает skip_reason="llm_error" и score_version соответствует режиму; кидает `openai.APITimeoutError` → исключение ВСПЛЫВАЕТ из assess_quality (не глотается). В `test_kb_glossary.py` замени legacy-тест на проверку, что glossary попадает в `user` structured-вызова (капчер kwargs).
- [ ] **Step 2: красный прогон** поимённо своих тестов.
- [ ] **Step 3: реализация.** В quality.py: импорт `from llm.egress import structured_completion, LLMTruncated, LLMBadOutput`. `_assess_with_structured_output`: убери параметр `client`, замени `client.responses.create(...)` + status-warning + json.loads на:

```python
    result = structured_completion(
        system=system, user=user,
        schema=schema["schema"], schema_name=schema_name,
        max_output_tokens=QUALITY_MAX_OUTPUT_TOKENS,
        cache_key=f"sqa-quality-v{version}" + ("-tpl" if template_driven else ""),
        model=os.getenv("SQA_LLM_MODEL_QUALITY", "gpt-5.4"),
    )
```
(конверсия speaker_roles и `result["score_version"] = version` остаются). `assess_quality` (:1159-1188): убери создание OpenAI-клиента и legacy-fallback; вместо них:

```python
    try:
        result = _assess_with_structured_output(
            transcript_text, sentiment_json, protocol,
            custom_instructions, criteria_instructions,
            use_extended_schema=use_extended_schema,
            prior_context=prior_context,
            template_driven=template_driven,
            glossary=glossary,
        )
    except (LLMTruncated, LLMBadOutput) as e:
        # Детерминированная ошибка — ретрай не поможет; честный skip вместо
        # тихого даунгрейда V4→V2 через legacy-путь (удалён, ревью D-1.3).
        logger.error(f"Structured quality failed deterministically: {e}")
        result = {"overall_score": None, "skip_reason": "llm_error",
                  "error": str(e),
                  "score_version": 4 if use_extended_schema else 2}
    # Транзиентные исключения НЕ ловим: всплывают в task-retry пайплайна.
```
Удали ЦЕЛИКОМ `_assess_with_legacy_prompt` и `ASSESSMENT_PROMPT`. `plan_next_call`: замени клиент+create на `structured_completion(..., max_output_tokens=PLAN_MAX_OUTPUT_TOKENS, cache_key="sqa-plan", model=os.getenv("SQA_LLM_MODEL_PLAN", "gpt-5.4-mini"), api_key=api_key)` — внешний широкий `except Exception → None` СОХРАНИ (план опционален и не должен блокировать пайплайн).
- [ ] **Step 4: зелёный прогон** своих тестов поимённо + smoke-импорт quality.
- [ ] **Step 5: коммит (контроллер)** — `feat(worker): quality на llm.egress, legacy-дубль удалён (D-1.3), модели по env`

---

### Task 5 (W2): card/extract/eval-rewrite на адаптер

**Files:**
- Modify: `worker/tasks/card.py`, `worker/tasks/extract.py`, `backend/app/eval_prompt_rewrite.py`
- Test: card/extract-кейсы `worker/tests/test_llm_call_params.py` (перепиши под monkeypatch `tasks.card.structured_completion`; source-тест extract обнови под новую форму вызова), `backend/tests/test_eval_prompt_rewrite.py` (fake-openai-модуль замени на monkeypatch `app.eval_prompt_rewrite.structured_completion` или `llm.egress.OpenAI`)

**Interfaces:** Consumes Task 1. Card: `model=os.getenv("SQA_LLM_MODEL_CARD", "gpt-5.4-mini")`, cache_key="sqa-card", CARD_MAX_OUTPUT_TOKENS. Extract: `model=os.getenv("SQA_LLM_MODEL_EXTRACT", "gpt-5.4")`, cache_key=f"sqa-extract-{template_id}", EXTRACT_MAX_OUTPUT_TOKENS. Eval-rewrite: `model=_MODEL` (существующий env QUALITY_OPENAI_MODEL), cache_key="sqa-eval-rewrite", EVAL_REWRITE_MAX_OUTPUT_TOKENS.

- [ ] **Step 1: тесты** (красные): card — капчер structured_completion, assert model-дефолт "gpt-5.4-mini" и override через env; extract — source-тест: `structured_completion(` в extract.py, `SQA_LLM_MODEL_EXTRACT` присутствует, прямого `OpenAI(` больше нет; eval — существующие 2 теста переведи на новый мок, добавь assert model из QUALITY_OPENAI_MODEL и cache_key.
- [ ] **Step 2: красный прогон поимённо.**
- [ ] **Step 3: реализация.** В каждом файле: убрать `from openai import OpenAI` (если больше не нужен), импорт `from llm.egress import structured_completion`; вызов заменить на адаптер с параметрами из Interfaces. Семантика ошибок сохраняется: card — внешний `except Exception → None` остаётся; extract — исключения всплывают (сессия фейлится, как сейчас); eval-rewrite — RuntimeError при отсутствии OPENAI_API_KEY остаётся, `api_key=api_key` прокинь в адаптер.
- [ ] **Step 4: зелёный прогон** своих тестов + `cd worker && ../.venv/bin/python -m pytest tests/test_llm_call_params.py -q` целиком (после T4+T5 файл зелёный весь).
- [ ] **Step 5: коммит (контроллер)** — `feat(llm): card/extract/eval-rewrite на llm.egress; card/plan по умолчанию gpt-5.4-mini`

---

## Волны исполнения (SDD)

- **W1 = [T1, T2, T3] параллельно** (llm/ ↔ pipeline.py ↔ prior_context.py — дизъюнктны).
- **W2 = [T4, T5] параллельно** (quality.py ↔ card/extract/eval; общий test_llm_call_params.py разведён по кейсам: T4 — quality/plan, T5 — card/extract; на границе волны контроллер гоняет файл целиком).
- Контроллер после W2: `.env.example` (блок SQA_LLM_MODEL_* + NO_DIALOGUE_* + PRIOR_CONTEXT_*), полные сьюты, ревью, финал-ревью, push, CI.

## Definition of Done

- `grep -rn "client.responses.create\|chat.completions.create" worker/tasks backend/app` — ноль вхождений вне `llm/egress.py`.
- `ASSESSMENT_PROMPT`/`_assess_with_legacy_prompt` отсутствуют в кодовой базе.
- Все сьюты зелёные; модели card/plan управляются env и по умолчанию mini; quality — gpt-5.4.
- Гейт no_dialogue отключаем env-ом; skip-строки доезжают до quality_results.
- prior_context ограничен LIMIT+бюджетом; поведение при малой истории не изменилось.
