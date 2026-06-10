# Мульти-тенантное ядро (Фаза 1) — дизайн

*Дата: 2026-06-10. Статус: на ревью (v2 — после адверсариального ревью против кодовой базы, 48 подтверждённых находок учтены).*

## 1. Контекст и видение

Продукт выходит за рамки одного клиента: домен **silentqa.com**, каждый клиент
(тенант) получает поддомен (`acme.silentqa.com`) с личным дашбордом, своими
промптами оценки, своими API-ключами и своими интеграциями. Северная звезда —
универсальная платформа речевой аналитики (оценка, цифровизация переговоров,
накопление базы знаний, обучение) по разным нишам; действующий
realestate-клиент — первый тенант, а не «сам продукт».

Будущие тенанты приходят с **другой телефонией и другой CRM** — AmoCRM/UIS
является частным случаем, и архитектура обязана это учитывать уже сейчас
(раздел 8).

### Декомпозиция на под-проекты

| Фаза | Содержание | Статус |
|---|---|---|
| **1. Мульти-тенантное ядро** | Сущность «тенант», резолв по поддомену, изоляция данных (схема на тенанта), пер-тенант авторизация, проброс тенанта в воркер | **этот спек** |
| 2. Пер-тенант конфиг и подключаемые интеграции | Промпты/сценарии/ключи в пер-тенант хранилище; реестр адаптеров CRM и телефонии (AmoCRM/UIS — первая реализация) | следующий спек |
| 3. Провижининг и инфра | Wildcard DNS/TLS на `*.silentqa.com`, nginx-роутинг (включая RU-relay), админ-flow создания клиента, перевод realestate-дашборда с легаси-домена | отдельный спек |

## 2. Цели и не-цели Фазы 1

**Цели:**
- Несколько тенантов на одной инсталляции, резолв по поддомену **и по
  легаси-домену** (realestate работает на `rogov.automate-it.fun` до Фазы 3).
- Полная изоляция данных: Postgres-схема на тенанта + изоляция файлового
  хранилища и Redis-ключей + закрытие существующих глобальных поверхностей
  (`/api/companies`, `/api/webhooks`, клиентский `company_id`) — раздел 5.6.
- Пер-тенант авторизация: аккаунты email+пароль с ролями (admin/viewer),
  Redis-сессии; пер-тенант API-ключ для ingestion-клиентов с управляемым
  rollout (раздел 6.4).
- Платформенный админ-контур (ты) отделён от тенант-контуров.
- Существующий realestate-поток продолжает работать без деградации — включая
  уже установленные desktop/extension клиенты и живые брокерские JWT.

**Не-цели (YAGNI, уходит в Фазы 2–3):**
- Саморегистрация, биллинг, email-верификация — клиентов заводит админ вручную
  (ожидается единицы–десятки тенантов).
- Перенос промптов/ключей/AmoCRM-интеграции в пер-тенант хранилище — в Фазе 1
  realestate-тенант продолжает читать `companies/realestate.json` и портальную
  БД токенов. Но доступ к этим глобальным ресурсам **закрывается** от других
  тенантов уже в Фазе 1 (раздел 5.6).
- Универсальный реестр CRM/телефония-адаптеров — в Фазе 1 только guardrails
  (раздел 8), реализация в Фазе 2.
- DNS/TLS/nginx для `*.silentqa.com` — Фаза 3; Фаза 1 тестируется с
  `Host`-заголовком / `/etc/hosts`, прод живёт на легаси-домене (раздел 3.2).

## 3. Архитектура и резолв тенанта

Топология не меняется: одно FastAPI-приложение (`realestate-backend.service`),
один Celery-воркер (`realestate-worker.service`), один Postgres, один Redis.

### 3.1 Схемы Postgres

- **`shared`** — глобальный реестр:
  - `tenants(id uuid pk, slug text unique, schema_name text unique,
    display_name text, status text in ('active','suspended'),
    api_key_hash text, api_key_required bool default true,
    company_config_id text,          -- Фаза 1: имя файла companies/*.json
    custom_domains text[] default '{}',  -- легаси/кастомные домены
    dashboard_base_url text,         -- override ссылок в CRM-нотах (5.5)
    created_at timestamptz)`
  - `platform_admins(id uuid pk, email text unique, password_hash text,
    created_at, last_login)`
