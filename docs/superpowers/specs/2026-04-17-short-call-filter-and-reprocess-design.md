# Multi-call context-aware evaluation + short-call filter + reprocess

**Date:** 2026-04-17
**Status:** Draft — awaiting user review
**Supersedes:** first draft of short-call filter spec (same date, same file)
**Restore point:** `pre-multicall-2026-04-17` (tag), commit `4f8c75a`

## Problem

Коллеги жалуются на два симптома:

1. **Не все звонки проходят оценку.** Операционная причина (токен AmoCRM
   отозван) — **вне скоупа спеки**.
2. **Короткие звонки (автоответчик / положили трубку) всё равно идут через
   GPT.** Нужен фильтр.

Плюс стратегический сдвиг в продукте:

3. **Каждый звонок оценивается в вакууме.** На втором звонке брокер не
   представляется заново (и правильно делает), но система штрафует за
   пропуск `greeting_by_name`. Нужна **context-aware evaluation**.
4. **Нет forward-looking рекомендаций.** Сейчас есть
   `improvement_suggestions` (критика брокера), но нет «что делать в
   СЛЕДУЮЩЕМ звонке с этим клиентом».
5. **Нет связности сделки.** Нельзя одним взглядом понять «что мы знаем об
   этом клиенте», «какие возражения открыты», «что было сделано».
6. **Нет обратной связи по рекомендациям.** Если LLM в прошлый раз
   порекомендовал что-то, сейчас мы не знаем, выполнил ли брокер.

Плюс частный запрос: **ручной повторный прогон исторического звонка**
(сделка 11425275, `note_id=46240441`).

## Scope — две фазы

### Фаза 1 (базовая функциональность)

1. Фильтр коротких звонков (< 20с реального аудио).
2. Endpoint + админ-форма ручного reprocess по `lead_id`.
3. Сбор `prior_context` для лида (SQL-агрегация прошлых звонков).
4. Context-aware evaluation (LLM call 1 с расширенной схемой V4).
5. Next-call plan (LLM call 2, отдельный промпт, отдельная заметка в
   AmoCRM — append-only).

### Фаза 2 (полировка и расширение)

6. Follow-through tracking — eval сравнивает с планом предыдущего звонка.
7. Deal-summary note — одна заметка на сделку, update-in-place.
8. Stage-aware рекомендации — тянем этап из AmoCRM `status_id`.
9. Offline-gap awareness — interactions_history включает AmoCRM events.
10. Redis-lock по `lead_id` — сериализация параллельной обработки.

### Вне скоупа обеих фаз

- Восстановление токена AmoCRM (ручная ops).
- Пуш в AmoCRM входящих звонков (сейчас — только outbound extended).
- Materialized `lead_profile` таблица (строим контекст on-the-fly из
  прошлых `quality_report`).
- Zoom-встречи: структура `interactions_history.type` готова к
  расширению, но pipeline для Zoom-аудио — отдельная работа.

---

# Фаза 1

## 1.1 Фильтр коротких звонков

**Где:** `worker/tasks/pipeline.py::_run_pipeline`, в начале, до шага 2.

**Порог:** хардкод `SHORT_CALL_THRESHOLD_SEC = 20`.

**Замер:** `_get_audio_duration(audio_path)` (существующая функция, через
ffprobe).

**Поведение при `audio_duration < 20` и `audio_duration > 0`:**

1. Пропускаем transcribe / diarize / sentiment / quality.
2. Собираем минимальный `quality_report`:
   ```python
   {
     "overall_score": None,
     "skip_reason": "too_short",
     "audio_duration": round(duration, 1),
     "brief_summary": f"⚠️ Короткий звонок ({int(duration)}с). "
                      f"Оценка не проводилась — вероятно, автоответчик "
                      f"или клиент не ответил.",
     "summary": f"Короткий звонок ({int(duration)}с), оценка пропущена.",
     "score_version": "short_call_v1",
   }
   ```
