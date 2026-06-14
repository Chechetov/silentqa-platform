# Единая идентичность user/manager вместо «брокера»

**Date:** 2026-06-14
**Status:** Draft — дизайн согласован; прошла адверсариальное ревью (Opus,
2026-06-14, 5 осей): исправлены argon2-vs-bcrypt (blocker), список тестов
(blocker), DB-отзыв токена, согласование атрибуции со скоупом, гейт роли,
phantom-поле `name`, реальность расширения, down-миграция. На ревью у владельца.
**Ветка/воркспейс:** `multi-tenant-core-phase1` в worktree `/root/projects/meet-mt`; прод-деплой — `/root/projects/silentqa`.

## Зачем

Рекордер (десктоп + расширение) ОБЯЗАТЕЛЬНО требует брокер-логин: без `brokerJwt`
приложение не доходит до экрана записи (`renderer.js:758`). Брокер-аккаунты
создаются ТОЛЬКО синхронизацией из AmoCRM (`worker/scripts/sync_brokers.py`),
а `brokers.amocrm_user_id` = `NOT NULL` — ручного создания нет. Поэтому
не-AmoCRM тенанты (fulldent, shuravin) не могут завести записывающих → их
сотрудники физически не могут войти в рекордер и писать звонки.

Сервер при этом уже принимает ingestion по чистому X-API-Key (доказано e2e
2026-06-14: запись 45с через fulldent прошла транскрипцию + скоринг). Узкое
место — модель идентичности рекордера, завязанная на AmoCRM-брокеров.

Решение: убрать понятие «брокер» из платформы и завязать аутентификацию
рекордера и атрибуцию звонков на единую модель **user/manager** (таблица
`users`, роли `admin/viewer/manager` из Plan 3b).

## Решения, принятые на брейншторме

| Вопрос | Решение |
|---|---|
| Граница изменений | Только код silentqa-платформы (multi-tenant ветка). Живой rogov-деплой НЕ трогаем — он на своём чекауте/БД и старом брокер-коде. |
| Будущий AmoCRM | На `users` добавляем nullable `amocrm_user_id` — задел под матчинг, когда rogov переедет в платформу. |
| Вход в рекордер | Личный логин manager-юзера (email+пароль) → токен. Лёгкий режим «ключ+имя» НЕ делаем. |
| Таблица `brokers` | Удаляем сразу (на платформе пуста), не оставляем дремать. |
| Атрибуция | По `user_id` (авторитетно) И `employee` (= employee_name, ключ дашборда менеджера Plan 3c). |

## Ключевое уточнение области (важно для безопасности изменения)

«Брокер» в коде живёт в ДВУХ несвязанных местах:

1. **Auth/идентичность рекордера** — таблица `brokers`, broker-JWT
   (`auth_jwt.py`), роуты `/api/auth/*` (`routes/auth.py`), `get_current_broker`,
   атрибуция в `sessions.py`, `sync_brokers.py`. **Это и убираем.**
2. **AmoCRM poll/sync-матчинг** — `amocrm_poll.py`/`amocrm_sync.py` используют
   `responsible_user_id` (int, приходит из AmoCRM-нот) и кеш AmoCRM-юзеров
   (`_users_cache`), а функция `_resolve_broker_name` лишь резолвит ИМЯ по
   этому id. **Таблицу `brokers` этот код НЕ читает.** Это внутренняя
   AmoCRM-механика, на платформе дремлющая (нет AmoCRM-тенанта,
   `AMOCRM_TENANT_SLUGS = ("realestate",)`). **НЕ трогаем** — переименование
   `responsible_user_id`/`_resolve_broker_name` вне области (косметика,
   риск для будущего rogov-матчинга). Дроп таблицы `brokers` его не ломает.

Инвариант: после удаления брокер-auth воркер и бэкенд должны импортироваться
и стартовать без ошибок; AmoCRM-путь остаётся валидным (просто неактивен).

## 1. Модель идентичности

`users` (Plan 3b: `id, email, password_hash, role, employee_name, created_at,
last_login`, уникальный `LOWER(email)`) — единственная идентичность платформы.

- Добавляем `amocrm_user_id BIGINT NULL` + частичный уникальный индекс
  `WHERE amocrm_user_id IS NOT NULL` (на платформе пусто; пригодится при
  миграции rogov, исключит дубль-привязку).
- Записывают звонки роли `manager` и `admin` (через user-token). `viewer`
  через user-token → 403 (см. §3 — гейт роли действует на user-token-плоскости).
- **Роль `manager` обязана иметь `employee_name`** (валидация при создании/
  правке юзера и при выдаче токена) — иначе атрибуция разойдётся со скоупом
  дашборда (см. §3). `admin` без `employee_name` допустим.
