# Loop progress — universal-knowledge-base: де-realestate-ификация за module-флаги

> **Это рабочий трекер автономного лупа.** Источник истины о прогрессе — этот файл + git-история (НЕ чекбоксы в плане `2026-06-16-universal-knowledge-base.md`: они устарели, работа шла коммитами). Цель/спека: `docs/superpowers/specs/2026-06-16-universal-knowledge-base-design.md` (§4.3 «Гейтинг RE-утечки», §9 «полная чистка RE-строк»).

## Конфиг лупа
- **Цель:** доделать universal-knowledge-base — вычистить остатки realestate-хардкода за module-флаги.
- **Режим:** self-paced (dynamic /loop, без интервала).
- **Автономность (выбор пользователя):** работаем прямо в ветке `multi-tenant-core-phase1`, **коммит + пуш по каждой под-задаче**.
- **Железное правило проекта:** каждое изменение = generic + config-gated + бэкенд-сьют зелёный (`cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/ -q`). Воркер-сьют (`cd worker && /root/projects/silentqa/.venv/bin/python -m pytest -q`) — для задач, трогающих `worker/`.
- **Python/venv:** dev-venv нет; используем прод-venv `/root/projects/silentqa/.venv/bin/python`.
- **Фронтенд:** vanilla-JS, тест-харнесса нет → верифицируется инспекцией/рассуждением, не pytest.

## Базовая линия (аудит 2 агентов, 2026-06-22) — УЖЕ СДЕЛАНО, не трогать
Фазы 0–4 плана + бо́льшая часть §4 дашборда уже приземлились 06-16:
- s004 (modules jsonb + RE-backfill), tenant-014 (kb_categories/kb_entries/kb_entry_mentions).
- `app/modules.py` (module_enabled + require_module, дефолты KB=on/complexes=off/amocrm=off).
- Реестры тянут `modules` (tenancy_http.py + tenancy/registry.py).
- `GET /api/tenancy/features` (флаги + display_name + scenarios; impersonation-correct).
- AmoCRM-гейт = require_module('amocrm') (auth_user.py + iter_amocrm_tenants по modules).
- `/api/sessions/{id}/extraction` за require_module('complexes'); `/{id}/tags` + `kb_tag`-фильтр (бэкенд).
- provision пишет явные modules + сидит категорию «Термины» (НЕ ставит company_config_id).
- `/api/knowledge` router (gated knowledge_base) + `worker/scripts/seed_realestate_kb.py`.
- Фронт: `#knowledge` страница (gated nav), features-boot (display/module/admin gating), KB-теги в карточке звонка, «Профиль ЖК»/«Презентация ЖК»-хинт загрузки/#complexes-nav — gated.
- Бэкенд чист от `rogovestate.amocrm.ru` / `amocrm_subdomain`.

## Очередь оставшихся под-задач (порядок: безопасное/ценное первым)

- [x] **B1** — Gate `/api/complexes/*` router за `require_module('complexes')`. Реальный пропуск (был доступен любому тенанту). TDD: `test_complexes_blocked_when_module_off`. → коммит `498cc75`, бэкенд-сьют 225 зелёных, запушено.
- [x] **F1** — `#reprocess` nav → `data-module="amocrm"` (index.html:34). → коммит `f02943e`.
- [x] **F3** — Нейтральный дефолт title/logo/icon «Meeting Recorder»→«SilentQA» (index.html). display_name перекрывает на boot. → коммит `f02943e`.
- [x] **F4** — Кнопка «Привязать к лиду AmoCRM» (app.js:530) за `moduleOn('amocrm')` + early-guard в linkLeadModal (app.js:2795). → коммит `a5e8871`.
- [x] **F2** — RE-примеры в редакторе шаблонов генерализованы (app.js:2116/2121/2122) + extraction empty-state (app.js:2438). Намеренно ОСТАВЛЕНО: страница #complexes («База ЖК», app.js:3062/3071 — собственная страница RE-модуля, gated) и коммент 2234. → коммит `a5e8871`. `node --check app.js` OK.
- [x] **B2** — Десктоп Zoom-ЖК автотемплейт: гейт `AMOCRM_TENANT_SLUGS`→`module_enabled(...,'complexes')` (sessions.py, create_session читает request.state.tenant). TDD: дискриминирующий тест `test_complexes_tenant_without_amocrm_gets_zoom_template`. → коммит `975a008`, бэкенд-сьют 226 зелёных.
- [x] **B6+F5** — `/api/tenancy/features` отдаёт `amocrm_subdomain` из company-config (`amocrm_subdomain` ключ, гейт по модулю amocrm); `amoBase()` больше не мёртвая ветка. realestate.json получил субдомен. Воркер-writeback пока на env `AMOCRM_BASE_URL` (полная унификация → Phase 2). TDD: helper + 2 features-теста. → коммит `ddf536d`, бэкенд-сьют 229 зелёных.
- [ ] **F6** — `kb_tag`-фильтр UI в `#calls` (callsFilters+params app.js:308-311, контрол в баре 355-366, ссылка с KB-бейджа 588-589 → отфильтрованный список). Бэкенд готов.
- [ ] **B4** — Ретайр `AMOCRM_TENANT_SLUGS` как гейт внутренних RE-путей (sessions.py:428, worker/tasks/pipeline.py:459,736) → `modules.amocrm`; удалить константу registry.py:11. Средний риск, трогает worker.
- [ ] **B3** — Не сидить Zoom-ЖК шаблон миграцией 008 в не-complexes схемы (008_seed_zoom_meeting_template.py фанится на все). Высокий риск (миграции). Осторожно/в конце.
- [ ] **B5** — `scenario_id="outbound_residential"` (amocrm_poll.py:448) в конфиг, а не worker-константа. Низкий приоритет.
- [ ] **F7** *(опц.)* — route-level guards для `#complexes`/`#reprocess` (app.js:58,70): редирект вместо рендера 403-страницы при выключенном модуле.

## Лог итераций
- **Итерация 1:** загрузил план+спеку; 2 аудит-агента (backend+frontend) → карта done/remaining; составил очередь; сделал B1 (TDD, коммит+пуш `498cc75`); создал этот файл.
- **Итерация 2:** фронтенд-кластер F1/F3/F4/F2 — гейтинг `#reprocess` + AmoCRM lead-link за `moduleOn('amocrm')`, нейтральный бренд SilentQA, generic-примеры шаблонов. `node --check app.js` OK; Python не затронут (только `static/`). Коммиты `f02943e`, `a5e8871`, запушено.
- **Итерация 3:** B2 — десктопный Zoom-ЖК автотемплейт переведён со слуг-гейта на module_enabled(complexes); create_session теперь читает modules из request.state.tenant. TDD (дискриминирующий тест), бэкенд-сьют 226 зелёных. Коммит `975a008`, запушено.
- **Итерация 4:** B6+F5 — `/api/tenancy/features` эмитит `amocrm_subdomain` из company-config (гейт amocrm); добавлен helper `amocrm_subdomain_for` + инвалидация кеша; realestate.json получил субдомен; фронт-комментарий amoBase обновлён. TDD (helper + 2 features-теста), бэкенд-сьют 229 зелёных, app.js OK. Коммит `ddf536d`, запушено. **Дальше: F6** (kb_tag-фильтр UI в #calls — фронт, средний размер; бэкенд `?kb_tag=` уже готов).
