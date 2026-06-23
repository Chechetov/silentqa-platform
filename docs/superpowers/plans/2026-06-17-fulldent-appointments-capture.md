# Fulldent — захват очных приёмов десктопом (API-ключ + атрибуция) — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: используйте superpowers:subagent-driven-development
> (рекомендуется) или superpowers:executing-plans для выполнения задача-за-задачей. Шаги — чекбоксы
> (`- [ ]`). Каждая backend-задача: написать падающий тест → прогнать (увидеть fail) → минимальная
> реализация реальным кодом → прогнать (зелёный) → коммит. Desktop-задачи — `node --check` +
> ручной чек-лист (E2E в этой среде невозможно).

**Goal:** Дать персоналу тенанта **fulldent** записывать очные приёмы через десктоп-приложение
**только по API-ключу** (без AmoCRM-брокера), с атрибуцией по имени (`metadata.employee`).
realestate broker-тракт — без изменений (всё условно/gated).

**Architecture:** Бэкенд ingestion уже принимает API-ключ (`require_ingestion_auth`); добавляем
санкционированное клиентское поле `employee` в `create_session` через чистый хелпер, гейтим
realestate-only авто-шаблон «Zoom-ЖК», (опц.) плюмбим `appointment_type → scenario_id` в enqueue, и
делаем заголовок дашборда per-tenant. Десктоп: дискриминатор «API-key mode» (ключ есть + username
пуст) пропускает broker-логин; в setup добавляется поле «Сотрудник», `employee` идёт в `createSession`.

**Tech Stack:** FastAPI + Starlette TestClient (юниты, без Postgres/Redis), SQLAlchemy async ORM,
pytest; Electron (vanilla JS, без бандлера) — `node --check` + ручной E2E; Python 3.12,
`/root/projects/silentqa/.venv/bin/python`.

**Spec:** `docs/superpowers/specs/2026-06-17-fulldent-appointments-capture-design.md`

---

## File Structure

| Файл | Действие | Ответственность |
|------|----------|------------------|
| `backend/app/routes/sessions.py` | Modify | Хелпер `build_session_metadata`; gating Zoom-шаблона; (опц.) `appointment_type→scenario_id` в `finish_session` |
| `backend/app/company_scenarios.py` | Create (опц. задача 5) | Backend-чтение допустимых scenario id из company-config тенанта |
| `backend/tests/test_session_employee.py` | Create | Юниты: санитизация employee, broker-сосуществование |
| `backend/tests/test_desktop_template_gate.py` | Create | Юнит: Zoom-шаблон только для AmoCRM-тенантов |
| `backend/tests/test_finish_scenario.py` | Create (опц. задача 5) | Юнит: валидный `appointment_type` → `scenario_id` в enqueue |
| `backend/app/routes/tenancy_check.py` | Modify (задача 6) | `display_name` в ответ `/api/tenancy/features` |
| `backend/app/tenancy_http.py` | Modify (задача 6) | `display_name` в SELECT+строку реестра |
| `backend/alembic/versions/*` | — | НЕ требуется (display_name уже в shared.tenants) |
| `backend/tests/test_tenancy_features.py` | Modify (задача 6) | `display_name` в features |
| `desktop-app/src/renderer/renderer.js` | Modify | `isApiKeyMode`, ветки setup/init/showRecorder, state employee |
| `desktop-app/src/renderer/recorder.js` | Modify | `setEmployee`, employee в `createSession` |
| `desktop-app/src/renderer/index.html` | Modify | Поля `#setupEmployee`/`#inputEmployee` |
| `backend/static/app.js` | Modify (задача 6) | `document.title`/logo из `display_name` |

**Порядок выполнения (как идут в документе):** backend-first → desktop → брендинг → откладываемое:
**1** (хелпер employee) → **2** (gating Zoom-шаблона) → **7** (десктоп API-key mode + UI) →
**8** (десктоп setEmployee/createSession) → **6** (брендинг) → **5** (сценарий, ОТКЛАДЫВАЕМАЯ).
Нумерация — это метки, не порядок. Зависимости: задача 2 опирается на хелпер из задачи 1
(общий файл `sessions.py`); задача 5 опирается на passthrough `appointment_type` из задачи 1; задача 8
дополняет UI задачи 7. Остальные независимы.

---

