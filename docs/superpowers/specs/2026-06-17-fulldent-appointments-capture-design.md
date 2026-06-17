# Fulldent — захват очных приёмов десктоп-приложением (API-ключ + атрибуция по имени) — дизайн

**Дата:** 2026-06-17
**Ветка:** `fulldent-appointments-capture`
**Статус:** дизайн (реализация по плану `docs/superpowers/plans/2026-06-17-fulldent-appointments-capture.md`)

---

## 1. Цель (продукт уже решён — НЕ переосмысляем)

Дать персоналу клиники **fulldent** записывать **очные приёмы** через десктоп-приложение
(Electron, `desktop-app/`) **только по API-ключу тенанта** — без AmoCRM-аккаунта «брокера», —
с атрибуцией записи к сотруднику **по имени** (`metadata.employee`). На выходе: транскрипт +
дентальная QA + «Карта приёма» (этот scoring-тракт для fulldent уже работает — см.
`2026-06-17-dental-mvp-fulldent-design.md`; данный документ — про **сторону захвата/ingestion**).

**Существующий broker-тракт realestate/rogov обязан продолжать работать без изменений** —
все изменения **условные/gated**, не удаление.

**Вне scope:** телефония, полная token-система `unify-recorder-identity`, CRM-writeback,
создание не-AmoCRM брокеров. Опциональный выбор сценария приёма (consultation/treatment_plan) —
последняя, откладываемая задача.

---

## 2. Где мы сейчас (проверено в коде)

### 2.1 Бэкенд ingestion уже поддерживает «только API-ключ»

`require_ingestion_auth` (`backend/app/auth_user.py:103-126`): валидная cookie-сессия **или**
`X-API-Key` тенанта (sha256, `secrets.compare_digest`); broker-JWT — **опциональная атрибуция**,
не требование. API-ключ тенанта: `shared.tenants.api_key_hash` + `api_key_required`
(DEFAULT TRUE), ротация `POST /api/platform/tenants/{slug}/rotate-key`
(`backend/app/routes/platform_tenants.py:142-149`).

`create_session` (`backend/app/routes/sessions.py:149-183`): атрибуция брокера — только если
`broker is not None` (строки 161-168). `_SERVER_OWNED_METADATA` (строки 129-134) вычищает из
клиентских метаданных `broker_id/amocrm_user_id/broker_name/responsible_user_id/company_id/scenario_id`.

### 2.2 КЛЮЧЕВАЯ НАХОДКА: `metadata.employee` сейчас НИКЕМ не пишется

`metadata.employee` **читается** в 5 местах:
- `backend/app/routes/sessions.py:81` — фильтр списка по scope менеджера;
- `backend/app/routes/sessions.py:199` — гейт доступа manager к одной сессии;
- `backend/app/auth_user.py:90` — `require_session_access` (файловые GET);
- `backend/app/routes/managers.py:30,81` — агрегаты экрана `#managers`;
- `backend/app/routes/knowledge.py:167` — фильтр БЗ по сотруднику.

Но **ни один путь backend/worker его не ПИШЕТ**:
- AmoCRM-сессии (`worker/tasks/amocrm_poll.py:450-459`) ставят `company_id/source/responsible_user_id/
  scenario_id/phone/...`, **но не `employee`**;
- broker-атрибуция (`sessions.py:161-168`) ставит `broker_name/broker_id/...`, **не `employee`**;
- по всему `worker/` нет ни одного присваивания `metadata["employee"]`.

→ Экран `#managers` и manager-scoping — **дремлющие фичи**: в обычном потоке у сессий нет `employee`.
`employee` **отсутствует** в `_SERVER_OWNED_METADATA` (sessions.py:129-134) — то есть клиент **уже
сейчас** может его проставить, и сервер его НЕ вычищает. Но опираться на это «по факту» — хрупко и
недокументировано. Делаем санкционированно (см. §4.1).

**Следствие для не-регресса:** realestate/AmoCRM-потоки `employee` не шлют и не начнут — добавление
санкционированного `employee` на стороне захвата ничего у них не меняет (риск регресса = 0).

### 2.3 Десктоп-приложение: broker-гейт блокирует не-AmoCRM тенантов

