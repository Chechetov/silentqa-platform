# AmoCRM Call Polling: автоматическая обработка звонков из AmoCRM

## Контекст

UIS Com (облачная телефония) интегрирован с AmoCRM и автоматически создаёт примечания-звонки (`call_in`/`call_out`) с привязкой к карточкам сделок. В поле `params.link` каждого примечания — прямая ссылка на запись на серверах UIS.

Вместо прямой интеграции с UIS Com Data API, мы используем AmoCRM как единый источник: забираем оттуда записи звонков, обрабатываем через наш pipeline (транскрипция, диаризация, оценка качества) и возвращаем обогащённые результаты обратно в AmoCRM.

## Архитектура и поток данных

```
UIS Com телефония
    ↓ (автоматическая интеграция)
AmoCRM: примечание call_in/call_out с params.link → запись.mp3
    ↓
Celery Beat (каждые 5 мин) → poll_amocrm_calls task
    ↓
GET /api/v4/events (новые звонки с last_poll timestamp)
    ↓
Для каждого нового звонка:
    1. Проверка в amocrm_calls (дедупликация по amo_note_id)
    2. INSERT со статусом "pending"
    3. Запуск process_amocrm_call.delay()
        ↓
    Скачивание mp3 → конвертация в wav (ffmpeg)
    Создание session (metadata: lead_id, phone, direction, amo_note_id)
    process_session_from_file → _run_pipeline()
        ↓
    Все звонки: V2 оценка → дашборд
    Исходящие: V3 оценка по скрипту → push в AmoCRM
        ↓
    amocrm_calls.status = "completed"
```

## Двухуровневая обработка

- **Все звонки** (in + out): транскрипция + базовая оценка (V2 schema) → отображение в дашборде
- **Исходящие** (out): расширенная оценка по скрипту `outbound_residential` (V3 schema) + push обратно в AmoCRM (оценка, краткое саммари, данные от клиента)

Выбор схемы определяется по `direction` из session metadata:
- `direction == "out"` → сценарий `outbound_residential`, V3, push в AmoCRM
- `direction == "in"` → V2 базовая оценка, только дашборд

## Таблица amocrm_calls

```sql
CREATE TABLE amocrm_calls (
    id SERIAL PRIMARY KEY,
    amo_note_id BIGINT UNIQUE NOT NULL,
    lead_id BIGINT NOT NULL,
    contact_phone VARCHAR(20),
    direction VARCHAR(10),
    duration INTEGER,
    recording_url TEXT,
    session_id VARCHAR(64),
    status VARCHAR(20) DEFAULT 'pending',
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

CREATE INDEX idx_amocrm_calls_status ON amocrm_calls(status);
CREATE INDEX idx_amocrm_calls_created ON amocrm_calls(created_at);
```

Дедупликация: `amo_note_id UNIQUE` — при повторном поллинге INSERT ... ON CONFLICT DO NOTHING.

## Celery Tasks

### poll_amocrm_calls (Celery Beat, каждые 5 мин)

Лёгкая задача — только polling и создание записей:

1. Определяем `last_poll_timestamp`: MAX(created_at) из amocrm_calls, fallback — env `AMOCRM_INITIAL_LOOKBACK_HOURS` (default: 24 часа назад)
2. `GET /api/v4/events?filter[type][]=incoming_call&filter[type][]=outgoing_call&filter[created_at][from]=<timestamp>`
3. Для каждого event'а: GET note → извлечь params (phone, duration, link, direction)
4. INSERT в amocrm_calls (ON CONFLICT DO NOTHING)
5. Для каждого нового: `process_amocrm_call.delay(amocrm_call_id)`

### process_amocrm_call (queue=transcription)

Тяжёлая задача — скачивание и обработка:

1. Скачиваем mp3 по `recording_url` → `data/audio/amocrm/{amo_note_id}/original.mp3`
2. ffmpeg: mp3 → wav 16kHz mono → `data/audio/amocrm/{amo_note_id}/full.wav`
3. Создаём session в БД (metadata: lead_id, phone, direction, amo_note_id, source="amocrm")
4. Вызываем `process_session_from_file(session_id, wav_path, config)`
5. Обновляем `amocrm_calls.status = "completed"` / `"failed"`

## Рефакторинг pipeline.py

Выносим шаги 2-8 из `process_session` в общую функцию:

```python
def _run_pipeline(task, session_id, audio_path, config):
    """Общий pipeline: транскрипция → диаризация → sentiment → quality → save → AmoCRM push"""
    # Шаги 2-8 текущего process_session

@app.task(...)
def process_session(self, session_id, config=None):
    """Entry point для записей из браузера (WebM чанки)."""
    audio_path = merge_chunks(session_id)
    return _run_pipeline(self, session_id, audio_path, config)

@app.task(...)
def process_session_from_file(self, session_id, audio_path, config=None):
    """Entry point для готовых аудиофайлов (AmoCRM, загрузка)."""
    return _run_pipeline(self, session_id, audio_path, config)
```

## Изменения в amocrm_sync.py

Новые функции чтения:

- `get_recent_call_events(since_timestamp)` — GET events с фильтром по типу и дате
- `get_note_details(entity_type, entity_id, note_id)` — GET полные данные примечания
- `download_recording(url, dest_path)` — скачивание файла записи с обработкой ошибок

## Граничные случаи

1. **Нет записи (params.link пустой)**: короткие звонки без соединения. Статус `skipped`.
2. **Ссылка протухла**: UIS удаляет записи по тарифу. При 404 → `status = "failed"`, `error = "recording_expired"`, без retry.
3. **Retry**: при ошибках скачивания/обработки — максимум 3 попытки (`retry_count`). Следующий poll подбирает failed записи с retry_count < 3.
4. **Дубли**: дедупликация по `amo_note_id UNIQUE`.
5. **Первый запуск**: при пустой таблице берём звонки за последние N часов (env `AMOCRM_INITIAL_LOOKBACK_HOURS`, default: 24).
6. **Наши собственные примечания**: при push в AmoCRM создаём примечания с `source: "Rogov AI"`. При polling фильтруем свои примечания по source, чтобы не обрабатывать их повторно.

## Файлы

**Новые:**
- `worker/tasks/amocrm_poll.py` — poll_amocrm_calls + process_amocrm_call tasks
- SQL миграция для таблицы `amocrm_calls`

**Изменения:**
- `worker/tasks/pipeline.py` — рефакторинг: `_run_pipeline()` + `process_session_from_file`
- `worker/tasks/amocrm_sync.py` — функции чтения: get_recent_call_events, get_note_details, download_recording
- `worker/tasks/celery_app.py` — beat_schedule для poll-amocrm-calls

**Без изменений:**
- `worker/tasks/quality.py` — V2/V3 схемы уже готовы
- `backend/static/app.js` — дашборд уже показывает V3 блоки
- `companies/realestate.json` — сценарий outbound_residential уже настроен

## Конфигурация (env)

- `AMOCRM_ACCESS_TOKEN` — уже используется
- `AMOCRM_BASE_URL` — уже используется
- `AMOCRM_INITIAL_LOOKBACK_HOURS` — новый, default: 24
