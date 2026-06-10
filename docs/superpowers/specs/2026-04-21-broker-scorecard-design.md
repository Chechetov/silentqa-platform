# Broker scorecard — analytics page for rogov-partner-portal

**Date:** 2026-04-21
**Status:** Draft — awaiting input from ком. директор on Q1 (stage-history strategy) and Q4 (privacy)
**Parent:** Phase 3.3b of `2026-04-17-phase3-phase4-roadmap.md`. Same principles apply: outcome over activity, privacy by default, аналитика живёт в нашей админке (не в AmoCRM).

## Why this exists

Ком. директор просит «единую картинку по отделу». Сегодня данные размазаны: «Просмотры ЖК» показывают ЖК × менеджер, «Звонки» — количество и средний рейтинг, но нет per-broker сводки и нет воронки «звонок → следующий этап». Задача страницы — ответить на вопросы пользователя дословно:

> сколько менеджер делает исходящих звонков, какой результат в переход на следующий этап, какие показы у каких брокеров, в каком формате и сколько их, со статистикой по каждому брокеру и анализом эффективности всего отдела.

Переводим в метрики per broker + department row, без добавления полей в AmoCRM (никакого задрачивания брокеров) и без webhook-зависимости (базовый тариф их не отдаёт).

## Source data & known constraints

- **Звонки.** AmoCRM notes `note_type=call_out`. Путь: `GET /api/v4/leads/notes?filter[note_type]=call_out` — уже реализован в `analytics_service._fetch_all_notes_by_type`. Брокер = `responsible_user_id` родительского лида (`lead_map[entity_id].responsible_user_id`). Оценка — `int` из `"Оценка: N/10"` в `note.text` (логика уже есть в `get_calls`).
- **Сделки и статусы.** `GET /api/v4/leads?with=custom_fields_values,contacts` + `GET /api/v4/leads/pipelines`. `pipeline_id` + `status_id` уже доступны (используется в `get_dashboard`).
- **История смен статуса.** AmoCRM НЕ отдаёт её на базовом тарифе через webhooks. Варианты (см. Q1):
  - (a) **Снапшот текущего распределения** по стадиям per broker — считается из `/leads` за один проход, 0 накладных расходов. Нет истории.
  - (b) **AmoCRM events API** — `GET /api/v4/events?filter[type]=lead_status_changed&filter[created_at][from]=…`. До ввода в спеку **проверить, что endpoint доступен на нашем OAuth-скоупе** (roadmap явно писал, что webhooks недоступны; про events — не подтверждено, нужно щёлкнуть curl-ом на проде перед закладкой в v2).
  - (c) **Собственные ежедневные снапшоты** в `portal.broker_scorecard_snapshots` — deltas из них же. Работает независимо от тарифа, но копит данные «с сегодня».
