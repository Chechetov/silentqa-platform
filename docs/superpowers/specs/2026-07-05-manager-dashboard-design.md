# Дашборд руководителя: результаты качества в БД + аналитика — Design

> **Дата:** 2026-07-05. Ветка: `multi-tenant-core-phase1`.
> **Источник:** системное ревью `docs/reviews/2026-07-05-system-review.md` (E-1, E-2, F-1..F-4).
> **Режим:** дизайн утверждён владельцем в формате «продумай сам и выполни» (оркестрация Fable, исполнение Opus-агентами); интерактивные ворота брейншторма сняты явным указанием.

## 1. Проблема

Отчёты качества живут **только** как JSON-файлы на диске (`RESULTS_STORAGE_PATH/<slug>/<session_id>/quality.json`). Следствия:

1. Агрегации невозможны в SQL — `/api/managers` тянет `SELECT * FROM sessions` в память (`backend/app/routes/managers.py:16`).
2. **Живой баг:** лидерборд усредняет `sessions.metadata->>'score'` (`managers.py:52`), которое **никто не пишет** — worker кладёт `overall_score` только в файл. `avg_score` пуст у всех менеджеров.
3. Тренды/алерты/агрегат возражений/talk-ratio — нечем питать.

## 2. Цель (один срез, решение владельца)

Фундамент **E-1** (таблица результатов + двойная запись + бэкфилл) и поверх него **сразу**: E-2 (реальный `avg_score`), F-1 (тренды за период), F-2 (алерт «рисковый звонок»), F-3 (агрегат возражений), F-4 (talk-ratio). Product-first: функциональность и визуал; 152-ФЗ вне скоупа.

**Инварианты:**
- Файлы JSON остаются источником истины для drill-down; БД — денормализованная проекция для аналитики. Пайплайн не ломаем.
- Запись в БД — **best-effort**: сбой аналитической записи никогда не фейлит пайплайн (try/except + WARNING).
- Идемпотентность: upsert `ON CONFLICT (session_id) DO UPDATE` — редоставка `analyze_session` безвредна.
- Изоляция тенантов: только через `tenancy.db.tenant_connect()` (хаус-рул), таблица в тенант-схеме.

## 3. Архитектурные развилки (рассмотрено → решено)

| Развилка | Варианты | Решение и почему |
|---|---|---|
| Где хранить | (а) отдельная таблица; (б) колонки в `sessions`; (в) внешнее хранилище (ClickHouse) | **(а)** `quality_results`: не трогает горячую `sessions`, чистый lifecycle (CASCADE), (в) — овер-инжиниринг для текущего масштаба |
| Точка записи в worker | (а) хук внутри `save_results`; (б) явные вызовы в 3 местах | **(б)** — явные вызовы в analyze + 2 гейтах: `save_results` остаётся тупым файл-райтером, у гейтов/analyze разные наборы данных (skip_reason vs полный отчёт) |
| Join vs денормализация | join к `sessions` за employee/датой | **денормализуем** `employee`, `session_created_at`, `duration_seconds` — дашборд-запросы без join, бэкфилл проще |
| Графики | (а) uPlot/Chart.js вендорить; (б) рукописный SVG | **(б)** `static/charts.js` — чистые функции «данные → SVG-строка» (~150 строк): ноль зависимостей, node-тестируемо, хватает для line/sparkline/bar |
| Talk-ratio в карточке звонка | (а) новый эндпоинт; (б) клиентский расчёт | **(б)** — транскрипт уже загружен в detail-view; сервер считает те же метрики в worker только для **агрегатов** |
| Группировка возражений | (а) по тексту; (б) по `category` | **(б)** — v4-схема уже даёт enum из 8 категорий (`quality.py:206-209`); карточные возражения без категории → `other` |

## 4. Модель данных

Тенант-миграция `backend/alembic/versions/018_quality_results.py` (raw-SQL стиль `014`):

