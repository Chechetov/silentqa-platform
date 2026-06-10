# Система оценки звонков — обзор архитектуры

*Срез на 2026-04-21. Пересматривать при существенных изменениях в пайплайне или схеме оценки.*

## 1. Обзор

Система принимает записи разговоров из трёх источников, прогоняет их через единый целеryй пайплайн (транскрипт → диаризация → sentiment → LLM-оценка → план следующего звонка) и пишет результаты обратно в AmoCRM в виде текстовых note'ов на сделке. Продакшн-процессы:

- `realestate-backend.service` — FastAPI (`uvicorn app.main:app`, `127.0.0.1:8002`), REST API приёма сессий.
- `realestate-worker.service` — Celery worker + Beat (`concurrency=4`, очереди `default,transcription`), все фоновые задачи.

Redis — брокер/бэкенд Celery. Postgres хранит сессии и метаданные AmoCRM-интеграции. Аудио и результаты оценки лежат на диске (не в БД).

## 2. Источники записей и привязка к сделке

| Источник | Ключевые файлы | Как попадает запись | Привязка к сделке AmoCRM |
|---|---|---|---|
| **AmoCRM polling** | `worker/tasks/amocrm_poll.py:232` (`poll_amocrm_calls`, beat каждые 5 мин), `worker/tasks/amocrm_sync.py:125` (`get_recent_call_events`) | Beat-задача опрашивает `/api/v4/events` на события `incoming_call` / `outgoing_call`, забирает `notes/{id}`, скачивает `note.params.link` в `/data/audio/amocrm/{note_id}/original.{ext}` | **Прямая.** `lead_id` берётся из `event.entity_id` (если `entity_type=lead`). Для `entity_type=contact` — резолв через `find_lead_by_phone()` (`amocrm_sync.py:291`) |
| **Desktop-app (Electron, системный звук + мик)** | `desktop-app/src/renderer/recorder.js:125` | Запись WebM/Opus чанками по 10с → `POST /api/sessions` → серия `POST /api/sessions/{id}/chunks` → `POST /api/sessions/{id}/finish` → celery enqueues `pipeline.process_session` | **Не выставляется при загрузке.** metadata = `{source, platform, recordedAt}`. Привязка сработает только если в metadata окажется `phone` — тогда `find_lead_by_phone` в пайплайне (`pipeline.py:536-541`) |
| **Chrome/Yandex extension (tab audio + мик)** | `extension/offscreen.js:89`, `extension/background.js:20`, `extension-yandex/` (идентичный код) | Тот же чанковый протокол. metadata = `{source, tabId, recordedAt}` | **Не выставляется при загрузке.** Авто-детекции AmoCRM-виджета или контекста сделки в расширении нет |

### Ручная привязка постфактум

`POST /api/amocrm/reprocess {lead_id, force?}` (`backend/app/routes/amocrm.py:61`) — сканирует лид и все его контакты, находит все note'ы типов `call_in`/`call_out`, ставит каждую в очередь на повторную обработку. Используется, когда нужно задним числом оценить уже произошедший звонок или перегенерировать оценку после изменения промпта.

### Дедупликация

Таблица `amocrm_calls` с `UNIQUE(amo_note_id)` + `INSERT ... ON CONFLICT DO NOTHING` (`amocrm_poll.py:91`). Safety window 15 мин (`POLL_SAFETY_WINDOW_MINUTES`, коммит `e7b33f9`): курсор `since` всегда откатывается назад, чтобы не пропустить события, которые AmoCRM events API выдал с задержкой. Дубли гасит ON CONFLICT.

### Телефоны брокеров (UIS Com SIM)

**Прямого матчинга брокера по его SIM-номеру нет.** Менеджера определяем двумя способами (`amocrm_sync.py:392` `_resolve_broker_name`):

1. `responsible_user_id` из AmoCRM-note → имя через `/api/v4/users` (кеш в памяти воркера).
2. Fallback: `speaker_roles` из quality_report — роль `manager` распознаёт LLM по содержимому разговора.

## 3. Пайплайн обработки

Оркестрация — `worker/tasks/pipeline.py`, последовательно:

```
1. merge chunks (только для desktop/extension) → full.wav (16 kHz mono, ffmpeg)
2. short-call gate (duration < 20 сек)         → pipeline.py:445   → score_version="short_call_v1"
3. broken-recording detection                   → pipeline.py:106-135 → score_version="broken_recording_v1"
4. transcription (faster-whisper или AssemblyAI — на компанию)
5. diarization (pyannote) + speaker-label merge
6. sentiment (RuBERT)
7. quality assessment (LLM, GPT-5.4 Responses API) → pipeline.py:562-570
8. autosave speaker_roles → session.metadata
9. next-call plan (LLM-вызов №2)                 → pipeline.py:578-597 (только если use_extended и классификация не brushoff_short)
10. push в AmoCRM (call-note + plan-note + rolling deal summary + тег) → pipeline.py:599-607
```

**Short-call gate** — всё, что короче 20 секунд, не транскрибируется: в AmoCRM падает заглушка «⚠️ Короткий звонок (Xс). Оценка не проводилась — вероятно, автоответчик или клиент не ответил.»

**Broken-recording detection** (коммит `b8a52ee`, серверная логика, применима ко всем источникам, но на практике ловит desktop-app) — сэмплирует 5 окон по 10с на позициях 0%/25%/50%/75%/90% длительности. Если начало > −70 dB, а ≥3 из 4 хвостовых окон < −70 dB, сессия флагится как `broken_client_recording` и Whisper+GPT пропускаются. Порог `BROKEN_RECORDING_MIN_DURATION_SEC=60` — мелкие обрезки не трогаем.

## 4. Логика оценки: одна или две оценки?

**В продакшене — одна оценка на звонок, но с выбором схемы.** Никакой «параллельной старой и новой» не бегает.

| Режим | Когда срабатывает | Промпт | Схема | `score_version` |
|---|---|---|---|---|
| **v2 (базовая)** | `use_extended=False` — у компании нет `scenario.prompt` | `SYSTEM_PROMPT` (`quality.py:706`) | `QUALITY_JSON_SCHEMA` (`quality.py:40-114`) — 5 базовых критериев | `2` |
| **v4 (расширенная)** | `use_extended=True` — у сценария есть кастомный prompt (это случай `realestate.json`) | `SYSTEM_PROMPT_V4` (`quality.py:920`) = V3 + CONTEXT_AWARE + MEETING_PLAYBOOK | `QUALITY_JSON_SCHEMA_V4` (`quality.py:308-476`) | `4` |
| `short_call_v1` | длительность < 20с | — | заглушка со `skip_reason="too_short"` | `short_call_v1` |
| `broken_recording_v1` | детектор мёртвого мика | — | заглушка со `skip_reason="broken_client_recording"` | `broken_recording_v1` |

Ключ переключения — одна строка в `pipeline.py:532`:

```python
use_extended = bool(scenario and scenario.get("prompt"))
```

Для компании **realestate** сценарий `outbound_residential` содержит `prompt` → **всегда v4**. Для desktop/extension записей, у которых `company_id` не проставляется, грузится `companies/default.json` (пустой) → **всегда v2**.

**Важный нюанс после коммита `4f8c836`**: v4 больше не зарезервирована под многозвонковые сделки. Даже на первом звонке по сделке, если сценарий с промптом, включается v4 с блоком `meeting_argumentation_assessment` — оценка закрытия на встречу.

Вывод по вопросу «старая + новая оценка одновременно»:

- Для realestate «старой» оценки v2 в проде **уже не бывает** — там всегда v4.
- Для desktop/extension — наоборот, всегда v2 (см. раздел gaps).
- Параллельно v2 **и** v4 на одном звонке в проде не считается.

### Офлайн A/B инструмент

`worker/scripts/reassess_quality.py` и `worker/scripts/compare_reassess.py` — **не** продакшн-путь, а средство валидации промптов:

- `reassess_quality.py` берёт уже обработанную сессию, пере-гоняет `assess_quality(use_extended_schema=True)` с текущим промптом и пишет `quality_v2.json` **рядом** с оригинальным `quality.json` (не перезаписывая).
- `compare_reassess.py` показывает расхождения по критериям, missed opportunities и coaching-value.

