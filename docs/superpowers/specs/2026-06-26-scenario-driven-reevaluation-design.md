# Сценарная переоценка: пикер сценариев + per-scenario карта (де-RE кнопки «Переоценить»)

**Дата:** 2026-06-26
**Ветка/воркспейс:** `multi-tenant-core-phase1` в `/root/projects/silentqa-dev`
**Статус:** дизайн утверждён на брейншторме (объём = «ядро + разная карта»); готов к плану реализации.

Спек B из пары. Пара A — `2026-06-26-dashboard-search-and-call-naming-design.md` (#3 поиск, #4 нейминг).

## Зачем

Исходная претензия юзера (#1): кнопка **«Переоценить с другим шаблоном…»** в карточке звонка предлагает legacy-шаблон недвижимости (extraction-шаблоны Zoom-ЖК), который должен быть **только под недвижимость**. На тенанте `chechetov` (модуль `complexes` выключен) она бессмысленна: `/api/templates` пуст → клик даёт тост «Нет доступных шаблонов».

При уточнении выяснилось, что нужна не «спрятать кнопку», а **сценарная переоценка**: у тенанта несколько **сценариев**, каждый — своя **структурная карта (что вытаскиваем)** + своя **оценка (критерии/без оценки)**; «перепрогнать» = выбрать, под каким сценарием пере-оценить звонок. Примеры:
- стоматология: «карта первичного приёма» + оценка / «карта повторного осмотра» + оценка / скрипты для менеджеров на обработку базы;
- `chechetov`: «первичная продажа» (важно — отработка, продали ли) vs «ведение проекта» (важно — зафиксировать все договорённости и правки клиента) — разная структура извлечения.

## Главный вывод исследования: бóльшая часть машинерии уже есть

Проверено по коду (read-only исследование 2026-06-26):

| Возможность | Статус сегодня | Где |
|---|---|---|
| Per-scenario **критерии** оценки | **ЕСТЬ** (в config `scenario.criteria`; дентал так и живёт) | `worker/tasks/pipeline.py:681`; `companies/dental.json:23-55` |
| Per-scenario **протокол/промпт** (v2 vs v4) | **ЕСТЬ** (`scenario.protocol` / `scenario.prompt`; `use_extended=bool(scenario.prompt)`) | `pipeline.py:679-683` |
| Per-scenario **eval-профиль в БД** (версии, активация, LLM-переписывание), редактор в дашборде | **ЕСТЬ** | `evaluation_profile_versions` (миграция 015); `routes/eval_profiles.py`; `#evaluation` (`app.js:2687`) |
| Роутинг по `scenario_id` (валидация клиентского `appointment_type`) | **ЕСТЬ** | `finish_session` (`sessions.py:530-546`); `valid_scenario` (`company_scenarios.py:100`) |
| `features.scenarios = [{id,name}]` во фронт | **ЕСТЬ** | `tenancy_check.py:51-53`; `scenarios_for` (`company_scenarios.py:91`) |
| Пайплайн читает `config.get('scenario_id')` на reprocess | **ЕСТЬ** (но endpoint его не шлёт) | `pipeline.py:835-838`; reprocess шлёт `process_session` **без** `config` (`sessions.py:357-365`) |
| Per-scenario **карта** | **НЕТ** (единственный структурный пробел) | `run_card_extraction(transcript, company_config)` — сценарий не передаётся (`card.py:28`; вызов `pipeline.py:776`) |

Вывод: «Переоценить с другим шаблоном…» — это и есть RE-легаси (`extraction_templates.kind ∈ {extraction, evaluation}`, Zoom-ЖК сеется только при `complexes=true`, миграция 008). Современный generic-механизм — **сценарии + eval-профили**, и он уже работает. Нужны мелкие правки, чтобы вывести его в кнопку «Переоценить» и добавить разную карту.

---

## Дизайн

Объём = **ядро** (1+3) **+ разная карта** (2). Контент-наполнение сценариев chechetov — отдельный шаг (см. ниже).

### Часть 1 — Reprocess под выбранный сценарий (бэкенд)

`reprocess_session` сейчас шлёт `pipeline.process_session` с `kwargs={"tenant_schema"}` (без `config`) → пайплайн всегда берёт дефолтный сценарий. `finish_session:544-560` — готовый паттерн: валидирует `scenario_id` через `valid_scenario(cfg_id, …)` и шлёт `config={"scenario_id": scen}`.

**Правки `backend/app/routes/sessions.py`:**
- `ReprocessBody` += `scenario_id: str | None = None` (рядом с `template_id`).
- В `reprocess_session`: если `scenario_id` задан — резолвим `cfg_id` тенанта (тот же `SELECT company_config_id FROM shared.tenants WHERE slug=:slug`, как `finish_session:539-543`) и валидируем `valid_scenario(cfg_id, scenario_id)`. **Любой** None-исход (неизвестный сценарий ИЛИ `company_config_id IS NULL` у тенанта) → **400** `detail="Unknown scenario"`. Валидно → `config = {"scenario_id": scenario_id}`. `scenario_id` не задан → `config = None` (как сейчас → дефолтный сценарий).
- В `send_task(...)` добавить `kwargs={"tenant_schema": tenant_schema, "config": config}` (зеркало `finish_session:560`).

**Воркер — без изменений:** `_process_session_body` уже читает `config.get('scenario_id')` и резолвит сценарий (`pipeline.py:835-838`), дальше работает существующая цепочка scenario → criteria/prompt → eval-профиль.

**Совместимость `template_id` ↔ `scenario_id`:** независимы. `template_id` (RE-путь) по-прежнему переопределяет quality (`pipeline.py:718-730`, evaluation-template). `scenario_id` задаёт резолв сценария (карта + база критериев). Оба могут прийти; приоритет в quality остаётся за evaluation-шаблоном (как сейчас). На generic-тенантах `template_id=None`, ведёт `scenario_id`.

### Часть 2 — Per-scenario карта (воркер + схема конфига)

**Что входит:** разная **СТРУКТУРА извлечения** под сценарий (данные карты). **Заголовок** карты (label) в дашборде остаётся config-level — см. «Вне scope» (per-scenario label тянет за собой ломающее изменение `GET /card` + тег сценария на результат, поэтому идёт отдельным шагом «per-scenario хранение результатов»).

**Схема конфига (документная, без кода):** объект сценария МОЖЕТ содержать `card_extraction: {label, prompt, json_schema}` — той же формы, что top-level `card_extraction`. Нет блока у сценария → фолбэк на config-level (обратная совместимость для ВСЕХ существующих конфигов: дентал/realestate/chechetov не меняются).

**`worker/tasks/card.py`** — добавить параметр `scenario`:
```python
def run_card_extraction(transcript, company_config, scenario=None):
    cfg = (scenario or {}).get("card_extraction") or company_config.get("card_extraction")
    if not cfg: return None
    ...
```

**`worker/tasks/pipeline.py:776`** — передать резолвнутый `scenario` (он уже в scope, используется на `:679-683`, `:689`):
```python
card = run_card_extraction(transcript_with_speakers, company_config, scenario)
```

**Очистка устаревшей карты (корректность переоценки).** Сегодня `card_extraction` один на тенант → карта при reprocess всегда та же. Per-scenario вводит случай: сценарий A даёт карту, сценарий B — нет (нет ни scenario-, ни config-level блока). Тогда `run_card_extraction → None`, а пайплайн **пропускает** сохранение (`pipeline.py:777 if card is not None`) → на диске остаётся **старая** `card.json` от A (отдаётся как актуальная). Фикс: на не-extraction-template прогоне перед шагом 6b удалять существующую `card.json` (`results_dir/'card.json'`, `unlink(missing_ok=True)`), затем сохранять новую, если `card is not None`. (Для chechetov config-level карта есть всегда → None не возникает; правка — для общей корректности и теста.)

### Часть 3 — Фронтенд: пикер сценариев vs RE-шаблон

В карточке звонка (`app.js:553`, кнопка admin-only) развести по контексту тенанта:
- `moduleOn('complexes')` (недвижимость) → как сейчас: **«Переоценить с другим шаблоном…»** → `reprocessSession` (template-пикер, `app.js:2558-2577`).
- иначе → **«Переоценить»** → `pickScenarioModal(features.scenarios)` → `POST …/reprocess {scenario_id}`.
  - Источник сценариев — кешированный `features.scenarios` (паттерн `<select>` уже есть в `renderEvaluation`, `app.js:2708`). Доп. запроса нет.
  - `features.scenarios` **пуст ИЛИ ровно один элемент** → без модалки: простой перепрогон (`POST …/reprocess {}` → дефолт; при одном сценарии он и есть дефолт). Модалка — только при ≥2 сценариях. (У chechetov сейчас один сценарий «general» — до контент-шага кнопка просто перепрогоняет.)
- `pickScenarioModal` — по образцу `pickTemplateModal` (`app.js:2983-3046`), Promise-overlay со списком `{id,name}`.

После запуска — тост «Переоценка запущена — обновите страницу через минуту» (как сейчас).

### Контракт API
```
POST /api/sessions/{session_id}/reprocess        dependencies=[require_admin]
body: { "template_id": uuid | null,              # как раньше (RE evaluation/extraction-шаблон)
        "scenario_id":  str  | null }            # НОВОЕ: сценарий из company-config тенанта
→ 200 SessionResponse | 400 Unknown scenario | 404 Session/Template not found
  | 409 Session is processing/uploading
```

> `GET /api/sessions/{id}/card` **не трогаем** (остаётся сырой `card` без label) — per-scenario label отложен (см. «Вне scope»). Ломающего изменения контракта нет.

### Edge cases
- `scenario_id` невалиден ИЛИ у тенанта `company_config_id IS NULL` → 400 (не молчаливый фолбэк — явная ошибка админу).
- Тенант без сценариев (`features.scenarios=[]`) или с одним → «Переоценить» делает простой перепрогон (дефолтный сценарий), без модалки.
- У сценария нет `card_extraction` → фолбэк на config-level карту (или None, если и её нет). Если итог None — старая `card.json` удаляется (Часть 2), не отдаётся как актуальная.
- Reprocess под сценарий B **перезаписывает** результат (card+quality) сессии (единый слот; вкл. удаление карты, если новый сценарий её не даёт). Это ожидаемо: переоценка = коррекция под верный сценарий, а не накопление. История оценок под разными сценариями — вне scope.
- `template_id` и `scenario_id` вместе: UI держит их **взаимоисключающими** (template-пикер только под `complexes`; scenario-пикер — иначе). API допускает оба; в RE-пути quality ведёт evaluation-шаблон, карта — по сценарию. На generic-тенантах `template_id=None`.
- `complexes`-тенант с заведёнными сценариями: получает template-пикер (RE-путь); сценарный путь для него — будущее расширение, не в MVP.
- Переименование сценария в конфиге осиротит eval-профили БД (привязка по строковому `scenario_id`) — известное свойство, не вводим валидацию в этом спеке.

### Тесты
**Бэкенд** (`backend/tests/`, `FakeRedis` из conftest + локальный override `get_db`):
- `test_reprocess_scenario.py`: валидный `scenario_id` → `send_task` получил `config={"scenario_id": …}` (мокнуть `celery_app.send_task`, проверить kwargs); невалидный / `company_config_id=null` → 400; `scenario_id=None` → `config=None` (как сейчас); admin-гейт (403 viewer/manager).
**Воркер** (`worker/tests/`):
- `test_card_per_scenario.py`: `run_card_extraction(tr, cfg, scenario_with_card)` берёт `scenario.card_extraction`; `scenario` без карты → фолбэк на config-level; оба отсутствуют → None. (Мок OpenAI-клиента — как в существующих card-тестах.)
- `test_card_stale_cleanup`: была `card.json`, прогон даёт `None` (сценарий без карты, нет config-level) → файл удалён (последующий `GET /card` → 404).
- Регресс: дентал/realestate-конфиги (без per-scenario карты) дают тот же результат — карта из config-level.

### Риски
- **Деплой-порядок:** Часть 1 (reprocess `scenario_id`) и Часть 3 (фронт) самодостаточны — воркер уже читает `config.scenario_id`, обновление воркера не требуется. Часть 2 (per-scenario карта + очистка `card.json`) — правка **воркера**, требует ре-деплоя воркера; обратносовместима по построению (фолбэк на config-level). `GET /card` НЕ меняем → ломающего изменения контракта нет.
- Перезапись результата при переоценке под другим сценарием (вкл. удаление карты) — задокументировать в UI/релиз-ноте.

---

## Вне scope (отложено осознанно)
- **Per-scenario заголовок карты (label) в дашборде** + изменение `GET /card` на `{label, card}` + тег сценария на сессии (`metadata.scenario_id`). Это косметика поверх данных и тянет ломающее изменение контракта + deploy-связность — идёт вместе с «per-scenario хранением результатов» (ниже). Сейчас данные карты различаются по сценарию (Часть 2), заголовок — config-level (`features.card_label`).
- **Per-scenario хранение результатов:** тег сценария на сохранённый quality/card, история оценок одного звонка под разными сценариями, фильтр списка по сценарию. Требует колонки/слотов результатов — отдельный план.
- **Дашборд-UI для авторинга сценариев и карт** (сейчас сценарии/карты — файловый конфиг `companies/*.json` + `PUT /api/companies/{id}`; критерии/промпт — да, редактируются в `#evaluation`).
- **`AsrUpdate` Literal без `elevenlabs`** (`routes/companies.py:42`) — admin-UI не переключит движок на EL (дентал/chechetov задают EL прямо в файле, поэтому работает). Мелкий долг, фиксится отдельно.

## Контент-шаг (после кода, нужен вход юзера)
Завести у `chechetov` сценарии «первичная продажа» / «ведение проекта»: в `companies/chechetov.json` — два `scenarios[]` с `criteria` (или через редактор `#evaluation`/eval-профили) и per-scenario `card_extraction`. Критерии для «первичной продажи» (отработка/закрытие) и «ведения проекта» (фиксация договорённостей, правки клиента) требуют доменного входа юзера — обсуждается отдельно.

---

## Приложение — прод-ребиндинг конфига chechetov (ops, запускает юзер)

Корень «странной оценки под шаблон врача» (из претензии #3) — **не дизайн**, а мис-биндинг: на проде `shared.tenants.company_config_id` у `chechetov` = `dental` вместо `chechetov` (диагноз — `docs/.../2026-06-24-domain-artifacts-audit.md`). Поэтому отдавалась дентал-«карта приёма» с «врачом». Лечение (требует прод-доступа к БД/systemctl — gated, запускает юзер):

```bash
# 1. Ребиндинг конфига (psql к проду; strip +psycopg2 из DATABASE_URL_SYNC)
UPDATE shared.tenants SET company_config_id='chechetov' WHERE slug='chechetov';

# 2. Сброс per-process кеша конфига в воркере
systemctl restart silentqa-worker

# 3. Reprocess двух записей chechetov из дашборда («Переоценить» → дефолтный сценарий)
#    ИЛИ POST /api/sessions/{id}/reprocess (без template_id) — заново card+quality
#    из сохранённого транскрипта по конфигу chechetov «Итоги созвона».
```
После ребиндинга карточка станет generic «Итоги созвона», QA — по chechetov-протоколу. Этот спек (Часть 1+3) даёт юзеру рабочую кнопку «Переоценить» в UI, так что шаг 3 делается мышкой.
