# Универсальная «База знаний» + per-tenant видимость дашборда

**Дата:** 2026-06-16 (v2 — после многоагентного адверсариального ревью, 53 находки сведены)
**Ветка/воркспейс:** `multi-tenant-core-phase1` в `/root/projects/silentqa-dev` (чистый клон GitHub `Chechetov/silentqa-platform`, не worktree прода).
**Статус:** дизайн утверждён на брейншторме (4 раздела) + усилен по ревью; готов к плану реализации.

## Зачем

Платформа (`silentqa`) — это движок realestate + мультитенантность. Но в дашборд и схему **каждого** тенанта протекает real-estate-специфика:
- таблицы `complexes`, `complex_extractions`, `extraction_templates` есть в каждой схеме — «База ЖК» в стоматологии = бессмыслица;
- пункт меню «База ЖК» (`#complexes`), секция «Профиль ЖК» в карточке звонка, подсказка «Презентация ЖК» на загрузке, дефолтный Zoom-ЖК-шаблон, хардкод-ссылка `rogovestate.amocrm.ru`;
- `word_boost` (правильные написания терминов) живёт в per-deployment `companies/<id>.json` (редактируется только через раздел «Компании», не является per-tenant базой знаний, и завязан на легаси-механизм AssemblyAI).

Нужен **универсальный инструмент**: каждый тенант заводит свою настраиваемую **Базу знаний** (бренды филлеров, болезни/методы лечения, термины), а RE-специфика прячется у тех, кто её не использует.

## Решения, принятые на брейншторме

1. Строим на `silentqa-dev`, не на realestate. realestate — прод, не трогаем.
2. **Подход 1:** новая generic-подсистема «База знаний» (`kb_categories`+`kb_entries`), отдельная от ЖК-стека (`complexes`/extraction). Старый стек остаётся как RE-фича, гейтится module-флагом.
3. **4 роли** одними записями: **A** ASR-корректность, **B** LLM-глоссарий, **C** накопление, **D** таксономия/теги.
4. **Наполнение — только курируемое:** вручную + импорт списком. Авто-извлечение в KB — за скобками v1.
5. **Роль A — корректность через пост-обработку транскрипта** (engine-agnostic). ASR-boost — лишь опциональный best-effort, корректность от него НЕ зависит.
6. **Видимость — per-tenant module-флаги.** ЖК прячется гейтингом, без разрушительного rename.
7. Дашборды по ролям (платформа/РОП/менеджер, Plan 3b/3c) НЕ переделываем — KB только врезается.
8. **Без удаления.** Только добавления + гейтинг.

---

## 1. Модель данных

Три новые таблицы в схеме **каждого** тенанта (tenant-track Alembic).

### `kb_categories`
```
id            uuid pk
name          text not null
slug          text not null
description   text
feeds_asr     boolean not null default false   -- роль A: записи → ASR-hint + нормализация
feeds_llm     boolean not null default false   -- роль B: записи → глоссарий
is_taxonomy   boolean not null default false   -- роли D+C: записями тегируются звонки
created_at    timestamptz not null default now()
updated_at    timestamptz not null default now()
unique(slug)
```

### `kb_entries`
```
id            uuid pk
category_id   uuid not null references kb_categories(id) on delete cascade
term          text not null
aliases       jsonb not null default '[]'
description   text
metadata      jsonb not null default '{}'
created_at    timestamptz not null default now()
updated_at    timestamptz not null default now()
unique(category_id, term)        -- дедуп на уровне БД; POST /entries → 409 при конфликте
index(category_id)
```

### `kb_entry_mentions` (роль C)
```
id            uuid pk
entry_id      uuid not null references kb_entries(id) on delete cascade
session_id    uuid not null references sessions(id) on delete cascade
count         integer not null default 1
created_at    timestamptz not null default now()   -- = время первой записи связи; для тренда join к sessions.created_at
updated_at    timestamptz not null default now()
unique(entry_id, session_id)
index(session_id, entry_id)   -- обратный join «теги этого звонка»; unique(entry_id,session_id) покрывает прямой
```
Каскады на `entry_id`/`session_id` — как в существующем `DELETE /api/sessions` (sessions.py:194).

---

## 2. Провязка в пайплайн

Новый модуль **`worker/tasks/knowledge_base.py`**. Все функции:
- открывают соединение сами через `tenant_connect()`/`tenant_engine()` (полагаясь на contextvar `set_tenant_schema`, который `process_session` уже ставит, pipeline.py:818) — **не принимают чужой `db`**, чтобы не обойти search_path;
- **обёрнуты в try/except → деградируют в пустой результат + log, НИКОГДА не валят транскрипцию** (правило [[always-add-fallbacks]]; пайплайн иначе ставит `failed` и ре-райзит — pipeline.py:864).

