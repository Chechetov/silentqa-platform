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

## silentqa platform — задеплоено 2026-06-12

Отдельный прод-деплой платформы (realestate/meet НЕ тронуты):
- Код: `/root/projects/silentqa` (git clone /root/projects/meet, ветка
  multi-tenant-core-phase1, коммит 68e2f12), свой .venv.
- БД `silentqa` (PG 5432, роль silentqa), Redis 6379/4, порт 8007,
  юниты `silentqa-{backend,worker}.service` (worker с -B).
- Секреты деплоя/клиентов: `/root/projects/silentqa/.deploy-secrets` (0600).
- Caddy: глобальный `on_demand_tls ask → :8007/api/tenancy/domain-check` +
  блок `silentqa.com, *.silentqa.com → :8007`. Бэкап: Caddyfile.bak-silentqa.
- ML-кеш общий: /root/.cache/huggingface (HOME в юнитах не переопределён).
- Тенанты: fulldent, shuravin (admin = alex.chechetov@gmail.com,
  api_key_required=true). Смоук пройден: login/роли, dual-auth
  (401/403/201), изоляция сессий и cookie между тенантами, ghost-хост 404.

Обновление платформы: коммит в ветку (worktree /root/projects/meet-mt) →
`cd /root/projects/silentqa && git pull github multi-tenant-core-phase1` (remote прода называется github) →
`systemctl restart silentqa-backend silentqa-worker` (backend сам гонит
миграции на старте).

Новый клиент: `cd /root/projects/silentqa/backend && set -a; source ../.env;
set +a; ../.venv/bin/python -m app.provision_tenant <slug> --name "<Имя>"
--admin-email <email> --admin-password "$(openssl rand -base64 15)"` —
ключ печатается один раз; поддомен и сертификат появляются сами.

DNS (Cloudflare, зона silentqa.com): `A * → 89.207.255.231` и
`A @ → 89.207.255.231`, обе DNS-only (серая тучка). Без них публичный
доступ/сертификаты не работают (relay - passthrough, на нём ничего не надо).

### Деплой Plan 3b (админка + команда + роль manager)

1. `cd /root/projects/silentqa && git pull github multi-tenant-core-phase1` (remote прода называется github)
2. `systemctl restart silentqa-backend silentqa-worker` — S003+013 накатятся
   на старте (journalctl: "[migrate] done").
3. Бутстрап владельца:
   `cd backend && set -a; source ../.env; set +a; ../.venv/bin/python -m app.platform_admin create --email alex.chechetov@gmail.com`
   — пароль печатается один раз. (provision_tenant теперь тоже печатает
   сгенерированный пароль админа клиента — изменение вывода CLI.)
4. Из `.env` удалить `AUTH_USERNAME`, `AUTH_PASSWORD` (Basic выпилен) и
   рестартнуть backend ещё раз.
5. Проверить Redis ≥ 6.2 (`redis-cli INFO server | grep redis_version`) —
   impersonation использует GETDEL.
6. Смоук: https://admin.silentqa.com → логин → список клиентов со
   статистикой; impersonate в fulldent (бейдж «режим поддержки»);
   suspend/activate тестом НЕ на живом клиенте; «Команда» у fulldent.

## Деплой после 2026-07: миграции — шаг деплоя, не старта

С коммита «feat(startup): lifespan — check-only …» рестарт юнита сам
миграции НЕ применяет (старт делает только read-only сверку и CRITICAL-лог).
Канонический деплой прода:

    /root/projects/silentqa-dev/scripts/deploy_prod.sh

(rollback-SHA в лог → FF-merge origin/multi-tenant-core-phase1 → pip install
→ python -m app.migrate → systemctl restart silentqa-backend silentqa-worker
→ curl /health/ready с ретраями до 30с).
Откат миграции при фейле: старый код продолжает работать; чинить миграцию
на стейджинге (silentqa-staging-*, :8008) и повторять деплой. Откат кода:
git reset --hard <SHA из лога деплоя> && systemctl restart silentqa-backend silentqa-worker.

Прим.: прод-remote назван `github`, а не `origin` (проверено 2026-07-02
`git -C /root/projects/silentqa remote -v` →
`https://github.com/Chechetov/silentqa-platform.git`). Скрипт по умолчанию
фетчит `github` (`REMOTE="${REMOTE:-github}"`); при иной раскладке —
`REMOTE=origin scripts/deploy_prod.sh`.

## io-воркер `analysis`: acks_late и редкий дубль AmoCRM-заметки

У `pipeline.analyze_session` (очередь `analysis`, юнит `silentqa-worker-io`)
`acks_late=true`: при жёсткой смерти io-воркера задача НЕ теряется, но
восстаёт ТОЛЬКО по visibility_timeout Redis-брокера (~1 час; рестарт воркера
unacked-сообщение не восстанавливает — проверено смоуком 2026-07-02). До этого
сессия висит в processing; страховка — watchdog-правило 3a (6ч). Занижать
visibility_timeout не надо: опция глобальная, значение ниже длительности
analyze-прогона даст конкурентный дубль задачи. В узком окне между созданием AmoCRM-заметки и записью `amo_note_id`
в метаданные сессии возможен редкий дубль заметки (только AmoCRM-тенанты).
При жалобе клиента на дубль — проверять журнал `silentqa-worker-io` на этот
момент (`journalctl -u silentqa-worker-io`).

