# Шаблоны извлечения и база ЖК

**Статус:** дизайн утверждён, готов к плану реализации
**Дата:** 2026-04-27
**Контекст:** brainstorm от 2026-04-27

## Цель

Добавить второй путь обработки записей — **структурированное извлечение фактов** (для презентаций ЖК от застройщика или брокера) — параллельно к существующей оценке звонка. Накапливать базу ЖК с автоматической агрегацией данных из множественных записей по одному комплексу.

## Что не делаем (out of scope)

- Визуальный form-builder для JSON Schema (только textarea с валидацией)
- Fuzzy/нечёткий матч ЖК (Levenshtein/embeddings) — только нормализация строк
- История версий шаблона
- Сравнение двух ЖК side-by-side
- Экспорт обучающего корпуса (CSV/JSONL) — добавим когда соберётся достаточно профилей
- Batch-перепрогон (выбрать N сессий → одна кнопка)
- Авто-определение типа записи по контенту транскрипта

## Архитектура

### Модель данных

Три новые таблицы:

**`extraction_templates`** — справочник шаблонов извлечения.

| Колонка | Тип | Описание |
|---|---|---|
| `id` | UUID PK | |
| `name` | VARCHAR(200) | Уникальное человекочитаемое имя |
| `description` | TEXT | Краткое описание |
| `kind` | VARCHAR(20) | `extraction` (на старте только этот) или `evaluation` (задел) |
| `prompt` | TEXT | Системный промпт для LLM |
| `json_schema` | JSONB | Валидная JSON Schema, описывающая структуру вывода |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

При миграции сидируем одной записью «Презентация ЖК» (см. ниже).

**`complex_extractions`** — одна запись = один сырой extraction из одной сессии.

| Колонка | Тип | Описание |
|---|---|---|
| `id` | UUID PK | |
| `session_id` | UUID FK → `sessions.id` ON DELETE CASCADE | |
| `template_id` | UUID FK → `extraction_templates.id` ON DELETE RESTRICT | |
| `complex_id` | UUID FK → `complexes.id` ON DELETE SET NULL, nullable | проставляется после матчинга |
| `raw_data` | JSONB | то что вернул LLM по схеме шаблона |
| `created_at` | TIMESTAMPTZ | |

**`complexes`** — агрегированный профиль ЖК.

| Колонка | Тип | Описание |
|---|---|---|
| `id` | UUID PK | |
| `name` | VARCHAR(300) | каноническое название (последнее видевшееся) |
| `name_normalized` | VARCHAR(300) | для матчинга, индекс |
| `developer` | VARCHAR(200) | |
| `developer_normalized` | VARCHAR(200) | для матчинга, индекс |
| `class` | VARCHAR(50) | например «бизнес», «комфорт», `null` если неизвестно |
| `district` | VARCHAR(200) | для фильтра |
| `aggregated_data` | JSONB | мёрджнутый профиль (вся схема) |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

Уникальный индекс: `UNIQUE(name_normalized, developer_normalized)` — по нему ищем матч.

Каскады:
- `chunks.session_id ON DELETE CASCADE` — уже есть
- `complex_extractions.session_id ON DELETE CASCADE` — добавляем
- `complex_extractions.complex_id ON DELETE SET NULL` — отвязка при удалении профиля
- После удаления extraction — пересчитать агрегат у её `complex`; если у профиля не осталось extraction-ов — удалить и его (логика в коде, не в БД).

### Хранение на диске

Помимо БД, сырой результат LLM пишем в `${RESULTS_STORAGE_PATH}/{session_id}/extraction.json` — для возможности глазами проверить и для восстановления БД из файлов.

### Pipeline

`metadata.template_id` сохраняется в сессии при создании. Отсутствует = «дефолт компании» (текущий путь оценки звонка).

В `pipeline.process_session`:

1. **Транскрипция** — без изменений, для всех путей.
2. **Диаризация** — без изменений, для всех путей (на презентации тоже полезно: презентер + Q&A).
3. **Развилка по `template.kind`:**
   - `evaluation` (или `template_id is null`): текущий путь — `quality.py` + `sentiment.py` + amocrm sync. Без изменений.
   - `extraction`: новый таск `extract.py`:
     1. Загружает транскрипт + speaker map
     2. Загружает `template` из БД (`prompt` + `json_schema`)
     3. **Safety cap:** если транскрипт > 500 000 токенов (грубая оценка `len(text) / 4`) — статус `failed`, сообщение в `metadata.error` про подозрительно длинный транскрипт. Иначе передаём целиком (контекст gpt-5.4 — 1.05M токенов).
     4. Зовёт OpenAI Responses API с `response_format = { type: "json_schema", schema: <template.json_schema>, strict: true }`. Модель — `gpt-5.4`. Получаем гарантированно валидный JSON.
     5. Сохраняет в `${RESULTS_STORAGE_PATH}/{session_id}/extraction.json`.
     6. Создаёт строку в `complex_extractions` с `raw_data`.
     7. Запускает `match_or_create_complex` (см. ниже).
     8. Sentiment и quality для `extraction` НЕ запускаются.
4. **Финализация** — `sessions.status = completed`.

### Матчинг и агрегация

`match_or_create_complex(extraction_id)`:
1. Берёт `name` и `developer` из `raw_data`.
2. Нормализация (`_normalize_complex_name` и `_normalize_developer`):
   - lowercase
   - strip ведущие префиксы: `жк`, `жилой комплекс`, `жилой район`, `мфк`, `апарт-комплекс`
   - strip кавычки (`«»`, `""`, `''`, `""`)
   - заменить `-`, `_`, `.` на пробел; collapse whitespace
   - strip leading/trailing пунктуация
   - нормализация `ё → е` (так что «Шагалёв» матчится с «Шагалев»)
3. SELECT в `complexes` по `(name_normalized, developer_normalized)`.
4. Найден → обновить `aggregated_data`, проставить `complex_id` в extraction, обновить `updated_at`.
5. Не найден → INSERT в `complexes` со стартовыми значениями (= данные этой extraction), проставить `complex_id`.

`recompute_aggregate(complex_id)` — берёт все `complex_extractions` этого `complex_id`, сортирует по `created_at` DESC, мёрджит:
- **Скаляр** (string/number/bool): значение из самой свежей extraction, у которой поле не `null` и не пустая строка.
- **Список** (`array`): объединение всех extraction-ов + дедупликация (нормализованным сравнением строк).
- **Объект**: рекурсивно по тем же правилам для каждого ключа.
- **Особый случай `additional_info[]`**: собирается из всех extraction-ов с пометкой `{ session_id, recorded_at, note }` для каждого элемента — это freeform-наблюдения, дедупа нет, нужен контекст.

Реализация — инкрементально: на новую extraction берём текущий agg + новую extraction и мёрджим. Полный rescan делаем только при удалении extraction (когда нужно «откатить» значения, пришедшие из удалённой записи).

### Перепрогон

`POST /api/sessions/{id}/reprocess { template_id }`:
- 409 Conflict если `sessions.status` ∈ {`processing`, `uploading`} — конкурентный запуск запрещён.
- 404 если шаблон не найден.
- Перезаписывает `metadata.template_id`. Если меняется `kind` — старые `quality.json`/`sentiment.json` остаются на диске (история), но дашборд их больше не показывает (рендерится по текущему kind).
- Запускает Celery task `pipeline.process_session_from_transcript` (новый, или флаг в существующем) — **без перетранскрипции**, со стадии диаризации/анализа.

### API endpoints

Все мутирующие endpoints (`POST/PATCH/DELETE` для templates и complexes) защищены тем же заголовком `X-Delete-Password`, что и `DELETE /api/sessions/{id}`.

**Templates:**
- `GET /api/templates` — список (без `prompt`/`json_schema`)
- `GET /api/templates/{id}` — деталь со всем
- `POST /api/templates` — create (валидация `json_schema` через `jsonschema` lib + проверка что схема валидна как JSON Schema)
- `PATCH /api/templates/{id}` — update
- `DELETE /api/templates/{id}` — запрещено если есть `complex_extractions` или `sessions.metadata.template_id` ссылающиеся на шаблон → 409 с подсказкой