- **`t_<slug>`** — схема на тенанта, полный набор текущих прикладных таблиц:
  `sessions`, `chunks`, `extraction_templates`, `complexes`,
  `complex_extractions`, `brokers`, `amocrm_calls`, `amocrm_deal_summaries`
  (+ новая `users`, раздел 5.2). Таблицы `amocrm_*` трактуются как
  интеграционные таблицы конкретного тенанта (раздел 8).

**Slug:** регекс `^[a-z][a-z0-9_]{1,30}$` (дефисы запрещены — slug
интерполируется в идентификатор схемы `t_<slug>`); резерв-лист
`admin, www, api, app, mail, apex`. Провижининг-CLI валидирует. `schema_name`
и `search_path` **никогда** не строятся из сырого `Host` — только из значения,
прочитанного из `shared.tenants` (защита от SQL-инъекции через поддомен).

### 3.2 Резолв тенанта (ASGI middleware)

Порядок резолва по `Host`:

1. Точное совпадение с `custom_domains` какого-либо тенанта → этот тенант.
   **Это штатный прод-механизм Фаз 1–2**: `rogov.automate-it.fun` заносится в
   `custom_domains` realestate-тенанта, и весь сегодняшний прод-трафик
   (дашборд, recorder-клиенты, RU-relay) продолжает работать без смены домена.
2. `Host` = поддомен `BASE_DOMAIN` (`acme.silentqa.com` → slug `acme`) →
   lookup в `shared.tenants` по slug.
3. Apex `silentqa.com` и `admin.silentqa.com` → платформенный контур
   (лендинг + админка, только схема `shared`). Тенантские API-роуты на
   платформенном контуре отвечают `404`.
4. Ничего не подошло (localhost, IP) → тенант из env `DEFAULT_TENANT`
   (для локальной разработки; пусто → платформенный контур).

Найденный тенант: `status != 'active'` → `404`. Тенант кладётся в
request-scoped контекст (`contextvars`). Реестр кешируется в памяти процесса
(TTL ~30с).

### 3.3 Переключение search_path — вариант A (выбран)

`search_path` тенанта везде равен **`t_<slug>, shared, public`** (`public`
обязателен: там остаются ENUM-тип `session_status` realestate-тенанта и
расширения).

Два механизма под одним контрактом:

- **Async-путь backend (SQLAlchemy `async_session`):** event listener на
  начало транзакции выполняет `SET LOCAL search_path ...`, тенант берётся из
  `contextvars`. После commit/rollback соединение возвращается в пул чистым.
- **Sync/raw-путь (критично, см. ниже):** в кодовой базе **нет** единой точки
  для сырых соединений — воркер и часть backend-роутов открывают ~27
  независимых короткоживущих `psycopg2.connect()` / ad-hoc `create_engine()`
  внутри хелперов (`amocrm_poll.py`, `pipeline.py`, `amocrm_sync.py`,
  `deal_summary.py`, `prior_context.py`, `session_watchdog.py`,
  `amocrm_reconcile.py`, `backend/app/routes/amocrm.py:113`,
  `routes/complexes.py` merge/relink). «Один SET LOCAL в начале таски» эти
  соединения не накроет. Поэтому вводится **единая фабрика**
  `tenant_db.connect()` (модуль, доступный и worker, и backend):
  открывает psycopg2-соединение с
  `options='-c search_path=t_<slug>,shared,public'`, тенант берёт из
  contextvar (`tenant_schema`), установленного на входе таски / запроса.
  **Все** существующие call-sites `psycopg2.connect(...)` и ad-hoc
  `create_engine(...)` переводятся на фабрику (механическая замена,
  ~27 мест); прямой `psycopg2.connect` в прикладном коде запрещается
  (lint-правило/grep в CI). Дублирующиеся `_get_sync_db_url()` хелперы
  (5 копий по модулям) схлопываются в фабрику.

Отвергнутые альтернативы: пул на тенанта (оправдан на сотнях тенантов, у нас
десятки); «БД на тенанта» (N строк подключения, тяжелее бэкапы/миграции).

### 3.4 Воркер: полная инвентаризация тасок

В Celery `Host` недоступен — тенант передаётся явно. Инвентаризация (по
фактическому коду, не по памяти):

