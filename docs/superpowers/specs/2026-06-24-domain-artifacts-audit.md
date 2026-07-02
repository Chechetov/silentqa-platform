# Аудит доменных артефактов «врач» / «брокер» (де-реалэстейт / де-дентал)

Дата: 2026-06-24. Повод: запись на тенанте **chechetov** отрисовалась как дентал-«карта
приёма» с выбором врача. Цель — выписать ВСЕ места, где доменные артефакты (дентал «врач»,
realestate «брокер»/«ЖК») протекают в общие клиентские поверхности и общий код.

## TL;DR — две природы артефактов

1. **Config-driven (мис-биндинг):** chechetov привязан в `shared.tenants.company_config_id`
   к `dental` вместо `chechetov`. Лечится ребиндингом конфига + рестартом воркера +
   reprocess двух записей. Контент «карты приёма», дентал-сценарии в приложении и QA-протокол
   — всё отсюда.
2. **Захардкоженные строки (де-доменизация):** даже при ПРАВИЛЬНОМ конфиге chechetov часть
   ярлыков останется дентал/брокерскими, потому что они зашиты в общую SPA/десктоп/воркер.
   Лечится только правкой кода (это и есть направление universal-knowledge-base).

---

## A. Лечится ребиндингом конфига (chechetov→chechetov) + reprocess

| Что видел юзер | Откуда | Механизм |
|---|---|---|
| «Карта приёма»: пациент / зубная формула / план лечения | `backend/static/app.js:2519` `renderDentalCard` | Диспетчер `renderCard` (app.js:2467) выбирает дентал-рендерер **по форме данных** (`card.dental_status !== undefined` или `card.patient`-объект). Дентал-конфиг отдал дентал-shaped `card.json` → 🦷. При своём конфиге chechetov карта generic → `renderGenericCard` (📋). |
| «Тип приёма»: «Очная консультация / Презентация плана лечения» | десктоп `loadAppointmentTypes` ← `/api/tenancy/features` (`backend/app/routes/tenancy_check.py:51` `scenarios_for(company_config_id)`) | Сценарии берутся из dental-конфига. При своём конфиге chechetov — один сценарий «Созвон». |
| Дентал-стиль QA-оценки | dental `quality.protocol` в `companies/dental.json` | Конфиг-резолв в воркере (`tenant_company_config_id()`), кеш на процесс → нужен рестарт. |

---

## B. Захардкожено — останется после ребиндинга (нужна правка кода)

### Дашборд `backend/static/`
| Место | Артефакт | Замечание |
|---|---|---|
| `app.js:511` | `const _currentCardLabel = 'Карта приёма'` | Передаётся в ЛЮБУЮ карточку (app.js:614). `card_extraction.label` («Итоги созвона») **нигде не используется** — card-эндпоинт (`routes/analysis.py:51`) отдаёт только `card.json` без label. Даже корректная generic-карточка получит заголовок «📋 Карта приёма». **Фикс:** прокинуть label из конфига (через `/api/tenancy/features` или в `card.json`). |
| `app.js:1490` | `<label>Сотрудник / врач</label>` (форма загрузки) | Виден всем тенантам. |
| `app.js:1743` | `<option value="doctor">Врач</option>` (редактор роли спикера) | Виден всем тенантам. |
| `app.js:2519-2554` | весь `renderDentalCard` (🦷, Пациент, Жалобы, Анамнез, Диагноз, Зубная формула, План лечения) | Дентал-специфичный рендерер в общей SPA. Дремлет для generic-карт, но триггерится чисто по форме данных. |

### Десктоп `desktop-app/src/renderer/`
| Место | Артефакт | Замечание |
|---|---|---|
| `index.html:171` | `<label for="appointmentType">Тип приёма</label>` | Дентал-ярлык; для созвонов д.б. «Сценарий»/«Тип созвона». Опции — config-driven (см. A), но **ярлык** захардкожен. |
| `index.html:244` | «Выйти из аккаунта брокера» | В панели настроек. |
| `index.html:84` | «Введите email и пароль брокера.» | Экран брокер-логина (mode-gated, скрыт в api-key-режиме chechetov). |
| `index.html:87,107` | placeholder `broker@example.com` | — |
| `renderer.js:192` | `label.textContent = 'Брокер: '` | Баннер идентичности (в api-key-режиме показывается «Сотрудник: », renderer.js:168 — это OK). |
| `renderer.js:516` | «Брокер с таким email не найден…» | — |
| `renderer.js:566` | «Брокер не найден или неактивен» | — |
| `recorder.js:251` | «Сессия брокера истекла…» | — |
| `styles.css:390-392` | `.broker-info` коммент/класс | косметика. |

Весь брокер-логин — mode-gated (скрыт в api-key-режиме), но строки лежат в общем бинарнике.

### Worker `worker/`
| Место | Артефакт | Замечание |
|---|---|---|
| `tasks/card.py:40` | user-msg `"Транскрипт приёма:\n\n"` | Хардкод «приёма» во ВСЕХ извлечениях карточки (включая generic). Безвредно по смыслу, но дентал-флейвор. |
| `tasks/quality.py:209-912` | вся V4/extended QA-схема+промпты: `broker_response`, `quote_from_broker`, «брокер элитной недвижимости», ЖК, застройщик, форматы встреч | Gated `use_extended = bool(scenario and scenario.get("prompt"))`. Для chechetov при верном конфиге НЕ срабатывает (v2-база). Но это realestate-хардкод в общем воркере. |