Это единственное место, где реально сосуществуют «две оценки», но только оффлайн и по команде оператора.

## 5. Что именно улетает в AmoCRM

Три типа сообщений на лид, всё через `worker/tasks/amocrm_sync.py`:

1. **Enriched call-note (основная оценка).** `create_enriched_note()` @ `amocrm_sync.py:709`, рендер `format_enriched_note()` @ `:409`. В тексте note'а:
   - оценка `N/10`, имя брокера, направление (вх/исх), классификация (brushoff / productive / meeting_scheduled и т.п.);
   - `brief_summary` или полное саммари;
   - извлечённый `client_info` (бюджет, локации, формат квартиры, сроки, форма оплаты, что смотрел);
   - статус и попытки закрытия на встречу;
   - прогресс по чек-листу (%);
   - объекции и качество их отработки;
   - follow-through по прошлому плану (какие рекомендации из предыдущего звонка выполнены);
   - meeting_argumentation блок (если слабый push) — упущенные категории playbook'а с рекомендацией;
   - топ-5 improvement_suggestions;
   - ссылка на дашборд: `{DASHBOARD_BASE_URL}/#call/{session_id}`.
2. **Next-call plan note.** `create_plain_note()`, формат `format_next_call_plan()` @ `amocrm_sync.py:624`. Генерится отдельным LLM-вызовом (`plan_next_call()`), шлётся **только если** `use_extended=True`, не `brushoff_short`, и есть `prior_context`. Содержит: цели, неразрешённые объекции, информационные пробелы, talking points с playbook category_id, рекомендованные ЖК, риски, готовый suggested_opener.
3. **Rolling deal summary.** Singleton-note на лид, обновляется в месте (`deal_summary.py:15` `build_deal_summary` → `push_deal_summary`). Хранится в таблице `amocrm_deal_summaries` (миграция 003).

**Тег** `"AI оценка звонка"` добавляется на лид, если есть `overall_score` (`pipeline.py:414`).

**Кастомные поля и статусы сделки система не трогает** — вся аналитика живёт в тексте note'ов.

### Авторизация и retry

Access token читается из портальной БД `portal.amocrm_tokens.access_token` (`amocrm_sync.py:41` `_read_token_from_portal_db`). Owner этой таблицы — `rogov-partner-portal`, который сам крутит refresh. Воркер только **читает**; локально refresh не делает, чтобы не ломать ротацию refresh_token.

При 401 (`_amo_request` @ `amocrm_sync.py:97-111`) воркер инвалидирует кеш токена, перечитывает из портальной БД и ретраит запрос **один раз**. Это коммиты `b67cd9d` (fallback при 401) и `8c5e8a6` (читать токен только из portal, не рефрешить локально).

### Два слоя «звонок не обработался»

Жалоба «звонок из АМО не обработался» может прилететь из двух независимых багов с одинаковым симптомом — звонка не видно в дашборде. Диагностику начинать **с обоих** запросов:

1. **Слой опроса** — попало ли событие в `amocrm_calls`? `SELECT * FROM amocrm_calls WHERE amo_note_id = X`. Если нет — копать в `get_recent_call_events` / курсор. Раньше курсор шёл по `MAX(created_at)`, события AmoCRM появляются с задержкой и проскакивали мимо. Фикс — `e7b33f9` (откат курсора на `POLL_SAFETY_WINDOW_MINUTES=15` назад, дубликаты гасит `ON CONFLICT (amo_note_id)`).
2. **Слой скачивания** — если строка есть, какое `status / error_message / retry_count`? Запись с comagic.ru может быть готова через 30+ минут после звонка, плюс случаются read-timeout-флейки. Старая `MAX_RETRIES=3` × 5-мин пул = 15 минут окна, дальше `status='failed'` навсегда. Фикс — `b7eee1c` (`MAX_RETRIES=36 ≈ 3ч`, плюс жёсткий потолок `RETRY_MAX_AGE_HOURS=24` чтобы умершие URL не молотили вечно).

