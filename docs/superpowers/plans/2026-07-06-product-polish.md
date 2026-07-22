# Пакет 1 «Продукт-вау» — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Полировка продукта по итогам анализа 2026-07-06: честный сентимент-таймлайн + клик-seek моментов, рабочий drill-down с дашборда, фильтр по сценарию, светлая тема, мелкая полировка (копировать резюме, overflow таблиц).

**Architecture:** Три задачи по дизъюнктным файлам: (1) backend — параметр `scenario_id` в `/api/stats/*` и `employee` в `/api/sessions`; (2) тема — токены в styles.css + новый `theme.js` + тумблер в index.html; (3) app.js — вся продуктовая логика фронта. Без миграций и новых зависимостей.

**Tech Stack:** FastAPI (bind-params only), vanilla JS no-build, CSS custom properties.

## Global Constraints

- Русский язык в UI/комментариях. Тесты pure-unit (`cd backend && ../.venv/bin/python -m pytest tests/`), node-глоб `node --test 'backend/static/tests/*.test.mjs'` из корня.
- SQL — только bind-параметры (идиома `_SCOPE_SQL` в stats.py); имена схем/колонок не интерполируются из ввода.
- **Агенты НЕ делают git** — коммитит контроллер. Прод не трогаем.
- app.js правит ТОЛЬКО Task 3; styles.css/index.html/theme.js — ТОЛЬКО Task 2; backend — ТОЛЬКО Task 1.

---

### Task 1: backend-filters — `scenario_id` в stats + `employee` в sessions

**Files:**
- Modify: `backend/app/routes/stats.py` (все 6 хелперов + 4 роута)
- Modify: `backend/app/routes/sessions.py` (`list_sessions`, ~:50)
- Test: `backend/tests/test_stats_api.py` (дополнить), `backend/tests/test_manager_scope.py` (дополнить фильтром employee) — следуй идиомам этих файлов (monkeypatch хелперов / FakeRedis / TenantRegistry).

**Interfaces:**
- Produces: query-параметр `scenario_id: str | None` на `/api/stats/{overview,managers,objections,risk-calls}`; query-параметр `employee: str | None` на `GET /api/sessions` (точное совпадение `metadata->>'employee'`). Оба опциональны, поведение без них не меняется.
- Consumes: колонка `quality_results.scenario_id` (уже существует, миграция 018).

- [ ] **Step 1: Падающие тесты**

В `test_stats_api.py` добавь (следуя существующей monkeypatch-идиоме файла): тест, что `scenario_id=general` прокидывается из каждого из 4 роутов в хелперы (захвати kwargs monkeypatch-обёрткой), и source-тест, что `_SCENARIO_SQL` присутствует в SQL всех 6 хелперов:

```python
def test_scenario_sql_in_all_helpers():
    import inspect
    from app.routes import stats as st
    src = inspect.getsource(st)
    # каждый хелпер использует единый _SCENARIO_SQL в WHERE
    assert src.count("{_SCENARIO_SQL}") >= 6
```

В `test_manager_scope.py` — тест: `GET /api/sessions?employee=Иванов` возвращает только сессии с `metadata.employee == "Иванов"` (создай в фикстуре две сессии с разными employee по идиоме файла).

- [ ] **Step 2: Прогнать — падает**

`cd backend && ../.venv/bin/python -m pytest tests/test_stats_api.py tests/test_manager_scope.py -q` → новые FAIL.

- [ ] **Step 3: Реализация stats.py**

Рядом с `_SCOPE_SQL` (:20):

```python
_SCENARIO_SQL = "AND (CAST(:scenario AS TEXT) IS NULL OR scenario_id = :scenario)"
```

Каждый из 6 хелперов (`_kpi_row`, `_series_rows`, `_manager_rows`, `_spark_rows`, `_objection_rows`, `_risk_rows`): добавь параметр `scenario` в сигнатуру, `{_SCENARIO_SQL}` в WHERE сразу после `{_SCOPE_SQL}`, `"scenario": scenario` в bind-словарь. Каждый из 4 роутов: параметр `scenario_id: str | None = Query(None, max_length=64)`, прокинуть в хелперы (в `stats_overview` — в оба `_kpi_row` и в `_series_rows`).

