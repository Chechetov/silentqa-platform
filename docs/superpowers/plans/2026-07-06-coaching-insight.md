# F-5 «Коучинг-инсайт по звонку» — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LLM-коучинг менеджеру по конкретному звонку: главный совет, сильные стороны с цитатами, разборы «момент → почему важно → как лучше сказать» с привязкой к секунде (клик мотает аудио — механизм seek уже есть), фокус-упражнение. Ревью F-5, последняя фича «дашбордной» линейки.

**Architecture:** Отдельный LLM-вызов ПОСЛЕ quality (не раздуваем strict-схемы отчёта; независимый домен сбоя, best-effort): `worker/tasks/coaching.py` через `llm.egress.structured_completion` → файл `coaching.json` → `GET /api/sessions/{id}/coaching` (идиома card.json) → блок «Коучинг» в карточке звонка. Плюс бэкфилл-скрипт для уже обработанных звонков. Три дизъюнктные задачи: T1 worker (+pipeline+бэкфилл), T2 backend-роут, T3 фронт.

**Tech Stack:** llm/egress (модель env `SQA_LLM_MODEL_COACH`, дефолт `gpt-5.4` — флагманская фича; кэш-ключ `sqa-coach`), vanilla JS.

## Global Constraints

- Русский в комментариях/UI/промпте. Тесты pure-unit. Агенты БЕЗ git; `.env.example` — контроллер.
- **Контракт coaching.json (единый для T1/T2/T3, менять нельзя):**

```json
{
  "headline": "Главный совет одной фразой",
  "strengths": [{"text": "что было сильно", "quote": "дословная цитата или null"}],
  "growth_areas": [{
    "moment_quote": "дословная цитата реплики менеджера",
    "why_it_matters": "почему это важно",
    "better_version": "как лучше сформулировать",
    "time": 123.4
  }],
  "drill": "Фокус-упражнение на следующий звонок"
}
```
`strengths` 1-2 элемента, `growth_areas` 1-3, `time` — number|null (start сегмента-источника цитаты). Strict json_schema: все поля required, additionalProperties false, nullable через `"type": ["number","null"]` / `["string","null"]`.
- Коучинг НЕ пишется в quality_results и НЕ уходит в AmoCRM — только дашборд.
- Гейты: не генерится при `skip_reason`/`overall_score is None`; kill-switch env `SQA_COACHING=0`.

---

### Task 1: worker — coaching.py + вайринг + бэкфилл

**Files:** Create `worker/tasks/coaching.py`, `worker/scripts/backfill_coaching.py`, `worker/tests/test_coaching.py`; Modify `worker/tasks/pipeline.py` (один блок в `_analyze_inner` после card-блока ~:987).

**Interfaces:** Produces `generate_coaching(transcript_with_speakers: list[dict], quality_report: dict) -> dict | None` (dict = контракт выше; None только при пустом транскрипте). Константы: `COACH_MAX_OUTPUT_TOKENS = 4_000`, cache_key `"sqa-coach"`, `model=os.getenv("SQA_LLM_MODEL_COACH", "gpt-5.4")`. Consumes: `llm.egress.structured_completion` (kwargs: system/user/schema/schema_name="coaching"/max_output_tokens/cache_key/model).

**coaching.py:**
- `_COACH_SCHEMA` — strict-схема контракта (образец схем — worker/tasks/quality.py NEXT_CALL_PLAN_SCHEMA).
- `_SYSTEM` (русский): «Ты — коуч по продажам/переговорам. По транскрипту и оценке дай менеджеру персональный разбор. Правила: цитаты — ДОСЛОВНО из транскрипта (копируй, не пересказывай); time — число секунд из скобок [t] у процитированной реплики; тон — поддерживающий и конкретный, без общих слов ("будь внимательнее" — запрещено, только конкретика "вместо X скажи Y"); better_version — 1-2 живые фразы от первого лица, готовые к произнесению; headline — самое важное одно; drill — одно микро-упражнение на следующий звонок; не дублируй generic-рекомендации из отчёта — давай именно разбор моментов этого разговора.»
- user-промпт: транскрипт строками `[{start:.1f}] {speaker}: {text}` (кап `COACH_MAX_TRANSCRIPT_CHARS=24_000`, обрезка с конца старейших строк + пометка «(начало усечено)») + компакт отчёта: `overall_score`, критерии со score ≤ 6 (name+score+comment), нерешённые objections (text+broker_response). Формы данных: criteria — список dict name/score/comment; objections v4 — {text, broker_response, resolved,...}.
- Пустой/безречевой транскрипт → return None без вызова LLM.

**pipeline.py — блок после card (~:987), зеркально best-effort идиоме card:**
```python
    # === 6.9 Коучинг-инсайт (F-5, best-effort, дашборд-only) ===
    if os.getenv("SQA_COACHING", "1") != "0" and quality_report.get("overall_score") is not None:
        try:
            from tasks.coaching import generate_coaching
            task.update_state(state="PROGRESS", meta={"step": "coaching", "progress": 92})
            coaching = generate_coaching(transcript_with_speakers, quality_report)
            if coaching:
                save_results(session_id, "coaching", coaching)
        except Exception:
            logger.exception(f"[{session_id}] coaching failed; skipping (best-effort)")
```