- Таблицу `brokers` удаляем (миграция, ниже).

## 2. Аутентификация рекордера — user-token

Рекордер — нативное приложение (не браузер), ему нужен носимый bearer-токен.

- Новый эндпоинт **`POST /api/user-auth/token`**: тело `{email, password}` →
  при успехе `{token, user: {email, role, employee_name}}`. Cookie НЕ ставит
  (в отличие от `/api/user-auth/login` для SPA).
  **Верификация пароля — argon2** (`argon2.PasswordHasher.verify`, как
  `routes/user_auth.py::_verify`), НЕ bcrypt: пароли `users` захешированы
  argon2 (012_users.py), а брокерский `auth_jwt.verify_password`/`hash_password`
  — bcrypt; если дёрнуть bcrypt-verify против argon2-хеша, верификация всегда
  False и ни один manager не получит токен. Из `auth_jwt` переиспользуем
  ТОЛЬКО JWT-кодек (encode/decode/exp + tenant-claim), парольный стек bcrypt
  не трогаем (он удаляется вместе с брокером, §4). Dummy-hash для выравнивания
  тайминга — argon2, как в `user_auth.py`.
  Роль: токен выдаём только `manager`/`admin`; `viewer` → 403
  `viewer_cannot_record`. `manager` без `employee_name` → 422
  `employee_name_required` (см. §1). Rate-limit — тот же
  `register_login_attempt` (email + IP backstop), что у SPA-логина.
- Токен — JWT (JWT-кодек из `auth_jwt`): payload
  `{sub: user_id, tenant: <slug>, role, exp}`. `employee_name`, `amocrm_user_id`
  и роль для авторизации в токен НЕ полагаемся — резолвим из `users` на сервере
  (токен не должен нести изменяемые/отзываемые атрибуты). `name` НЕ кладём —
  такой колонки в `users` нет.
- `auth_jwt.get_current_user_token(x_user_token: Header("X-User-Token"))` →
  верифицирует подпись + `exp`, проверяет tenant-claim (== текущий тенант
  по хосту, иначе 401), затем **перечитывает юзера из БД по `sub`**: если юзер
  не найден или его текущая роль ∉ {manager, admin} → 401 (отзыв при
  удалении/понижении работает мгновенно, не ждёт `exp`). Возвращает ctx
  `{user_id, role, employee_name, amocrm_user_id}` — `employee_name`/
  `amocrm_user_id` берутся из БД (актуальные), не из payload.
  **Grace-окна нет** (это новый токен, в проде нет ранее выданных user-JWT;
  `BROKER_JWT_TENANT_GRACE_UNTIL` удаляем вместе с брокером).
- Рекордер: экран входа = email+пароль; шлёт токен заголовком `X-User-Token`
  на ingestion-запросы. `X-Broker-Token` убираем. TTL токена — как у сессий
  (`SESSION_TTL_SECONDS`); при 401 рекордер показывает экран входа (сохранить
  существующий authExpired-флоу, §5).

## 3. Ingestion-авторизация и атрибуция

`require_ingestion_auth` принимает (любой из трёх):
1. валидная cookie-сессия (SPA-юзер) — как сейчас;
2. `X-API-Key` тенанта — как сейчас (headless / escape-hatch);
3. **валидный `X-User-Token`** (manager/admin — роль проверяется в
   `get_current_user_token` через БД, §2), tenant-claim совпадает.