## Body-size limit в Caddy (ревью C-2, добавлено 2026-07-06)

Приложение держит потолки само (`MAX_REQUEST_BODY_MB=600` middleware +
капы ингеста в chunks.py). Внешний слой на проде — Caddy: в блок
`silentqa.com, *.silentqa.com` добавить

    request_body {
        max_size 600MB
    }

и `systemctl reload caddy`. ✅ **Применено на проде 2026-07-06** (validate + reload,
все контуры 200) — блок выше оставлен как справка для новых окружений.

## Бэкапы (B-4, 2026-07-06)

Ежедневный off-box бэкап платформы `/root/projects/silentqa` на релей-VPS
(`root@89.207.255.231`, SSH-ключ `/root/.ssh/rogov_relay`). Скрипт —
`scripts/backup_prod.sh`; юниты — `ops/systemd/silentqa-backup.{service,timer}`.

**Что бэкапится** (локальный staging `/root/backups/daily/YYYY-MM-DD/`, umask 077):

- `db.dump` — `pg_dump -Fc` БД из `DATABASE_URL_SYNC` (прод `localhost:5432/silentqa`).
- `globals.sql` — `pg_dumpall --globals-only --no-role-passwords` (роли/tablespaces БЕЗ паролей — non-superuser не читает pg_authid; восстановить ДО pg_restore, пароли ролей задать заново вручную).
- `redis-dump.rdb` — копия `dump.rdb` (путь из `redis-cli CONFIG GET dir/dbfilename`).
  **Best-effort**: основная durability Redis переведена на **AOF** (`appendonly yes`,
  выставлено контроллером в `redis.conf` + `redis-cli CONFIG SET appendonly yes`);
  если snapshot-а нет — шаг пропускается с WARN, бэкап не падает.
- `config.tar.gz` (0600) — прод `.env` + `companies/`.

**Куда / ротация**: rsync на релей — (а) дампы дня → `…/silentqa-box/daily/YYYY-MM-DD/`,
(б) зеркало медиа `data/` → `…/silentqa-box/data-mirror/` (`rsync -az --delete`).
Держатся последние `BACKUP_KEEP_DAILY=7` daily-каталогов **и локально, и на релее**
(старше — удаляются по имени-дате). Конфиг скрипта — env с дефолтами
(`BACKUP_SSH_KEY/BACKUP_REMOTE/BACKUP_REMOTE_DIR/BACKUP_KEEP_DAILY/PROD_DIR`).

**⚠️ Безопасность**: `config.tar.gz` содержит прод-секреты (`.env`) → на релее лежат
секреты. Каталог `…/silentqa-box` и все дампы создаются с правами 0700/0600; держать
`/root/backups` на релее только root-доступным.

**Установка (контроллер, один раз)**:

    cp ops/systemd/silentqa-backup.* /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now silentqa-backup.timer

Таймер: `OnCalendar=*-*-* 03:30:00`, `RandomizedDelaySec=15m`, `Persistent=true`
(пропущенный из-за простоя запуск догоняется). Первый прогон вручную:
`systemctl start silentqa-backup.service` → проверить файлы на релее.

**Проверка**:

    systemctl list-timers | grep silentqa-backup     # next/last запуск
    journalctl -u silentqa-backup -n 50               # лог + итоговая строка [backup] OK …
    ssh -i /root/.ssh/rogov_relay root@89.207.255.231 'ls -la /root/backups/silentqa-box/daily'

**Восстановление**:

- Роли/tablespaces: `psql -h localhost -p 5432 -U <su> -f globals.sql`.
- БД: `pg_restore -Fc -h localhost -p 5432 -U <user> -d silentqa --clean --if-exists db.dump`
  (или в свежую БД без `--clean`). `db.dump` — custom-format, не SQL-текст.
- Конфиг: `tar -xzf config.tar.gz -C /root/projects/silentqa` (перезапишет `.env`+`companies/`).
- Медиа: `rsync -az -e "ssh -i /root/.ssh/rogov_relay" root@89.207.255.231:/root/backups/silentqa-box/data-mirror/ /root/projects/silentqa/data/`
  (обратное направление; ключ `-i /root/.ssh/rogov_relay`).
- Redis: обычно не восстанавливается (сессии/кеш/брокер — эфемерны); при нужде
  остановить redis, положить `redis-dump.rdb` в `dir` как `dbfilename`, стартовать.

**Верификация тулинга** (агент, 2026-07-06): `bash -n scripts/backup_prod.sh` — чисто;
`systemd-analyze verify` обоих юнитов — exit 0; shellcheck на боксе не установлен (не прогнан).
