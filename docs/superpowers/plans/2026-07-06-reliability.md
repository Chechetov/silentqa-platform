# Пакет 3 «Надёжность» — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть надёжностные дыры ревью: risk-алерт не теряется при сбое записи в БД (новая находка), B-3 транзиентность по isinstance + бэкоф, B-1 ре-энкью потерянного analyze (готовый транскрипт не выбрасывается), B-2 идемпотентность analyze (без дубль-спенда LLM/дубль-нот при редоставке), B-4 ежедневные off-box бэкапы на релей-VPS (решение владельца 2026-07-06; ключ `~/.ssh/rogov_relay` уже на боксе).

**Architecture:** Четыре дизъюнктные задачи: T1 `results_db.py` (алерт через Redis-дедуп, независимый от upsert), T2 `pipeline.py` (isinstance-транзиентность + эксп. бэкоф + guard редоставки + дедуп plan-ноты), T3 `session_watchdog.py` (правило ре-энкью analyze), T4 бэкап-тулинг (скрипт + systemd-юниты + ранбук; установка на прод — контроллер). Прод-раскатка юнитов и Redis AOF — шаг контроллера после ревью.

**Tech Stack:** Redis SETNX (worker уже ходит в Redis — идиома `tasks/tenant_slots.py`), Celery retry, systemd timer, rsync поверх SSH.

## Global Constraints

