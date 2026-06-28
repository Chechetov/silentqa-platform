# «Claude-warm» тёплая тёмная тема — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перекрасить дашборд из холодной GitHub-dark+зелёный палитры в тёплую Claude-inspired (тёмный тёплый фон + коралловый бренд-акцент), разведя бренд и семантику «хорошо».

**Architecture:** Базовая смена — переписать `:root` токены в `styles.css` (покрывает `var(--accent)`×40 и т.д.). Затем вычистить захардкоженные «холодные»/«бренд-зелёные» литералы в `styles.css`, инлайнах `app.js`, `<style>` админки и отдельной палитре `download/index.html`. Source-бейджи (категориальная бренд-идентичность) НЕ трогаем. Тестов на CSS нет — верификация `node --check` + визуальный скриншот.

**Tech Stack:** Vanilla CSS (CSS custom properties) + инлайн-стили в vanilla JS SPA. Без сборки.

**Spec:** `docs/superpowers/specs/2026-06-28-claude-warm-theme-design.md`.

## Global Constraints

- Только цвета/токены. Разметку/компоненты/отступы/типографику не трогаем.
- Развести роли: `--accent` (коралл `#d97757`) = бренд/интерактив; новый `--good` (`#84a86c`) = семантика «хорошо». Зелёный больше не бренд.
- Одна холодная нота допустима: `--info` (`#7ea7c4`) для info-бейджей/ссылок.
- Source-бейджи (`.badge-source-*`, `styles.css:461-493`) — категориальная бренд-идентичность (Chrome-синий, Yandex-жёлтый…) — **оставить как есть**.
- Подсветка поиска `rgba(210,153,34,*)` и warning `#d29922` — тёплые, **оставить**.
- Проверка JS: `node --check backend/static/app.js`. CSS: визуально (скриншот).

---

## Task 1: `:root` токены + `--good`

**Files:**
- Modify: `backend/static/styles.css:11-32` (`:root`)

- [ ] **Step 1: Переписать `:root`**

Заменить блок `:root { … }` (`styles.css:11-32`) на:

```css
:root {
  --bg-primary: #191816;
  --bg-secondary: #201e1b;
  --bg-card: #252220;
  --bg-card-hover: #2e2a27;
  --bg-input: #191816;
  --border: #393431;
  --border-light: #2a2622;
  --text-primary: #f4f1ea;
  --text-secondary: #b2a99b;
  --text-muted: #746b5e;
  --accent: #d97757;
  --accent-hover: #c4623f;
  --accent-dim: rgba(217, 119, 87, 0.15);
  --good: #84a86c;
  --danger: #d64a3f;
  --warning: #d99a3c;
  --info: #7ea7c4;
  --sidebar-width: 220px;
  --radius: 8px;
  --radius-sm: 4px;
  --transition: 0.2s ease;
}
```

- [ ] **Step 2: Проверить синтаксис + быстрый визуальный sanity**

Run: `node --check backend/static/app.js` (app.js не менялся — sanity, что окружение ок)
Expected: без ошибок. (CSS проверяется визуально в Task 6.)

- [ ] **Step 3: Commit**

```bash
git add backend/static/styles.css
git commit -m "feat(theme): тёплая Claude-палитра в :root + токен --good"
```

---

## Task 2: Вычистка литералов в `styles.css`

**Files:**
- Modify: `backend/static/styles.css` (перечисленные строки + stray-литералы)

**Interfaces:**
- Consumes: токены из Task 1 (`var(--accent)`, `var(--good)`, `var(--info)`, bg/border/text-токены).

- [ ] **Step 1: Точечные правки бейджей/транскрипта/чипов**

| Строка | Было | Стало |
|---|---|---|
| `:439-440` `.badge-completed` | `rgba(76,175,80,.15)` / `#4CAF50` | `rgba(132,168,108,.15)` / `var(--good)` |
| `:450-451` `.badge-created,.badge-uploading` | `rgba(88,166,255,.15)` / `#58a6ff` | `rgba(126,167,196,.15)` / `var(--info)` |
| `:604-605` `.transcript-line--active` | `rgba(88,166,255,.08)` / `#58a6ff` | `rgba(217,119,87,.10)` / `var(--accent)` |
| `:625-626` `.speaker-0` | `rgba(88,166,255,.15)` / `#58a6ff` | `rgba(217,119,87,.15)` / `#d97757` |
| `:634-636` `.speaker-2` | `rgba(163,113,247,.15)` / `#a371f7` | `rgba(181,138,166,.15)` / `#b58aa6` |
| `:1534` `.tpl-kind-extraction` bg | `rgba(88,166,255,.15)` | `rgba(126,167,196,.15)` |
| `:1535` `.tpl-kind-evaluation` bg | `rgba(76,175,80,.18)` | `rgba(217,119,87,.18)` |
| `:1755-1757` `.cx-chip--yes` | `rgba(76,175,80,.4)` / `var(--accent)` / `rgba(76,175,80,.08)` | `rgba(132,168,108,.4)` / `var(--good)` / `rgba(132,168,108,.08)` |
| `:1779` `.cx-badge--yes` bg + color | `var(--accent)` / `rgba(76,175,80,.12)` | `var(--good)` / `rgba(132,168,108,.12)` |

