# SilentQA 2.0 Техдолг — План 2/3: Мультитенантные клиенты записи — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chrome/Yandex-расширение перестаёт хардкодить `rogov.automate-it.fun` + Basic-креды в исходнике: настройки «Сервер + API-ключ + Имя сотрудника» в popup, Basic-путь из расширения удаляется, `extension-yandex/` становится генерируемым клоном с CI-гардом.

**Architecture:** Плумбинг API-key в расширении **уже есть** (`background.js` читает `apiKey` из storage, `offscreen.js` шлёт `X-API-Key`) — хардкод сидит только в `popup.js:21-23` (включая пароль в исходнике), `popup.html:190-191` (hidden input) и `manifest.json` (name + host_permissions). Делаем: чистую конфиг-логику в `extension/config.js` (node-тестируемую, паттерн `desktop-app/src/renderer/mode.js`), settings-UI в popup (`<details>`-блок), удаляем Basic из background/offscreen, добавляем `metadata.employee` (как desktop `recorder.js:91` — поле НЕ в `_SERVER_OWNED_METADATA`, клиенту слать можно), ребрендим манифест. Yandex-клон синхронизируется скриптом `rsync --exclude=README.md` + `--check`-режим в CI.

**Tech Stack:** Chrome MV3 (tabCapture + offscreen + storage), vanilla JS, `node --test` для чистой логики, bash+rsync для синка клона.

**Роадмап-источник:** `~/reviews/silentqa-roadmap-2.0-3.0.md`, пункт 2.1 (+«Техдолг и риски» №2).

**Серверный контракт (уже существует, не меняем):** `require_ingestion_auth` (`backend/app/auth_user.py:103`) — валидная cookie-сессия ИЛИ `X-API-Key`, совпадающий с ключом ИМЕННО этого тенанта (иначе 403); без креденшелов — только пока `tenants.api_key_required=false`. Протокол: `POST /api/sessions` → `POST /api/sessions/{id}/chunks` (WebM/Opus, 10с) → `POST /api/sessions/{id}/finish`.

## Global Constraints

- Desktop-приложение НЕ трогаем: оно уже двухрежимное (`isApiKeyMode`, `normalizeServerUrl` в `mode.js`; API-key-режим для тенантов, broker-режим для realestate). Остатки Basic в десктопе — это broker-режим realestate, который сознательно живёт до спеки `2026-06-14-unify-recorder-identity-design.md`. **Вне scope.**
- Бэкенд НЕ трогаем (контракт ингеста готов).
- Продовые пользователи rogov-расширения живут на СТАРОМ однотенантном чекауте `/root/projects/realestate` со своей копией расширения — правки здесь их не ломают.
- `extension-yandex/` руками не редактировать — только через `scripts/sync_yandex_extension.sh` (Task 5).
- Новые UI-строки настроек — на русском (консистентно с download-страницей, которая УЖЕ инструктирует «впишите Сервер и API-ключ»); существующие английские лейблы кнопок не переводим (вне scope).
- Node-тесты: plain-assert файлы, exit 0 = pass (конвенция `desktop-app/tests/`); запуск `node --test extension/tests/`.
- CI-джоба Task 5 дописывается в `.github/workflows/tests.yml` из Плана 1 Task 6; если План 1 ещё не выполнен — создать workflow только с этой джобой (шапка `name/on` из Плана 1).
- Смоук (Task 6) требует стейджинг из Плана 1 Task 7 (тенант `stagetest` + его API-ключ); допустимая замена — любой активный прод-тенант с ключом.

---

### Task 1: `extension/config.js` — чистая конфиг-логика + node-тесты

**Files:**
- Create: `extension/config.js`
- Test: `extension/tests/config.test.js` (создать; папка попадает в yandex-клон — это ок, байт-идентичность важнее чистоты zip)

**Interfaces:**
- Produces: `normalizeServerUrl(raw) -> string` — трим, срез хвостовых `/`, дефолтная схема `https://` (семантика 1-в-1 как `desktop-app/src/renderer/mode.js:16-22` — дублируем осознанно: у расширения нет общего модуля с десктопом). `validateConfig(c) -> {ok:true, serverUrl, apiKey} | {ok:false, error}` — оба поля обязательны, error — русская строка для errorText. В браузере кладутся в глобальную область видимости popup'а (обычный `<script src="config.js">` до `popup.js`), в Node — CommonJS-экспорт.
- Consumes: ничего (без chrome.*/DOM — иначе не тестируется в Node).