## Задача 1 — чистый хелпер `build_session_metadata`: санитизация employee `[backend: unit-tested]`

Вынести сборку метаданных из `create_session` в чистую sync-функцию (как `_SERVER_OWNED_METADATA`),
санкционировать `employee` (str, strip, ≤120). DB-специфику (Zoom-шаблон, commit) оставить в маршруте.

**Files:**
- Modify: `backend/app/routes/sessions.py`
- Create: `backend/tests/test_session_employee.py`

- [ ] **Шаг 1: падающий тест** — `backend/tests/test_session_employee.py`:

```python
"""build_session_metadata: санкционированный employee + сосуществование с broker."""
import uuid

from app.routes.sessions import build_session_metadata
from app.schemas import BrokerInfo


def _broker():
    return BrokerInfo(id=uuid.uuid4(), amocrm_user_id=42, email="b@x.io", name="Брокер Б")


def test_employee_kept_trimmed():
    meta = build_session_metadata({"source": "desktop-app", "employee": "  Иванов  "}, None)
    assert meta["employee"] == "Иванов"
    assert meta["source"] == "desktop-app"


def test_employee_empty_dropped():
    assert "employee" not in build_session_metadata({"employee": "   "}, None)
    assert "employee" not in build_session_metadata({"employee": ""}, None)


def test_employee_nonstring_dropped():
    assert "employee" not in build_session_metadata({"employee": {"x": 1}}, None)
    assert "employee" not in build_session_metadata({"employee": 123}, None)


def test_employee_truncated_to_120():
    meta = build_session_metadata({"employee": "Я" * 500}, None)
    assert len(meta["employee"]) == 120


def test_server_owned_stripped_even_if_client_sends():
    meta = build_session_metadata(
        {"company_id": "evil", "scenario_id": "evil", "broker_id": "x", "employee": "Пётр"}, None)
    assert "company_id" not in meta and "scenario_id" not in meta and "broker_id" not in meta
    assert meta["employee"] == "Пётр"


def test_broker_attribution_when_present():
    meta = build_session_metadata({"source": "amocrm"}, _broker())
    assert meta["broker_name"] == "Брокер Б"
    assert meta["amocrm_user_id"] == 42
    assert meta["responsible_user_id"] == 42
    assert "broker_id" in meta


def test_employee_and_broker_coexist():
    meta = build_session_metadata({"employee": "Иванов"}, _broker())
    assert meta["employee"] == "Иванов"
    assert meta["broker_name"] == "Брокер Б"


def test_appointment_type_passthrough():
    # appointment_type — НЕ server-owned (используется задачей 5 в finish_session).
    # Должен доживать до сохранения, не вычищаться.
    meta = build_session_metadata({"appointment_type": "consultation"}, None)
    assert meta["appointment_type"] == "consultation"
```

- [ ] **Шаг 2: прогнать — упадёт (ImportError build_session_metadata):**
  `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_session_employee.py -v`

- [ ] **Шаг 3: реализация** — в `backend/app/routes/sessions.py` добавить хелпер **до** `create_session`
  (после `_SERVER_OWNED_METADATA`/`_DEFAULT_DESKTOP_TEMPLATE_NAME`, перед `_resolve_default_desktop_template_id`):

```python
_EMPLOYEE_MAX_LEN = 120


def build_session_metadata(raw_meta: dict | None, broker: "BrokerInfo | None") -> dict:
    """Собрать метаданные сессии (без БД): вычистить server-owned, санкционировать
    клиентский `employee` (атрибуция для recorder-only тенантов), проставить
    broker_* при наличии брокера. Чистая функция — юнит-тестируется без БД."""
    meta = dict(raw_meta or {})
    # Strip any server-owned keys the client tried to supply.
    for k in _SERVER_OWNED_METADATA:
        meta.pop(k, None)

    # Санкционированный `employee`: только непустая строка, обрезаем до лимита.
    # Намеренно НЕ в _SERVER_OWNED_METADATA — это легитимная клиентская атрибуция
    # (десктоп по API-ключу проставляет имя сотрудника; AmoCRM-потоки его не шлют).
    emp = meta.get("employee")
    if isinstance(emp, str):
        emp = emp.strip()[:_EMPLOYEE_MAX_LEN]
        if emp:
            meta["employee"] = emp
        else:
            meta.pop("employee", None)
    else:
        meta.pop("employee", None)

    if broker is not None:
        # Auto-attribute to the authenticated broker (authoritative).
        meta["broker_id"] = str(broker.id)
        meta["amocrm_user_id"] = broker.amocrm_user_id
        meta["broker_name"] = broker.name
        meta["responsible_user_id"] = broker.amocrm_user_id

    return meta
```

  Затем переписать начало `create_session` (строки 156-168) на использование хелпера:

```python
    meta = build_session_metadata(body.metadata, broker)
```
  (удалив старый блок `meta = dict(...)` + цикл strip + `if broker is not None:`; блок Zoom-шаблона
  строк 170-177 и `Session(...)`/commit — пока без изменений; gating Zoom — задача 2).

- [ ] **Шаг 4: прогнать — зелёный:**
  `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_session_employee.py -v`

- [ ] **Шаг 5: регресс — server-owned тест цел:**
  `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_server_owned_metadata.py -v`

- [ ] **Шаг 6: commit:** `git add backend/app/routes/sessions.py backend/tests/test_session_employee.py &&
  git commit -m "feat(sessions): sanctioned employee attribution via build_session_metadata helper"`

---

## Задача 2 — gating авто-шаблона «Zoom-ЖК» только для AmoCRM-тенантов `[backend: unit-tested]`

Не привязывать realestate-evaluation-шаблон к desktop-сессиям не-AmoCRM тенантов (иначе fulldent-приёмы
оцениваются по чужим критериям — см. спека §2.5/§4.5).

**Files:**
- Modify: `backend/app/routes/sessions.py`
- Create: `backend/tests/test_desktop_template_gate.py`

- [ ] **Шаг 1: падающий тест** — `backend/tests/test_desktop_template_gate.py`. `create_session` делает
  `db.add/commit/refresh` + (для realestate) `_resolve_default_desktop_template_id(db)` → переопределяем
  `get_db` фейковой async-сессией и патчим резолвер шаблона:

```python
"""Авто-Zoom-шаблон применяется ТОЛЬКО на AmoCRM-тенантах (realestate), не на fulldent."""
import asyncio
import uuid

import pytest
from starlette.testclient import TestClient


class _FakeResult:
    def __init__(self, val): self._val = val
    def scalar(self): return self._val
    def first(self): return None


class _FakeDB:
    """Минимальная async-сессия: ловит добавленный Session, отдаёт chunks_count=0."""
    def __init__(self): self.added = []
    def add(self, obj): self.added.append(obj)
    async def commit(self): pass
    async def refresh(self, obj):
        import datetime as _dt
        if getattr(obj, "id", None) is None: obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = _dt.datetime(2026, 6, 17, tzinfo=_dt.timezone.utc)
    async def scalar(self, *a, **k): return 0
    async def execute(self, *a, **k): return _FakeResult(0)


def _client(monkeypatch, fake_redis, slug, host, api_key_hash="h"):
    from app.tenancy_http import TenantRegistry
    rows = [{"slug": slug, "schema_name": f"t_{slug}", "status": "active",
             "custom_domains": [], "api_key_hash": api_key_hash,
             "api_key_required": True, "modules": {}}]

    async def fake_all(self): return rows
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)

    from app.main import app
    from app.database import get_db
    fake = _FakeDB()

    async def _get_db_override():
        yield fake
    app.dependency_overrides[get_db] = _get_db_override
    return TestClient(app, base_url=host), fake


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app
    app.dependency_overrides.clear()


def _api_key_headers(monkeypatch):
    # Заставляем сверку ключа проходить независимо от хэша.
    from app.routes import sessions as sess_mod
    monkeypatch.setattr(sess_mod, "_api_key_matches", lambda *a, **k: True, raising=False)
    return {"X-API-Key": "sqa_test"}


def test_fulldent_desktop_session_no_zoom_template(monkeypatch, fake_redis):
    # AMOCRM_TENANT_SLUGS не содержит fulldent → шаблон НЕ ставится.
    c, fake = _client(monkeypatch, fake_redis, "fulldent", "https://fulldent.silentqa.com")
    # ключ сверяется в auth_user._api_key_matches; патчим его там
    from app import auth_user
    monkeypatch.setattr(auth_user, "_api_key_matches", lambda *a, **k: True)
    r = c.post("/api/sessions", headers={"X-API-Key": "sqa_test"},
               json={"metadata": {"source": "desktop-app", "employee": "Доктор"}})
    assert r.status_code == 201
    sess = fake.added[-1]
    assert (sess.metadata_ or {}).get("template_id") is None
    assert (sess.metadata_ or {}).get("employee") == "Доктор"


def test_realestate_desktop_session_gets_zoom_template(monkeypatch, fake_redis):
    # realestate ∈ AMOCRM_TENANT_SLUGS → шаблон ставится (резолвер запатчен).
    c, fake = _client(monkeypatch, fake_redis, "realestate", "https://realestate.silentqa.com")
    from app import auth_user
    monkeypatch.setattr(auth_user, "_api_key_matches", lambda *a, **k: True)
    from app.routes import sessions as sess_mod

    async def fake_resolve(db): return "tpl-zoom-id"
    monkeypatch.setattr(sess_mod, "_resolve_default_desktop_template_id", fake_resolve)

    r = c.post("/api/sessions", headers={"X-API-Key": "sqa_test"},
               json={"metadata": {"source": "desktop-app"}})
    assert r.status_code == 201
    assert (fake.added[-1].metadata_ or {}).get("template_id") == "tpl-zoom-id"
```