**Sessions (расширения):**
- `POST /api/sessions/{id}/upload-audio` — добавляется form-field `template_id` (опциональный)
- `POST /api/sessions/{id}/reprocess` — body `{ template_id }`, возвращает обновлённый статус
- `GET /api/sessions/{id}/extraction` — `{ raw_data, complex_id, template: { id, name } }` или 404

**Complexes:**
- `GET /api/complexes?developer=&class=&district=&q=&limit=&offset=` — список с фильтрами и поиском по name (ILIKE)
- `GET /api/complexes/{id}` — `{ ...complex, sources: [{ session_id, created_at, extraction_id }] }`
- `PATCH /api/complexes/{id}` — обновить только `name` (и `name_normalized` пересчитывается)
- `DELETE /api/complexes/{id}` — `complex_extractions.complex_id = NULL`, удаляем профиль
- `POST /api/complexes/{id}/merge` — body `{ target_complex_id }` — переносит все extraction-ы в target, удаляет источник, пересчитывает агрегат
- `POST /api/extractions/{id}/relink` — body `{ complex_id }` — перепривязать конкретную extraction (исправление auto-match)

### Frontend

**Новая страница «Шаблоны» (`#templates`)**

- Сайдбар: пункт «Шаблоны».
- Список шаблонов с действиями (Редактировать / Удалить).
- Форма создания/редактирования:
  - `name`, `description`, `kind` (dropdown), `prompt` (textarea), `json_schema` (textarea с моноширинным шрифтом).
  - Кнопка «Validate JSON Schema» — клиентская проверка через AJV или серверный `POST /api/templates/_validate { json_schema }`.

**Страница «Загрузка» (существующая)**

- Над полем «Файл» — dropdown «Шаблон обработки»: «Звонок брокера (по умолчанию)» + список шаблонов из БД.
- Выбранный `template_id` → form-field в upload запросе.

**Страница сессии (детали)**

- В шапке детали — бейдж «Шаблон: {name}» (если задан).
- В кнопках сверху — добавить «Перепрогнать с шаблоном…» (split-button или dropdown с подтверждением).
- Если `kind = extraction`:
  - Скрыть текущие секции Анализ/Sentiment.
  - Показать секцию «Профиль ЖК» с полями по схеме (универсальный рендер: пара label/value, списки — bullet-list).
  - Кнопка «Перепривязать к другому ЖК…» (POST relink).
  - Кнопка «Открыть профиль ЖК» → `#complex/{complex_id}`.

**Новая страница «База ЖК» (`#complexes`)**

- Сайдбар: пункт «База ЖК».
- Фильтры: `developer` (select из distinct), `class` (select), `district` (text), сортировка.
- Карточки: name + class-бейдж + developer + N-источников + превью.
- Клик → деталь `#complex/{id}`:
  - Полный профиль из `aggregated_data`.
  - Сайдбар «Источники» — список сессий с датами.
  - Действия: Переименовать / Удалить / Слить с другим…

### Pre-seeded шаблон «Презентация ЖК»

JSON Schema, отражающая 7-пунктовый чек-лист пользователя:

```json
{
  "type": "object",
  "properties": {
    "name":      {"type": ["string", "null"], "description": "Название ЖК"},
    "developer": {"type": ["string", "null"], "description": "Название застройщика"},
    "class":     {"type": ["string", "null"], "description": "эконом / комфорт / комфорт+ / бизнес / премиум / элит"},
    "location": {
      "type": "object",
      "properties": {
        "district":      {"type": ["string", "null"]},
        "address":       {"type": ["string", "null"]},
        "parks":         {"type": "array", "items": {"type": "string"}},
        "embankments":   {"type": "array", "items": {"type": "string"}},
        "transport":     {"type": "array", "items": {"type": "string"}, "description": "Метро/автобусы/дороги, время до точек"},
        "malls":         {"type": "array", "items": {"type": "string"}},
        "venues":        {"type": "array", "items": {"type": "string"}, "description": "Театры, рестораны и т.п."},
        "future_plans":  {"type": ["string", "null"], "description": "Что будет в районе в будущем"}
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
        "parking":          {"type": ["string", "null"], "description": "Описание паркинга"},
        "engineering":      {"type": "array", "items": {"type": "string"}, "description": "VRV, фанкойл, очистка воздуха/воды и т.п."},
        "yard": {
          "type": "object",
          "properties": {
            "area_ha":   {"type": ["number", "null"]},
            "zones":     {"type": "array", "items": {"type": "string"}, "description": "Отдых / спорт / детские / ландшафт"}
          }
        }
      }
    },
    "delivery": {
      "type": "object",
      "properties": {
        "overall_year":  {"type": ["integer", "null"]},
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
        "options":      {"type": "array", "items": {"type": "string"}, "description": "без / white box / чистовая / дизайнерская"},
        "design_styles": {"type": "array", "items": {"type": "string"}}
      }
    },
    "pricing": {
      "type": "object",
      "properties": {
        "cash":         {"type": ["string", "null"], "description": "Цена при 100% оплате"},
        "mortgage":     {"type": ["string", "null"], "description": "Условия по ипотеке"},
        "installment":  {"type": ["string", "null"], "description": "Условия по рассрочке"},
        "min_price":    {"type": ["number", "null"], "description": "Минимальная цена квартиры (если упоминалась)"},
        "currency":     {"type": ["string", "null"]}
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
```