`.speaker-1` (`:629-631`, янтарь `#d29922`) — **оставить** (тёплый). Source-бейджи (`:461-493`) — **оставить**.

- [ ] **Step 2: Stray cool-литералы → токены**

Найти и заменить литеральные использования (ВНЕ `:root`) холодных hex, дублирующих токены, на `var(--…)`:

Run: `grep -nE '#0d1117|#161b22|#1c2128|#21262d|#30363d|#e6edf3|#8b949e|#484f58' backend/static/styles.css`

Для каждого совпадения **вне** `:root` (строки 11-32 не трогать — там новые значения уже стоят): заменить на соответствующий токен — `#0d1117`→`var(--bg-primary)`, `#161b22`→`var(--bg-secondary)`, `#1c2128`→`var(--bg-card)`, `#21262d`→`var(--bg-card-hover)`, `#30363d`→`var(--border)`, `#e6edf3`→`var(--text-primary)`, `#8b949e`→`var(--text-secondary)`, `#484f58`→`var(--text-muted)`.

- [ ] **Step 3: Остаточные cool/green rgba**

Run: `grep -nE 'rgba\(76, ?175, ?80|rgba\(88, ?166, ?255' backend/static/styles.css`

Ожидаемо все известные сайты уже покрыты Step 1. Любой оставшийся: green-rgba в «хорошо»-контексте → `rgba(132,168,108,*)`; в бренд/active-контексте → `rgba(217,119,87,*)`; blue-rgba → `rgba(126,167,196,*)`. (Не трогать `rgba(210,153,34,*)` подсветки поиска и `rgba(248,81,73,*)` danger.)

- [ ] **Step 4: Commit**

```bash
git add backend/static/styles.css
git commit -m "feat(theme): styles.css — литералы на тёплые токены (active-line/speaker/badges/chips)"
```

---

## Task 3: Инлайн-цвета в `app.js`

**Files:**
- Modify: `backend/static/app.js` (строки 665, 1321-1322, 1898, 2837)

- [ ] **Step 1: Перекрасить инлайн-литералы**

| Строка | Контекст | Было → Стало |
|---|---|---|
| `:665` | classification map | `partial:'#58a6ff'`→`'#7ea7c4'`; `productive:'#4CAF50'`→`'#84a86c'`; `meeting_scheduled:'#238636'`→`'#d97757'` |
| `:1321` | бейдж «Очно» | `rgba(163,113,247,0.15);color:#a371f7` → `rgba(181,138,166,0.15);color:#b58aa6` |
| `:1322` | бейдж «Звонок» | `rgba(88,166,255,0.15);color:#58a6ff` → `rgba(126,167,196,0.15);color:#7ea7c4` |
| `:1898` | rows «Уже обработаны» | `'#58a6ff'` → `'#7ea7c4'` |
| `:2837` | бейдж «активна» bg | `#2e7d32` → `#5f7d4c` |

`:118` (offline-баннер `#ff8f00`) — **оставить** (тёплый). Прочие `#fff`/`#eee`/`#888` — оставить.

- [ ] **Step 2: Проверить синтаксис**

Run: `node --check backend/static/app.js`
Expected: без ошибок.

- [ ] **Step 3: Commit**

```bash
git add backend/static/app.js
git commit -m "feat(theme): app.js — инлайн-цвета на тёплую гамму"
```

---

## Task 4: `<style>`-блок админки

**Files:**
- Modify: `backend/static/admin.html` (`<style>` блок)

- [ ] **Step 1: Аудит литералов в admin.html**

Run: `grep -nE '#[0-9a-fA-F]{3,8}|rgba?\(' backend/static/admin.html`