### Расширение `extension/`
| Место | Артефакт | Замечание |
|---|---|---|
| `popup.js:21` | `SERVER_URL = "https://rogov.automate-it.fun"` | Вся extension — realestate-эра, не знает про per-tenant API-key (известный дрифт, CLAUDE.md). |
| `popup.js:23` | `AUTH_PASSWORD = "rogov2025***"` | хардкод Basic-пароля. |

---

## C. Корректно gated — НЕ протекает (для справки)

- nav «База ЖК» (`index.html:46` `data-module="complexes"`), «База знаний» (`:42` `data-module="knowledge_base"`).
- complexes-страница, «Профиль ЖК», «Презентация ЖК», «Формат квартиры» — за `moduleOn('complexes')` (app.js:501,1487,595…).
- AmoCRM deep-link субдомен — `module_enabled(modules,'amocrm')` (tenancy_check.py:56).
- Миграция `008` Zoom-ЖК шаблона — уже gated за `complexes` (B3). Существующие копии чистит `worker/scripts/cleanup_zoom_template.py` (B3, прод-прогон ждёт dry-run-ревью).

Для chechetov (complexes OFF, amocrm OFF, knowledge_base ON) вся ЖК/брокер-ветка скрыта.
Протекла **только дентал-ветка** — через мис-биндинг конфига + горстку захардкоженных
дентал-ярлыков (B: card label, «Сотрудник / врач», роль «Врач», десктоп «Тип приёма»).

---

## Переоценка двух уже сделанных записей (без перезаписи)

`POST /api/sessions/{id}/reprocess` (дашборд: «Перепрогнать сессию», без шаблона) повторно
гоняет `pipeline.process_session` из СОХРАНЁННОГО транскрипта (ASR пропускается), заново
считая card + quality с ТЕКУЩИМ company-config. Поэтому порядок:

1. Ребиндинг: `UPDATE shared.tenants SET company_config_id='chechetov' WHERE slug='chechetov'`.
2. Рестарт воркера (сброс `_TENANT_COMPANY_CACHE`): `systemctl restart silentqa-worker`.
3. Reprocess каждой из двух сессий → карточка станет generic «Итоги созвона», QA — по
   chechetov-протоколу.

Шаги 1–2 требуют доступа к прод-БД/systemctl (gated — запускает пользователь).

---

## Статус правок (2026-06-24, ветка multi-tenant-core-phase1, только КОД)

### Сделано (де-доменизация пользователь-видимых утечек на api-key-тенант)
| Правка | Файлы | Тест |
|---|---|---|
| Заголовок карточки **config-driven** через `/features` (`card_label` из `card_extraction.label`) — дашборд больше не хардкодит «Карта приёма» | `backend/app/company_scenarios.py` (`card_label_for` + lru-кеш + инвалидация), `backend/app/routes/tenancy_check.py` (`features.card_label`), `backend/static/app.js:511` (`features.card_label`, фолбэк → рендерер) | `backend/tests/test_tenancy_features.py` (3 теста: helper / endpoint / omit) — TDD RED→GREEN |
| «Сотрудник / врач» → «Сотрудник» (форма загрузки) | `backend/static/app.js:1490` | регресс |
| Десктоп «Тип приёма» → «Сценарий» (chooser сценариев, api-key-режим) | `desktop-app/src/renderer/index.html:171` | регресс |
| «Выйти из аккаунта брокера» → «Выйти из аккаунта» | `desktop-app/src/renderer/index.html:244` | регресс |
| Worker user-msg «Транскрипт приёма:» → «Транскрипт разговора:» | `worker/tasks/card.py:40` | регресс |

Регресс: backend 236 passed, worker 191 passed (+4 пред-существующих env-ошибки `test_complex_match`), desktop 2 passed.

### Отложено — с причиной (НЕ утечки на chechetov)
- **Брокер-логин строки** (`desktop index.html:84`, `renderer.js:192/516/566`, `recorder.js:251`): **mode-gated** — показываются ТОЛЬКО в broker-режиме (realestate), скрыты в api-key-режиме (chechetov/fulldent). Для realestate они корректны. Полная замена брокер-логина на универсальную identity — отдельное направление **unify-recorder-identity**.
- **Роль спикера «Врач»** (`app.js:1743`): dual-use enum — fulldent (дентал) её использует; убрать → сломать дентал-атрибуцию. Малозначима (ручной пикер роли). Нужен per-tenant конфиг ролей.
- **`renderDentalCard`** (`app.js:2519-2554`): срабатывает ТОЛЬКО на дентал-shaped `card.json` (по форме данных). Для generic-карты chechetov не вызывается. Дентал-рендерер легитимен, пока fulldent на дентал-конфиге.
- **`quality.py` V4-схема** (брокер/ЖК): gated `use_extended = bool(scenario.prompt)`; для chechetov при верном конфиге не срабатывает (v2-база).
- **Extension** (`popup.js:21,23` rogov-хардкод): вся extension — realestate-эра, отдельный rewrite под per-tenant API-key.