3. `save_results(session_id, "quality", quality_report)`.
4. Если `use_extended` (outbound) — пушим в AmoCRM.

**`format_enriched_note` — short-circuit:**

```python
if quality_report.get("skip_reason") == "too_short":
    duration = quality_report.get("audio_duration", 0)
    return (
        f"⚠️ Короткий звонок ({int(duration)}с).\n\n"
        f"Оценка не проводилась — вероятно, автоответчик "
        f"или клиент не ответил."
    )
```

Тег «AI оценка звонка» на короткий звонок не ставим (уже контролируется
условием `if score is not None` в pipeline).

**Плана на следующий звонок для коротких не генерируем.**

**Граничное:** `_get_audio_duration` вернул `0.0` (сбой ffprobe) — идём
обычным путём, не глотаем потенциально реальный разговор.

## 1.2 Reprocess endpoint + админ-форма

**Endpoint:** `POST /api/amocrm/reprocess`
Файл: `backend/app/routes/amocrm.py` (новый).

**Body:**
```json
{"lead_id": 11425275, "force": false}
```

**Логика:**
1. `GET /api/v4/leads/{lead_id}?with=contacts` → `entity_ids`:
   `[("leads", lead_id)] + [("contacts", c.id) for c in linked]`.
2. Для каждого entity — `GET /notes?filter[note_type][]=call_in&filter[note_type][]=call_out&limit=100`.
3. Для каждой call-note:
   - Если `params.source == "Rogov AI"` — пропускаем.
   - Если нет `params.link` — в `missing_recording`.
   - Пытаемся `_insert_call` (`ON CONFLICT DO NOTHING`):
     - Вставилось → `process_amocrm_call.delay(call_id)`, в `queued`.
     - Уже есть и `force=true` → reset `status='created', retry_count=0, error_message=NULL`, enqueue, в `queued`.
     - Иначе → в `already_processed`.

**Вызов task из backend:**
`celery_app.send_task("amocrm_poll.process_amocrm_call", args=[call_id])` —
чтобы backend не импортировал worker. Celery-app инстанс создаётся
локально в backend с тем же broker URL.

**Response:**
```json
{
  "lead_id": 11425275,
  "queued": [{"call_id": 42, "note_id": 46240441, "duration": 1050, "direction": "out"}],
  "already_processed": [],
  "missing_recording": []
}
```

**Админ-форма:**
- Новый пункт меню «Повторный прогон» в `backend/static/index.html`.
- Роутер в `backend/static/app.js`: `#reprocess` → рендер формы.
- Поля: `lead_id` (number), чекбокс `force`, кнопка «Запустить».
- После submit — таблица результата.

## 1.3 Prior context — сбор

**Где:** `worker/tasks/pipeline.py`, новая функция `_build_prior_context(lead_id, current_session_created_at)`.

**Что запрашиваем из БД (sessions):**
```sql
SELECT id, created_at, metadata
FROM sessions
WHERE (metadata->>'lead_id')::bigint = %s
  AND created_at < %s
  AND status = 'completed'
ORDER BY created_at ASC
```

**Как достаём quality_report:** результаты хранятся не в БД, а как JSON
на диске — `{RESULTS_PATH}/{session_id}/quality.json` (пишет
`save_results`). Для каждой найденной сессии читаем этот файл; если файла
нет или он не парсится — игнорируем (skip в истории, не роняем
pipeline).

**Что извлекаем из каждого прошлого `quality_report`:**
- `brief_summary`, `summary`
- `client_info` (все поля)
- `objections` (все + `resolved` flag)
- `conversation_outcome`
- `call_classification.type`
- `audio_duration` (если есть) или длительность из `amocrm_calls.duration`

**Что собираем в `prior_context`:**