Реальные пути файлов: `desktop-app/src/renderer/renderer.js`, `desktop-app/src/renderer/recorder.js`,
`desktop-app/src/renderer/index.html`, `desktop-app/src/main/index.js`, `desktop-app/src/main/preload.js`
(а не `desktop-app/renderer.js` из исходных «якорей»).

- `renderer.js:231-253` (`btnSaveSetup`) после сохранения **всегда** уходит на `showLoginScreen()`
  (строка 247).
- `renderer.js:736-798` (`init()`): `if (!brokerJwt) { showLoginScreen(); return; }` (строки 758-761).
- Setup-экран (`index.html:38-59`) уже собирает Server URL / Username / Password / **API Key (optional)**.
- `recorder.js:63-68` (`buildHeaders`) уже шлёт `X-API-Key` (строка 66) и `X-Broker-Token` (строка 65).
- `recorder.js:83-98` (`createSession`) шлёт `metadata: { source:'desktop-app', platform, recordedAt }` —
  **поля сотрудника нет** (сюда добавим).
- Brokers создаются **только** из AmoCRM-юзеров (`worker/scripts/sync_brokers.py`), не-AmoCRM
  способа нет → персонал fulldent **физически не может** пройти broker-гейт сегодня.
- Креды хранятся непрозрачным JSON-блобом через `safeStorage` (`main/index.js:136-158`,
  `preload.js:4-6`) → **добавление поля `employee` НЕ требует правок main-процесса**.

### 2.4 Сценарий приёма (опциональная задача)

`worker/tasks/pipeline.py:813,878`: `scenario_id = config.get("scenario_id")` — берётся **только из
явного `config`** Celery-таска, осознанно **НЕ** из метаданных сессии (комментарий 809-811: server-owned,
спека 5.6). Места enqueue (`sessions.py:457` finish, `:280` reprocess, `:390` link-lead) **config не
передают** → `scenario_id=None` → `get_default_scenario_id` = `consultation` для dental
(`companies/dental.json`: `default_scenario_id="consultation"`, сценарии `consultation`/`treatment_plan`).
`get_scenario`/`get_default_scenario_id` — `worker/tasks/company_config.py:93,108`.

### 2.5 НАХОДКА-ДОЛЖНО-ИСПРАВИТЬ: авто-шаблон «Zoom-встреча ЖК» протекает на fulldent

`create_session` (`sessions.py:170-177`): для **любой** сессии с `source=="desktop-app"` без
`template_id` авто-привязывает evaluation-шаблон по имени
`_DEFAULT_DESKTOP_TEMPLATE_NAME = "Zoom-встреча брокера (презентация ЖК)"` (sessions.py:138).

Этот шаблон сидится миграцией **008 в tenant-tree** (`backend/alembic/versions/008_seed_zoom_meeting_template.py`)
→ применяется ко **ВСЕМ** тенантам, включая fulldent. А в воркере evaluation-шаблон **перебивает**
протокол/критерии сценария (`pipeline.py:685-707`: `quality_protocol = tpl_prompt`, `quality_criteria =
tpl_criteria`, `use_extended=True`, `template_driven=True`).

→ Очный приём fulldent получил бы realestate-протокол «презентация ЖК» вместо дентального сценария.
**CRM-утечки нет** (`_push_to_amocrm` рано выходит для не-AmoCRM тенантов: `pipeline.py:458-463`,
гейт `AMOCRM_TENANT_SLUGS`), но **оценка была бы по чужим критериям**. Должно быть исправлено
gating-ом авто-шаблона (см. §4.5).

---

## 3. Архитектура / поток (целевой)

```
[Десктоп fulldent]
  Setup: Server URL + API-ключ (sqa_...) + Сотрудник (имя). Username/Password ПУСТЫЕ.
        ▼ (API-key mode: ключ есть → broker-логин ПРОПУСКАЕМ)
  POST /api/sessions   X-API-Key: sqa_...        body.metadata = { source:'desktop-app',
                                                   employee:'<имя>', appointment_type?:'consultation' }
        ▼ require_ingestion_auth: ключ совпал с этим тенантом → ок (broker=None)
  create_session: strip server-owned; broker=None → НЕ ставит broker_*;
                  ставит sanitized metadata.employee; (НЕ авто-Zoom-шаблон для не-RE тенанта)
        ▼ chunks (X-API-Key на каждом) → finish
  finish_session: читает sanitized appointment_type → enqueue config={scenario_id:...}
        ▼ Celery pipeline (tenant_schema=t_fulldent)
  ASR(ElevenLabs) → diarize → sentiment → quality (dental scenario) → card («Карта приёма»)
        ▼
  Дашборд fulldent: запись с атрибуцией employee=<имя>; #managers агрегирует по сотрудникам;
                    manager-роль видит только свои приёмы.
```