Перезагрузить застрявшую строку: `UPDATE amocrm_calls SET retry_count=0, error_message=NULL WHERE id=…`, затем `process_amocrm_call.delay(id)` — следующий поллинг сам её подхватит, ON CONFLICT защищает от дублей сессий.

## 6. Критерии оценки

**Базовые критерии v2** (`DEFAULT_CRITERIA` @ `quality.py:698`, каждый 0–10): `protocol_adherence`, `politeness`, `listening`, `clarity`, `empathy`.

**Расширенная схема v4** добавляет поверх v2 блоки:

- `call_classification` (`brushoff_short | brushoff_with_attempt | partial | productive | meeting_scheduled`);
- `protocol_checklist` — 6 групп (`greeting`, `initiative`, `needs_discovery`, `proposal`, `meeting`, `agreements`), статусы `completed | attempted | not_applicable | not_reached`;
- `objections[]` с `handling_quality` (0–10) и флагом `resolved`;
- `client_info` (цель покупки, локации, формат квартиры, сроки, бюджет, форма оплаты, важные факторы, что смотрел, текущая ситуация, озвученные объекции);
- `stage_progression`, `applicable_checklist_items`, `previous_recommendations_follow_through` — контекст-aware, требуют `prior_context`;
- **`meeting_argumentation_assessment`** (ключевой блок из `4f8c836`): `arguments_used[]` с `category_id` и `effectiveness`, `missed_opportunities[]`, `overall_push_quality` (`strong/adequate/weak/not_applicable`), `meeting_formats_offered[]`.

**Sales playbook** (`worker/prompts/sales_playbook.py`) — 17 категорий аргументов для закрытия на встречу: 7 «прямых» (`portfolio_presentation`, `structured_walkthrough`, `map_overview`, `comparison_and_distinction`, `decision_support`, `walk_through_nuances`, `joint_preparation`) + 10 «ответов на отказ» (`saves_time`, `filter_errors`, `buying_strategy`, `interactive_dialog`, `internal_analytics`, `selection_logic`, `hidden_nuances`, `cases_and_scenarios`, `better_final_selection`, `short_and_flexible`). Инжектится одновременно в `SYSTEM_PROMPT_V4` и в `PLAN_SYSTEM_PROMPT`.

## 7. Хранение данных

### Postgres

- `sessions` (миграция 001): `id, status, created_at, finished_at, duration_seconds, file_size_bytes, metadata JSONB`. Quality report сам по себе **в БД не пишется**.
- `amocrm_calls` (миграция 002): индекс по `(amo_note_id UNIQUE, status, created_at)`. См. drift в разделе 8.
- `amocrm_deal_summaries` (миграция 003): `lead_id PK, amo_note_id, updated_at, content_json JSONB`.

`sessions.metadata` используется как мусорное ведро с ключами: `company_id`, `source` (`amocrm` / `desktop-app` / `chrome-extension`), `amo_source_note_id`, `amo_note_id` (ответный), `plan_amo_note_id`, `responsible_user_id`, `lead_id`, `phone`, `direction`, `scenario_id`, `speaker_map`, `next_call_plan`.

### Файлы

- **Аудио**: `AUDIO_STORAGE_PATH/sessions/{id}/chunk_*.webm` (desktop/extension) или `AUDIO_STORAGE_PATH/amocrm/{note_id}/original.{ext}` (AmoCRM).
- **Результаты оценки**: `RESULTS_STORAGE_PATH/{session_id}/` — `transcript_raw.json`, `transcript.json`, `diarization.json`, `sentiment.json`, `quality.json`, `next_call_plan.json` (опционально), `quality_v2.json` (только после офлайн `reassess_quality`).

## 8. Known gaps / дрейф