```sql
CREATE TABLE quality_results (
    session_id UUID PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    overall_score SMALLINT,              -- NULL: skip-гейт или оценка не удалась
    version SMALLINT,                    -- 2 | 4 (профиль схемы оценки)
    scenario_id TEXT,                    -- из sessions.metadata->>'scenario_id'
    employee TEXT,                       -- денормализовано из metadata->>'employee'
    session_created_at TIMESTAMPTZ NOT NULL,
    duration_seconds REAL,
    skip_reason TEXT,                    -- too_short | broken_recording | NULL
    criteria JSONB,                      -- [{name, score, comment}] как в отчёте
    objections JSONB,                    -- нормализованный список, см. §5.2
    talk_metrics JSONB,                  -- {manager_sec, client_sec, other_sec, talk_ratio, longest_monologue_sec}
    sentiment_counts JSONB,              -- {positive, neutral, negative}
    risk_flags JSONB NOT NULL DEFAULT '[]'::jsonb,  -- ["low_score", ...]
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_qr_created ON quality_results (session_created_at);
CREATE INDEX ix_qr_employee_created ON quality_results (employee, session_created_at);
```

Downgrade — `DROP TABLE`. ORM-модель НЕ добавляем (worker пишет psycopg2, backend читает `text()` — как `users`/`kb_*`).

## 5. Worker: запись + метрики + алерт

### 5.1 `worker/tasks/results_db.py` (новый)

- `upsert_quality_result(session_id, quality_report, *, skip_reason=None, talk_metrics=None, sentiment_counts=None)` — читает сессию (metadata → employee/scenario_id, created_at, duration_seconds) и upsert'ит строку через `tenant_connect()`. Оборачивается try/except: любой сбой → `logger.warning`, **не** исключение (инвариант best-effort). `version` — из `quality_report.get("version")` (уже пишется в отчёт).
- Чистые функции (unit-тесты без БД):
  - `extract_objections(quality_report, card)` — нормализует **оба** формата: v4-quality `{text, category, broker_response, handling_quality, resolved}` (`quality.py:200-216`) и card-extraction `{objection, raised_by, handled, handling, improvement}` (`companies/chechetov.json`) → единый `[{text, category|"other", resolved: bool, handling_quality: int|null}]`. Приоритет: quality-возражения; карточные добавляются, если quality-список пуст.
  - `compute_talk_metrics(transcript, speaker_roles)` — суммы `end-start` по спикерам; роль из `speaker_roles` (LLM, `quality.py:714`); без ролей — топ-2 спикера по времени = manager/client (первый заговоривший = manager-эвристика НЕ применяем, берём роль или помечаем `unattributed: true`). `talk_ratio = manager_sec / (manager_sec + client_sec)`, `longest_monologue_sec` — максимальная непрерывная серия сегментов одного спикера.
  - `compute_risk_flags(overall_score, sentiment_counts, objections, thresholds)` — флаги: `low_score` (score < `risk_score_below`, дефолт 4), `negative_sentiment` (доля negative-сегментов > `negative_sentiment_ratio`, дефолт 0.4), `unresolved_objections` (≥1 возражение с `resolved=false`). Пороги — из company-config `alerts:{...}` с код-дефолтами; skip-гейтам флаги не ставим.

### 5.2 Точки вызова в `worker/tasks/pipeline.py`

1. `_analyze_inner` — после сохранения quality (`:858`) и card (`:870`): собрать objections/talk/sentiment_counts/risk_flags → один `upsert_quality_result(...)`. Работает и для AmoCRM-пути (`_run_pipeline` зовёт тот же `_analyze_inner`) — бесплатно.
2. Гейты в `_run_gates` (short-call `:587`, broken-recording `:611`): `upsert_quality_result(session_id, report, skip_reason="too_short"|"broken_recording")` — строка с NULL-скором, чтобы «всего звонков» в дашборде билось со списком.

### 5.3 Алерт «рисковый звонок» (F-2)

`worker/tasks/alerts.py` (новый, по образцу `amocrm_alerts.py`): `send_risk_alert(slug, session_id, employee, score, flags)` — Telegram при `TELEGRAM_BOT_TOKEN` + chat_id (порядок: company-config `alerts.telegram_chat_id` → env `RISK_ALERT_CHAT_ID` → env `TELEGRAM_CHAT_ID`), иначе WARNING-лог; **никогда не raise**. Текст: `⚠️ SilentQA [slug]: рисковый звонок <менеджер>, оценка N/10 — <флаги по-русски> + URL звонка` (`https://<slug>.silentqa.com/#call/<id>`). Вызов — в `_analyze_inner` после upsert, только если `risk_flags` непуст и не skip.