- [ ] **Step 4: Реализация sessions.py**

В `list_sessions` добавь параметр `employee: str | None = None` (после `phone`); в блок фильтров:

```python
    if employee:
        filters.append(Session.metadata_["employee"].astext == employee.strip())
```

(manager-scope фильтр ниже по коду остаётся — пересечение корректно: менеджер не увидит чужого employee).

- [ ] **Step 5: Зелёный прогон**

`cd backend && ../.venv/bin/python -m pytest tests/ -q` — весь сьют зелёный (было 284+2).

- [ ] **Step 6: Коммит (контроллер)** — `feat(api): scenario_id в /api/stats/*, employee-фильтр в /api/sessions`

---

### Task 2: светлая тема

**Files:**
- Modify: `backend/static/styles.css` (блок light-токенов + light-правки точечных мест)
- Create: `backend/static/theme.js`
- Modify: `backend/static/index.html` (script в `<head>`, тумблер в `.sidebar-footer` :67)
- Modify: `.github/workflows/tests.yml` (theme.js в `node --check` джобы clients)

**Interfaces:**
- Produces: `window.toggleTheme()` (глобальная, для onclick), атрибут `data-theme="light"|"dark"` на `<html>`, localStorage-ключ `sqa_theme`.
- Consumes: существующие CSS-токены `:root` (styles.css:11-33, тёмная Claude-warm палитра — эталон настроения для светлой).

- [ ] **Step 1: theme.js**

```js
// Тема: применяется ДО отрисовки (скрипт в <head>), чтобы не мигало тёмным.
(function () {
  const saved = localStorage.getItem('sqa_theme');
  const theme = saved === 'light' || saved === 'dark'
    ? saved
    : (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  document.documentElement.dataset.theme = theme;
})();

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('sqa_theme', next);
  const btn = document.getElementById('themeToggle');
  if (btn) btn.textContent = next === 'light' ? '🌙 Тёмная' : '☀️ Светлая';
}
```

- [ ] **Step 2: index.html**

В `<head>` (до styles.css или сразу после — до рендера body): `<script src="theme.js"></script>`. В `.sidebar-footer` (:67), перед кнопкой «Выйти»:

```html
<button class="nav-logout" id="themeToggle" onclick="toggleTheme()" title="Переключить тему">☀️ Светлая</button>
```

После вставки: маленький скрипт не нужен — подпись кнопки поправь в toggleTheme (выше) и инициализируй в theme.js хвостом:

```js
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('themeToggle');
  if (btn && document.documentElement.dataset.theme === 'light') btn.textContent = '🌙 Тёмная';
});
```

- [ ] **Step 3: styles.css — светлые токены**

После тёмного `:root` (:33):

```css
:root { color-scheme: dark; }

:root[data-theme="light"] {
  color-scheme: light;
  --bg-primary: #faf9f5;
  --bg-secondary: #f0eee6;
  --bg-card: #ffffff;
  --bg-card-hover: #f5f3ec;
  --bg-input: #ffffff;
  --border: #d9d3c8;
  --border-light: #e8e4da;
  --text-primary: #1f1e1b;
  --text-secondary: #5d5648;
  --text-muted: #8a8171;
  --accent: #c4623f;
  --accent-hover: #a94f30;
  --accent-dim: rgba(196, 98, 63, 0.12);
  --good: #5f8a47;
  --danger: #bf3a2f;
  --warning: #9c6d1f;
  --info: #3f7396;
}
```

- [ ] **Step 4: аудит контраста точечных мест**

Пройди styles.css на хардкод-цвета вне токенов (grep `#[0-9a-f]{3,6}` и `rgba(`): всё, что задаёт фон/текст интерфейса (не логотипы/тени) — переведи на токены или добавь light-переопределение селектором `:root[data-theme="light"] .класс`. Известные кандидаты: sentiment-цвета сегментов, `.badge`, hover-состояния, скроллбары. Инлайновые хексы в app.js (classColors, `#fff` на цветных бейджах) НЕ трогай — цветные бейджи с белым текстом валидны в обеих темах.