| Таска | Тип | Стратегия тенанта |
|---|---|---|
| `pipeline.process_session` | enqueue из backend (3 call-site в `sessions.py`) | аргумент `tenant_schema` от backend (из request-контекста) |
| `amocrm_poll.process_amocrm_call` | **рабочая лошадь AmoCRM-пути**; enqueue из 3 мест: поллер, reconcile, backend reprocess-роут | аргумент `tenant_schema`; делает собственный сырой SQL до и после inline-пайплайна → contextvar выставляется до первого обращения к БД |
| `pipeline.process_session_from_file` | зарегистрирована, но AmoCRM-путь через неё **не** идёт | аргумент `tenant_schema` (для будущих вызовов) |
| `amocrm_poll.poll_amocrm_calls` | beat, без аргументов | итерирует активных тенантов с AmoCRM-интеграцией (Фаза 1: только realestate), для каждого выставляет contextvar |
| `session_watchdog.sweep_stuck_sessions` | beat, без аргументов | итерирует **всех** активных тенантов из `shared.tenants` |
| `amocrm_reconcile.reconcile_amocrm_calls` | beat, без аргументов | как поллер: тенанты с AmoCRM-интеграцией |

Правило fail-fast уточняется: **enqueue-таски** без `tenant_schema` — ошибка
(исключение на входе); **beat-таски** аргументов не получают по определению —
их контракт «итерируй тенантов сам». `session_meta` тенант-маркер **не несёт**
(он читается из БД уже после установки search_path и был бы вторым источником
истины) — slug выводится из `tenant_schema` (`t_<slug>` → `<slug>`) для
файловых путей и Redis-ключей.

Очереди Celery (`default` + `transcription`, с явным `queue=` при enqueue)
остаются общими для всех тенантов — пер-тенант очереди не вводятся.

## 4. Модель данных и миграция существующих данных

### 4.1 Alembic: два трека и бутстрап версионирования

Сейчас: один трек, ревизии 001–010, stamp живёт в `public.alembic_version`
(head = `010`), и `main.py:77-88` на каждом старте backend гоняет
`alembic upgrade head` (RuntimeError при провале — backend не стартует).

Целевое состояние:

- **shared-трек** (новая директория `alembic_shared/`), version table —
  `shared.alembic_version`. Ревизии: `S001` (создание `shared.tenants`,
  `shared.platform_admins`), `S002` (cutover realestate, см. 4.2).
- **tenant-трек** — существующие ревизии 001–010 становятся его базой +
  новые: `011_amocrm_calls_drift` (`ALTER TABLE amocrm_calls ADD COLUMN IF
  NOT EXISTS responsible_user_id BIGINT` — колонка была добавлена на проде
  руками мимо миграций; `IF NOT EXISTS` делает ревизию идемпотентной и для
  realestate, и для новых схем), `012_users` (таблица `users`, раздел 5.2).
  Version table — `alembic_version` **внутри каждой тенант-схемы**
  (`version_table_schema=t_<slug>`).
- **Стартовый хук** `main.py` заменяется на двухтрековый раннер:
  shared-трек до head, затем цикл по `shared.tenants` — tenant-трек до head
  в каждой схеме. Fail-fast сохраняется.
- **Seed-ревизии 005/008** (шаблоны «оценка звонка», «Zoom-встреча») —
  общеприменимые, остаются в tenant-треке: каждый новый тенант получает оба
  стартовых шаблона.

### 4.2 Cutover realestate (shared-ревизия S002 + сид-CLI)

1. `CREATE SCHEMA t_realestate`; `ALTER TABLE public.<имя> SET SCHEMA
   t_realestate` для всех прикладных таблиц (3.1). Данные не копируются,
   операция мгновенная; индексы/констрейнты/sequences переезжают вместе с
   таблицами. ENUM `session_status` остаётся в `public` (тип resolve'ится
   через `search_path`; у новых тенантов ревизия 001 создаст собственный
   ENUM внутри их схемы — оба варианта корректны при search_path из 3.3).
2. Перенос stamp'а: создать `t_realestate.alembic_version` со значением
   `010` (текущий head старого трека), удалить `public.alembic_version` —
   его место занимает `shared.alembic_version` нового shared-трека. После
   этого двухтрековый раннер доводит t_realestate до head (011, 012).