```python
{
  "call_number": N,  # 1-based; текущий звонок
  "previous_calls_count": N - 1,
  "client_profile": {
      # merge по времени; новее перекрывает старое;
      # если значение изменилось — сохраняем обе версии с датами
      "budget": "20-25 млн (последнее обновление 2026-04-10)",
      "locations": ["Хамовники", "Остоженка"],
      # ...
  },
  "interactions_history": [
      {
          "date": "2026-04-03",
          "type": "call_out",
          "duration": 840,
          "classification": "partial",
          "outcome": "info_provided",
          "brief": "..."
      },
      # ...
  ],
  "open_objections": [
      {"text": "...", "raised_at": "2026-04-10", "category": "..."}
  ],
  "resolved_objections": [
      {"text": "...", "resolved_at": "2026-04-10", "how": "..."}
  ]
}
```

Функция мержа profile: последнее ненулевое значение побеждает. Если
значение изменилось (новый звонок переписал бюджет с «20М» на «15М») —
сохраняем как `"budget": "15 млн (было 20, пересмотрено 2026-04-15)"`.
Правило не штрафуем LLM — пусть сам видит изменение.

**Граничные:**
- `lead_id` отсутствует (нет связи со сделкой) → `prior_context = None`.
  Pipeline работает как сейчас (V3 без изменений).
- Первый звонок на лиде → `prior_context.previous_calls_count = 0`,
  `call_number = 1`, профиль пустой. LLM ведёт себя как сейчас.

## 1.4 Context-aware evaluation (LLM call 1)

**Новая версия схемы:** `QUALITY_JSON_SCHEMA_V4` в `worker/tasks/quality.py`.

**Что добавляем поверх V3:**

```python
"stage_progression": {
    "new_info_learned": ["array of strings"],
    "objections_resolved": ["array: was open → now closed"],
    "objections_raised": ["array: новые возражения"],
    "profile_updates": ["array: как изменился профиль клиента"],
    "progress_delta": "string: как продвинулись (нейтрально/positive/negative)",
    "stage_advanced": "boolean: сделка реально двинулась"
}
```

В чек-листе (`protocol_checklist`) для повторных звонков LLM ставит
существующий статус `not_applicable` + в поле `comment` указывает
причину: `"Уже сделано в звонке от 2026-04-10"`. Схема не меняется,
инструкция — только в промпте.

**Изменения промпта:**

Передаём в `user` часть промпта дополнительный блок `prior_context`
(JSON-строка, сериализованный словарь сверху).

В `system` добавляем секцию:

```
## Контекст взаимодействий

Звонок может быть не первым. В prior_context есть список прошлых
взаимодействий + накопленный профиль клиента.

Правила:
- Если это call_number > 1 — не штрафуй брокера за пропуск
  приветствия/представления (пункты greeting_by_name, introduced_self,
  mentioned_project). Ставь им status="not_applicable" с comment
  вида "Уже сделано в прошлом звонке от <date>".
- Если информация уже известна (бюджет, локация, сроки) — не требуй
  задавать вопрос заново. Status="not_applicable" + comment с
  указанием источника.
- Оценивай прогресс относительно prior_context. Ключевые метрики —
  в блоке stage_progression.
- Если клиент изменил позицию (раньше говорил "3 спальни", теперь
  "2") — зафиксируй в profile_updates.
- Используй имена, проекты, детали из prior_context. Звонок №N —
  не первое знакомство.
```

**Выбор схемы:** `use_extended` теперь расширяется:
- Если есть scenario.prompt + нет prior_context → V3 (как сейчас).
- Если есть scenario.prompt + есть prior_context → V4.

**Score_version:** `4` в результате.

## 1.5 Next-call plan (LLM call 2)

**Новая функция:** `worker/tasks/quality.py::plan_next_call(prior_context, current_quality_report, deal_stage=None)`.

Во Фазе 1 `deal_stage=None` (во Фазе 2 подтянем из AmoCRM).

**Схема `NEXT_CALL_PLAN_SCHEMA`:**