**Точная семантика гейта роли (важно — ревью подсветило неоднозначность):**
роль-гейт «viewer не пишет» действует ТОЛЬКО на user-token-плоскости (путь 3).
Cookie-сессия (путь 1) и X-API-Key (путь 2) остаются НЕ роль-гейчены для
ingestion — как сейчас. То есть «viewer не может записывать» строго верно для
рекордера (он ходит user-token'ом); cookie-плоскость намеренно открыта всем
ролям (это внутренний SPA-контур), X-API-Key — tenant-wide escape-hatch вне
ролевой модели. (Не вводим роль-гейт в cookie-ветку — это вне области и
изменило бы существующее поведение SPA.)

Атрибуция в `create_session` (заменяет брокер-блок). **Единый источник истины:**
для аутентифицированного юзера (user-token ИЛИ cookie) делаем один
`SELECT employee_name, amocrm_user_id FROM users WHERE id = :sub` и ставим
(авторитетно, переопределяя клиентское):
- `metadata.user_id = <user_id>`
- `metadata.employee = employee_name` **строго** (без фолбэка на name/email).
  Инвариант §1 гарантирует, что у `manager` `employee_name` не NULL; для
  `admin` без `employee_name` запись через рекордер не ожидается (admin —
  не записывающая роль), но если случится — `employee` останется NULL
  (в дашборд менеджера не попадёт, что корректно). Формула согласована с
  `employee_scope`/`require_session_access` (ключ = `employee_name`), иначе
  менеджер не увидит свои записи.
- если `amocrm_user_id` не NULL → `metadata.responsible_user_id =
  amocrm_user_id` (под будущий AmoCRM-матчинг; на платформе всегда NULL).
- Если только `X-API-Key` (нет юзера) → `employee` берётся из клиентского
  metadata как сейчас (escape-hatch; рекордер этот путь не использует).
- `_SERVER_OWNED_METADATA`: убрать `broker_id, amocrm_user_id, broker_name`;
  добавить `user_id`. `responsible_user_id` оставить server-owned. `employee`
  — server-owned ТОЛЬКО когда есть аутентифицированный юзер (иначе клиентский).

Атрибуция/скоуп авторитетно ведутся по `user_id`; `employee` (= employee_name)
— производный ключ для дашборда. Риск дублей `employee_name` между юзерами —
known-limitation (полная уникальность — future); `user_id` дублей не имеет.

## 4. Что удаляем (auth-идентичность брокера)

- `backend/app/routes/auth.py` — весь файл (`/api/auth/claim/start|complete`,
  `/login`, `/me`); убрать `include_router(auth.router)` из `main.py`.
- `backend/app/auth_jwt.py` — брокерские `create_token`/`get_current_broker`
  переписать в `create_user_token`/`get_current_user_token`; убрать
  grace-логику и `amocrm_user_id`/`name` из токена. Удалить мёртвый после
  дропа `routes/auth.py` bcrypt-стек (`hash_password`/`verify_password`,
  `CryptContext`/passlib) — он больше никем не используется (парольная
  верификация user-token = argon2, §2). Обновить докстринг/fail-loud-сообщение
  модуля («broker auth module» → user-token).
- `backend/app/models.py` — удалить модель `Broker`.
- `backend/app/schemas.py` — удалить `BrokerClaimStart*`, `BrokerLogin*`,
  `BrokerInfo`, `BrokerTokenResponse`; добавить схемы user-token.
- `backend/app/routes/sessions.py` — заменить `get_current_broker`-атрибуцию
  на user-атрибуцию (§3).
- `backend/app/auth_user.py` — обновить докстринг (упоминание broker JWT).
- `backend/app/config.py` + `.env` — удалить `BROKER_JWT_TENANT_GRACE_UNTIL`.
- `worker/scripts/sync_brokers.py` — удалить (брокеров из AmoCRM больше не
  заводим; будущий AmoCRM-user-sync — отдельный план при миграции rogov).
- `backend/tests/test_broker_jwt_tenant.py` — заменить на user-token тесты.
- `backend/tests/test_server_owned_metadata.py` — **обязательно обновить**:
  сейчас жёстко ассертит `broker_id, amocrm_user_id, broker_name` в
  `_SERVER_OWNED_METADATA` → после §3 эти ключи уходят, добавляется `user_id`.
- `backend/tests/test_access_matrix.py` — удалить/переписать
  `test_broker_auth_contour_stays_open` (бьёт `/api/auth/claim/start`, после
  удаления `routes/auth.py` вернёт 404/405).
- Миграция: DROP TABLE `brokers`.

**НЕ трогаем** (AmoCRM-внутренности, §«уточнение области»):
`amocrm_poll.py`, `amocrm_sync.py` (`responsible_user_id`, `_resolve_broker_name`),
`prior_context.py`, `quality.py`, `routes/amocrm.py`, `compare_reassess.py` —
там «broker» = AmoCRM-имя по `responsible_user_id`, не зависит от таблицы
`brokers` и не от recorder-auth. (Косметический ре-нейминг — вне области.)

## 5. Приложения (рекордер + расширение)

**Десктоп (`desktop-app/`):**
- Экран входа: убрать вкладки claim/login брокера → один экран
  email+пароль (manager). На сабмит → `POST /api/user-auth/token` → хранить
  токен в `credentials.enc` (как раньше `brokerJwt`).
- `recorder.js`: `buildHeaders` — убрать `X-Broker-Token`, добавить
  `X-User-Token`; `setBrokerToken` → `setUserToken`.
- `renderer.js`: гейт `if (!brokerJwt)` → `if (!userToken)`; логин-флоу
  переписать на `/api/user-auth/token`.
- Версия приложения: bump (4.0.0 → 4.1.0) — новый протокол входа.

**Расширение (`extension/`, `extension-yandex/`):**
- Реальное состояние (ревью уточнило): расширение использует **захардкоженный
  admin-Basic + захардкоженный rogov serverUrl**, без поля ввода ключа/сервера.
  Basic на платформе мёртв (Plan 3b снял Basic). Перевод на user-token — это НЕ
  «тот же контракт», а **новый popup-UI** (`popup.html`/`popup.js`): поля
  сервер + email + пароль → `/api/user-auth/token` → хранить токен →
  `offscreen.js` слать `X-User-Token`.
- Десктоп — приоритет и делается первым; **расширение — отдельный (второй)
  блок задач плана** или вовсе следующий план, т.к. объём = новый UI.
  В этой спеке фиксируем контракт, чтобы не разъехалось.

## 6. Миграция (тенант-трек 014)

```
ALTER TABLE users ADD COLUMN amocrm_user_id BIGINT;
CREATE UNIQUE INDEX ix_users_amocrm_user_id ON users (amocrm_user_id)
  WHERE amocrm_user_id IS NOT NULL;
DROP TABLE brokers;
```
- `down_revision = '013'` (текущий head тенант-трека = 013, проверено).
- На fulldent/shuravin `brokers` пуста → дроп безопасен. Свежие тенанты:
  007_brokers создаст таблицу, 014 её снесёт (идемпотентность трека
  сохраняется; альтернатива — отредактировать 007, но не трогаем историю).
- Дроп безопасен: на `brokers` нет внешних FK (атрибуция сессий — через
  jsonb-metadata, не FK), модель `Broker` нигде не импортируется по имени
  (только raw SQL в удаляемых `auth_jwt.py`/`routes/auth.py`).
- `down()` = дословная копия тела `007_brokers.upgrade()` (CREATE TABLE
  brokers со всеми server_default + UniqueConstraint `uq_brokers_amocrm_user_id`
  + индекс по `LOWER(email)`) ПЛЮС `DROP INDEX ix_users_amocrm_user_id` +
  `DROP COLUMN users.amocrm_user_id`. Сверить точный DDL по `007_brokers.py`.

## 7. Тестирование (TDD)

- `POST /api/user-auth/token`: manager/admin с argon2-паролем → 200 + JWT;
  viewer → 403; manager без employee_name → 422; неверный пароль → 401;
  rate-limit → 429; cookie НЕ ставится. (Регресс: argon2-пароль, НЕ bcrypt.)
- `get_current_user_token`: валидный → ctx; чужой tenant-claim → 401;
  протухший/битый → 401; отсутствует → None; **юзер удалён после выдачи →
  401**; **роль понижена до viewer после выдачи → 401** (DB re-read).
- `require_ingestion_auth`: user-token (manager) пускает; user-token viewer
  → отказ; X-API-Key и cookie по-прежнему работают; нет ничего → 401.
  (Зафиксировать: cookie-viewer на ingestion НЕ режется — это намеренно, §3.)
- `create_session` атрибуция: user-token → `metadata.user_id` + `employee`
  = employee_name; юзер с `amocrm_user_id` → `responsible_user_id` проставлен;
  X-API-Key-only → `employee` из клиента; server-owned ключи вычищены.
- Матрица доступа 5.6: добавить user-token-плоскость; брокер-кейсы убрать.
- Скретч-БД: миграция 014 (add column + drop brokers) зелёная; провижининг
  нового тенанта проходит; вставка manager-юзера и выдача токена работают.
- Регрессия воркера: `import` всех `worker/tasks/*` после удаления брокера
  без ошибок; AmoCRM-путь (дормант) не падает на отсутствии таблицы.
- e2e через silentqa после деплоя: manager логинится токеном → запись →
  транскрипт+скоринг → видна в дашборде под этим менеджером.

## 8. Деплой

Тот же путь: коммиты в ветку (`/root/projects/meet-mt`) → `git pull` в
`/root/projects/silentqa` → restart юнитов (миграция 014 на старте) →
из `.env` убрать `BROKER_JWT_TENANT_GRACE_UNTIL`. Пересобрать рекордер
(Win/Linux здесь, Mac на маке владельца) и опубликовать на `/download`
(инфраструктура уже стоит). Завести по тестовому manager-юзеру на
fulldent/shuravin для проверки. Откат: git revert + down-миграция 014.

## 9. rogov — будущая миграция (НЕ в этой спеке)

Когда rogov переедет в платформу: его `brokers` (с `amocrm_user_id`)
мигрируют в `users` (role=manager, `amocrm_user_id` заполнен), recorder-логин
станет user-token, AmoCRM-матчинг пойдёт через `users.amocrm_user_id` →
`metadata.responsible_user_id`. Отдельный план миграции; здесь только
закладываем `users.amocrm_user_id`.

## 10. Вне области (future)

- Ре-нейминг AmoCRM-внутренностей (`responsible_user_id`, `_resolve_broker_name`).
- Лёгкий режим «API-ключ + имя» без логина.
- AmoCRM-user-sync в `users` (часть rogov-миграции).
- Самброкер-провижининг / приглашения по email (SMTP).