3. `INSERT INTO shared.tenants (slug, schema_name, status, company_config_id,
   custom_domains, api_key_required) VALUES ('realestate', 't_realestate',
   'active', 'realestate', '{rogov.automate-it.fun}', false)`.
   `api_key_required=false` — до завершения rollout ключей (6.4).
4. **Сид-CLI сразу после миграции** (обязательный шаг деплоя, тестируется в
   разделе 9): создаёт первого `admin`-юзера realestate (иначе после снятия
   Basic Auth в дашборд никто не войдёт — локаут) и генерирует API-ключ,
   печатая plaintext **один раз** в stdout (в БД — только хеш).

`downgrade` симметричен (`SET SCHEMA public`, восстановление
`public.alembic_version` из `t_realestate.alembic_version`, `DROP SCHEMA`).
Прогон сначала на копии прод-БД, затем на проде.

### 4.3 Провижининг нового тенанта

CLI `python -m app.provision_tenant <slug> --name ... --admin-email ...`:
валидация slug (3.1) → `CREATE SCHEMA t_<slug>` → tenant-трек 001→head на
пустой схеме → сид (первый admin-юзер, API-ключ с однократной печатью
plaintext) → строка в `shared.tenants`. Админ-UI — вне Фазы 1.

Побочный выигрыш: глобальные уники (`extraction_templates.name`,
`amocrm_calls.amo_note_id`, `brokers.amocrm_user_id`) автоматически становятся
пер-тенант.

### 4.4 Изоляция файлового хранилища и Redis

- **Файлы:** пути становятся `{AUDIO_STORAGE_PATH}/{slug}/...` и
  `{RESULTS_STORAGE_PATH}/{slug}/...`. Миграция: однократный `mv`
  существующих **каталогов** под `realestate/` + разбор нефайловых хвостов в
  корне results (в т.ч. `webhooks.json` — см. 5.6). Все построения путей в
  backend и worker получают slug из контекста (3.3/3.4).
- **Redis:** прикладные ключи с префиксом тенанта: сессии дашборда —
  `t:{slug}:sess:{sid}`, платформенные админ-сессии — `padmin:sess:{sid}`,
  лок — `lock:{slug}:{lead_id}` (6.3). Celery-брокерные ключи общие.
- **Операционные скрипты** (`worker/scripts/reassess_quality.py`,
  `compare_reassess.py`, `sync_brokers.py`, `backfill_amocrm_push.py`) —
  живой инструментарий, после миграции сломаются (raw-соединения без
  search_path, захардкоженный results-путь): получают обязательный
  `--tenant <slug>`, который выставляет contextvar фабрики 3.3 и
  slug-компонент путей.

## 5. Авторизация

Три плоскости: платформенный админ, тенант-пользователи (дашборд), брокеры
(recorder-идентичность). Плюс API-ключ уровня приложения.

### 5.1 Платформенный админ

`shared.platform_admins`, вход на `admin.silentqa.com` (до Фазы 3 — через
`/etc/hosts`/`Host`). Redis-сессии `padmin:sess:{sid}`, cookie скоупится на
admin-поддомен. В Фазе 1 админка минимальна: логин + список тенантов
(создание — CLI 4.3). На платформенном контуре доступен `/api/companies`
(см. 5.6).

### 5.2 Тенант-пользователи (дашборд)

Таблица `users` (tenant-трек, ревизия 012): `id uuid pk`, `email` (unique),
`password_hash` (argon2), `role in ('admin','viewer')`, `created_at`,
`last_login`.

- **Сессии — server-side в Redis**: HttpOnly+Secure cookie с session id,
  данные в `t:{slug}:sess:{sid}` с TTL. Cookie на конкретный домен тенанта,
  `SameSite=Lax` (CSRF-защита для cookie-плоскости; recorder-клиенты cookie
  не используют). CORS-конфигурация остаётся для extension-клиентов
  (X-API-Key, без credentials).
- **Эндпоинты дашборд-авторизации — отдельный неймспейс
  `/api/user-auth/login|logout|me`**, потому что `/api/auth/*` уже занят
  брокерским контуром (claim-flow). Редирект «401 → логин» в SPA применяется
  **только** к ответам cookie-гейченных эндпоинтов (не к broker-JWT и не к
  API-key ошибкам).