```python
{
    "name": "next_call_plan",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "goals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "priority": {"type": "integer"},
                        "text": {"type": "string"}
                    },
                    "required": ["priority", "text"],
                    "additionalProperties": False
                }
            },
            "unresolved_objections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "suggested_response": {"type": "string"},
                        "priority": {"type": "string"}
                    },
                    "required": ["text", "suggested_response", "priority"],
                    "additionalProperties": False
                }
            },
            "information_gaps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "why": {"type": "string"}
                    },
                    "required": ["field", "why"],
                    "additionalProperties": False
                }
            },
            "talking_points": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "argument": {"type": "string"},
                        "personalized_hook": {"type": "string"}
                    },
                    "required": ["topic", "argument", "personalized_hook"],
                    "additionalProperties": False
                }
            },
            "recommended_properties": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string"},
                        "reason": {"type": "string"}
                    },
                    "required": ["project", "reason"],
                    "additionalProperties": False
                }
            },
            "risks": {"type": "array", "items": {"type": "string"}},
            "suggested_opener": {"type": "string"}
        },
        "required": ["goals", "unresolved_objections", "information_gaps",
                     "talking_points", "recommended_properties",
                     "risks", "suggested_opener"],
        "additionalProperties": False
    }
}
```

**Входные данные в LLM (без транскрипта — экономим токены):**
- System prompt: «Ты — consultative-sales coach для брокера элитной
  недвижимости Москвы. Построй план следующего взаимодействия».
- User payload: JSON `{prior_context, current_call: quality_report, deal_stage: null}`.