- [ ] **Step 5: CI**

В `.github/workflows/tests.yml`, джоба clients: добавь `backend/static/theme.js` в команду `node --check`.

- [ ] **Step 6: Верификация**

`node --check backend/static/theme.js` (из корня). Ручная проверка вёрстки невозможна — перечисли в отчёте все light-переопределения и места, где сомневаешься в контрасте.

- [ ] **Step 7: Коммит (контроллер)** — `feat(front): светлая тема (data-theme + токены + тумблер)`

---

### Task 3: app.js — таймлайн, seek, drill-down, фильтры, полировка

**Files:**
- Modify: `backend/static/app.js` (ТОЛЬКО этот файл)

**Interfaces:**
- Consumes: `GET /api/sessions?employee=` и `/api/stats/*?scenario_id=` (Task 1, параметры ТОЧНО `employee` / `scenario_id`); `features.scenarios` = `[{id, name}]`; глобалы `showToast`, `navigate`, `escapeHtml`, `formatSeconds`.
- Produces: `callsFilters.employee` (строка, '' = выключен).

- [ ] **Step 1: сентимент-таймлайн пропорциональный + клик-seek** (~:635-641)

В map сегментов: ширина по длительности, время в data-атрибут:

```js
${sentiments.map(s => {
  const cls = s.sentiment === 'positive' ? 'sentiment-positive'
            : s.sentiment === 'negative' ? 'sentiment-negative'
            : 'sentiment-neutral';
  const dur = (s.end != null && s.start != null) ? Math.max(0.5, s.end - s.start) : 1;
  const seek = s.start != null ? ` data-t="${s.start}" style="flex:${dur};cursor:pointer"` : ` style="flex:${dur}"`;
  return `<div class="sentiment-segment ${cls}"${seek} title="${s.start != null ? formatSeconds(s.start) + ' · ' : ''}${s.sentiment}${s.text ? ': ' + escapeHtml(s.text) : ''}"></div>`;
}).join('')}
```

- [ ] **Step 2: клик-seek ключевых моментов** (~:930-940)

`moment-item` получает `data-t` и курсор, когда время известно:

```js
return `
  <div class="moment-item"${m.time != null ? ` data-t="${m.time}" style="cursor:pointer" title="Перейти к моменту"` : ''}>
    ${time ? `<div class="moment-time">${time}</div>` : ''}
    ${escapeHtml(text)}
  </div>`;
```

Рядом с существующим транскрипт-seek-листенером (~:1022, идиома `transcriptContainer`) добавь ОДИН делегированный листенер на весь `app`-контейнер карточки (после innerHTML):

```js
    // Клик-seek: сентимент-сегменты и ключевые моменты
    app.querySelectorAll('.sentiment-segment[data-t], .moment-item[data-t]').forEach(el =>
      el.addEventListener('click', () => {
        const player = $('#audioPlayer');
        if (!player) return;
        player.currentTime = parseFloat(el.dataset.t);
        player.play();
        player.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }));
```

- [ ] **Step 3: drill-down менеджера с дашборда**

а) `callsFilters` (:294): добавь `employee: ''` в объект. ЛОВУШКА: у объекта есть ещё ДВА пересоздания wholesale — ресет-кнопка (~:457) и `filterByKbTag` (~:487) — добавь `employee: ''` в ОБА, иначе фильтр «застрянет».

б) `renderCalls` (:316-320): `if (callsFilters.employee) params.set('employee', callsFilters.employee);` и включи employee в `filtersActive` (:334). Рядом с существующим чипом kb_tag добавь чип employee (по той же идиоме отображения активного фильтра с крестиком-сбросом — найди kb_tag-чип в разметке renderCalls и повтори структуру: подпись `Менеджер: ${escapeHtml(callsFilters.employee)}`, клик по крестику обнуляет `callsFilters.employee` и зовёт `renderCalls(0)`).

в) Дашборд (:1187-1189): замени переход на общий #managers на drill-down по образцу `filterByKbTag`:

```js
    $$('#mgrTable tr.clickable').forEach(tr => tr.addEventListener('click', () => {
      callsFilters = { source: '', phone: '', template_id: '', kb_tag: '', kb_tag_label: '',
                       employee: tr.dataset.name || '' };
      navigate('#calls');
    }));
```