> Примечание для исполнителя: точное имя/путь патча сверки ключа уточнить при первом fail — ключевой
> инвариант теста: **fulldent → нет `template_id`; realestate → есть**. Если проще — добавить в
> `require_ingestion_auth` обход через `api_key_required=False` строкой реестра (тогда X-API-Key не нужен),
> но тогда тест должен слать `source: desktop-app` без ключа и проверять тот же инвариант.

- [ ] **Шаг 2: прогнать — упадёт** (fulldent сейчас тоже получает шаблон, т.к. гейта нет; либо тест
  ещё не отражает gating): `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_desktop_template_gate.py -v`

- [ ] **Шаг 3: реализация** — в `backend/app/routes/sessions.py` обернуть блок авто-шаблона (строки 170-177)
  гейтом по AmoCRM-контуру. `AMOCRM_TENANT_SLUGS` и `get_tenant_slug` уже импортируются (строки 14-15):

```python
    # Desktop-app recordings on AmoCRM-tenants (realestate) are Zoom presentations —
    # авто-применяем Zoom-evaluation-шаблон. Для не-AmoCRM тенантов (fulldent и пр.)
    # этого НЕ делаем: их desktop-приёмы оцениваются сценарием company-config,
    # а realestate-протокол «презентация ЖК» перебил бы его (pipeline.py:685-707).
    if (meta.get("source") == "desktop-app" and not meta.get("template_id")
            and get_tenant_slug() in AMOCRM_TENANT_SLUGS):
        tid = await _resolve_default_desktop_template_id(db)
        if tid:
            meta["template_id"] = tid
```

- [ ] **Шаг 4: прогнать — зелёный:**
  `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_desktop_template_gate.py -v`

- [ ] **Шаг 5: commit:** `git add backend/app/routes/sessions.py backend/tests/test_desktop_template_gate.py &&
  git commit -m "fix(sessions): gate desktop Zoom-template auto-apply to AmoCRM tenants only"`

---

## Задача 7 — десктоп: дискриминатор «API-key mode» + поле «Сотрудник» `[desktop: human-verified]`

Чистая логика (`isApiKeyMode`) + UI/поток. E2E невозможно — `node --check` + ручной чек-лист.

**Files:**
- Modify: `desktop-app/src/renderer/renderer.js`
- Modify: `desktop-app/src/renderer/index.html`

- [ ] **Шаг 1 (опц. микро-юнит чистой логики):** в конец `renderer.js` добавить экспорт под Node для
  проверки `isApiKeyMode` (не влияет на браузер):

```js
// --- Тестовый экспорт чистой логики (Node) — не используется в Electron-окне ---
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { isApiKeyMode };
}
```
  Юнит (запускать `node desktop-app/tests/isApiKeyMode.test.js`) — создать
  `desktop-app/tests/isApiKeyMode.test.js`:
```js
const assert = require('assert');
// renderer.js трогает DOM на верхнем уровне; для чистого юнита держим isApiKeyMode
// БЕЗ зависимостей от DOM (она такова) и импортим её осторожно — если top-level DOM
// мешает, исполнитель выносит isApiKeyMode в отдельный модуль desktop-app/src/renderer/mode.js
// и реэкспортит из renderer.js. Базовый инвариант:
function isApiKeyMode(c) { return !!(c && c.apiKey && !c.username); }
assert.strictEqual(isApiKeyMode({ apiKey: 'k', username: '' }), true);
assert.strictEqual(isApiKeyMode({ apiKey: 'k', username: 'u' }), false);
assert.strictEqual(isApiKeyMode({ apiKey: '', username: '' }), false);
assert.strictEqual(isApiKeyMode(null), false);
console.log('isApiKeyMode OK');
```
> Если top-level DOM-обращения `renderer.js` ломают прямой `require`, исполнитель выносит `isApiKeyMode`
> в `desktop-app/src/renderer/mode.js` (подключив `<script src="mode.js">` перед `renderer.js`) — это
> предпочтительный «extractable pure logic» путь из спеки §6.

- [ ] **Шаг 2: реализация renderer.js.**
  2a. Объявить состояние рядом с прочими (после `let apiKey = '';` строка 93):
```js
  let employee = '';
```
  2b. Добавить чистый хелпер (рядом с `basicAuthHeader`, ~строка 102):
```js
  // API-key mode: тенант без AmoCRM-брокеров (fulldent) — ключ есть, broker-логин не нужен.
  // realestate вводит username/password → broker-mode, поток не меняется.
  function isApiKeyMode(c) {
    return !!(c && c.apiKey && !c.username);
  }
```
  2c. Поле «Сотрудник» элементы (рядом с `setupApiKey`, строка 25; и `inputApiKey`, строка 77):
```js
  const setupEmployee = document.getElementById('setupEmployee');
  const inputEmployee = document.getElementById('inputEmployee');
```
  2d. `persistCredentials()` (строки 257-267): добавить `employee` в объект.
  2e. `btnSaveSetup` (строки 231-253): после `apiKey = setupApiKey.value;` добавить
      `employee = setupEmployee.value;`; синхронизировать `if (inputEmployee) inputEmployee.value = employee;`;
      заменить хвост `showLoginScreen();` (строка 247) на:
```js
    if (isApiKeyMode({ apiKey, username })) {
      showRecorderScreen();
    } else {
      showLoginScreen();
    }
```
  2f. `showRecorderScreen()` (строки 138-173): в начале — ветка API-key-mode:
```js
    if (isApiKeyMode({ apiKey, username })) {
      hide(brokerInfo);
      if (employee) {
        brokerInfo.innerHTML = '';
        const label = document.createElement('span'); label.textContent = 'Сотрудник: ';
        const b = document.createElement('b'); b.textContent = employee;
        brokerInfo.appendChild(label); brokerInfo.appendChild(b);
        show(brokerInfo);
      }
      if (window.Recorder && window.Recorder.setBrokerToken) window.Recorder.setBrokerToken('');
      if (window.Recorder && window.Recorder.setEmployee) window.Recorder.setEmployee(employee);
      if (window.Recorder && window.Recorder.setApiKey) window.Recorder.setApiKey(apiKey);
      if (btnLogout) hide(btnLogout);
      if (!recoveryStarted) { recoveryStarted = true; runRecovery().catch(() => {}); }
      return;
    }
```
      (существующий broker-блок остаётся ниже без изменений.)
  2g. `init()` (строки 736-798): после загрузки кредов и `setApiKey` (строка 756), **перед**
      `if (!brokerJwt)` (строка 758) добавить:
```js
  employee = creds.employee || '';
  if (inputEmployee) inputEmployee.value = employee;
  if (isApiKeyMode(creds)) {
    showRecorderScreen();
    return;
  }
```
  2h. `btnSaveSettings` (строки 712-724): добавить `if (inputEmployee) employee = inputEmployee.value;`
      перед `persistCredentials()`.

- [ ] **Шаг 3: реализация index.html.**
  3a. Setup-экран: после блока API Key (строки 50-53) добавить:
```html
    <div class="settings-field">
      <label>Сотрудник (имя для атрибуции записей)</label>
      <input type="text" id="setupEmployee" placeholder="Напр. Иванова А.А.">
    </div>
```
  3b. Settings-панель: после блока API Key (строки 229-232) добавить:
```html
      <div class="settings-field">
        <label>Сотрудник</label>
        <input type="text" id="inputEmployee" placeholder="имя сотрудника">
      </div>
```

- [ ] **Шаг 4: синтаксис:**
  `node --check desktop-app/src/renderer/renderer.js && node --check desktop-app/src/renderer/recorder.js &&
   echo OK` (и `node desktop-app/tests/isApiKeyMode.test.js` если задача 7 шаг 1 сделан).

- [ ] **Шаг 5: ручной чек-лист (записать в PR/коммит):**
  - [ ] `cd desktop-app && npm install && npm start`; ввести Server URL + API-ключ fulldent + имя
        сотрудника, **username/password пусто** → после «Save & Start» сразу открывается экран записи
        (broker-логина НЕТ), показывается «Сотрудник: <имя>».
  - [ ] Перезапуск приложения → сразу экран записи (минуя `/api/auth/me`).
  - [ ] Запись приёма → в дашборде fulldent у сессии `metadata.employee = <имя>`.
  - [ ] **Регресс realestate:** ввести Server URL + username + password (без API-ключа) → broker-логин
        как раньше; запись атрибутируется брокером.

- [ ] **Шаг 6: commit:** `git add desktop-app/src/renderer/renderer.js desktop-app/src/renderer/index.html &&
  git commit -m "feat(desktop): API-key mode skips broker login + employee attribution field"`

---

## Задача 8 — десктоп: `setEmployee` + employee в `createSession` `[desktop: human-verified]`

**Files:**
- Modify: `desktop-app/src/renderer/recorder.js`

- [ ] **Шаг 1: реализация.**
  1a. Module-var рядом с `let apiKey = '';` (строка 22):
```js
let employee = '';
```
  1b. В `createSession()` (строки 83-98) — добавить employee в metadata:
```js
  const metadata = {
    source: 'desktop-app',
    platform: window.electronAPI.platform,
    recordedAt: new Date().toISOString(),
  };
  if (employee) metadata.employee = employee;
  const resp = await fetchWithRetry(`${serverUrl}/api/sessions`, {
    method: 'POST',
    headers: buildHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ metadata }),
  });
```
  1c. Экспорт `window.Recorder` (строки 663-671) — добавить метод:
```js
  setEmployee: (v) => { employee = (v || '').trim(); },
```

- [ ] **Шаг 2: синтаксис:** `node --check desktop-app/src/renderer/recorder.js && echo OK`

- [ ] **Шаг 3: ручной чек:** запись по API-ключу → созданная сессия имеет `metadata.employee`
      (проверяется в чек-листе задачи 7, шаг 5).

- [ ] **Шаг 4: commit:** `git add desktop-app/src/renderer/recorder.js &&
  git commit -m "feat(desktop): Recorder.setEmployee sends employee in session metadata"`

---

## Задача 6 — брендинг: per-tenant заголовок дашборда `[backend: unit-tested]` + `[desktop: n/a]`

Заголовок из `shared.tenants.display_name` через `/api/tenancy/features`; SPA ставит `document.title`.

**Files:**
- Modify: `backend/app/tenancy_http.py`, `backend/app/routes/tenancy_check.py`, `backend/static/app.js`
- Modify: `backend/tests/test_tenancy_features.py`

- [ ] **Шаг 1: падающий тест** — добавить в `backend/tests/test_tenancy_features.py` (читать существующий
  файл для скаффолдинга; реестр-строки там — добавить `display_name`):

```python
def test_features_includes_display_name(monkeypatch, fake_redis):
    # реестр-строка с display_name → features отдаёт его наружу
    from app.tenancy_http import TenantRegistry
    rows = [{"slug": "fulldent", "schema_name": "t_fulldent", "status": "active",
             "custom_domains": [], "api_key_hash": None, "api_key_required": True,
             "modules": {}, "display_name": "Клиника Фулдент"}]

    async def fake_all(self): return rows
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    from starlette.testclient import TestClient
    c = TestClient(app, base_url="https://fulldent.silentqa.com")
    r = c.get("/api/tenancy/features")
    assert r.status_code == 200
    assert r.json().get("display_name") == "Клиника Фулдент"
```

- [ ] **Шаг 2: прогнать — упадёт:**
  `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_tenancy_features.py::test_features_includes_display_name -v`