## 6. Backend: агрегирующие эндпоинты

`backend/app/routes/stats.py` (новый), prefix `/api/stats`, все — `Depends(require_viewer)` + `employee_scope` (менеджер видит только себя: `WHERE employee = :scope`). Идиома модуля: `_rows_*()`-хелперы с `text()`-SQL (monkeypatch в тестах, как `managers._all_sessions`) + чистые shaping-функции.

| Эндпоинт | Ответ |
|---|---|
| `GET /api/stats/overview?days=30&granularity=day\|week` | `{kpi: {calls, scored_calls, avg_score, risk_calls, avg_talk_ratio}, prev_kpi: {…то же за предыдущее окно}, series: [{bucket, calls, avg_score}]}` — серия через `date_trunc(:granularity, session_created_at)` |
| `GET /api/stats/managers?days=30` | `[{name, calls, avg_score, risk_calls, avg_talk_ratio, last_call_date, spark: [avg_score по неделям]}]` — **взвешенно** в SQL (`AVG(overall_score)` по строкам, не среднее средних) |
| `GET /api/stats/objections?days=30` | `[{category, count, resolved_rate, avg_handling_quality, examples: [до 3 текстов]}]` — `jsonb_array_elements(objections)` + `GROUP BY category` |
| `GET /api/stats/risk-calls?days=30&limit=20` | `[{session_id, employee, score, risk_flags, created_at}]` |

`avg_score` считается только по строкам с `overall_score IS NOT NULL` (skip-гейты не портят среднее); `calls` — все строки.

**Починка `/api/managers` (E-2 + A-1):** `list_managers` переписывается на один SQL-агрегат по `quality_results` (`GROUP BY employee`), форма ответа прежняя (`name, total_calls, avg_score, last_call_date`); `get_manager_sessions` — SQL-фильтр по `metadata->>'employee'` + один `GROUP BY` чанк-каунт вместо N+1. Роутер `stats` включается в `main.py` (тенантный контур — путь не в `PLATFORM_PREFIXES`, гейтится автоматически).

## 7. Фронтенд (vanilla, без сборки)

- **`static/charts.js`** (новый): чистые функции → SVG-строка: `svgLineChart(points, {w,h,yMax})`, `svgSparkline(values)`, `svgBarRow(value, max)`. Без DOM-зависимостей (node-тестируемо `node --test`), экранирование значений числовое (инъекций нет). Подключается `<script src="charts.js">` перед `app.js` в `index.html`.
- **Новая страница `#dashboard`** («Дашборд», первый пункт nav в `index.html`, виден admin/viewer/manager — менеджер получает свои данные через employee_scope):
  - селектор периода 7/30/90 дней (переключает `days`, гранулярность: 7→day, 30→day, 90→week);
  - KPI-ряд: звонки, средняя оценка, рисковые, talk-ratio — с дельтой к прошлому периоду (`prev_kpi`, стрелка ↑/↓ цветом);
  - линия тренда `avg_score` по бакетам (`svgLineChart`);
  - таблица менеджеров: имя, звонки, взвешенный avg (цветокод как в `#managers`), спарклайн, рисковые; клик → `#managers/<name>`-переход как сейчас;
  - блок «Возражения»: топ категорий с resolved-rate барами (русские ярлыки категорий: `too_expensive`→«Дорого» и т.п.) + примеры;
  - блок «Рисковые звонки»: список с переходом в `#call/<id>`.
  - Пустое состояние: «Нет данных за период — обработайте звонки или запустите бэкфилл».