(коммент «точечного роута нет» удали — он больше не прав).

- [ ] **Step 4: фильтр по сценарию на #dashboard**

`let _dashScenario = '';` рядом с `_dashDays` (:1074). В `renderDashboard`:
- ко всем 4 URL добавь `${_dashScenario ? `&scenario_id=${encodeURIComponent(_dashScenario)}` : ''}`;
- в `page-header` рядом с кнопками периода — селект (только если есть сценарии):

```js
const scenarios = (features && features.scenarios) || [];
const scenarioSel = scenarios.length ? `
  <select id="dashScenario" class="filter-select">
    <option value="">Все сценарии</option>
    ${scenarios.map(s => `<option value="${escapeHtml(s.id)}" ${_dashScenario === s.id ? 'selected' : ''}>${escapeHtml(s.name || s.id)}</option>`).join('')}
  </select>` : '';
```

вставь `${scenarioSel}` в `.dash-period`; после innerHTML:

```js
    const sel = $('#dashScenario');
    if (sel) sel.addEventListener('change', () => { _dashScenario = sel.value; renderDashboard(); });
```

(класс `filter-select` уже используется фильтрами звонков — если его нет, возьми класс существующего селекта из renderCalls).

- [ ] **Step 5: «Копировать резюме» в карточке звонка**

В заголовочной области карточки (рядом с существующими кнопками экспорта JSON/CSV, ~:554) добавь кнопку `onclick="copyCallSummary()"` с подписью «Копировать резюме» и функцию:

```js
function copyCallSummary() {
  const a = _currentCallData && _currentCallData.analysis;
  const s = _currentCallData && _currentCallData.session;
  if (!a) { showToast('Резюме ещё нет', 'error'); return; }
  const lines = [
    `Звонок ${s && s.created_at ? new Date(s.created_at).toLocaleString('ru-RU') : ''}`,
    a.overall_score != null ? `Оценка: ${a.overall_score}/10` : '',
    '', a.brief_summary || a.summary || '',
    '', (a.improvement_suggestions || []).length ? 'Рекомендации:' : '',
    ...(a.improvement_suggestions || []).map(x => `— ${typeof x === 'string' ? x : (x.text || '')}`),
  ].filter(Boolean);
  navigator.clipboard.writeText(lines.join('\n'))
    .then(() => showToast('Резюме скопировано'))
    .catch(() => showToast('Не удалось скопировать', 'error'));
}
```

- [ ] **Step 6: overflow таблиц дашборда**

Оберни таблицы `#mgrTable`, риск-таблицу и таблицу возражений в `<div style="overflow-x:auto">…</div>` (инлайн, чтобы не трогать styles.css другой задачи).

- [ ] **Step 7: Верификация**

Из корня: `node --check backend/static/app.js`; `node --test 'backend/static/tests/*.test.mjs'` — 6/6. Grep-самопроверки: `grep -c "employee: ''" backend/static/app.js` ≥ 3; `grep -n "navigate('#managers')" backend/static/app.js` — не должен остаться в обработчике mgrTable.

- [ ] **Step 8: Коммит (контроллер)** — `feat(front): пропорциональный таймлайн+seek, drill-down менеджера, фильтр сценария, копия резюме, overflow таблиц`

---

## Волны исполнения (SDD)

- **W1 = [Task 1, Task 2, Task 3] параллельно** — файлы дизъюнктны.
- Контроллер: полные сьюты + пер-таск ревью + коммиты + финал-ревью + push + CI.

## Definition of Done

- `/api/stats/*` принимают `scenario_id`, `/api/sessions` — `employee`; сьюты зелёные.
- Клик по менеджеру на дашборде открывает #calls, отфильтрованный по нему; чип с крестиком работает; ресет очищает employee.
- Сегменты таймлайна пропорциональны времени и мотают аудио; ключевые моменты кликабельны.
- Светлая тема переключается тумблером, переживает перезагрузку (localStorage), не мигает тёмным при загрузке.
- node --check покрывает app.js/admin.js/charts.js/theme.js в CI.