| Функция | Назначение |
|---|---|
| `kb_keyterms() -> list[str]` | роль A слой 2: термины из `feeds_asr`, cap ≤1000, дедуп |
| `kb_build_matcher() -> Automaton` | компилирует Aho-Corasick (или единый `re.compile('|'.join(map(re.escape, aliases)))`) из всех (alias→term, entry_id) **один раз за прогон** |
| `kb_normalize(transcript, matcher) -> (transcript, hits)` | роль A слой 1 + D/C |
| `kb_glossary() -> str` | роль B: блок из `feeds_llm`, cap по токенам |
| `kb_record_mentions(session_id, hits)` | роль C |

### Роль A — корректность написания (двухслойно)

**Слой 1 (авторитетный) — нормализация транскрипта, engine-agnostic.**
Размещение: на **уже смёрженном со спикерами** транскрипте, ПОСЛЕ ветки disk-cache (pipeline.py ~612), чтобы **reprocess тоже нормализовал** (репроцесс грузит `transcript.json` и пропускает `transcribe_audio`, pipeline.py:579-587). Если сессия идёт по extraction-ветке (ранний return ~630) — kb_normalize выполняется до неё (extraction-сессии тоже нормализуются/тегируются; см. §2 «extraction»).
- матч через `matcher` (Aho-Corasick / единая escape'нутая альтернация) по `term`+`aliases` (регистронезависимо, по границам слов) — **O(T + matches), не O(T×E)**;
- **переписываем `segment['text']`** (его читают и LLM, и сохранённый транскрипт: quality.py:1063, extract.py:34) — это обязательно; пословный `words[]` опционально для плеера;
- оригинал сегмента сохраняем в `segment['orig_text']` (новое опциональное поле; даунстрим читает известные ключи — не ломается);
- нормализация **идемпотентна** (канон на входе → канон на выходе, no-op) — безопасно для reprocess;
- возвращает `hits=[(entry_id, count)]` для ролей D/C.
- **Корректность гарантирована независимо от ASR-движка.**

**Слой 2 (опциональный upstream-hint) — best-effort, корректность от него НЕ зависит.**
⚠️ **Уточнено ревью:** в установленном SDK `assemblyai==0.58.0` `keyterms_prompt` документирован как фича модели **Slam-1**, а текущий код использует `speech_models: ["universal-3-pro","universal-2"]` (transcribe.py:91). Маркетинг-доки AssemblyAI заявляют keyterms_prompt и для universal-3-pro — **противоречие, верифицировать на этапе плана против живого API** (см. §11). Поэтому:
- **Текущая модель (universal):** оставляем рабочий `word_boost` как upstream-hint, наполняя его из KB (`kb_keyterms()`), а не только из конфига. `word_boost` на universal-моделях работает (хоть и легаси).
- **keyterms_prompt** используем ТОЛЬКО если/когда тенант на slam-1 (отдельное модельное решение).
- **Единое правило выбора (одно, без неоднозначности):**
  `IF движок=assemblyai И модель=slam-1 И kb_keyterms непуст → keyterms_prompt=kb_keyterms (БЕЗ word_boost; они взаимоисключающи, как и с prompt). ELSE → word_boost = (config.word_boost ∪ kb_keyterms), дедуп.`
- НЕ выставляем `speech_model` (singular enum — `universal-3-pro` не входит в enum SDK); модель задаётся существующим `speech_models` (list[str]).
- **Cap ≤1000 + дедуп применяется на границе `transcribe.py` к тому списку, что реально уходит в AssemblyAI** (и keyterms, и word_boost), с логом отброшенного.

### Роль B — глоссарий в LLM
`kb_glossary()` вставляется в **user**-промпт:
- `extract.py::run_extraction` (~line 84, перед транскриптом в user-сообщении);
- `quality.py::assess_quality` (signature получает `glossary: str|None`): и в structured-путь (`_assess_with_structured_output`, USER_PROMPT ~1209), **и в legacy-путь** (`_assess_with_legacy_prompt`, `ASSESSMENT_PROMPT` ~1252) — иначе при фоллбэке structured→legacy глоссарий молча исчезает.
- **Cap:** ≤ ~2000 токенов (мерить `_approx_tokens`, extract.py:30); при превышении — усечь с маркером «… ещё N записей опущено»; приоритет: категории `feeds_llm` по `created_at`, записи по `created_at`. Пусто → блок не добавляется.

### Роли D + C — теги и накопление
Из `hits` (категории `is_taxonomy`) → `kb_record_mentions(session_id, hits)`:
- upsert по `unique(entry_id, session_id)`, **`count = EXCLUDED.count` (замена, НЕ `+=`)** — чтобы reprocess не раздувал статистику;
- даёт фильтр звонков по тегу (D) и накопление (C).
- v1 — детерминированный матч; LLM-тегирование за скобками.

---

## 3. Бэкенд API

Новый роутер **`backend/app/routes/knowledge.py`** по образцу `routes/templates.py`. Префикс `/api/knowledge`. Все роуты за **module-gate `knowledge_base`** (§5.3).

| Метод | Путь | Гейт | Назначение |
|---|---|---|---|
| GET | `/categories?include_entries=true` | viewer | категории; `include_entries` → вложенные записи одним JOIN (анти-N+1 для страницы) |
| POST | `/categories` | admin | создать |
| PATCH | `/categories/{id}` | admin | изменить (имя/флаги). **Изменение флагов НЕ ретроактивно** (см. §8) |
| DELETE | `/categories/{id}` | admin | удалить (каскад записей) |
| GET | `/categories/{id}/entries` | viewer | записи (lazy-load/edit) |
| POST | `/entries` | admin | создать (409 при `unique(category_id,term)`) |
| PATCH/DELETE | `/entries/{id}` | admin | изменить/удалить |
| POST | `/import` | admin | импорт CSV/строк → пачка; дедуп по `term` |
| GET | `/entries/{id}/mentions` | viewer + **employee_scope для manager** | `[{session_id, created_at, count}]` (роль C; основа тренда) |

Также (для §4):
- **`GET /api/sessions/{id}/tags`** (viewer + employee_scope) — `kb_entry_mentions` сессии: term, категория, count.
- **`GET /api/sessions?kb_tag=<entry_id>`** — фильтр списка звонков через join к `kb_entry_mentions` (+ employee_scope для manager, как везде).

**RBAC:** мутации — admin; чтение — viewer; **manager — чтение, но mentions/tags/`kb_tag`-фильтр СКОУПятся по `employee_scope`** (sessions.metadata->>'employee' == user.employee_name; auth_user.py:67-71), иначе менеджер увидит чужие звонки.
**Импорт/матчинг — безопасность:** жёсткий лимит тела (конкретное число задать на плане, §11) + max записей за импорт; алиасы только через `re.escape`/Aho-Corasick (никогда raw-regex — иначе ReDoS); cap общего числа алиасов на тенанта (питает и ≤1000 keyterms, и матчер).

---

## 4. Дашборд

Дашборды по ролям (Plan 3b/3c) **не редизайним**. Добавляем:

### 4.1 Страница «База знаний» (`#knowledge`)
По образцу `#templates`: категории (тумблеры флагов) → записи (`term`,`aliases`,`description`); CRUD (admin) + «Импорт списком»; на карточке записи — статистика роли C (упоминания, связанные звонки; **тренд** строится фронтом из `[{created_at,count}]`). Пункт меню виден при `knowledge_base`.

### 4.2 Врезка KB в существующие дашборды
- **В карточке звонка `#call/<id>`** — новая секция «Теги KB» из `GET /api/sessions/{id}/tags`.
- **Фильтр по KB-тегу** в `#calls` и дашбордах §6.1–6.3 (param `kb_tag`).
- Синергия с банком возражений (§6.4) — за скобками этой спеки.

### 4.3 Гейтинг RE-утечки (полный список из ревью)
За `module.complexes` (фронт + бэкенд) прячем НЕ только `#complexes`, а ВСЁ:
- секцию «Профиль ЖК» в карточке звонка (app.js:547-557) **и эндпоинт `GET /api/sessions/{id}/extraction`** (sessions.py) → `require_module('complexes')`;
- подсказку «Презентация ЖК» на загрузке (app.js:1423);
- примеры «Презентация ЖК» в редакторе шаблонов (app.js:1890,1895);
- авто-применение Zoom-ЖК-шаблона десктоп-записям (`_DEFAULT_DESKTOP_TEMPLATE_NAME`, sessions.py:123,159-162) + сид этого шаблона в migration 008 (фанится на все схемы).
- хардкод-ссылку `rogovestate.amocrm.ru` (app.js:2275,2322) сделать tenant-конфигурируемой (из bootstrap) — гейтится `module.amocrm`.

---

## 5. Module-флаги (ядро «универсального дашборда»)

### 5.1 Хранение и семантика дефолтов (ИСПРАВЛЕНО по ревью)
Shared-track миграция `s004` (lowercase, `down_revision='s003'`): `modules jsonb not null default '{}'` в `shared.tenants`.
**Семантика отсутствующих ключей (критично — иначе ЖК возвращается новым стоматологиям):**
- `knowledge_base` отсутствует → **ON** (generic, по умолчанию у всех);
- `complexes`, `amocrm` отсутствуют → **OFF** (RE-специфика, строго opt-in).
То есть resolver трактует каждый ключ по своему дефолту, а не «{} = всё включено».

### 5.2 Чтение фронтом
Новый **`GET /api/tenancy/features`** (читает `request.state.tenant['modules']`, заполняемый middleware) — корректно работает под impersonation (отдаёт модули целевого тенанта, не платформы). НЕ грузим в `/api/user-auth/me` (он user-scoped). SPA прячет пункты меню по флагам. **UI-сокрытие — не защита.**

### 5.3 Гейтинг бэкенда (реально подключить)
- **Расширить SELECT'ы реестра**, которые сейчас НЕ тянут modules: `tenancy_http.py` (async TenantRegistry, ~line 36-49) и worker-side `tenancy/registry.py` (~line 19). Без этого `require_module` нечего читать.
- Зависимость `require_module(name)` читает `request.state.tenant['modules']` с дефолтами §5.1.
- **`invalidate()` реестра после любой мутации флагов** (как при suspend/rotate) — иначе TTL-кеш отдаёт старое.
- **Реконсиляция AmoCRM:** существующий хардкод `AMOCRM_TENANT_SLUGS=('realestate',)` (registry.py:11) + `require_amocrm_tenant` (auth_user.py:129) **заменяются** на `require_module('amocrm')`; worker-beat (`iter_*`-пути) тоже читают modules. Один источник истины.

### 5.4 Дефолты и бэкофилл
- Провижининг **пишет явный `modules`** (не полагается на `{}`): новый тенант → `{knowledge_base:true, complexes:false, amocrm:false}`.
- Бэкофилл в `s004` — **по явному правилу для ВСЕХ существующих тенантов** (не только трёх по слугам): всем `knowledge_base:true`; `complexes/amocrm:true` только для `realestate`; остальным — false.

### 5.5 Воркер
KB-шаги в пайплайне **data-driven**: пустые категории = no-op. Module-флаг = UI/API-гейт. (Опционально воркер тоже скипает KB при `knowledge_base:false`, что требует тех же modules в worker-снапшоте — низкий приоритет, т.к. пустой результат безвреден.)

---

## 6. Миграции и сидинг (без DROP)

**① Tenant-track — KB-таблицы** (`kb_categories`, `kb_entries`, `kb_entry_mentions`).
**Номер фиксируется на старте плана** (`alembic heads` на тенант-треке): спека `2026-06-14-unify-recorder-identity` тоже метит `014` (`down_revision='013'`). Решение: **эта спека = `014`, unify-recorder-identity ребейзится на `015`** (его `revision='015'`, `down_revision='014'`). Не оставлять «координацию на потом» — два `014` дадут Alembic `MultipleHeads` и backend не стартует.

**② Shared-track `s004`** — `modules jsonb` в `shared.tenants` + бэкофилл по правилу §5.4. (Двух-трековый раннер: `run_shared()` до тенант-цикла, migrate.py:54-56 — порядок безопасен.)

**③ «База ЖК» — НЕ переименовываем, НЕ дропаем.** Гейтим `module.complexes`.

**④ Сидинг (идемпотентно, без коллизий `unique(slug)`):**
- дефолтная категория «Термины» (`feeds_asr+feeds_llm`) создаётся **find-or-create** при провижининге;
- **перенос realestate `word_boost` — отдельный one-shot ops-скрипт** `worker/scripts/seed_realestate_kb.py` (НЕ tenant-track миграция: та фанится на ВСЕ схемы и засеяла бы 53 RE-термина в стоматологию). Скрипт: ключ `slug='realestate'` через `shared.tenants`, читает `asr.word_boost` (**53 термина**, не ~30), **upsert** в find-or-create категорию «Термины», idempotent (skip если непусто). Старый `word_boost` в конфиге остаётся fallback.

**⑤ Провижининг:** `backend/app/provision_tenant.py::provision()` (НЕ `worker/scripts/`): добавить `modules` в `INSERT INTO shared.tenants` (~line 104) и сид KB-категории после `run_tenant(schema)` (~line 112).

---

## 7. Тестирование (TDD)

- `kb_normalize`: регистр/границы слов/многословные алиасы; **идемпотентность** (повторный прогон = no-op); **`segment['text']` нормализован** (его читает LLM); Aho-Corasick корректность на большом транскрипте.
- `kb_keyterms`/`kb_glossary`: cap, дедуп, выборка по флагам, пустой случай; глоссарий и в structured, и в **legacy** quality-пути.
- `kb_record_mentions`: upsert count=replace; **reprocess не раздувает count**.
- Роуты: RBAC; **module-gate** (`knowledge_base:false`→403 на `/api/knowledge/*`; `complexes:false`→403 на `/api/complexes/*` и `/api/sessions/{id}/extraction`); **изоляция менеджера** (mentions/tags/`kb_tag` отдают только свои звонки); изоляция тенантов (search_path).
- Фоллбэк: при отсутствии KB-таблиц/ошибке kb_* — транскрипция НЕ падает.
- Миграции: up/down tenant `014` + shared `s004`; бэкофилл флагов (новый/неизвестный тенант → complexes OFF); seed-скрипт realestate (только realestate, идемпотентен).
- Провижининг: новый тенант получает явные modules + категорию «Термины».

---

## 8. Совместимость и «без удаления»

- `complexes`/`complex_extractions`/`extraction_templates` — остаются, гейтятся флагом.
- `word_boost` в конфигах — остаётся fallback; редактируемость через «Компании» сохраняется.
- Ни одной DROP-операции. Пайплайн с фоллбэками — поведение realestate не меняется.
- **Смена флагов категории НЕ ретроактивна:** выключение `is_taxonomy` не удаляет старые `kb_entry_mentions`, включение не бэкофиллит историю. Ретро-бэкофилл (батч-reprocess) — §9.

---

## 9. За скобками v1 (future)

- Авто-извлечение сущностей из записей в KB.
- LLM-тегирование вместо строкового матча.
- Ретроактивный бэкофилл mentions при смене `is_taxonomy`.
- Кластеризация банка возражений на инфраструктуре KB (Plan 3c §6.4).
- Полная чистка остальных хардкод-RE строк (`_DEFAULT_DESKTOP_TEMPLATE_NAME` sessions.py:123, гейтинг сида migration 008, `scenario_id="outbound_residential"`) — если не закрыто в §4.3.
- Phase 2: реестр интеграционных адаптеров (`2026-06-10-multi-tenant-core-design.md §8`); module-флаги — первый шаг.

---

## 10. Деплой (ИСПРАВЛЕНО — реальная топология)

**Промоушен-путь.** silentqa-прод `origin` = локальный `/root/projects/meet` (не GitHub); `meet` имеет remote `canonical`→`/root/projects/silentqa-platform.git`; dev-воркспейс — клон GitHub. Поэтому «push GitHub → prod git pull» как есть **не работает**. До деплоя:
- **перенацелить `origin` silentqa-прода на GitHub** (`git remote set-url`) — однократно, тогда `git pull` тянет dev-ветку; **либо** пушить dev→GitHub и отдельно синхронизировать локальный канон/прод. Зафиксировать ОДИН канонический remote и проверить `git -C /root/projects/silentqa fetch && git log origin/multi-tenant-core-phase1` показывает нужный коммит ДО заявления «задеплоено».

**Порядок рестарта (критично — окно «нет таблицы»).** Миграции гонит ТОЛЬКО backend на старте (`python -m app.migrate`, main.py:26-33); воркер рестартят отдельно (run.sh) и он миграции НЕ гонит. → **Сначала рестарт backend (применит `014`/`s004`) + проверить `alembic heads`/наличие таблиц, ПОТОМ рестарт worker.** Независимо: фоллбэки в `knowledge_base.py` (§2) защищают от гонки.

**Лок номера миграции** — на старте плана (`alembic heads`), см. §6①.

---

## 11. Верифицировать на этапе плана (открытые технические пункты)

1. **keyterms_prompt × модель:** SDK 0.58.0 docstring говорит Slam-1; маркетинг-доки — universal-3-pro. Проверить против живого AssemblyAI API, какой параметр+модель реально boost'ит термины. От результата зависит реализация слоя 2 (но НЕ корректность — её держит слой 1). Запинить версию SDK явно.
2. **Конкретные лимиты:** байт-cap тела импорта, max записей за импорт, max алиасов на тенанта, точный токен-cap глоссария.
3. **Aho-Corasick vs compiled-alternation** — выбрать по бенчу на реальном 40-мин транскрипте × 1000 записей; решить кэш/инвалидацию скомпилированного матчера при правке KB.