realestate/rogov: username/password заполнены, broker-логин как раньше; `employee` не шлётся;
`appointment_type` не шлётся; авто-Zoom-шаблон для realestate сохраняется.

---

## 4. Компоненты и решения по открытым вопросам

### 4.1 Q1 — атрибуция по сотруднику (выбранный подход)

**Подход:** санкционировать клиентское поле `employee` при создании сессии, **отдельно** от
broker-атрибуции, через **чистый хелпер** (тестируется без БД, как `test_server_owned_metadata.py`).

- В `create_session` логику сборки метаданных вынести в чистую функцию
  `build_session_metadata(raw_meta, broker)` (sync, без БД):
  1. копия `raw_meta`;
  2. вычистить `_SERVER_OWNED_METADATA`;
  3. **санитизация `employee`**: только `str`, `strip()`, отбросить пустую, обрезать до 120 симв.;
     нестроку/пустую — удалить ключ;
  4. если `broker is not None` — проставить `broker_*` (как сейчас, строки 164-168). Брокер и
     `employee` не конфликтуют: realestate использует broker_name (в т.ч. для AmoCRM-резолва),
     fulldent — `employee` (для дашборд-атрибуции/scope).
- Маршрут `create_session` использует хелпер, затем делает БД-специфику (Zoom-шаблон — см. §4.5,
  теперь gated) и `db.add/commit`.

**Почему так, а не «оставить как есть» (клиент и так может писать employee):** явная санитизация
(тип/длина) + документированный контракт + юнит-тест фиксируют поведение и закрывают спуфинг-поверхность
(нельзя засунуть `employee` как объект/огромную строку). `employee` **намеренно НЕ** в
`_SERVER_OWNED_METADATA` — это легитимное клиентское поле атрибуции для recorder-only тенантов.

**Не-регресс realestate:** broker-ветка не тронута; realestate `employee` не шлёт → у его сессий
ключа не появится; AmoCRM-резолв имени (`responsible_user_id`/`broker_name`) не затронут.

**Тестируемость:** чистый хелпер → юниты без БД (см. план, задачи 1-2).

### 4.2 Q2 — дискриминатор «API-key mode» в десктопе (выбранный подход)

**Правило:** **API-key mode = есть `apiKey` И пусто `username`** (логин/пароль брокера не введены).
Простой, явный, обратносовместимый: realestate всегда вводит username/password → остаётся в broker-mode.

Хелпер в `renderer.js` (чистый, тестируемый Node-ом):
```js
function isApiKeyMode(creds) {
  return !!(creds && creds.apiKey && !creds.username);
}
```

Правки потоков:
- **`btnSaveSetup` (`renderer.js:231-253`)**: после `persistCredentials()` —
  `if (isApiKeyMode({apiKey, username})) { showRecorderScreen(); } else { showLoginScreen(); }`.
- **`init()` (`renderer.js:736-798`)**: перед `if (!brokerJwt)` (строка 758) —
  `if (isApiKeyMode(creds)) { showRecorderScreen(); return; }` (минуя broker-валидацию `/api/auth/me`).
- **`showRecorderScreen()` (`renderer.js:138-173`)**: в API-key-mode прятать `brokerInfo`, показывать
  плашку «Сотрудник: <имя>»; `setBrokerToken('')` (брокера нет), `setEmployee(employee)`, `setApiKey(apiKey)`.

UI (`index.html`):
- В setup-экран добавить поле **«Сотрудник»** (`#setupEmployee`) после API Key.
- В settings-панель — зеркальное `#inputEmployee` (как уже сделано для apiKey).
- Логаут-ссылка `#btnLogout` (broker-only) скрывается в API-key-mode.

