# Phase 1 cutover — прод-ранбук

Прод: /root/projects/realestate, systemd realestate-{backend,worker}.service.

1. Бэкап: pg_dump + tar data/. Прогнать весь Task-15 сценарий на копии прод-БД.
2. `systemctl stop realestate-worker realestate-backend` (beat переживёт
   паузу: POLL_SAFETY_WINDOW). Backend тоже стоит: сессия, залитая между
   этим шагом и шагом 4, легла бы в legacy-раскладку `$AUDIO/sessions/<id>`,
   и новый worker её чанков не найдёт (merge_chunks смотрит в
   `<slug>/sessions/`) → сессия уйдёт в failed.
3. git pull; `.venv/bin/pip install -r backend/requirements.txt` (argon2-cffi).
4. `bash scripts/migrate_storage_layout.sh` (с env из .env!).
5. В .env добавить: `BASE_DOMAIN=silentqa.com`, `DEFAULT_TENANT=` (пусто —
   прод резолвится через custom_domains=rogov.automate-it.fun из S002).
6. `systemctl restart realestate-backend` — lifespan гонит app.migrate
   (S001→S002 cutover→tenant 011-012). Смотреть journalctl: "[migrate] done".
7. Сид: `cd backend && ../.venv/bin/python -m app.provision_tenant realestate
   --seed-only --admin-email <email>` → записать API-ключ.
8. `systemctl start realestate-worker`.
9. Smoke: дашборд по https://rogov.automate-it.fun открывается (Basic Auth
   пока жив — Plan 2 заменит); POST тестовой сессии; AmoCRM-поллер в логах
   работает по t_realestate.
10. Откат: stop both → СНАЧАЛА тенант-трек
    `alembic -x tenant_schema=t_realestate downgrade 010` (снимает 011-012,
    т.е. DROP users; без этого s002-downgrade падает на DROP SCHEMA
    t_realestate — схема не пуста) → затем
    `alembic -c alembic_shared.ini downgrade s001` (таблицы возвращаются в
    public, stamp '010' переносится автоматически — ручной UPDATE не нужен,
    проверить: `SELECT version_num FROM public.alembic_version` → 010) →
    обратный mv каталогов (см. scripts/migrate_storage_layout.sh) →
    git checkout прежнего коммита → start.

## Интеграционная верификация (репетиция на скретч-БД) — выполнено 2026-06-11

Сценарий Task 15 прогнан на чистой БД `phase1_mt_scratch` (PG 16, юзер из
.env). Итоги:

- «Старый прод» эмулируется `alembic upgrade 010` (НЕ `head`: на ветке head
  тенант-трека = 012, а прод стоит на stamp '010').
- `python -m app.migrate`: S001 → S002 (cutover: 8 таблиц public →
  t_realestate, stamp '010' перенесён, тенант realestate зарегистрирован,
  api_key_required=f) → тенант-трек 010→012. `public.sessions` = NULL,
  `public.alembic_version` = NULL, `t_realestate.alembic_version` = 012,
  `t_realestate.users` существует. Повторный прогон идемпотентен.
- Провижининг второго тенанта: `python -m app.provision_tenant acme --name
  "ACME" --admin-email admin@acme.test --admin-password secret123` →
  t_acme.alembic_version = 012, admin-юзер создан, API-ключ `sqa_...`
  напечатан один раз. Сиды шаблонов легли в t_acme: 3 шт (004 «Презентация
  ЖК» + 005 «Звонок брокера» + 008 «Zoom-встреча») — идентично t_realestate.
- Если provision_tenant упал ПОСЛЕ регистрации тенанта (т.е. после первого
  commit), остаётся полусозданный тенант: строка в shared.tenants + схема
  t_<slug> без миграций/сидов. Повторный запуск заблокируется проверкой
  «already exists». Зачистка перед ретраем:
  `DELETE FROM shared.tenants WHERE slug='<slug>'; DROP SCHEMA IF EXISTS
  t_<slug> CASCADE;` — затем повторить провижининг.
- SQL-изоляция: INSERT в t_acme.sessions не виден при
  `search_path=t_realestate,...` (count = 0).
- Откат: порядок строго «тенант-трек → shared-трек» (см. шаг 10). Прямой
  `downgrade s001` без снятия 011-012 падает (проверено) и безопасно
  откатывается транзакцией — БД остаётся в рабочем состоянии.

## Plan 2 (auth) — дополнение к деплою

Деплой Plan 2 идёт ТЕМ ЖЕ релизом, что и ядро (одна ветка). Дополнительно к
шагам выше:

1. Перед рестартом backend в .env добавить:
   `BROKER_JWT_TENANT_GRACE_UNTIL=<дата деплоя + 30 дней, YYYY-MM-DD>`
   (без неё все ранее выданные брокерские JWT умрут сразу — спека 5.4).
2. Убедиться, что admin-юзер посеян (сид-CLI шаг 7 выше; иначе после снятия
   Basic в дашборд никто не войдёт — риск «локаут» из спеки §10).
   Проверка: `POST /api/user-auth/login` с сид-кредами → 200 + Set-Cookie.
3. DELETE_PASSWORD из .env можно удалить (больше не читается).
   AUTH_USERNAME/AUTH_PASSWORD ОСТАВИТЬ — interim-гейт /api/companies
   (доступен только с платформенного контура: Host=silentqa.com или IP
   при пустом DEFAULT_TENANT).
4. Прод-статика realestate: из каталога Caddy (/home/dev/projects/...)
   удалить посторонние zip-артефакты расширений, если лежат (спека 5.2);
   в этом репо их нет.
5. Smoke после рестарта:
   - GET / → index.html БЕЗ Basic-промпта; логин-форма в SPA;
   - login viewer-юзером → кнопки удаления/переоценки скрыты;
   - POST /api/sessions без ключа → 201 (api_key_required=false);
   - POST /api/sessions с X-API-Key: sqa_wrong → 403;
   - GET /api/companies на тенант-домене → 404; на платформенном — 401/Basic;
   - GET /api/webhooks → 404;
   - звонок из AmoCRM-поллера: нота содержит ссылку на
     https://rogov.automate-it.fun/#call/... (dashboard_base_url из S002).
6. Завершение rollout (через ≤30 дней, по факту обновления клиентов):
   `UPDATE shared.tenants SET api_key_required = TRUE WHERE slug='realestate';`
   и удалить BROKER_JWT_TENANT_GRACE_UNTIL из .env (конец grace).
   Ключ генерится вручную кодом _set_api_key (флага ротации в CLI нет):
   `cd backend && ../.venv/bin/python -c "
   from app.provision_tenant import _set_api_key
   from tenancy.db import shared_connect
   conn = shared_connect(); print(_set_api_key(conn, 'realestate')); conn.commit(); conn.close()"`
   — вывод показывается один раз, раздаётся клиентам.

### Откат Plan 2
Код-откат = git revert ветки; данных Plan 2 не создаёт (users уже была в 012,
сессии в Redis истекают сами). После отката вернуть DELETE_PASSWORD в .env.