- Роли: `viewer` — чтение; `admin` — то же + мутации (шаблоны, удаления,
  reprocess; в Фазе 2 — настройки интеграций и юзеров).
- Уточнение текущего состояния (важно для имплементора): сегодня
  `BasicAuthMiddleware` защищает **только UI-роуты**, весь `/api/*` открыт
  (кроме X-Delete-Password на трёх префиксах). Заменяем: Basic и
  `DELETE_PASSWORD` уходят, доступ по матрице 5.6.
- Статические ассеты SPA остаются публичными **by design** — граница
  безопасности — это API. Из `static/` удаляются посторонние артефакты
  (zip-исходники расширений); роут `/recorder` указывает на отсутствующий
  файл — удаляется.

### 5.3 Ingestion: API-ключ + полная поверхность recorder-протокола

Recorder-клиенты (desktop, расширения) авторизуются пер-тенант ключом
`X-API-Key` (хеш в `shared.tenants`). Правило двойного совпадения: ключ
обязан принадлежать тенанту, зарезолвленному по домену, иначе `403`.

Фактическая поверхность recorder-протокола (по коду клиентов) шире трёх
эндпоинтов — полная матрица в 5.6: помимо `POST /api/sessions`, `/chunks`,
`/finish` это `GET /api/sessions/{id}/missing-chunks` (crash-recovery),
`GET /api/sessions/{id}` (поллинг статуса) и брокерский контур
`/api/auth/claim/*`, `/api/auth/login`, `/api/auth/me` (desktop-логин).

Ingestion-эндпоинты используются **и дашбордом** (upload-audio, reprocess,
link-lead) — поэтому принимают **двойную авторизацию**: валидная
cookie-сессия тенанта ИЛИ API-ключ.

### 5.4 Сосуществование с брокерской авторизацией

`users` = доступ к дашборду; `brokers` = идентичность записывающего
менеджера (JWT, claim-flow). Не сливаем. Таблица `brokers` переезжает в
схему тенанта как есть. JWT брокера дополняется claim'ом `tenant`;
**переходное правило**: токены без `tenant`-claim (все выданные до миграции,
живут до 30 дней) трактуются как realestate-тенант до истечения
grace-окна 30 дней, после чего claim обязателен. Привязка claim-flow к
AmoCRM (`amocrm_user_id`) — специфика realestate, кандидат на обобщение в
Фазе 2 («agent»).

Recorder-клиенты шлют оба креденшела: API-ключ (приложение) + опциональный
JWT брокера (атрибуция).

### 5.5 Ссылки на дашборд из CRM-нот

Ссылка строится из `shared.tenants.dashboard_base_url`, если задан, иначе
`https://{slug}.{BASE_DOMAIN}`. **Для realestate в Фазах 1–2
`dashboard_base_url='https://rogov.automate-it.fun'` обязателен** — поддомены
silentqa.com станут достижимы (включая RU-relay-путь) только после Фазы 3;
иначе в AmoCRM-ноты поедут мёртвые ссылки. Глобальный `DASHBOARD_BASE_URL`
env остаётся только как dev-фоллбек.

### 5.6 Матрица доступа и закрытие глобальных поверхностей