- [ ] **Step 1: Написать падающий тест**

Создать `extension/tests/config.test.js`:

```javascript
const assert = require('assert');

// Чистая логика конфига вынесена в config.js без chrome.*/DOM,
// чтобы гоняться node-юнитом (паттерн desktop-app/src/renderer/mode.js).
const { normalizeServerUrl, validateConfig } = require('../config.js');

// --- normalizeServerUrl (семантика = desktop mode.js) ---
assert.strictEqual(normalizeServerUrl('acme.silentqa.com'), 'https://acme.silentqa.com');
assert.strictEqual(normalizeServerUrl('https://x.silentqa.com'), 'https://x.silentqa.com');
assert.strictEqual(normalizeServerUrl('HTTP://x.local'), 'HTTP://x.local');
assert.strictEqual(normalizeServerUrl('https://x.silentqa.com///'), 'https://x.silentqa.com');
assert.strictEqual(normalizeServerUrl('  acme.silentqa.com  '), 'https://acme.silentqa.com');
assert.strictEqual(normalizeServerUrl(''), '');
assert.strictEqual(normalizeServerUrl(null), '');

// --- validateConfig ---
assert.deepStrictEqual(
  validateConfig({ serverUrl: 'acme.silentqa.com', apiKey: ' sqa_abc ' }),
  { ok: true, serverUrl: 'https://acme.silentqa.com', apiKey: 'sqa_abc' });
assert.strictEqual(validateConfig({ serverUrl: '', apiKey: 'k' }).ok, false);
assert.ok(validateConfig({ serverUrl: '', apiKey: 'k' }).error.length > 0);
assert.strictEqual(validateConfig({ serverUrl: 'x.com', apiKey: '' }).ok, false);
assert.strictEqual(validateConfig({ serverUrl: 'x.com', apiKey: '   ' }).ok, false);
assert.strictEqual(validateConfig(null).ok, false);
assert.strictEqual(validateConfig(undefined).ok, false);

console.log('config OK');
```

- [ ] **Step 2: Прогнать — падает**

Run: `node --test extension/tests/`
Expected: FAIL — `Cannot find module '../config.js'`.

- [ ] **Step 3: Реализация `extension/config.js`**

```javascript
// Чистая конфиг-логика расширения — без chrome.* и DOM, чтобы гоняться
// node-юнитами (паттерн desktop-app/src/renderer/mode.js). В popup
// подключается <script src="config.js"> ПЕРЕД popup.js.

// Нормализация адреса сервера: https:// по умолчанию, без хвостовых слэшей.
// Семантика 1-в-1 с desktop-app/src/renderer/mode.js (осознанный дубль:
// общего модуля между расширением и десктопом нет).
function normalizeServerUrl(raw) {
  let s = String(raw == null ? '' : raw).trim();
  if (!s) return '';
  s = s.replace(/\/+$/, '');
  if (!/^https?:\/\//i.test(s)) s = 'https://' + s;
  return s;
}

// Готовность к записи: нужен и сервер, и API-ключ тенанта.
function validateConfig(c) {
  const serverUrl = normalizeServerUrl(c && c.serverUrl);
  const apiKey = String((c && c.apiKey) || '').trim();
  if (!serverUrl) return { ok: false, error: 'Укажите адрес сервера в настройках' };
  if (!apiKey) return { ok: false, error: 'Укажите API-ключ в настройках' };
  return { ok: true, serverUrl, apiKey };
}

// Node (юнит-тест): CommonJS-экспорт. В браузере функции и так глобальные.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { normalizeServerUrl, validateConfig };
}
```

- [ ] **Step 4: Прогнать — зелёный**

Run: `node --test extension/tests/`
Expected: PASS (1 файл, `config OK`).

- [ ] **Step 5: Commit**

```bash
git add extension/config.js extension/tests/config.test.js
git commit -m "feat(extension): config.js — нормализация сервера + валидация настроек (node-тесты)"
```

---

### Task 2: popup — settings-UI вместо хардкода