**backfill_coaching.py** — зеркало структуры `backfill_quality_results.py` (`--tenant <slug> | --all`, dry-run по умолчанию, `--apply`): кандидаты = сессии тенанта, у которых есть `quality.json` с `overall_score not None` И `transcript.json`, И НЕТ `coaching.json`; apply → generate+save; счётчики per-tenant. ВНИМАНИЕ: apply жжёт реальные LLM-вызовы — предупреждение в докстринге и в dry-run выводе.

**Тесты (monkeypatch `tasks.coaching.structured_completion` капчером):** kwargs — model-дефолт/override, cache_key, потолок, schema strict; транскрипт с [t]-строками и усечением при >24К; слабые критерии и нерешённые возражения попали в user, сильные (score 9) — нет; пустой транскрипт → None без вызова; вайринг pipeline — стаб generate_coaching: зовётся при score=7, НЕ зовётся при skip_reason/score None/SQA_COACHING=0 (идиома стабов — `_stub_analyze_deps` в test_pipeline_stages.py; НЕ трогай сам test_pipeline_stages.py — добавь свои тесты в test_coaching.py).

TDD; фокусные тесты + весь worker-сьют.

- [ ] Steps; **коммит (контроллер):** `feat(worker): F-5 коучинг-инсайт — coaching.py + вайринг + бэкфилл`

---

### Task 2: backend — GET /api/sessions/{id}/coaching

**Files:** Modify `backend/app/routes/analysis.py` (рядом с get_card :50-54); Test: дополни `backend/tests/test_session_card.py` (или зеркальный новый test_session_coaching.py — посмотри, как устроен card-тест, и повтори).

```python
@router.get("/{session_id}/coaching", dependencies=[Depends(require_session_access)])
async def get_coaching(session_id: uuid.UUID):
    """Коучинг-инсайт (coaching.json, F-5). 404, если не сгенерирован."""
    return _read_result_file(session_id, "coaching.json")
```

Тест: файл есть → 200 с телом; файла нет → 404. Полный backend-сьют зелёный.

- [ ] Steps; **коммит (контроллер):** `feat(api): GET /sessions/{id}/coaching (F-5)`

---

### Task 3: frontend — блок «Коучинг» в карточке

**Files:** Modify `backend/static/app.js`, `backend/static/styles.css` (новые классы — единственная задача волны, трогающая styles).

1. В `renderCallDetail` Promise.all добавь `opt(api(\`/api/sessions/${id}/coaching\`))` → переменная `coaching` (в `let [...]`-деструктуризацию).
2. Блок рендерится сразу ПОСЛЕ хедера/summary, ДО критериев (самое видное место):
   - `headline` — крупно, с 🎯, class `coach-headline`;
   - `strengths` — строки с ✓ (class `coach-strength`, цвет var(--good)), quote курсивом в кавычках-ёлочках, если есть;
   - `growth_areas` — карточки: цитата (class `coach-quote`, курсив, приглушённо) → «Почему важно:» текст → «Лучше так:» `better_version` (class `coach-better`, акцент var(--accent)); при `time != null` — кнопка `<button class="btn btn-sm coach-seek" data-t="${g.time}">▶ к моменту</button>`;
   - `drill` — футер блока: «🏋️ Фокус на следующий звонок:» текст.
   - ВСЕ интерполяции текста — через `escapeHtml`; `data-t` — числовой.
3. Seek: расширь существующий селектор клик-seek листенера `'.sentiment-segment[data-t], .moment-item[data-t]'` → добавь `, .coach-seek[data-t]` (один листенер, тот же обработчик).
4. styles.css: классы coach-* на токенах (--bg-card/--good/--accent/--text-secondary), рамка слева var(--accent) у growth-карточек; светлая тема получится сама из токенов — хардкод-цветов не вводить.
5. Блока нет (coaching==null) → ничего не рендерим (никаких заглушек).

Верификация: `node --check backend/static/app.js` (из корня), `node --test 'backend/static/tests/*.test.mjs'` 6/6; grep: все `${` внутри coach-разметки идут через escapeHtml или числовые.

- [ ] Steps; **коммит (контроллер):** `feat(front): блок «Коучинг» в карточке звонка с seek к моментам (F-5)`

---

## Волны исполнения (SDD)

- **W1 = [T1, T2, T3] параллельно** (worker/* ↔ routes/analysis.py ↔ static/*).
- Контроллер: `.env.example` (SQA_COACHING, SQA_LLM_MODEL_COACH), полные сьюты, 3 ревью, финал-ревью, push, CI; прод: деплой + бэкфилл коучинга (`--all` dry-run → `--apply`) + смоук `GET /coaching` (401/404-контракт) и глазами блок в карточке.

## Definition of Done

- Все сьюты зелёные; coaching.json генерится только для оценённых звонков; kill-switch работает.
- Роут отдаёт 200/404; блок в карточке рендерит все 4 секции и мотает аудио по кнопке «к моменту».
- Бэкфилл dry-run/apply работает; предупреждение о стоимости в выводе.
- Ни одной неэкранированной интерполяции в новой разметке.