- **`#managers`**: топ-стата «средний балл» — взвешенная (сервер уже отдаёт правильные avg; клиентский reduce заменить на `Σ(avg·calls)/Σcalls`).
- **Карточка звонка**: блок «Кто говорил» — talk-ratio бар менеджер/клиент + длиннейший монолог, **клиентский расчёт** из уже загруженного транскрипта (+`speaker_map` из metadata, если есть); функция-двойник `computeTalkMetrics(segments, speakerMap)` живёт в `charts.js` (node-тестируемо) — с той же семантикой, что в worker.
- Роутер: `#dashboard` в `router()` (`app.js:38-88`), дефолтный маршрут оставить `#calls` (не менять привычку).
- XSS-гигиена: все строки через `escapeHtml`; числа в SVG — через `Number()`. (Глобальная починка кавычек в `escapeHtml` — отдельная задача C-1 ревью, не в этом срезе, но новый код не должен добавлять уязвимых мест: никаких `JSON.stringify` в onclick.)

## 8. Бэкфилл истории

`worker/scripts/backfill_quality_results.py` (по конвенции `cleanup_zoom_template.py`: **dry-run по умолчанию**, `--apply`):
- `--tenant <slug>` или `--all` (по `iter_active_tenants`);
- для каждого `<results>/<slug>/<session_id>/`: читает `quality.json` (+`card.json`, `sentiment.json`, `transcript.json` если есть), тянет строку `sessions` (metadata/created_at/duration), вычисляет objections/talk/sentiment_counts/risk_flags **теми же** функциями `results_db`, upsert;
- сессии без строки в БД — скип с WARNING; отчёт: `N найдено / M upsert / K скип`;
- идемпотентен (upsert), безопасно гонять повторно (в т.ч. после `reassess_quality.py`).

## 9. Тестирование (без Postgres/Redis, идиомы репо)

- **worker** (`cd worker && python -m pytest`):
  - `test_results_db.py`: `extract_objections` (оба формата, пустые, приоритет), `compute_talk_metrics` (роли есть/нет, монолог, пустой транскрипт), `compute_risk_flags` (пороги, дефолты, кастом-конфиг); `upsert_quality_result` со стабом `tenant_connect` (курсор-мок) — SQL и параметры; best-effort (стаб кидает → нет исключения);
  - `test_alerts.py`: без env → False + WARNING, формат сообщения, порядок chat_id;
  - wiring-тест по идиоме `test_kb_pipeline_wiring.py`: `_analyze_inner` зовёт upsert; гейты зовут upsert со skip_reason.
- **backend** (`cd backend && python -m pytest tests/`):
  - `test_stats_api.py`: monkeypatch `_rows_*` → форма ответов, деление на ноль (0 звонков), `prev_kpi`, manager-scope (менеджер получает только своё: SQL-хелпер получает `scope`), viewer без скоупа — всё;
  - `test_managers_sql.py`: новая реализация `/api/managers` — форма прежняя, avg взвешенный (канned rows);
  - контур/авторизация: `/api/stats/*` без сессии → 401 (идиома `test_access_matrix`).
- **фронт**: `node --check` на `app.js`/`charts.js`; `node --test static/tests/charts.test.mjs` — SVG-функции и `computeTalkMetrics` (чистые строки/числа).
- Прогон всех сьютов зелёный — ворота мерджа.

## 10. Раскатка

1. Код + миграция 018 в ветку → CI зелёный.
2. Деплой штатно: `scripts/deploy_prod.sh` (миграция применится ко всем тенантам до рестарта).
3. Бэкфилл на проде: `python worker/scripts/backfill_quality_results.py --all` (dry-run → `--apply`).
4. Алерты: задать `RISK_ALERT_CHAT_ID` (или per-tenant `alerts.telegram_chat_id` в company-config) — иначе алерты деградируют в лог (сознательно).

Совместимость: старые воркеры без нового кода просто не пишут в таблицу (дашборд показывает меньше данных до бэкфилла); фронт при 404/пустых данных показывает empty-state. Отката не требует — таблица аддитивна.

## 11. Вне скоупа (сознательно)

- Перевод transcripts/detail-view на БД; FTS-поиск по диалогам.
- Глобальная починка `escapeHtml` (C-1 ревью — отдельный срез).
- Email-доставка алертов, настройка порогов в UI.
- `reassess_quality.py` пишет только файлы — дрейф закрывается повторным бэкфиллом (упомянуть в docstring скрипта).