1. **Схема `amocrm_calls` дрейфит от Alembic.** Колонка `responsible_user_id integer` присутствует в живой БД (подтверждено `\d amocrm_calls`), но **ни в одной миграции её нет** (`grep -r responsible_user_id backend/alembic/versions` — 0 совпадений). Добавлена руками после миграции 002. На текущей БД работает, но при деплое с нуля по миграциям вставка в `amocrm_calls` упадёт. **Фикс**: новая миграция `004_amocrm_calls_responsible_user_id.py` с `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
2. **Нет UI для привязки desktop/extension записи к сделке.** Ни клиентские приложения, ни дашборд не дают выставить `lead_id` или `phone` при загрузке. Если телефон не попал в metadata — сессия остаётся сиротой, в AmoCRM ничего не пишется. Единственный обходной путь — узнать `lead_id` и руками дёрнуть `POST /api/amocrm/reprocess`, что для Zoom-встреч не работает (они не проходят через AmoCRM events).
3. **Desktop/extension источники всегда используют `default.json`.** `company_id` не передаётся ни desktop-app, ни расширением. Результат: Zoom-встречи, которые команда записывает через наш desktop-app, оцениваются **базовой v2-схемой** (5 критериев), а не богатой v4 с playbook-аналитикой. Это фактическое расхождение между намерением и реальностью — новая логика про аргументацию на встречу там не применяется.
4. **Шум в логах worker'а: `204 No Content` логируется как WARNING.** `amocrm_sync.py:149` — `logger.warning(f"AmoCRM events fetch failed: {resp.status_code}")` — но 204 это нормальный «нет новых событий», а не ошибка. Каждые 5 минут в journalctl. Должно быть `DEBUG` или явная ветка для 204.
5. **Повторное скачивание аудио при reprocess.** `POST /api/amocrm/reprocess` не проверяет, лежит ли уже файл на диске (по `amo_note_id`), и скачивает заново. Трафик и риск, что `recording_url` уже протух на стороне AmoCRM.
6. **`amocrm_calls.responsible_user_id` типа `integer`, а не `bigint`.** Сейчас AmoCRM user id помещается в int32, но миграция на крупные id сломает вставку без предупреждения.

## 9. Верификация «как понять, что всё живо»

| Что проверяем | Команда |
|---|---|
| Сервисы | `systemctl status realestate-backend realestate-worker` → `active (running)` |
| Beat опрашивает AmoCRM | `journalctl -u realestate-worker --since "1 hour ago" \| grep poll-amocrm-calls` → строки каждые 5 мин |
| Сегодняшние сессии | `psql realestate -c "SELECT status, count(*) FROM sessions WHERE created_at >= CURRENT_DATE GROUP BY 1;"` — только `completed`, без `failed`/`in_flight` |
| Результаты на диске | `ls /data/results/{session_id}/` — есть `quality.json`, для realestate-звонков иногда `next_call_plan.json` |
| Возможные score_version | `grep -rn "score_version" worker/tasks/` — увидеть все варианты и места проставления |

## Приложение: быстрая навигация по коду

**Ingestion**
- `worker/tasks/amocrm_poll.py` — beat-задача, дедуп, создание сессии
- `worker/tasks/amocrm_sync.py` — AmoCRM API клиент, токены, note'ы
- `backend/app/routes/sessions.py`, `backend/app/routes/chunks.py` — приём desktop/extension
- `backend/app/routes/amocrm.py` — reprocess-endpoint
- `desktop-app/src/renderer/recorder.js` — Electron recorder
- `extension/offscreen.js`, `extension/background.js` — Chrome extension

**Оценка**
- `worker/tasks/pipeline.py` — оркестрация всего пайплайна
- `worker/tasks/quality.py` — LLM-оценка, схемы v2/v3/v4, plan_next_call
- `worker/prompts/sales_playbook.py` — 17 категорий аргументов

**Контекст**
- `worker/tasks/prior_context.py` — агрегация истории по сделке
- `worker/tasks/deal_summary.py` — rolling summary

**Writeback**
- `worker/tasks/amocrm_sync.py` — `format_enriched_note`, `format_next_call_plan`, `push_deal_summary`, `tag_lead`

**Per-tenant config**
- `companies/default.json`, `companies/realestate.json`
- `worker/tasks/company_config.py`

**Миграции**
- `backend/alembic/versions/001_initial.py` — sessions
- `backend/alembic/versions/002_amocrm_calls.py` — polling tracking
- `backend/alembic/versions/003_amocrm_deal_summaries.py` — rolling summary

**Офлайн-tooling (для валидации промптов, не прод)**
- `worker/scripts/reassess_quality.py`
- `worker/scripts/compare_reassess.py`