| Поверхность | Плоскость / auth | Решение Фазы 1 |
|---|---|---|
| `GET /api/sessions`, аналитика, transcripts, scorecard | cookie-сессия (viewer+) | тенант-скоуп через search_path |
| Мутации: templates, complexes, extractions, удаления, reprocess | cookie-сессия (admin) | заменяет X-Delete-Password |
| `POST /api/sessions`, `/chunks`, `/finish`, `GET .../missing-chunks`, `GET /api/sessions/{id}`, upload-audio, link-lead | API-ключ ИЛИ cookie-сессия | двойная авторизация (5.3) |
| `/api/auth/*` (брокерский контур) | открыт (как сейчас) + tenant-claim правило 5.4 | desktop-логин работает без изменений |
| `/api/user-auth/*` | открыт (это и есть логин) | rate-limit на login |
| `/api/companies` (CRUD над глобальными `companies/*.json`) | **только платформенный контур** (platform-admin сессия) | сегодня это всемирно-открытая запись в чужие промпты; тенантам недоступен, их конфиг-UI придёт в Фазе 2 |
| `/api/amocrm/*` (reprocess, статусы) | cookie-сессия (admin) + только тенант с AmoCRM-интеграцией | использует фабрику 3.3 вместо raw psycopg2 |
| `/api/webhooks` | — | **удаляется**: глобальный `webhooks.json` без auth и без тенант-скоупа, события никто не отправляет — мёртвая поверхность |
| `company_id`/`scenario_id` в metadata сессии | — | **вычищаются из клиентского ввода** (добавляются в `_SERVER_OWNED_METADATA`); сервер берёт `company_config_id` из строки тенанта. Закрывает маршрут «чужой тенант шлёт `company_id=realestate` + phone → его звонок уезжает в AmoCRM realestate» |
| AmoCRM-push в пайплайне | — | гейтится флагом тенанта (Фаза 1: только realestate), не только `scenario.prompt` |
| `/health` | открыт | без изменений |
| Статика SPA, лендинг | публичны | граница — API (5.2) |

## 6. Ingestion и rollout

### 6.1 Recorder-клиенты

Desktop-app и расширения получают два параметра: base URL и API-ключ
(заголовок `X-API-Key`). Чанковый протокол не меняется; missing-chunks и
поллинг статуса покрыты двойной авторизацией (5.3/5.6).

### 6.2 AmoCRM-поллер и beat-таски

Поллер и reconcile в Фазе 1 работают только для realestate, но через
итерацию «тенанты с AmoCRM-интеграцией» (3.4) — в Фазе 2 список просто
расширится. Watchdog итерирует всех активных тенантов.

### 6.3 lead_lock

Ключ: `lock:{slug}:{lead_id}` (Фаза 2 обобщит до
`lock:{tenant}:{provider}:{external_deal_ref}`).

### 6.4 Последовательность выкатки (без деградации)

1. Деплой кода + миграция + сид-CLI. `api_key_required=false` у realestate —
   уже установленные клиенты (без ключа) продолжают писать.
2. Обновление desktop/extension клиентов с ключом; брокерские JWT без
   tenant-claim работают по grace-правилу 5.4.
3. Через окно (30 дней или раньше, по факту обновления всех клиентов):
   `api_key_required=true`, конец grace для JWT.

Новые тенанты сразу создаются с `api_key_required=true`.

## 7. Дашборд (SPA)

Статический SPA остаётся один и общий — ходит в `/api/*` своего origin,
тенант-скоупинг автоматический. Добавляются: экран логина
(`/api/user-auth/*`), обработка 401 от cookie-гейченных эндпоинтов →
редирект на логин, сокрытие admin-действий для роли viewer. Пер-тенант
брендинг — вне Фазы 1.

## 8. Forward compatibility: интеграционный шов (guardrails Фазы 1)

Ядро пайплайна (транскрипт → диаризация → sentiment → оценка → план) —
CRM-агностично и общее. Вся специфика клиента живёт за двумя портами
(реализация портов — Фаза 2, правила действуют с Фазы 1):

- **Порт 1 — Ingestion adapter** (телефония/источник → сессия): аудио +
  нормализованные метаданные `SessionInput {audio_ref, direction, phone,
  external_deal_ref?, external_contact_ref?, recorded_at, raw_payload}`.
  Сегодняшние реализации: AmoCRM-поллинг (UIS), desktop, extension.
- **Порт 2 — CRM push adapter** (результат → CRM):
  `resolve_deal_by_phone(phone)`, `push_call_note(ref, rendered)`,
  `push_plan_note(...)`, `upsert_deal_summary(...)`, `tag(...)`.
  Первая реализация — AmoCRM; будущие — Bitrix24/HubSpot/`none`
  (dashboard-only).

**Три правила для нового кода Фазы 1:**
1. Результаты ядра привязаны к внутреннему `session_id`, никогда к
   `amo_note_id`; маппинг «сессия ↔ внешняя сущность CRM» — в интеграционных
   таблицах тенанта.
2. Идентификатор сделки в ядре — opaque `external_deal_ref` + `provider`;
   AmoCRM `lead_id` — частный случай.