- Русский в комментариях. Тесты pure-unit (FakeRedis-идиомы worker-тестов), cwd worker, python `../.venv/bin/python`.
- **Агенты НЕ делают git и НЕ трогают прод/systemd/Redis-конфиг** — установка юнитов, `CONFIG SET appendonly`, первый прогон бэкапа — шаги контроллера.
- Контракты не менять: сигнатура `record_quality_result(...)` публична (пайплайн+бэкфилл); контракт возврата `_run_pipeline`/`_analyze_inner`; семантика suppress_alert в бэкфилле.
- Файловые множества задач дизъюнктны: T1=results_db.py(+alerts.py при нужде)+его тесты; T2=pipeline.py+его тесты; T3=session_watchdog.py+его тесты; T4=scripts/backup_prod.sh+ops/systemd/*+ранбук.

---

### Task 1: risk-алерт развязан от успеха DB-записи

**Files:** Modify `worker/tasks/results_db.py`; тесты — существующий файл тестов results_db (`grep -l record_quality_result worker/tests/*.py`), дополнить.

**Проблема (новая находка ревью 2026-07-06):** весь `record_quality_result` — best-effort try/except (`results_db.py:272-274`), алерт живёт внутри и шлётся только при первом успешном insert (`:265`, `RETURNING xmax=0`). Упал upsert → алерт потерян навсегда (бэкфилл его не спасает — `suppress_alert=True`).

**Требования:**
1. Вычисление `flags` и решение об алерте НЕ зависят от исхода upsert'а. Alert-once — через Redis: `SET t:{slug}:risk_alerted:{session_id} 1 NX EX 604800` (7 дней TTL; slug — `tenancy.context.get_tenant_slug()`); ключ захвачен → шлём, не захвачен → уже алертили. Redis-клиент — по идиоме `tasks/tenant_slots.py` (прочитай её и переиспользуй подключение/паттерн).
2. Фолбэк: Redis недоступен (исключение) → использовать прежнюю семантику `inserted` (xmax) от УСПЕШНОГО upsert'а; если и upsert упал — алерт шлём безусловно (лучше редкий дубль при двойном сбое, чем потерянный риск) с WARNING в лог.
3. `suppress_alert=True` (бэкфилл) — как раньше: никаких алертов и никаких Redis-ключей.
4. Upsert остаётся best-effort; провал upsert больше НЕ проваливает и алерт (разнеси try/except: отдельно запись, отдельно алерт-блок).
5. Возврат функции (list[str] флагов) не меняется.

**Тесты (дополнить):** алерт шлётся при упавшем upsert (monkeypatch `_upsert`→raise); повторный вызов с тем же session_id не шлёт второй раз (FakeRedis с setnx-семантикой); redis-down → фолбэк на inserted; redis-down+upsert-down → алерт+WARNING; suppress_alert не трогает Redis. FakeRedis worker-тестов — проверь, есть ли `set(nx=...)`; при нужде расширь фейк.

TDD: тест → красный → реализация → зелёный → полный worker-сьют.

- [ ] Steps 1-4 по TDD; **коммит (контроллер):** `fix(worker): risk-алерт развязан от успеха записи в quality_results (Redis alert-once)`

---

### Task 2: pipeline — B-3 (isinstance+бэкоф) и B-2 (guard редоставки + дедуп plan-ноты)

**Files:** Modify `worker/tasks/pipeline.py`; тесты — `worker/tests/test_pipeline_stages.py` (дополнить, существующие ассерты не ослаблять).

**B-3 (`pipeline.py:52-58, 1041-1044`):**
1. `_is_transient` → приоритетно `isinstance` по кортежу: `openai.APITimeoutError|APIConnectionError|RateLimitError|InternalServerError`, `httpx.TimeoutException|ConnectError|ReadError`, `ConnectionError`, `redis.exceptions.ConnectionError|TimeoutError` (импорт redis в worker уже есть). Substring-маркеры по имени класса ОСТАВИТЬ как фолбэк (ловят обёртки других библиотек).
2. Ретрай с экспоненциальным бэкофом: `countdown=RETRY_BASE_SEC * (2 ** task.request.retries)` (env `ANALYZE_RETRY_BASE_SEC=120`; было фикс. 120) — найди точку `task.retry(countdown=...)` и поправь; max_retries не менять.

**B-2 guard (`_analyze_session_body`):** в самом начале тела (до KB/sentiment/LLM): если статус сессии уже `completed` И `quality.json` существует на диске (`tenant_results_dir(...)/quality.json`) → лог `[{sid}] Redelivery of completed session — skipping (acks_late)` и ранний return БЕЗ работы. Это закрывает главный дубль-спенд LLM при редоставке acks_late (visibility timeout ~1ч). Статус читай тем же способом, каким тело уже читает сессию (найди существующее чтение).

**B-2 дедуп plan-ноты:** ревью B-2 фиксировало: `amo_note_id` main-ноты пишется после create (узкое окно дубля — оставить как есть, задокументировано в ранбуке), а plan-нота (`create_plain_note`, поиск по pipeline.py/amocrm_sync) создаётся БЕЗусловно на каждом прогоне. Сделай зеркально main-ноте: после создания сохраняй `plan_note_id` в metadata сессии; перед созданием — если `plan_note_id` уже в metadata, пропусти с логом. Найди, как main-нота сохраняет `amo_note_id`, и повтори паттерн 1-в-1.

**Тесты:** isinstance-матрица `_is_transient` (реальные классы openai/httpx + фолбэк-подстрока + не-транзиентный ValueError→False); бэкоф-формула (retries=0→120, 1→240, 2→480 при дефолте); guard: completed+quality.json → ранний return (мок статуса и файла), incomplete → полный путь; plan-нота: второй прогон с plan_note_id в metadata не зовёт create (капчер).

- [ ] Steps по TDD; **коммит (контроллер):** `fix(worker): B-3 isinstance-транзиентность+бэкоф, B-2 guard редоставки + дедуп plan-ноты`

---

### Task 3: watchdog — B-1 ре-энкью потерянного analyze

**Files:** Modify `worker/tasks/session_watchdog.py`; тесты — `worker/tests/test_watchdog_processing_stale.py` (дополнить).

**Проблема:** правила 3a/3b (`session_watchdog.py:116-147`) помечают stale-processing сессию `failed` через 6ч; если стадия-1 успела (transcript.json на диске), потерянный handoff analyze выбрасывает дорогой транскрипт.

**Требования:**
1. Новое правило МЕЖДУ существующими (до 3a/3b-фейла): сессия `processing` + `transcript.json` существует + `quality.json` НЕ существует + фактический stale > env `ANALYZE_REENQUEUE_AFTER_MIN=30` (мин) → `celery_app.send_task("pipeline.analyze_session", queue="analysis", kwargs={...})` — сигнатуру kwargs возьми из места, где стадия-1 делает handoff (найди `send_task("pipeline.analyze_session"` в pipeline.py и повтори тот же состав; недостающие поля возьми из строки сессии/метадаты).
2. Счётчик в metadata сессии `analyze_reenqueue_count`: ре-энкьюим максимум 2 раза, дальше правило молчит и сессию добивают существующие 3a/3b (6ч, failed). Инкремент — тем же способом, каким watchdog уже пишет metadata (посмотри существующие правила).
3. Тот же временной маркер, что у 3a/3b (фактический старт, не created_at) — переиспользуй их вычисление stale.
4. Идемпотентность двойного ре-энкью безопасна: вход analyze теперь guard'ится (Task 2), но всё равно не энкьюй, если с прошлого ре-энкью прошло < ANALYZE_REENQUEUE_AFTER_MIN (пиши в metadata и timestamp последнего ре-энкью).

**Тесты:** transcript есть+quality нет+stale → send_task вызван с очередью analysis (капчер), счётчик=1; счётчик=2 → send_task НЕ вызван; quality.json есть → правило молчит; свежий последний ре-энкью → молчит. Идиомы — существующий test_watchdog_processing_stale.py.

- [ ] Steps по TDD; **коммит (контроллер):** `feat(worker): watchdog ре-энкьюит потерянный analyze (B-1) вместо выброса транскрипта`

---

### Task 4: бэкап-тулинг (B-4) — файлы в репо, установка за контроллером

**Files:** Create `scripts/backup_prod.sh`, `ops/systemd/silentqa-backup.service`, `ops/systemd/silentqa-backup.timer`; Modify `docs/superpowers/specs/2026-06-10-phase1-deploy-runbook.md` (секция «Бэкапы»).

**Требования к `scripts/backup_prod.sh`** (bash, `set -euo pipefail`, идемпотентный, безопасный к повторному запуску):
1. Конфиг через env с дефолтами: `BACKUP_SSH_KEY=/root/.ssh/rogov_relay`, `BACKUP_REMOTE=root@89.207.255.231`, `BACKUP_REMOTE_DIR=/root/backups/silentqa-box`, `BACKUP_KEEP_DAILY=7`, `PROD_DIR=/root/projects/silentqa`.
2. Дампы в локальный staging-каталог `/root/backups/daily/YYYY-MM-DD/`: `pg_dump -Fc` БД из `DATABASE_URL_SYNC` прод-`.env` (пароль через `PGPASSWORD`, разобрать URL; НЕ печатать секреты в stdout) + `pg_dumpall --globals-only`; копия Redis `dump.rdb` (путь из `redis-cli CONFIG GET dir/dbfilename`, через `--rdb` НЕ надо — просто cp файла); tar.gz прод-`.env` и `companies/` (в тар — права 0600).
3. rsync на релей: (а) дампы дня → `$BACKUP_REMOTE_DIR/daily/YYYY-MM-DD/`; (б) зеркало медиа `$PROD_DIR/data/` → `$BACKUP_REMOTE_DIR/data-mirror/` (rsync -az --delete). SSH-опции: `-i $BACKUP_SSH_KEY -o BatchMode=yes -o ConnectTimeout=10`.
4. Ротация: локально и на релее держать `BACKUP_KEEP_DAILY` последних каталогов daily (удаление стрinformed по имени-дате, на релее — через ssh find/sort|tail).
5. По завершении — однострочный итог в stdout (размеры, длительность) и exit 0; любой сбой — ненулевой exit (timer подсветит failed unit).

**systemd-юниты** (`ops/systemd/`): service Type=oneshot, `ExecStart=/root/projects/silentqa-dev/scripts/backup_prod.sh`, `Nice=10`, `IOSchedulingClass=idle`; timer `OnCalendar=*-*-* 03:30:00`, `RandomizedDelaySec=15m`, `Persistent=true`, `WantedBy=timers.target`. В шапках юнитов — коммент, что установка: `cp ops/systemd/silentqa-backup.* /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now silentqa-backup.timer`.

**Ранбук:** секция «Бэкапы (B-4, 2026-07-06)»: что бэкапится, куда, ротация, как восстановить (pg_restore -Fc, rsync медиа обратно), как проверить (`systemctl list-timers`, `journalctl -u silentqa-backup`), предупреждение «.env в бэкапе = секреты на релее, каталог 0700», и что Redis переведён на AOF (`appendonly yes`) контроллером.

**Верификация агента:** `bash -n scripts/backup_prod.sh` (синтаксис); `systemd-analyze verify ops/systemd/silentqa-backup.*` если доступен (иначе отметить в отчёте); НИЧЕГО не устанавливать и не запускать против прода/релея.

- [ ] Steps; **коммит (контроллер):** `feat(ops): ежедневный off-box бэкап на релей-VPS (скрипт+systemd) — B-4`

---

## Волны исполнения (SDD)

- **W1 = [T1, T2, T3, T4] параллельно** — файлы дизъюнктны.
- Контроллер после W1: полные сьюты, 4 ревью, финал-ревью (fable), push, CI; затем прод-шаги: установка бэкап-юнитов, первый прогон бэкапа с проверкой файлов на релее, `redis-cli CONFIG SET appendonly yes` + правка redis.conf, деплой кода пакетов 1-3 (`scripts/deploy_prod.sh`).

## Definition of Done

- Все сьюты зелёные; существующие тесты не ослаблены.
- Алерт переживает сбой записи в БД и не дублируется при редоставке (тесты).
- Редоставка completed-сессии не жжёт LLM; plan-нота не дублируется.
- Потерянный analyze ре-энкьюится ≤2 раз по фактическому stale ≥30 мин; транскрипт переиспользуется.
- `bash -n` бэкап-скрипта чист; юниты валидны; ранбук описывает restore.