`renderer.js` состояние: добавить `let employee = ''`; читать/писать в `persistCredentials()` и `init()`;
`btnSaveSettings` — сохранять `employee`.

`recorder.js`:
- добавить module-var `let employee = ''` и метод `setEmployee(v){ employee = (v||'').trim(); }`
  в экспорт `window.Recorder` (строки 663-671), рядом с `setApiKey`/`setBrokerToken`;
- в `createSession()` (строки 83-98) — `if (employee) metadata.employee = employee;`.

**Не-регресс:** при заполненных username/password (realestate) `isApiKeyMode → false` → broker-логин
как раньше; `createSession` без employee → метаданные realestate не меняются.

**Main-процесс:** правок НЕ требует (креды — непрозрачный JSON-блоб, §2.3).

### 4.3 Q3 — выбор сценария приёма (ОТДЕЛЬНАЯ, откладываемая задача)

**Подход (санкционированный серверный путь, RE-safe):**
- Клиент при создании сессии может прислать `metadata.appointment_type` (строка, напр.
  `"consultation"`/`"treatment_plan"`). Это **НЕ** `scenario_id` (тот остаётся server-owned и
  вычищается). `appointment_type` сохраняется в метаданных как намерение клиента.
- При **enqueue** в `finish_session` (`sessions.py:437-468`) сервер:
  1. читает `appointment_type` из метаданных сессии;
  2. **валидирует** против сценариев company-config текущего тенанта (нельзя выбрать чужой/несуществующий
     сценарий) — резолвер на стороне worker/конфига (`get_scenario`), но проверка значения серверная;
  3. если валидно — передаёт `config={"scenario_id": appointment_type}` в Celery-таск; иначе — `config`
     не передаёт (поведение по умолчанию).
- Когда `appointment_type` отсутствует/невалиден — **поведение не меняется**: `scenario_id=None` →
  `get_default_scenario_id` (для realestate это и сейчас None/без сценария; для fulldent —
  `consultation`).

**Валидация сценария на сервере:** company-config — это файлы (`companies/<id>.json`), бэкенд их
читает через `routes/companies.py`. Резолв `company_config_id` тенанта — из `shared.tenants`. Чтобы не
тащить worker-резолвер в backend-хендлер, валидируем по **списку допустимых id из company-config
тенанта** (минимальный backend-хелпер чтения сценариев из того же `COMPANIES_PATH`). Невалидное
значение → молча игнорируем (fallback к дефолту), не 4xx (запись не должна падать из-за плохого
appointment_type).

**Почему отдельной задачей:** трогает enqueue-путь и добавляет backend-чтение company-config; для
fulldent дефолт `consultation` уже корректен в большинстве случаев → фича — улучшение, не блокер.
Если scope раздувается — оставляем последней.

**Не-регресс:** места enqueue realestate (reprocess/link-lead) НЕ трогаем; меняем только `finish_session`,
и только когда есть валидный `appointment_type` (realestate его не шлёт).

### 4.4 Q4 — брендинг (низкий приоритет)

Заголовок `backend/static/index.html:6` сейчас хардкод `<title>Meeting Recorder</title>`; логотип-текст
«Meeting Recorder» (index.html:13). `shared.tenants` имеет колонку `display_name`
(`backend/alembic_shared/versions/s001_shared_registry.py:23`). Реестр в middleware
(`tenancy_http.py:36-49`) её НЕ выбирает.

**Подход:** добавить `display_name` в ответ `/api/tenancy/features`
(`backend/app/routes/tenancy_check.py:37-47`) — там уже есть `request.state.tenant`, но в реестре нет
`name`. Два варианта:
- (A) добавить `display_name` в SELECT реестра (`tenancy_http.py:36-49`) + в строку (строки 41-52) —
  одна правка горячего пути, далее `features` отдаёт `display_name` из `request.state.tenant`.

SPA (`backend/static/app.js`): после загрузки `features` (строка 94) — если есть `display_name`,
`document.title = display_name` и обновить `.logo-text`. Если пусто — оставить дефолт.

**Не-регресс:** platform-контур и realestate без `display_name` → дефолтный заголовок; добавление поля
в SELECT не ломает существующих потребителей (реестр-строка — dict, лишний ключ безвреден).