- [ ] **Шаг 3: реализация.**
  3a. `backend/app/tenancy_http.py` SELECT (строки 36-39) — добавить `display_name`:
```python
                    "SELECT slug, schema_name, status, custom_domains, "
                    "api_key_hash, api_key_required, modules, display_name "
                    "FROM shared.tenants"
```
      и в dict-строку (строки 41-52) — `"display_name": r.display_name,`.
  3b. `backend/app/routes/tenancy_check.py` `get_features` (строки 37-47) — добавить в ответ:
```python
    result = {name: module_enabled(modules, name) for name in MODULE_DEFAULTS}
    if tenant.get("display_name"):
        result["display_name"] = tenant["display_name"]
    return result
```
      (Совместимость: `display_name` — отдельный строковый ключ; SPA уже фильтрует модули по булям,
       лишний строковый ключ безвреден — но исполнителю проверить `app.js:95-98`, что не-bool значение
       не считается «модулем»; селектор там `[data-module]` идёт по DOM, не по ключам features → ок.)
  3c. `backend/static/app.js` после строки 94 (`features = await api(...)`):
```js
    if (features && features.display_name) {
      document.title = features.display_name;
      const logoText = document.querySelector('.logo-text');
      if (logoText) logoText.textContent = features.display_name;
    }
```

- [ ] **Шаг 4: прогнать — зелёный + регресс features:**
  `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_tenancy_features.py -v`

- [ ] **Шаг 5: commit:** `git add backend/app/tenancy_http.py backend/app/routes/tenancy_check.py
  backend/static/app.js backend/tests/test_tenancy_features.py &&
  git commit -m "feat(branding): per-tenant dashboard title from display_name via /features"`

---

## Задача 5 (ОТКЛАДЫВАЕМАЯ) — `appointment_type → scenario_id` в enqueue `[backend: unit-tested]`

Дать десктопу выбирать тип приёма; сервер валидирует против сценариев company-config тенанта и
передаёт `config={"scenario_id": ...}` в `finish_session`-enqueue. RE-safe: нет `appointment_type` →
поведение не меняется. **Делать последней; если scope раздувается — оставить задокументированной.**

**Files:**
- Create: `backend/app/company_scenarios.py`
- Modify: `backend/app/routes/sessions.py` (`finish_session`)
- Create: `backend/tests/test_finish_scenario.py`

- [ ] **Шаг 1: backend-хелпер чтения допустимых сценариев.** `backend/app/company_scenarios.py`:
```python
"""Backend-сторона: допустимые scenario id из company-config тенанта.

company-config — файлы COMPANIES_PATH/<id>.json (см. CLAUDE.md, routes/companies.py).
company_config_id тенанта — из shared.tenants. Возвращаем множество валидных id, чтобы
finish_session мог санкционировать клиентский appointment_type → scenario_id."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

# ВАЖНО: backend `settings` НЕ содержит COMPANIES_PATH (проверено: backend/app/config.py).
# Worker берёт его из env (worker/tasks/company_config.py:14). Читаем env напрямую —
# единый источник с воркером, дефолт /companies (как в CLAUDE.md / docker-mount).
def _companies_root() -> Path:
    return Path(os.getenv("COMPANIES_PATH", "/companies"))


@lru_cache(maxsize=64)
def _scenarios_for_config(config_id: str) -> frozenset[str]:
    path = _companies_root() / f"{config_id}.json"
    if not path.exists():
        return frozenset()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    return frozenset(s["id"] for s in data.get("scenarios", []) if s.get("id"))


def valid_scenario(config_id: str | None, scenario_id: str | None) -> str | None:
    """Вернуть scenario_id, если он валиден для config_id; иначе None."""
    if not config_id or not scenario_id:
        return None
    return scenario_id if scenario_id in _scenarios_for_config(config_id) else None
```
  > Проверить имя `settings.COMPANIES_PATH` (в worker — env `COMPANIES_PATH`, default `/companies`,
  > см. `worker/tasks/company_config.py:14`). Если в backend `settings` его нет — читать
  > `os.getenv("COMPANIES_PATH", "/companies")` напрямую.

- [ ] **Шаг 2: падающий тест** — `backend/tests/test_finish_scenario.py` (юнит хелпера + опц. маршрут).
  Минимум — чистый хелпер на временном файле конфига:
```python
import json
from pathlib import Path

import pytest


@pytest.fixture
def companies_dir(tmp_path, monkeypatch):
    (tmp_path / "dental.json").write_text(json.dumps({
        "id": "dental",
        "scenarios": [{"id": "consultation"}, {"id": "treatment_plan"}],
    }), encoding="utf-8")
    monkeypatch.setenv("COMPANIES_PATH", str(tmp_path))
    # хелпер кеширует через lru_cache — сбросить между тестами (env поменялся)
    import app.company_scenarios as cs
    cs._scenarios_for_config.cache_clear()
    return tmp_path


def test_valid_scenario_accepts_known(companies_dir):
    from app.company_scenarios import valid_scenario
    assert valid_scenario("dental", "consultation") == "consultation"
    assert valid_scenario("dental", "treatment_plan") == "treatment_plan"


def test_valid_scenario_rejects_unknown(companies_dir):
    from app.company_scenarios import valid_scenario
    assert valid_scenario("dental", "evil") is None
    assert valid_scenario("dental", None) is None
    assert valid_scenario(None, "consultation") is None
    assert valid_scenario("nonexistent_cfg", "consultation") is None
```

- [ ] **Шаг 3: прогнать — упадёт (нет модуля):**
  `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_finish_scenario.py -v`

- [ ] **Шаг 4: реализация хелпера** (шаг 1) + плюминг в `finish_session` (`sessions.py:437-468`):
  перед enqueue определить scenario из сохранённого `appointment_type`:
```python
    # company_config_id текущего тенанта — короткий async SQL к shared.tenants
    # (worker делает то же синхронно: worker/tasks/company_config.py:32-43).
    async def _tenant_company_config_id(db) -> str | None:
        from tenancy.context import require_tenant_slug
        row = (await db.execute(
            text("SELECT company_config_id FROM shared.tenants WHERE slug = :slug"),
            {"slug": require_tenant_slug()},
        )).first()
        return row[0] if row else None

    meta = session.metadata_ or {}
    appt = meta.get("appointment_type")
    config: dict | None = None
    if appt:
        from app.company_scenarios import valid_scenario
        cfg_id = await _tenant_company_config_id(db)
        scen = valid_scenario(cfg_id, appt)
        if scen:
            config = {"scenario_id": scen}

    # ... (status=processing, commit — как сейчас) ...
    celery_app.send_task("pipeline.process_session", args=[str(session_id)],
                         kwargs={"tenant_schema": tenant_schema, "config": config},
                         queue="transcription")
```
  > Уточнения для исполнителя: (1) `appointment_type` должен **доживать** до `finish` — он сохраняется в
  > `create_session` (НЕ в `_SERVER_OWNED_METADATA`, как валидное клиентское поле; добавить мини-тест в
  > задачу 1, что он не вычищается). (2) `_tenant_company_config_id(db)` — короткий async SQL
  > `SELECT company_config_id FROM shared.tenants WHERE slug=:slug` (slug из `require_tenant_slug()`);
  > worker уже делает это синхронно (`company_config.py:32-43`). (3) `process_session` сигнатура уже
  > принимает `config` (`pipeline.py:855`) и `config=None` безопасно (строка 803 `config = config or {}`).
  > (4) realestate `finish` без `appointment_type` → `config=None` → текущее поведение.

- [ ] **Шаг 5: прогнать — зелёный:**
  `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/test_finish_scenario.py -v`

- [ ] **Шаг 6 (десктоп, человеком):** добавить в setup/recorder выбор `appointment_type`
  (`<select>` consultation/treatment_plan) → в `createSession` metadata; `node --check`; ручной E2E.

- [ ] **Шаг 7: commit:** `git add backend/app/company_scenarios.py backend/app/routes/sessions.py
  backend/tests/test_finish_scenario.py &&
  git commit -m "feat(sessions): sanctioned appointment_type -> scenario_id at enqueue (RE-safe)"`

---

## Финальная проверка (после всех backend-задач)

- [ ] Весь backend-сьют зелёный:
  `cd backend && /root/projects/silentqa/.venv/bin/python -m pytest tests/ -q`
- [ ] `node --check` всех правленых десктоп-`.js`.
- [ ] Human punch-list из спеки §8 (выпуск API-ключа, сборка, live E2E, регресс realestate, деплой).