- [ ] **Step 2: Перекрасить холодные/зелёные**

Для каждого совпадения применить те же правила: холодные bg/border/text → тёплые токены/значения из Task 1; зелёный бренд → коралл; зелёный «хорошо» → `--good`; синий → `--info`. Тёплые (warning/amber/danger-red) — оставить. `admin.html` линкует общий `/styles.css`, поэтому многое уже унаследует токены — править только то, что задано литералами в `<style>`.

- [ ] **Step 3: Commit**

```bash
git add backend/static/admin.html
git commit -m "feat(theme): admin.html <style> — тёплая палитра"
```

---

## Task 5: Палитра страницы загрузки

**Files:**
- Modify: `backend/static/download/index.html:9-12` (`:root`)

- [ ] **Step 1: Переназначить `:root` страницы загрузки**

Заменить значения в `:root` (`download/index.html:9-12`):

```css
    :root {
      --bg: #191816; --surface: #252220; --surface2: #2e2a27; --border: #393431;
      --text: #f4f1ea; --text2: #b2a99b; --text3: #746b5e;
      --green: #d97757; --radius: 12px;
```

(имя `--green` оставляем как есть — это просто CTA-токен, теперь коралловый; переименование = лишний diff.)

- [ ] **Step 2: Остаточные литералы на странице**

Run: `grep -nE '#[0-9a-fA-F]{3,8}|rgba?\(' backend/static/download/index.html | grep -vE '#191816|#252220|#2e2a27|#393431|#f4f1ea|#b2a99b|#746b5e|#d97757'`

Любые холодные литералы вне `:root` → тёплые токены/значения. Тёплые — оставить.

- [ ] **Step 3: Commit**

```bash
git add backend/static/download/index.html
git commit -m "feat(theme): страница загрузки — тёплая палитра"
```

---

## Task 6: Визуальная верификация (скриншот)

**Files:**
- Create: `<scratchpad>/theme-preview.html` (одноразовый превью; в репозиторий НЕ коммитим)

- [ ] **Step 1: Собрать превью-страницу**

Создать в скретчпаде `theme-preview.html`, который `<link>`-ает реальный `/root/projects/silentqa-dev/backend/static/styles.css` (через `file://` абсолютный путь) и рендерит репрезентативные компоненты на реальных классах: сайдбар, таблицу списка звонков (с `.badge-completed/processing/failed`, source-бейджами), карточку звонка с `.score-circle`, `.transcript-line`+`.transcript-line--active`+`.speaker-0/1/2`+`.transcript-text`+подсветку `mark.transcript-match`, кнопки `.btn-primary/.btn-secondary`, сентимент-сегменты, `.cx-chip--yes/--no`. Цель — увидеть палитру на настоящих стилях без бэкенда.

- [ ] **Step 2: Скриншот через Playwright**

Открыть `file://<scratchpad>/theme-preview.html` в Playwright MCP, сделать full-page скриншот. Проверить глазами: тёплый фон, коралловый акцент на кнопках/навигации/active-line, сейдж-зелёный только на «хорошо» (completed/score-high/✓), читаемый контраст текста, отсутствие резкого холодного синего (кроме сдержанного info).

- [ ] **Step 3: Показать пользователю**

Приложить скриншот в ответе. Если контраст/оттенок где-то не нравится — точечно подправить токен и переснять.

- [ ] **Step 4 (опц.): финальный коммит правок после визуала**

Если по скриншоту что-то докрутили:

```bash
git add backend/static/
git commit -m "fix(theme): точечная докрутка по визуальной проверке"
```

---

## Self-Review (заполнено автором плана)

- **Покрытие спека:** `:root` + `--good` → Task 1; миграция литералов styles.css (active-line/speaker/badges/chips/stray) → Task 2; app.js инлайны → Task 3; admin.html → Task 4; download page → Task 5; маппинг семантики (completed→good, created→info, ✓→good, классификации) → Task 2/3; визуал → Task 6. Source-бейджи оставлены (спек). Светлый режим — вне scope (спек).
- **Плейсхолдеры:** конкретные значения и сайты приведены; для stray-литералов и admin/download дан точный grep + правило замены значение→значение (это и есть «как»).
- **Согласованность:** один набор значений (`#191816/#252220/#2e2a27/#393431/#f4f1ea/#b2a99b/#746b5e/#d97757/#84a86c/#7ea7c4/#d64a3f/#d99a3c/#b58aa6`) используется единообразно во всех задачах.