### 4.5 Должно-исправить — gating авто-шаблона «Zoom-встреча ЖК» (§2.5)

Авто-привязка `_DEFAULT_DESKTOP_TEMPLATE_NAME` (`sessions.py:174-177`) должна применяться **только для
тенантов с AmoCRM-контуром** (realestate), чтобы dental-приёмы не получали realestate-evaluation.

**Подход:** гейт по членству тенанта в AmoCRM-контуре — как уже делается в `sessions.py:349` и
`pipeline.py:713` (`get_tenant_slug() in AMOCRM_TENANT_SLUGS` / `require_tenant_slug() in
AMOCRM_TENANT_SLUGS`). В `create_session`:
```python
from tenancy.registry import AMOCRM_TENANT_SLUGS  # уже импортируется в sessions.py:15
...
if (meta.get("source") == "desktop-app" and not meta.get("template_id")
        and get_tenant_slug() in AMOCRM_TENANT_SLUGS):
    tid = await _resolve_default_desktop_template_id(db)
    if tid:
        meta["template_id"] = tid
```
`get_tenant_slug` уже импортируется (`sessions.py:14`). Под тестом: realestate (в AMOCRM_TENANT_SLUGS)
→ шаблон ставится; fulldent → НЕ ставится.

**Не-регресс:** realestate в `AMOCRM_TENANT_SLUGS` → ветка как раньше. Клиент по-прежнему может
прислать явный `template_id` (он не вычищается) — поведение сохранено для всех.

---

## 5. Стратегия не-регресса realestate (свод)

| Изменение | Почему realestate не ломается |
|---|---|
| Санкц. `employee` в `create_session` | realestate не шлёт `employee`; broker-ветка не тронута; AmoCRM-резолв по `responsible_user_id`/`broker_name` цел |
| Десктоп API-key mode | `isApiKeyMode` = ключ + ПУСТОЙ username; realestate вводит username/password → broker-mode как раньше |
| `appointment_type` → `scenario_id` в finish | realestate не шлёт `appointment_type`; enqueue без config → текущее поведение; reprocess/link-lead не тронуты |
| Брендинг `display_name` | без `display_name` (platform/realestate) → дефолтный заголовок; лишний ключ в реестре безвреден |
| Гейт Zoom-шаблона | realestate ∈ `AMOCRM_TENANT_SLUGS` → ветка сохраняется; явный `template_id` по-прежнему уважается |

Регресс-покрытие тестами: существующие `test_server_owned_metadata.py`, `test_manager_scope.py`,
`test_access_matrix.py` остаются зелёными; новые тесты добавляют realestate-кейсы (broker-атрибуция всё
ещё работает; Zoom-шаблон для realestate всё ещё ставится).

---

## 6. Подход к тестированию

- **Backend — юнит-тесты** (Starlette `TestClient` + `FakeRedis` + monkeypatch `TenantRegistry.all_tenants`),
  `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest`. Паттерн скаффолдинга — из
  `backend/tests/test_session_card.py` (тенант `fulldent`) и `test_manager_scope.py`.
  - Чистые хелперы (`build_session_metadata`, серверная валидация сценария) — юниты без БД (как
    `test_server_owned_metadata.py`).
  - Маршрутные тесты, где нужен `db.commit` — переопределяем `app.dependency_overrides[get_db]` фейковой
    async-сессией, перехватывающей добавленный `Session` (паттерн `_FakeDB` из `test_manager_scope.py`).
- **Desktop (Electron) — НЕ E2E-тестируемо в этой среде.** Для извлекаемой чистой логики
  (`isApiKeyMode`, санитизация employee на клиенте) — юниты можно гонять `node` (функции экспортируемы
  при `typeof module !== 'undefined'`-guard) ИЛИ минимально — `node --check` синтаксис всех правленых
  `.js`. UI-поток — ручной чек-лист (§8).

---

## 7. OUT OF SCOPE

- Телефония, полная token-система `unify-recorder-identity`, не-AmoCRM создание брокеров.
- CRM-writeback для fulldent (его и так нет — `_push_to_amocrm` рано выходит).
- ASR/QA/«Карта приёма» — отдельная спека `2026-06-17-dental-mvp-fulldent-design.md` (уже сделана).
- Изменение broker-логина/claim как механизма (оставляем для realestate).
- Сборка/подпись/распространение десктоп-артефактов (`static/download/dl/`).