`prompt` (краткое содержание, финальный текст пишется при имплементации):

> Ты извлекаешь структурированные факты о жилом комплексе из транскрипта презентации. Заполни поля JSON Schema. Если факт не упомянут — оставь `null` (для скаляров) или `[]` (для массивов). НЕ выдумывай. В `additional_info` собери всё значимое, что не попало в стандартные поля.

## Риски / неочевидные места

1. **Длинные транскрипты.** GPT-5.4 имеет 1.05M context. Реальный максимум у нас — 78-минутная запись (~30k tokens). Никакой обрезки/чанкинга. Safety cap 500k tokens → `failed`.

2. **Нормализация русских названий ЖК.** Кавычки разные, регистр, числа в названиях («Шагал-2»), `ё/е`. Реализуем `_normalize_complex_name()` с unit-тестами на корпусе реальных названий из БД.

3. **Каскад при удалении.** `complex_extractions.session_id ON DELETE CASCADE` + после-удалительный пересчёт агрегата (если у профиля 0 extractions — удалить профиль).

4. **JSON Schema валидация.** При сохранении шаблона — проверять, что `json_schema` сама по себе валидна как JSON Schema (не просто JSON) через `jsonschema` lib.

5. **Reprocess vs running task.** 409 Conflict если `status ∈ { processing, uploading }`.

6. **Шаблон с привязанными сессиями.** Удаление шаблона — 409 если на него ссылается хотя бы одна сессия или extraction. Альтернатива (отвязать) — слишком разрушительная для обучающего корпуса.

7. **Существующий редактор сценариев vs новые шаблоны.** Сейчас в `companies/realestate.json` есть `scenarios[]` для оценки звонков. Это **отдельная сущность** от `extraction_templates`. В этой итерации **не трогаем** — `evaluation` kind в новой таблице оставляем как задел. **Сразу следующей задачей** мигрируем сценарии в `extraction_templates` (с `kind=evaluation`), переключим текущий пайплайн оценки на новую таблицу, и удалим параллельный механизм в `companies/*.json`. Это вынесено в отдельную спеку, чтобы текущая итерация была фокусированной и не задевала рабочий путь оценки звонков.

## Контрольный список реализации

(Финальный план — отдельным документом через `writing-plans`.)

- [ ] Миграция БД: 3 таблицы + индексы + сидирование «Презентация ЖК»
- [ ] `worker/tasks/extract.py` + интеграция в `pipeline.py`
- [ ] `worker/tasks/complex_match.py` + нормализация + recompute_aggregate
- [ ] Backend routes: templates CRUD, sessions extension, complexes API
- [ ] Frontend: страница «Шаблоны»
- [ ] Frontend: dropdown шаблона на загрузке
- [ ] Frontend: «Перепрогнать с шаблоном» + рендер «Профиль ЖК» в деталях
- [ ] Frontend: страница «База ЖК» + страница профиля комплекса
- [ ] Защита `X-Delete-Password` на мутирующих endpoint-ах
- [ ] Unit-тесты на нормализацию имён + матчинг + агрегацию
- [ ] e2e тест на 3 реальных файлах (которые у пользователя есть готовые)