**Files:**
- Modify: `extension/popup.html` (стили + строки 137/139 ребренд, 190-191 замена hidden-input, 193 подключение config.js)
- Modify: `extension/popup.js` (строки 21-23, блок Load saved state, обработчик btnStart, `updateUI` строка 217)

**Interfaces:**
- Consumes: `normalizeServerUrl`/`validateConfig` из Task 1 (глобальные после `<script src="config.js">`).
- Produces: настройки в `chrome.storage.local`: `serverUrl` (нормализованный), `apiKey`, `employee`. Ключи `authUsername`/`authPassword` больше НЕ пишутся и вычищаются при открытии popup'а. Контракт для Task 3: background читает `serverUrl`/`apiKey`/`employee`.

- [ ] **Step 1: popup.html — стили настроек**

В `<style>` перед закрывающим `</style>` (после блока `.error-text`, строка 123) добавить:

```css
    .settings { margin-top: 12px; border-top: 1px solid #374151; padding-top: 10px; }
    .settings summary { font-size: 11px; color: #9ca3af; cursor: pointer; user-select: none; }
    .settings label { display: block; font-size: 11px; color: #9ca3af; margin-top: 8px; }
    .settings input {
      width: 100%; margin-top: 4px; padding: 8px;
      background: #1f2937; color: #e5e7eb;
      border: 1px solid #374151; border-radius: 6px; font-size: 12px;
    }
    .btn-save {
      margin-top: 10px; width: 100%; padding: 8px; border: none; border-radius: 6px;
      background: #374151; color: #e5e7eb; font-size: 12px; font-weight: 600; cursor: pointer;
    }
    .btn-save:hover { background: #4b5563; }
    .saved-note { font-size: 11px; color: #34d399; margin-top: 6px; text-align: center; }
```

- [ ] **Step 2: popup.html — ребренд + markup настроек**

Строка 137: `<span class="title">Rogov Recorder</span>` → `<span class="title">SilentQA Recorder</span>`.
Строка 139: `<span class="version">v1.3</span>` → `<span class="version">v2.0</span>`.

Строки 190-193 (`<!-- Server URL hidden — hardcoded -->` + hidden input + script) заменить на:

```html
  <!-- Настройки: сервер тенанта + API-ключ + сотрудник -->
  <details class="settings" id="settingsBlock">
    <summary>Настройки сервера</summary>
    <label>Сервер (адрес вашего тенанта)
      <input type="text" id="serverUrl" placeholder="acme.silentqa.com">
    </label>
    <label>API-ключ
      <input type="password" id="apiKey" placeholder="sqa_…">
    </label>
    <label>Имя сотрудника (атрибуция звонков)
      <input type="text" id="employee" placeholder="Иван Иванов">
    </label>
    <button class="btn-save" id="btnSaveSettings" type="button">Сохранить</button>
    <div class="saved-note" id="settingsSaved" style="display:none">Сохранено ✓</div>
  </details>

  <script src="config.js"></script>
  <script src="popup.js"></script>
```

- [ ] **Step 3: popup.js — заменить константы на элементы настроек**

Строки 21-23:

```javascript
const SERVER_URL = "https://rogov.automate-it.fun";
const AUTH_USERNAME = "admin";
const AUTH_PASSWORD = "rogov2025***";  // литерал замаскирован в плане; фактическое значение — в popup.js:23
```

заменить на:

```javascript
// Конфиг живёт в chrome.storage (см. settings-блок ниже), не в исходнике.
const apiKeyInput = document.getElementById("apiKey");
const employeeInput = document.getElementById("employee");
const btnSaveSettings = document.getElementById("btnSaveSettings");
const settingsSaved = document.getElementById("settingsSaved");
const settingsBlock = document.getElementById("settingsBlock");
```

- [ ] **Step 4: popup.js — загрузка/сохранение настроек**

После блока `// Load saved state` (после строки 105) добавить:

```javascript
// --- Настройки ---
// Легаси Basic-креды из старых установок вычищаем.
chrome.storage.local.remove(["authUsername", "authPassword"]);
chrome.storage.local.get(["serverUrl", "apiKey", "employee"], (cfg) => {
  serverUrlInput.value = cfg.serverUrl || "";
  apiKeyInput.value = cfg.apiKey || "";
  employeeInput.value = cfg.employee || "";
  // Не настроено — раскрыть блок сразу
  if (!cfg.serverUrl || !cfg.apiKey) settingsBlock.open = true;
});

btnSaveSettings.addEventListener("click", () => {
  const v = validateConfig({ serverUrl: serverUrlInput.value, apiKey: apiKeyInput.value });
  if (!v.ok) {
    show(errorText);
    errorText.textContent = v.error;
    return;
  }
  hide(errorText);
  chrome.storage.local.set({
    serverUrl: v.serverUrl,
    apiKey: v.apiKey,
    employee: (employeeInput.value || "").trim(),
  }, () => {
    serverUrlInput.value = v.serverUrl;
    show(settingsSaved);
    setTimeout(() => hide(settingsSaved), 1500);
  });
});
```

- [ ] **Step 5: popup.js — btnStart валидирует ПОЛЯ настроек (implicit-save), не пишет креды**

В обработчике `btnStart` блок `chrome.storage.local.set({ serverUrl: SERVER_URL, authUsername: AUTH_USERNAME, authPassword: AUTH_PASSWORD, ... })` (строки 268-278) заменить на:

```javascript
  // Источник истины — поля настроек: пользователь мог заполнить их,
  // не нажав «Сохранить». Implicit-save перед стартом — иначе Start
  // работал бы по устаревшему storage при заполненных полях.
  const v = validateConfig({ serverUrl: serverUrlInput.value, apiKey: apiKeyInput.value });
  if (!v.ok) {
    settingsBlock.open = true;
    show(errorText);
    errorText.textContent = v.error;
    btnStart.disabled = false;
    return;
  }
  await chrome.storage.local.set({
    serverUrl: v.serverUrl,
    apiKey: v.apiKey,
    employee: (employeeInput.value || "").trim(),
  });
  serverUrlInput.value = v.serverUrl;

  chrome.storage.local.set({
    recordingStartedAt: Date.now(),
    recordingEndedAt: null,
    error: null,
    chunksUploaded: 0,
    status: "connecting",
    micDenied,
  });
```

- [ ] **Step 6: popup.js — ссылки из storage, не из константы**

В `updateUI` строку 217 `const url = SERVER_URL;` заменить на:

```javascript
    const url = data.serverUrl || "";
```

(`data.serverUrl` уже приходит: он в списках `chrome.storage.local.get` на строках 101 и 110.)

- [ ] **Step 7: Проверка — хардкода не осталось**

Run: `grep -rn "rogov\|automate-it\|AUTH_USERNAME\|AUTH_PASSWORD\|authUsername\|authPassword" extension/ --include='*.js' --include='*.html'`
Expected: совпадения ТОЛЬКО в `background.js`/`offscreen.js` (уберёт Task 3) и строка `chrome.storage.local.remove(["authUsername", "authPassword"])` в popup.js.

- [ ] **Step 8: Commit**

```bash
git add extension/popup.html extension/popup.js
git commit -m "feat(extension): настройки Сервер/API-ключ/Сотрудник в popup вместо хардкода rogov+Basic"
```

---

### Task 3: background.js + offscreen.js — убрать Basic, добавить employee

**Files:**
- Modify: `extension/background.js:27-40`
- Modify: `extension/offscreen.js` (строки 9-11, 27-42, 52-55, 73-76, 90-95, 197)

**Interfaces:**
- Consumes: storage-ключи `serverUrl`/`apiKey`/`employee` из Task 2.
- Produces: сообщение `start-recording` несёт `{streamId, serverUrl, apiKey, employee, tabId}` (БЕЗ authUsername/authPassword); все fetch'и — только `X-API-Key`; `POST /api/sessions` шлёт `metadata.employee`, если задан (зеркало desktop `recorder.js:91`; поле не в `_SERVER_OWNED_METADATA` — сервер его сохраняет, менеджер-скоупинг дашборда матчится по нему).

- [ ] **Step 1: background.js — конфиг без Basic**

Строки 27-40 заменить на:

```javascript
  // Конфиг из настроек popup'а (single source — chrome.storage)
  const { serverUrl = "", apiKey = "", employee = "" } =
    await chrome.storage.local.get(["serverUrl", "apiKey", "employee"]);

  // Send to offscreen document to start recording
  chrome.runtime.sendMessage({
    type: "start-recording",
    streamId,
    serverUrl,
    apiKey,
    employee,
    tabId,
  });
```

- [ ] **Step 2: offscreen.js — состояние и заголовки**

Строки 9-11:

```javascript
let serverUrl = "";
let authHeader = "";
let apiKey = "";
```

заменить на:

```javascript
let serverUrl = "";
let apiKey = "";
let employee = "";
```

`createSession` (строки 27-42) заменить на:

```javascript
// Create a session on the server
async function createSession(tabId) {
  const metadata = {
    source: "chrome-extension",
    tabId,
    recordedAt: new Date().toISOString(),
  };
  if (employee) metadata.employee = employee;
  const resp = await fetch(`${serverUrl}/api/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
    body: JSON.stringify({ metadata }),
  });
  if (!resp.ok) throw new Error(`Failed to create session: ${resp.status}`);
  const data = await resp.json();
  return data.id;
}
```

В `uploadChunk` (строка 54) заголовки → `headers: { "X-API-Key": apiKey }`.
В `finishSession` (строка 75) заголовки → `headers: { "X-API-Key": apiKey }`.

- [ ] **Step 3: offscreen.js — сигнатура startRecording**

Строки 90-95:

```javascript
async function startRecording(streamId, url, tabId, username, password, key) {
  serverUrl = url;
  authHeader = username ? "Basic " + btoa(username + ":" + password) : "";
  apiKey = key || "";
```

заменить на:

```javascript
async function startRecording(streamId, url, tabId, key, emp) {
  serverUrl = url;
  apiKey = key || "";
  employee = (emp || "").trim();
```

Строку 197 (листенер) заменить на:

```javascript
    startRecording(message.streamId, message.serverUrl, message.tabId, message.apiKey, message.employee);
```

- [ ] **Step 4: Проверка — Basic выпилен целиком**

Run: `grep -rn "authHeader\|authUsername\|authPassword\|Basic\|btoa" extension/ --include='*.js'`
Expected: единственное совпадение — `chrome.storage.local.remove(["authUsername", "authPassword"])` в popup.js (миграционная чистка).

- [ ] **Step 5: Commit**

```bash
git add extension/background.js extension/offscreen.js
git commit -m "feat(extension): только X-API-Key (Basic удалён), metadata.employee для атрибуции"
```

---

### Task 4: manifest.json — ребренд + host_permissions

**Files:**
- Modify: `extension/manifest.json`

**Interfaces:**
- Produces: имя «SilentQA Recorder», версия 2.0.0, `host_permissions: ["https://*.silentqa.com/*"]`. Тенанты на custom-доменах работают через серверный CORS (у бэкенда `ALLOWED_ORIGINS="*"` по умолчанию — обычный CORS-запрос проходит и без host_permissions); если прод сузит `ALLOWED_ORIGINS` — надо будет добавить origin расширения (`chrome-extension://<id>`) в список.

- [ ] **Step 1: Правка manifest.json**

```json
{
  "manifest_version": 3,
  "name": "SilentQA Recorder",
  "version": "2.0.0",
  "description": "Запись звонков (Zoom, Meet и др.) для речевой аналитики SilentQA",
  "permissions": [
    "tabCapture",
    "offscreen",
    "storage",
    "activeTab"
  ],
  "host_permissions": [
    "https://*.silentqa.com/*"
  ],
  "action": {
    "default_popup": "popup.html",
    "default_icon": {
      "16": "icons/icon16.png",
      "48": "icons/icon48.png",
      "128": "icons/icon128.png"
    }
  },
  "icons": {
    "16": "icons/icon16.png",
    "48": "icons/icon48.png",
    "128": "icons/icon128.png"
  },
  "background": {
    "service_worker": "background.js"
  }
}
```

Run: `python3 -c "import json; json.load(open('extension/manifest.json')); print('json ok')"`
Expected: `json ok`.

- [ ] **Step 2: Commit**

```bash
git add extension/manifest.json
git commit -m "feat(extension): ребренд SilentQA Recorder 2.0.0 + host_permissions *.silentqa.com"
```

---

### Task 5: yandex-клон — генерация скриптом + CI-гард

**Files:**
- Create: `scripts/sync_yandex_extension.sh`
- Modify: `extension-yandex/` (перегенерация скриптом — руками не трогать)
- Modify: `.github/workflows/tests.yml` (джоба `clients`; создан Планом 1 Task 6)

**Interfaces:**
- Produces: `scripts/sync_yandex_extension.sh` — синхронизация `extension/` → `extension-yandex/` (байт-в-байт, `README.md` клона сохраняется); `--check` — только проверка, exit 1 при расхождении. CI-джоба `clients`: node-тесты расширения + `--check`.

- [ ] **Step 1: Создать `scripts/sync_yandex_extension.sh`**

```bash
#!/usr/bin/env bash
# extension-yandex/ — генерируемый байт-в-байт клон extension/ (+ свой README.md).
# Без флагов — синхронизировать; --check — только проверить (CI-гард).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" = "--check" ]; then
    if diff -r --exclude=README.md extension extension-yandex >/dev/null; then
        echo "extension-yandex синхронизирован"
    else
        echo "extension-yandex РАЗЪЕХАЛСЯ с extension/ — запусти scripts/sync_yandex_extension.sh" >&2
        diff -rq --exclude=README.md extension extension-yandex >&2 || true
        exit 1
    fi
    exit 0
fi

# --delete НЕ трогает excluded README.md (rsync удаляет excluded только с --delete-excluded)
rsync -a --delete --exclude=README.md extension/ extension-yandex/
echo "extension-yandex обновлён из extension/"
```

Run: `chmod +x scripts/sync_yandex_extension.sh && bash -n scripts/sync_yandex_extension.sh`
Expected: exit 0.

- [ ] **Step 2: Прогнать синк + проверку**

Run: `scripts/sync_yandex_extension.sh && scripts/sync_yandex_extension.sh --check && ls extension-yandex/README.md`
Expected: «обновлён» → «синхронизирован» → README.md на месте. `git status` показывает обновлённые файлы клона (popup/background/offscreen/manifest/config.js/tests).

- [ ] **Step 3: CI-джоба**

В `.github/workflows/tests.yml` добавить джобу:

```yaml
  clients:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - run: node --test extension/tests/
      - run: bash scripts/sync_yandex_extension.sh --check
```

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/tests.yml')); print('yaml ok')"`
Expected: `yaml ok`.

- [ ] **Step 4: Commit + push, проверить Actions**

```bash
git add scripts/sync_yandex_extension.sh extension-yandex/ .github/workflows/tests.yml
git commit -m "feat(extension): yandex-клон генерируется скриптом, CI-гард на расхождение"
git push origin multi-tenant-core-phase1
```

Run: `gh run list -L 1 --repo Chechetov/silentqa-platform`
Expected: джоба `clients` зелёная.

---

### Task 6: E2E-смоук на стейджинге + сверка download-страницы

**Files:**
- Modify (возможно): `backend/static/download/index.html` (строки ~86-137 — формулировки инструкции)

**Interfaces:**
- Consumes: стейджинг из Плана 1 Task 7 (тенант `stagetest`, его API-ключ, хост `stagetest.staging.silentqa.com` или `staging-t.silentqa.com`).

- [ ] **Step 1: Негатив-проверки контракта (curl, до браузера)**

```bash
# Без ключа → 401 (api_key_required=true)
curl -s -o /dev/null -w "%{http_code}\n" -H "Host: stagetest.staging.silentqa.com" \
  -H "Content-Type: application/json" -d '{"metadata":{}}' localhost:8008/api/sessions   # 401
# С чужим/битым ключом → 403
curl -s -o /dev/null -w "%{http_code}\n" -H "Host: stagetest.staging.silentqa.com" \
  -H "X-API-Key: sqa_wrong" -H "Content-Type: application/json" -d '{"metadata":{}}' \
  localhost:8008/api/sessions                                                            # 403
# С ключом stagetest → 200/201 + id
curl -s -H "Host: stagetest.staging.silentqa.com" -H "X-API-Key: <ключ stagetest>" \
  -H "Content-Type: application/json" -d '{"metadata":{"source":"smoke"}}' \
  localhost:8008/api/sessions
```

- [ ] **Step 2: Браузерный смоук расширения**

1. Chrome → Расширения → «Режим разработчика» → «Загрузить распакованное» → папка `extension/`.
2. Открыть popup: блок «Настройки сервера» раскрыт сам (конфиг пуст). Ввести сервер `staging-t.silentqa.com` (или адрес стейджинг-тенанта), API-ключ stagetest, имя сотрудника «Смоук Тестов» → «Сохранено ✓».
3. Открыть любую вкладку со звуком (YouTube) → Start Recording → говорить в микрофон ~30 сек → Stop.
4. Ожидаемо: чанки растут в popup'е; после Stop — статус Processing, ссылка Result ведёт на `https://<сервер>/#call/<id>`.
5. В дашборде stagetest звонок появился, обработался (или срезан short-call гейтом <20 сек — тогда записать >20 сек), у сессии `metadata.employee = "Смоук Тестов"`.

- [ ] **Step 3: Start без настроек — дружелюбная ошибка**

В чистом профиле Chrome (или после очистки storage расширения): Start сразу → блок настроек раскрывается, ошибка «Укажите адрес сервера в настройках», записи нет.

- [ ] **Step 4: Сверить download-страницу с фактическим UI**

Открыть `backend/static/download/index.html` (строки ~86-137): инструкция уже говорит «в настройках расширения впишите Сервер и API-ключ» — сверить дословно с реальными лейблами («Настройки сервера», «Сервер (адрес вашего тенанта)», «API-ключ», «Имя сотрудника»); при расхождении поправить текст страницы. Упомянуть поле «Имя сотрудника» (для менеджер-скоупинга).

- [ ] **Step 5: Вычистить пароль из рабочего дерева (вне расширений)**

Полный литерал пароля живёт не только в расширениях: `INSTRUCTIONS.md:32`, `docs/superpowers/specs/2026-06-24-domain-artifacts-audit.md:64`, `docs/superpowers/specs/2026-03-21-multi-platform-recorder-prompt.md:65`. Заменить в этих трёх местах литерал на `rogov2025***` (git-историю лечит только отзыв пароля — см. «Вне scope», — но держать его в рабочем дереве незачем).

Run: `git grep -n "rogov2025secure"`
Expected: пусто.

- [ ] **Step 6: Commit**

```bash
git add backend/static/download/index.html INSTRUCTIONS.md docs/superpowers/specs/
git commit -m "docs: инструкция download сверена с settings-UI; пароль замаскирован в рабочем дереве"
```

---

## Порядок и зависимости

1 → 2 (popup использует validateConfig) → 3 (контракт storage/сообщений) → 4 (независим, но логично после) → 5 (клон синкается ПОСЛЕ всех правок extension/) → 6 (нужен стейджинг Плана 1). Правки `extension/` между Task 2-4 НЕ синкать в клон по одной — один синк в Task 5.

## Verification (итоговая)

- `node --test extension/tests/` зелёный; `scripts/sync_yandex_extension.sh --check` зелёный; CI-джоба `clients` зелёная.
- `grep -rn "rogov\|automate-it\|Basic\|btoa" extension/ extension-yandex/ --include='*.js' --include='*.html' --include='*.json'` — ноль совпадений, кроме миграционного `remove(["authUsername", "authPassword"])`.
- `git grep -n "rogov2025secure"` по всему репо — пусто (маскированные `rogov2025***` в доках допустимы; полный литерал остаётся только в git-истории — лечится отзывом пароля на стороне старого realestate-деплоя).
- Смоук Task 6: звонок из расширения дошёл до дашборда стейджинг-тенанта с правильным `employee`.

## Вне scope (сознательно)

- Desktop-приложение (уже двухрежимное; Basic-остатки = broker-режим realestate → спека unify-recorder-identity).
- Перевод существующих английских лейблов popup'а (Start Recording и т.п.).
- Публикация в Chrome Web Store / Яндекс-каталоге (сейчас распространение zip'ом через /download).
- Отзыв/ротация утёкшего в git пароля (`rogov2025***`, полное значение — в git-истории и старом `popup.js:23`) — он от СТАРОГО однотенантного деплоя (`/root/projects/realestate`, Basic на Caddy); зафиксировать владельцу отдельно: пароль светится в истории git этого репо.