3. Никакого нового AmoCRM-специфичного кода в ядре/тенант-механике.
   Существующий путь (`amocrm_sync.py`, `_push_to_amocrm`, `deal_summary.py`,
   `find_lead_by_phone`) сохраняется рабочим как «AmoCRM-адаптер до реестра» —
   в Фазе 2 оборачивается в интерфейс Порта 2 без переписывания.

## 9. Тестирование

- **Изоляция (критично):** два тенанта — запрос A не видит
  сессии/шаблоны/юзеров B (middleware + реальные SQL с переключённым
  search_path + файловые пути). Клиентский `company_id` другого тенанта
  игнорируется; AmoCRM-push не срабатывает для не-realestate тенанта.
- **Резолв:** поддомен / custom_domain (легаси-прод!) / apex / admin /
  неизвестный / suspended / localhost-dev; тенантские роуты на платформенном
  контуре → 404.
- **Фабрика соединений:** каждое соединение получает search_path; grep-тест
  «нет прямых psycopg2.connect вне фабрики».
- **Auth:** argon2; Redis-сессии (логин/логаут/инвалидация); роли; матрица
  5.6 (в т.ч. `/api/companies` недоступен тенантам, `/api/webhooks`
  отсутствует); API-ключ (валидный / чужого тенанта / отсутствует — при
  `api_key_required` true/false); JWT брокера: без claim в grace-окне ок,
  с чужим claim — 401; двойная авторизация ingestion-эндпоинтов
  (cookie-путь и ключ-путь).
- **Миграция:** upgrade+downgrade на копии прод-БД; после upgrade сессии,
  оценки, скоркард читаются; **realestate-admin логинится** (сид-CLI);
  поллер и обе beat-таски работают; перенос stamp'а — двухтрековый раннер
  идемпотентен на повторном запуске.
- **Провижининг:** CLI на пустой схеме: tenant-трек 001→head проходит
  (включая 011/012), admin логинится, ingestion по ключу работает,
  стартовые шаблоны (005/008) на месте.
- **Воркер:** enqueue-таска без `tenant_schema` падает; beat-таски итерируют
  тенантов; `lead_lock`-ключи различаются между тенантами; ops-скрипты с
  `--tenant` работают, без — отказываются стартовать.

## 10. Риски

| Риск | Митигция |
|---|---|
| Cutover-миграция ломает realestate-поток | Репетиция на копии БД; обратимый downgrade; окно с остановленным воркером; сид-CLI в чек-листе деплоя |
| Утечка search_path между запросами через пул | `SET LOCAL` в транзакции (async) / search_path в options при connect (sync) + тест чистоты соединения |
| Пропущенный raw-connect вне фабрики | Механическая замена всех 27 call-sites + grep-тест в CI |
| Забытая таска без тенант-стратегии | Инвентаризация 3.4 исчерпывающая (включая beat) + fail-fast + grep-аудит enqueue-сайтов |
| Локаут дашборда после снятия Basic | Сид-CLI обязателен в деплое + тест «admin логинится после миграции» |
| Поломка установленных recorder-клиентов | `api_key_required=false` для realestate до завершения rollout (6.4) |
| Брокерские JWT инвалидированы | Grace-правило 5.4 (30 дней без claim = realestate) |
| Мёртвые ссылки в AmoCRM-нотах | `dashboard_base_url` realestate = легаси-домен до Фазы 3 (5.5) |
| Кросс-тенант запись через company_id/companies API | Матрица 5.6: server-owned metadata, платформенный скоуп companies, гейт push по тенанту |

## 11. Вне скоупа Фазы 1 (фиксируется, чтобы не потерять)

- Реестр адаптеров, пер-тенант интеграции/ключи/промпты, конфиг-UI тенанта —
  Фаза 2.
- Wildcard DNS/TLS, nginx/RU-relay для `*.silentqa.com`, перевод realestate
  на silentqa-домен, прод-деплой поддоменов — Фаза 3.
- Админ-UI провижининга (в Фазе 1 — CLI), кросс-тенантная аналитика,
  пер-тенант брендинг SPA, биллинг, саморегистрация.
- Обобщение `brokers` → `agents`, отвязка claim-flow от AmoCRM.
- Ротация API-ключей и пер-юзерные API-токены.