---

## 8. Human punch-list (ручные шаги — НЕ автоматизируются здесь)

1. **Выпустить API-ключ fulldent:** `POST /api/platform/tenants/fulldent/rotate-key` (платформ-админ),
   передать ключ (`sqa_...`) персоналу клиники. Убедиться `fulldent.api_key_required=true`.
2. **Собрать десктоп:** `cd desktop-app && npm install && npm run build:win` (или `build:linux`).
3. **Настроить клиент:** Server URL = prod SilentQA, API-ключ, имя сотрудника; username/password —
   **пусто**. Проверить, что приложение сразу открывает экран записи (минуя broker-логин).
4. **Live E2E (на проде):** записать тестовый приём → дождаться `completed` → проверить в дашборде
   fulldent: транскрипт + дентальная QA + «Карта приёма» + атрибуция `employee=<имя>`; экран `#managers`
   показывает сотрудника; роль manager видит только свои приёмы.
5. **(Опц., если задача 3 сделана)** Проверить выбор `appointment_type` (consultation/treatment_plan) →
   сценарий в логах воркера соответствует.
6. **Регресс realestate (на проде/стейдже):** прогнать broker-десктоп realestate — логин/claim/запись/
   AmoCRM-writeback работают как раньше; Zoom-ЖК-шаблон по-прежнему применяется к их desktop-сессиям.
7. **Деплой:** код из ветки → прод (`/root/projects/silentqa`, :8007), рестарт backend+worker
   (`python -m app.migrate` для §4.4-миграции, если она добавлена; правки `create_session`/`finish` —
   только код, миграций не требуют, кроме опционального брендинга).

---

## 9. Якоря (file:line — проверено)

- `backend/app/auth_user.py:103-126` — `require_ingestion_auth` (cookie OR X-API-Key).
- `backend/app/routes/sessions.py:129-134` — `_SERVER_OWNED_METADATA`.
- `backend/app/routes/sessions.py:149-183` — `create_session` (broker-атрибуция, Zoom-шаблон).
- `backend/app/routes/sessions.py:170-177` — авто-Zoom-шаблон по `source=='desktop-app'`.
- `backend/app/routes/sessions.py:437-468` — `finish_session` (enqueue без config).
- `backend/app/routes/sessions.py:81,199`, `auth_user.py:90`, `managers.py:30,81`, `knowledge.py:167`
  — чтение `metadata.employee`.
- `backend/app/routes/platform_tenants.py:142-149` — rotate-key.
- `backend/app/routes/tenancy_check.py:37-47` — `/api/tenancy/features`.
- `backend/app/tenancy_http.py:36-49` — SELECT реестра (нет `display_name`).
- `backend/alembic_shared/versions/s001_shared_registry.py:23` — колонка `display_name`.
- `backend/alembic/versions/008_seed_zoom_meeting_template.py` — сид Zoom-шаблона (tenant-tree).
- `worker/tasks/pipeline.py:801-816` (`_process_session_body`), `:867-881`
  (`_process_session_from_file_body`) — `scenario_id = config.get("scenario_id")`.
- `worker/tasks/pipeline.py:685-707` — evaluation-template override протокола/критериев.
- `worker/tasks/pipeline.py:458-463` — `_push_to_amocrm` early-return для не-AmoCRM.
- `worker/tasks/company_config.py:93,108` — `get_scenario`/`get_default_scenario_id`.
- `companies/dental.json` — `default_scenario_id="consultation"`, сценарии `consultation`/`treatment_plan`.
- `desktop-app/src/renderer/renderer.js:231-253` (`btnSaveSetup`), `:736-798` (`init`), `:138-173`
  (`showRecorderScreen`).
- `desktop-app/src/renderer/recorder.js:63-68` (`buildHeaders`), `:83-98` (`createSession`),
  `:663-671` (экспорт `window.Recorder`).
- `desktop-app/src/renderer/index.html:38-59` (setup), `:215-236` (settings).
- `desktop-app/src/main/index.js:136-158` — opaque save/get credentials.