**Когда вызываем:**
- `use_extended=True` (есть scenario.prompt).
- `classification != "brushoff_short"` (на brush-off'ах план бесполезен).
- `skip_reason != "too_short"`.

**Запись в AmoCRM — ВТОРОЙ note, append:**

В `_push_to_amocrm` после создания/апдейта основной note:
```python
if plan_report:
    plan_text = format_next_call_plan(plan_report)
    create_note(lead_id, note_type="common", text=plan_text)
```

Формат (markdown-стиль, AmoCRM сам отрендерит новые строки):

```
📋 План следующего звонка (AI)

🎯 Цели:
1. Организовать просмотр ЖК X в четверг
2. Уточнить верхнюю границу бюджета

❗ Открытые возражения:
• «Нужно посоветоваться с женой» (высокий приоритет)
  → Предложить совместный визит на объект

🔍 Чего не хватает:
• Готовность к депозиту (одобрение есть — сколько сразу?)

💬 Аргументы:
• Видовые характеристики
  ЖК X секция B — прямой вид на Москва-реку
  (клиент дважды упоминал важность вида)

🏢 Рекомендуем показать:
• ЖК X — бюджет совпадает, локация совпадает, вид есть
• ЖК Y — запасной вариант

⚠️ Риски:
• Клиент рассматривает вторичку — конкурируем не только с новостройками

🗣️ Фраза для начала:
«Добрый день, Иван! В четверг в 15:00 показ в ЖК X. Подъеду
к подъезду — удобно?»
```

Сохраняем `plan_amo_note_id` **и сам структурированный `next_call_plan`**
в `sessions.metadata` — второе потребуется во Фазе 2 для follow-through.

**Обработка ошибок LLM call 2:** если `plan_next_call` упал с исключением
или вернул невалидный JSON — логируем warning, НЕ роняем pipeline,
evaluation-note всё равно публикуем. Отсутствие плана не должно блокировать
основную оценку.

**Важно:** при reprocess того же звонка (Фаза 1) план перегенерируется
и пишется НОВОЙ заметкой. Это допустимо во Фазе 1. Во Фазе 2, когда
появится follow-through, потребуется политика «при reprocess обновляем
существующий план, не плодим». Обсудим во Фазе 2, пока так.

## Testing (Фаза 1)

- **1.1 short-call:** юнит-тест `_run_pipeline` с моком ffprobe → 15с.
  Проверить: transcribe не вызван, quality_report содержит skip_reason.
- **1.1 integration:** прогнать `note_id=46048433` (5с) через reprocess.
  Проверить: в AmoCRM появился короткий note, tag не добавлен.
- **1.2 reprocess:** curl lead_id=11425275, `force=false` → 2 queued.
  Второй вызов → 2 already_processed. С force=true → 2 queued.
- **1.2 UI:** открыть админку, вбить lead_id, убедиться в отображении.
- **1.3 prior_context:** юнит-тест merge'а двух quality_report в profile.
- **1.4 context-aware eval:** прогнать второй звонок на одном лиде,
  проверить `stage_progression` в output, `not_applicable_already_done`
  в чек-листе.
- **1.5 plan:** проверить, что после второго звонка в AmoCRM появляются
  ДВА note подряд: eval + план.
- **1.5 e2e:** прогнать 1050с звонок по сделке 11425275 через
  reprocess → верифицировать оба note в AmoCRM.

## Migrations (Фаза 1)

Нет. Все изменения — код и JSON-схемы LLM. `sessions`, `amocrm_calls`
не меняются.

---

# Фаза 2

Стартует после выкатки Фазы 1 в прод и пары дней «щупанья».

## 2.1 Follow-through tracking

**В схеме V4 добавляется блок:**

```python
"previous_recommendations_follow_through": {
    "total_recommendations": "integer",
    "executed_count": "integer",
    "items": [
        {
            "recommendation_text": "string",
            "executed": "boolean | partially",  # enum: yes/no/partial
            "evidence": "string | null",
            "effectiveness": "string | null"
        }
    ]
}
```

**Сбор данных:**
Перед LLM call 1 достаём план ПРЕДЫДУЩЕГО звонка из
`sessions.metadata.plan_amo_note_id` или напрямую из prior_context
(сохраняем `next_call_plan` результат в `sessions.metadata.next_call_plan`).

Передаём в system prompt: «В prior_context есть `last_call_plan` —
рекомендации, которые были даны после предыдущего звонка. Для каждой
рекомендации определи, выполнена ли она в текущем звонке, подтверди
цитатой из транскрипта».

**Follow-through измеряем ТОЛЬКО по плану непосредственно предыдущего
звонка.** Долги по старым рекомендациям подтягиваются в НОВЫЙ план
через `talking_points.personalized_hook`: «напомни про Y —
рекомендовалось 3 звонка назад».

**Отображение:**
- В AmoCRM eval-note добавляется блок «✅ Выполнено 3 из 5 рекомендаций
  прошлого звонка» с разбивкой.
- В UI `backend/static/app.js::renderCallDetail` — новая карточка
  follow-through со значком ✅/❌ для каждого пункта.

## 2.2 Deal-summary note

**Новая таблица:**

```sql
CREATE TABLE amocrm_deal_summaries (
    lead_id BIGINT PRIMARY KEY,
    amo_note_id BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content_json JSONB NOT NULL  -- последний сформированный summary
);
CREATE INDEX idx_deal_summaries_updated ON amocrm_deal_summaries(updated_at);
```

Alembic-миграция: `003_amocrm_deal_summaries.py`.

**Генерация summary:**

После каждого non-short звонка (в `_push_to_amocrm`):
1. Собираем `deal_summary` из prior_context + current_quality_report
   (детерминированная функция, без LLM — это агрегация, не интерпретация).
2. Форматируем в markdown-текст.
3. Если `amocrm_deal_summaries.lead_id=X` существует →
   `update_note(lead_id, amo_note_id, text)`.
4. Если нет → `create_note`, upsert в таблицу.

**Формат заметки:**

```
🏠 Сводка по сделке (AI, обновлена 2026-04-17 14:23)

📊 Этап: Квалификация
📞 Контактов: 3 звонка + 1 встреча

👤 Профиль клиента:
• Цель — жильё
• Локация: Хамовники, Остоженка
• Формат: 3 спальни, 120+ м²
• Бюджет: 20-25 млн (пересмотрено 2026-04-10, было 18М)
• Оплата: ипотека, одобрено 22М
• Сроки: полгода

📅 История:
• 2026-04-03 (звонок, 14 мин) — Первый контакт, сняли базу
• 2026-04-10 (звонок, 20 мин) — Назначили встречу
• 2026-04-15 (встреча офлайн, данных нет) — между звонками
• 2026-04-17 (звонок, 17 мин) — Обсуждали впечатления

❗ Открытые возражения:
• «Хочет увидеть вид с южной стороны»

✅ Закрытые возражения:
• «Дорого» — показали рассрочку (2026-04-10)
• «Нужно посоветоваться с женой» — клиент подтвердил согласие
  (2026-04-17)

➡️ Следующие шаги (из последнего плана):
• Показ ЖК X секция B, четверг 15:00
```

**При reprocess:** тоже обновляем (идемпотентно).

## 2.3 Stage-aware рекомендации

**AmoCRM API:**
- `GET /api/v4/leads/pipelines` — список пайплайнов со стадиями.
  Кэшируем в памяти воркера с TTL 1 час.
- `GET /api/v4/leads/{lead_id}` → `status_id`, `pipeline_id`.
- Резолвим в `stage_name` через кэш.

**Добавляем в prior_context:**
```python
"current_deal_stage": {
    "pipeline_name": "Квартиры",
    "stage_name": "Квалификация",
    "stage_id": 12345
}
```

**Промпт плана дополняется:**

```
## Стадия сделки

Текущий этап: «{stage_name}». Рекомендации должны быть уместны
для этого этапа:
- «Квалификация»: фокус на discovery-вопросах, глубже узнавать
  потребности, не форсировать встречу.
- «Показы»: аргументы по конкретным объектам, отработка
  «ещё посмотрю другое», договорённости о следующем показе.
- «Сделка/Договор»: closing-сценарии, детали оформления,
  снятие финальных возражений.
- (для других стадий — используй здравый смысл)
```

## 2.4 Offline-gap awareness

**Расширяем `interactions_history`:**

Теперь включаем не только call_in/call_out из наших `quality_report`, но
ВСЕ события на лиде из AmoCRM:
- `incoming_call` / `outgoing_call` — наши звонки (есть данные).
- `common_note_added` (не от нас) — возможно, офлайн-встреча или
  руками написанный комментарий брокера.
- `lead_status_changed` — смена этапа.

**Новый тип `offline_gap_inferred`:**

Если `lead_status_changed` произошёл в период, когда НЕ было наших
звонков — добавляем запись:
```python
{
    "date": "2026-04-15",
    "type": "offline_gap_inferred",
    "has_data": False,
    "detail": "Этап сменился Квалификация → Показы без звонка в системе"
}
```

**Промпт LLM (дополнение):**

```
Между некоторыми взаимодействиями могут быть периоды, где у нас
нет данных (офлайн-встречи, мессенджеры). Элементы
interactions_history с type="offline_gap_inferred" или
has_data=false означают, что там была коммуникация — не делай
выводов из их отсутствия. Если в текущем звонке клиент ссылается
на обсуждение, которого нет в истории — предположи, что оно
было в офлайн-формате.
```

## 2.5 Redis-lock по lead_id

**Зачем:** два параллельных звонка на один лид могут одновременно читать
`prior_context` и записывать результаты — race condition.

**Реализация:**
В `worker/tasks/pipeline.py::_run_pipeline`:

```python
import redis
from contextlib import contextmanager

_redis = redis.from_url(os.getenv("REDIS_URL"))

@contextmanager
def _lead_lock(lead_id: int, timeout: int = 600):
    if not lead_id:
        yield
        return
    key = f"lead_lock:{lead_id}"
    token = str(uuid.uuid4())
    acquired = _redis.set(key, token, nx=True, ex=timeout)
    while not acquired:
        time.sleep(1)
        acquired = _redis.set(key, token, nx=True, ex=timeout)
    try:
        yield
    finally:
        # Lua-скрипт для безопасного del только своего токена
        _redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end",
            1, key, token
        )
```

В `_run_pipeline`:
```python
lead_id = session_meta.get("lead_id")
with _lead_lock(lead_id):
    # ... существующая логика ...
```

Timeout 600с с запасом покрывает транскрипт+диар+LLM — если pipeline
уходит в ступор, lock сам expire, следующий звонок подхватит.

## Testing (Фаза 2)

- **2.1:** два последовательных звонка на одном лиде. Проверить, что в
  eval-отчёте второго есть `previous_recommendations_follow_through`
  с корректным подсчётом.
- **2.2:** первый звонок на лиде → создаётся amocrm_deal_summaries row
  и note. Второй звонок → тот же note обновлён, row.updated_at свежий.
- **2.3:** подменить `status_id` у тестового лида, пересобрать план —
  убедиться, что промпт получил правильное stage_name.
- **2.4:** лид со сменой этапа между звонками без покрытия → в
  interactions_history присутствует `offline_gap_inferred`.
- **2.5:** параллельный запуск двух `process_amocrm_call` на один lead_id —
  убедиться, что один ждёт другого (через логи воркера).

## Migrations (Фаза 2)

`003_amocrm_deal_summaries.py` — создаёт таблицу.

---

## Известные риски и открытые вопросы

### Риски

- **Стоимость GPT.** C2 (+30-40% от вызова 1) × рост входного промпта
  (prior_context для горячих лидов). Прикидка: при 100 звонках/день
  месячные расходы на GPT-5.4 могут вырасти в 1.5-2х.
  **Митигация:** мониторинг token usage; при росте — материализовать
  lead_profile в БД и не слать всю историю каждый раз.
- **LLM-галлюцинации в плане.** Рекомендация «покажи ЖК X» без
  фактических данных о ЖК может выдумать детали (виды, этажи).
  **Митигация:** в промпте жёстко: «не выдумывай факты об объектах —
  только то, что клиент сам озвучил». V2 — подключить catalog ЖК как
  грунд-трасс.
- **Устаревший prior_context при reprocess.** При перезапуске старого
  звонка контекст формируется из звонков, которые были ПОСЛЕ него
  (если смотреть по absolute date), что ломает логику. **Митигация в
  дизайне:** фильтр `created_at < current.created_at`.

### Открытые вопросы (решать перед/в процессе Фазы 2)

- Политика reprocess для плана в Фазе 2: обновлять существующий
  plan note или создавать новый? (Сейчас — создаём новый. Это
  дублирует заметки при reprocess. Кандидаты решения: сохранять
  `plan_amo_note_id` в sessions.metadata и при reprocess — update.)
- Формат `deal_summary` при очень длинной истории (>20 звонков):
  схлопывать старое в «ранняя история: X звонков, ключевые факты».

---

## Implementation status

**Фаза 1 — ✅ Завершена 2026-04-17.** Реализация по плану
`docs/superpowers/plans/2026-04-17-multi-call-phase1.md`.

Итоговые коммиты (ветка `dev`): `67bdc42` → `37d54bd` (15 коммитов,
включая два bugfix-а поверх плана: `c86debf` — роутинг задач на queue
`transcription` вместо default «celery»; `37d54bd` — resolve lead_id
через phone до построения prior_context для случая, когда call-note
висит на контакте, а не на лиде).

**End-to-end смоук** прогнан на сделке 11425275 через `/api/amocrm/reprocess`:

- Короткий звонок 5с (`note_id=46048433`) → pipeline exited за 2.5с,
  transcribe/diarize/sentiment/LLM пропущены, в AmoCRM создана короткая
  заметка «⚠️ Короткий звонок (5с). Оценка не проводилась…».
- Длинный звонок 17.5 мин (`note_id=46240441`), 3-й прогон (с prior-context
  из двух предыдущих прогонов):
  - `score_version: 4` — использована V4-схема.
  - `stage_progression` заполнен (`new_info_learned`, `progress_delta`,
    `objections_resolved`/`raised`).
  - `applicable_checklist_items.skipped_as_already_done` заполнен.
  - `overall_score: 8` (на V3-прогоне без контекста — 7; модель учла,
    что брокер не обязан представляться в 5-м звонке).
  - `plan_next_call` вернул 3 цели, 4 открытых возражения, 6 talking
    points, 3 рекомендуемых ЖК, персонализированный `suggested_opener`
    с именем клиента.
  - В AmoCRM созданы ДВА note подряд: обогащённая оценка + план
    следующего звонка (`note_id=46332009` + `46333507`).

**Unit-тесты:** 14 pytest-тестов проходят
(`test_short_call.py`, `test_prior_context.py`, `test_quality_schemas.py`).

**Restore point:** `pre-multicall-2026-04-17` (tag), коммит `4f8c75a`.

**Фаза 2 — ✅ Завершена 2026-04-17.** Реализация по плану
`docs/superpowers/plans/2026-04-17-multi-call-phase2.md`.

Итоговые изменения:

- **Redis-lock по lead_id** — `worker/tasks/lead_lock.py` + `_run_pipeline_inner`
  extraction (чтобы не переиндентировать всё тело).
- **V4 `previous_recommendations_follow_through`** — LLM сверяет текущий
  звонок с планом прошлого, выдаёт `yes | no | partial` по каждой
  рекомендации. Рендерится и в AmoCRM-note, и в админ-UI (✅ ❌ ~).
- **`prior_context.last_call_plan`** — читается из
  `<RESULTS_PATH>/<prev_session>/next_call_plan.json`; в `prior_context`
  кладётся самый свежий план из прошлых.
- **Deal-summary note** (3-я заметка, singleton на сделку) — таблица
  `amocrm_deal_summaries (lead_id PK, amo_note_id, updated_at,
  content_json)`, миграция 003. Первый раз `create_plain_note`, затем
  `update_note` по сохранённому `amo_note_id` — чистый update-in-place.
- **Stage-aware рекомендации** — `GET /api/v4/leads/pipelines` с
  in-memory кэшем (TTL 1 час), `get_lead_stage(lead_id)` резолвит
  `pipeline_id`+`status_id` → имя. Stage-name прокидывается в
  `plan_next_call` через `deal_stage=...`; промпт плана содержит
  развилку по стадиям «Квалификация/Показы/Сделка/Договор».
- **Offline-gap inferred** — `fetch_lead_events(lead_id)` достаёт
  `lead_status_changed` события. Смена этапа в день без нашего звонка
  помечается `offline_gap_inferred` в `interactions_history`. LLM
  инструктирован не делать выводов из отсутствия данных в таких
  периодах.

**End-to-end смоук** на сделке 11425275:

- Первый прогон (call #6): созданы три заметки — eval `46344529`,
  plan `46344531`, summary `46344533`.
  `score_version: 4`, `stage_progression: True`,
  `applicable_checklist_items: True`,
  `previous_recommendations_follow_through: 6/9` (LLM определил, что
  брокер выполнил 6 из 9 рекомендаций прошлого плана).
  `deal_stage = Клиенты / Квалификация` (через pipelines cache).
- Второй прогон (call #7, 6 prior, 19 open objections): plan-note и
  eval-note новые (46344559/46344561); summary-note **обновлён
  in-place**: `amo_note_id` остался `46344533`, `updated_at`
  18:04:28 → 18:07:59.

**Unit-тесты:** 29 pytest проходят
(`test_short_call.py`, `test_prior_context.py`, `test_quality_schemas.py`,
`test_lead_lock.py`, `test_deal_summary.py`). Регрессий нет.

**Ручные фиксы по код-ревью:** timeout-escape test для lead_lock
(`3492149`).

**Что осталось на будущее (вне Phase 2):**
- Материализованный `lead_profile` для горячих лидов (>50 звонков) —
  YAGNI сейчас, добавим когда упрёмся в токен-бюджет.
- `common_note_added` как отдельный источник истории (в v1 используем
  только `lead_status_changed`).
- Zoom-встречи: структура `interactions_history.type` готова, нужен
  только pipeline для аудио-дорожек Zoom.