- **Просмотры ЖК и формат встречи.** Поля «Встреча 1–15» (`ZK_FIELD_IDS` в `analytics_service.py`) — multiselect, в котором сегодня лежат и format-метки («Встреча в Zoom», «Встреча в офисе компании»), и ЖК («High life», «Сбер Сити»), часто в одном multiselect. Нормализатор (пишется параллельно) отдаст `{format: "zoom"|"office"|"site"|"unknown", zk_name: "…"}` с дедупом «Сбер Сити» vs «СберСити». Scorecard **потребляет нормализованное**, сам не парсит.
- **Отдельного custom-поля «Формат встречи» в AmoCRM нет.** См. Q2 — рекомендуем попросить админа AmoCRM завести реальный dropdown, параллельно продолжая парсить multiselect (нормализатор остаётся fallback'ом).
- **Bugdet.** Для среднего чека и распределений берём `lead.price` (integer, уже грузится). Phase 3.2 `budget_parser` для клиентского бюджета здесь не нужен — scorecard считает только по закрытым сделкам.

## Metrics catalogue

Таблица per-broker + строка «Отдел» (агрегат по всем). Все метрики — за выбранный период (по умолчанию 30 дней), фильтр `since=<ts>` в query.

| Колонка | Тип | Формула / источник | Cost (v1/v2) |
|---|---|---|---|
| `broker_name` | str | `users[responsible_user_id].name` | v1 |
| `outgoing_calls` | int | `count(notes where note_type='call_out' and lead.responsible_user_id=broker and created_at>=since)` | v1 |
| `avg_call_rating` | float\|null | `mean(rating)` по его звонкам, где `rating is not null` | v1 |
| `rated_call_share` | float | `rated / total` (доля звонков с распарсенной оценкой) | v1 |
| `short_call_ratio` | float | `count(duration<20) / total` (duration — из `params.duration`) | v1 |
| `viewings_total` | int | `count(нормализованных записей просмотров)` по менеджеру | v1 (после нормализатора) |
| `viewings_by_format` | `{office:int, zoom:int, site:int, unknown:int}` | group by `format` | v1 |
| `viewings_by_zk_top3` | `[{zk, count}]` | top-3 ЖК менеджера | v1 |
| `active_leads` | int | `count(leads where responsible=broker and status not in [142,143])` | v1 |
| `leads_by_status` | `{status_name: count}` | текущее распределение по стадиям воронки (снапшот) | v1 |
| `won_count`, `won_sum` | int, int64 | `count/sum(price)` по `status_id=142` за период (по `closed_at` если доступен, иначе `updated_at`) | v1 |
| `lost_count` | int | `count` по `status_id=143` за период | v1 |
| `conversion_to_won` | float | `won_count / (won_count + lost_count)` | v1 |
| **Funnel (v2, нужна история):** | | | |
| `moved_to_showings` | int | `count(events type=lead_status_changed, new_status_id in (статусы «Показы»), broker_at_time=this)` | v2 (см. Q1) |
| `moved_to_deal` | int | аналогично до стадии «Сделка/Договор» | v2 |
| `stage_conversion[k→k+1]` | float | `moved_to_{k+1} / entered_stage_{k}` | v2 |
| `follow_through_rate` | float | из `sessions.metadata.previous_recommendations_follow_through` (Phase 2 пайплайна) — `sum(executed_count)/sum(total_recommendations)` по его звонкам | v1 (если Phase 2 пайплайна в проде — уже так) |
| `median_days_in_stage` | int | `median(now - last_status_change)` для его активных лидов | v2 (нужна история) |

**Строка «Отдел»:**
- Суммы: `outgoing_calls`, `viewings_total`, `won_sum`, `active_leads`.
- Медианы: `median(outgoing_calls_per_broker_per_week)`, `median(viewings_per_deal)` (просмотры ÷ unique deals с ≥1 показом), `median(avg_call_rating)`.
- Воронка по всему отделу: `enter_stage_k → enter_stage_k+1` — те же числа, просуммированные по брокерам.
- Gini/std по звонкам: простая std активности (чтобы видеть «один тащит, остальные спят»).

## Data pipeline

**Новый модуль:** `app/services/broker_scorecard_service.py`. Не ломаем `analytics_service.py`, переиспользуем `_amo_get`, `_fetch_all`, `_fetch_all_notes_by_type`, `_fetch_users`. (Можно вытащить эти три helper'а в `_amo_utils.py`, если захочется причесать — но это полировка.)

**Основной entrypoint:** `async def get_scorecard(db, since: datetime, until: datetime, force=False) -> dict`.

Шаги:
1. `leads = await _fetch_all(db, "leads", {"with": "custom_fields_values,contacts"})` — один раз, большой запрос (уже делаем в других endpoint'ах).
2. `notes = await _fetch_all_notes_by_type(db, ["call_out"])` — фильтровать по `created_at` клиентски (API filter есть: `filter[updated_at][from]` и `filter[created_at][from]` — используем сразу, чтобы не тянуть историю за весь год).
3. `users = await _fetch_users(db)`, `pipelines = await _amo_get(db, "leads/pipelines")`.
4. `viewings_normalized = await viewings_normalizer.get_normalized(db)` — **зависимость на новый сервис нормализатора**, который возвращает `[{lead_id, broker_id, format, zk_name, date}]`.
5. In-memory агрегация: `broker_id → {outgoing_calls: [...], viewings: [...], leads: [...]}`.
6. Свёртка метрик (pure-python, никакого LLM).
7. Кэш: ключ `f"scorecard:{since.isoformat()}:{until.isoformat()}"`, TTL 5 минут (как у всех остальных страниц analytics_service).

**Что on-the-fly vs persisted:**

| Слой | v1 (on-the-fly) | v2 (persisted) |
|---|---|---|
| Звонки, просмотры, leads-by-status | Пересчёт из AmoCRM, кэш 5 мин | — |
| `won/lost` за период | Снапшот AmoCRM, фильтр по `closed_at` | — |
| Переходы по стадиям (funnel) | **Нет** (снапшот распределения вместо funnel) | Daily cron → `portal.broker_scorecard_snapshots (date, broker_id, status_id, count)` + `portal.broker_stage_transitions (date, lead_id, from_status, to_status, broker_id)` |
| Follow-through | Join с `sessions.metadata.previous_recommendations_follow_through` (Phase 2 пайплайна) | — |

**Новые таблицы (v2):**
```sql
CREATE TABLE portal.broker_scorecard_snapshots (
  snapshot_date DATE NOT NULL,
  broker_id BIGINT NOT NULL,
  pipeline_id BIGINT NOT NULL,
  status_id BIGINT NOT NULL,
  leads_count INT NOT NULL,
  PRIMARY KEY (snapshot_date, broker_id, pipeline_id, status_id)
);

CREATE TABLE portal.broker_stage_transitions (
  event_id BIGINT PRIMARY KEY,           -- AmoCRM event id (идемпотентность)
  occurred_at TIMESTAMPTZ NOT NULL,
  lead_id BIGINT NOT NULL,
  broker_id BIGINT NOT NULL,             -- snapshot на момент перехода
  pipeline_id BIGINT NOT NULL,
  from_status_id BIGINT,
  to_status_id BIGINT NOT NULL
);
CREATE INDEX idx_bst_broker_time ON portal.broker_stage_transitions (broker_id, occurred_at);
CREATE INDEX idx_bst_lead ON portal.broker_stage_transitions (lead_id);
```

**Cron (v2):** `apscheduler` job, каждое утро в 03:00 МСК:
1. Для каждого лида сравниваем текущий `status_id` со snapshot вчерашнего дня → если отличается **И** events API недоступен, пишем синтетический transition с `event_id = hash(date, lead_id)`.
2. Пишем свежий снапшот.

Если events API **доступен** (Q1 резолвнется в пользу (b)), то cron дополнительно подтягивает `GET /events?filter[type]=lead_status_changed&filter[created_at][from]=<last_cursor>` и апсёртит по `event_id`.

## Pseudocode for aggregation

```
def build_rows(leads, notes, viewings, users, pipelines, since, until):
    status_name = { s.id: s.name for p in pipelines for s in p.statuses }
    by_broker = defaultdict(_empty)
    for lead in leads:
        b = by_broker[lead.responsible_user_id]
        if lead.status_id not in (WON, LOST):
            b.active_leads += 1
            b.leads_by_status[status_name[lead.status_id]] += 1
        elif since <= closed_at(lead) <= until:
            if lead.status_id == WON:
                b.won_count += 1; b.won_sum += lead.price or 0
            else:
                b.lost_count += 1
    for n in notes:
        if not (since <= n.created_at <= until): continue
        lead = lead_map.get(n.entity_id)
        if not lead: continue
        b = by_broker[lead.responsible_user_id]
        b.outgoing_calls += 1
        if n.params.duration < 20: b.short_calls += 1
        if rating_of(n): b.ratings.append(rating_of(n))
    for v in viewings:
        if not (since <= v.date <= until): continue
        b = by_broker[v.broker_id]
        b.viewings_total += 1
        b.viewings_by_format[v.format] += 1
        b.zk_counter[v.zk_name] += 1
    # финальная свёртка в DTO
    return [finalize(bid, b, users) for bid, b in by_broker.items()] + [department_row(by_broker)]
```

## API

Следуем `app/api/analytics.py`:

- `GET /analytics/scorecard` — Jinja-страница (`staff/analytics/scorecard.html`), `Depends(require_staff)`.
- `GET /analytics/api/scorecard?since=YYYY-MM-DD&until=YYYY-MM-DD&force=1` — JSON.
- `GET /analytics/api/scorecard/download` — Excel (pandas, как у `viewings/download`): 3 листа — «По брокерам», «Отдел — воронка», «Все просмотры с форматом».

## UI sketch

`app/templates/staff/analytics/scorecard.html`, тот же каркас что `viewings.html` (stat-cards + tabs + table).

```
┌─────────────────────────────────────────────────────────────────┐
│ Scorecard брокеров          [период: 30 дней ▼]  [Обновить][Excel]│
├─────────────────────────────────────────────────────────────────┤
│ [Звонков: 412] [Показов: 87] [Сделок: 4] [Отдел-conv: 8.1%]     │
├─────────────────────────────────────────────────────────────────┤
│ Tabs: [По брокерам*] [Воронка отдела] [Просмотры: формат × ЖК]  │
├─────────────────────────────────────────────────────────────────┤
│ Брокер       │Звон│Ср.оц│<20c│Показы (оф/зум/объект)│Акт│Won│Conv│
│ Иванов       │ 87 │ 7.4 │18% │ 14 (6/3/5)            │ 23│ 1 │ 5% │
│ Петрова      │112 │ 8.1 │ 9% │ 22 (10/4/8)           │ 31│ 2 │ 9% │
│ …            │    │     │    │                       │   │   │    │
│ ОТДЕЛ        │412 │ 7.6 │14% │ 87 (40/15/32)         │118│ 4 │ 8% │
└─────────────────────────────────────────────────────────────────┘
```

Клик по имени брокера → drill-down панель справа: funnel-столбики по стадиям, топ-5 ЖК, последние 10 звонков (ссылка в AmoCRM).

**Privacy toggle** (см. Q4 в roadmap): боковой переключатель "Показать имена" — по умолчанию только роль `commercial_director` видит имена; `broker` видит свою строку выделенной + остальные анонимизированы («Брокер A», «Брокер B», сортировка сохранена). Роль читаем из `staff.role`; маскирование — на сервере (НЕ на фронте), иначе F12 раскрывает.

## Privacy & access

- Всё внутри `require_staff`. Дополнительно внутри endpoint-а: если `staff.role != 'commercial_director'` и `staff.role != 'admin'` → ответ с анонимизированными именами + `me_broker_id` чтобы фронт подсветил свою строку.
- Excel-download доступен только `commercial_director` / `admin` — анонимизация в Excel теряет смысл, либо exportim с реальными именами только привилегированным.

## Rollout plan

**v1 (cheap, 1–2 недели после того, как нормализатор встреч в проде):**
- Модуль `broker_scorecard_service.py`.
- Эндпоинты `/analytics/scorecard` + `/api/scorecard` + `/api/scorecard/download`.
- Шаблон `scorecard.html` (по образцу `viewings.html`).
- Метрики v1 из таблицы выше (всё, что не funnel).
- **НЕ делаем:** stage-transition funnel, median_days_in_stage.
- В шаблоне показываем плашку: «Переходы по стадиям — в v2, когда подключим собственные снапшоты / events API.»

**v2 (после резолва Q1, 1–2 недели):**
- Миграция: `portal.broker_scorecard_snapshots` + `portal.broker_stage_transitions`.
- Cron job (apscheduler) на ежедневный снапшот; опционально — events-API poll.
- Funnel-колонки в DTO; воронка отдела в отдельной вкладке (bar-chart).
- `median_days_in_stage` из накопленных снапшотов (после ≥14 дней сбора).

**v3 (nice to have):**
- Календарный фильтр «за квартал / за год».
- Export PDF для еженедельного разбора на летучке.
- Сравнение «этот период vs прошлый» (delta-колонки).

## Open questions

- **Q1 (критично, для v2):** События смен стадий. (1) Доступен ли `GET /api/v4/events?filter[type]=lead_status_changed` на нашем OAuth-скоупе базового тарифа? (2) Если нет — OK ли нам решение (c) — собственные ежедневные снапшоты, первые цифры funnel появятся через 14 дней после деплоя? Нужен ответ ком. директора + проверка curl-ом события API перед закладкой в v2.
- **Q2 (для AmoCRM-админа):** Ввести custom-поле «Формат встречи» (dropdown: офис / zoom / на объекте / другое)? Сейчас парсим из multiselect «Встреча N» — работает, но хрупко. Отдельное поле закрывает проблему в корне; нормализатор остаётся как fallback на исторические данные.
- **Q3 (привязка звонка к брокеру):** `responsible_user_id` на момент звонка может отличаться от текущего (лид передали). Базовая версия — использует «кто отвечает сейчас». Хватит ли? Альтернатива: сохранять `broker_id_at_note_creation` в отдельной таблице в момент ingest'а звонка — дешево (мы уже и так обрабатываем каждый call-note в пайплайне) и честнее. Рекомендуем последнее.
- **Q4 (privacy — уточнение Q3 roadmap):** Подтвердить: `broker` видит себя + анонимизированных коллег, `commercial_director` видит всё. OK? Если нет — дефолт «только себя, без сравнения» → ломает «анализ эффективности всего отдела», нужна явная санкция.
- **Q5:** Ключевые этапы воронки. Какие `status_id` считать «Квалификация → Показы → Сделка → Оплачено» для отдела? В `pipelines` статусы именованы свободно; нужно либо маппинг в конфиге, либо спросить ком. директора, какие имена статусов — канонические stage-переходы, которые он хочет видеть в колонках.
- **Q6:** Excel-выгрузка для всех staff или только для директора? (Сейчас предлагаем второе.)

## Out of scope

- Полноценная BI-витрина (Metabase / Superset). Живём в своей админке, никаких внешних инструментов.
- Гейминг брокеров (бейджи, прогресс-бары, рейтинги типа «№1 недели»). Принцип «не задрачивание».
- Speed-to-first-call как метрика (уже исключено в roadmap).
- Любые push-нотификации брокеру от scorecard'а. Scorecard — только для ком. директора; инфоповоды брокеру идут через Phase 4 digest.
- Парсер транскриптов под метрики вида «доля разговоров с упоминанием депозита». Если понадобится — отдельная фича поверх `sessions.quality_report`.
- Real-time обновление. 5-минутного кэша хватает.
