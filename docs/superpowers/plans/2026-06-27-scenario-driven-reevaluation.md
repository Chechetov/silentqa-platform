# Сценарная переоценка: пикер сценариев + per-scenario карта — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** «Переоценить» в карточке звонка перестаёт навязывать legacy-шаблон недвижимости: для не-RE тенантов — выбор **сценария** (переоценка по его критериям), и сценарии могут отличаться **структурой карты** (per-scenario `card_extraction`).

**Architecture:** Бэкенд — `ReprocessBody` получает `scenario_id`, валидируется `valid_scenario`, прокидывается в Celery как `config={"scenario_id": …}` (пайплайн уже читает `config.get("scenario_id")` — воркер не меняем для этой части). Воркер — `run_card_extraction` получает `scenario` и берёт `scenario.card_extraction` с фолбэком на config-level; при пустой карте чистит устаревшую `card.json`. Фронт — кнопка ветвится по `moduleOn('complexes')`: RE → старый template-пикер, иначе → пикер сценариев (переиспользует `pickTemplateModal`).

**Tech Stack:** FastAPI/SQLAlchemy backend; Celery worker (`worker/tasks/pipeline.py`, `card.py`); vanilla JS SPA; pytest.

**Spec:** `docs/superpowers/specs/2026-06-26-scenario-driven-reevaluation-design.md`.

## Global Constraints

- Backend-тесты: `cd backend && python -m pytest …`. Worker-тесты: `cd worker && python -m pytest …` (pytest.ini ставит `pythonpath=.`).
- Тесты без Postgres/Redis: `FakeRedis` из `backend/tests/conftest.py`; `get_db` override локально в тест-файле; куки — паттерн `test_team_users.py:_cookie`.
- `valid_scenario(config_id, scenario_id)` (`backend/app/company_scenarios.py:100`) и `require_tenant_slug` уже импортированы в `sessions.py` (используются в `finish_session`).
- Пайплайн уже резолвит сценарий из `config.get("scenario_id")` (`worker/tasks/pipeline.py:835-838`) — **Часть 1 не требует деплоя воркера**. Часть 2 (карта) — правка воркера, требует ре-деплоя воркера; обратносовместима (фолбэк).
- `GET /api/sessions/{id}/card` НЕ меняем (per-scenario label отложен — см. спек «Вне scope»).
- Русскоязычный UI/комментарии.

---

## Task 1: backend — `reprocess` принимает `scenario_id`

**Files:**
- Modify: `backend/app/routes/sessions.py` (`ReprocessBody` @315-319; валидация+`config` в `reprocess_session` @340-365)
- Test: `backend/tests/test_reprocess_scenario.py` (создать)

**Interfaces:**
- Produces: `POST /api/sessions/{id}/reprocess` body `{template_id?: uuid|null, scenario_id?: str|null}`. Валидный `scenario_id` → Celery `config={"scenario_id": …}`; неизвестный/`company_config_id IS NULL` → 400; нет `scenario_id` → `config=None`.
- Consumes: `valid_scenario`, `require_tenant_slug`, `celery_app`, `text` (все уже в `sessions.py`).

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/tests/test_reprocess_scenario.py`:

```python
import asyncio
import uuid
import datetime as _dt

import pytest
from starlette.testclient import TestClient

from app.auth_sessions import SESSION_COOKIE
from app.models import Session, SessionStatus

ROWS = [{"slug": "acme", "schema_name": "t_acme", "status": "active",
         "custom_domains": [], "api_key_hash": None, "api_key_required": True, "modules": {}}]
SID = uuid.UUID("00000000-0000-0000-0000-000000000020")


def _cookie(fake_redis, role="admin", email="a@x.io"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u1", email, role)
        finally:
            reset_tenant_schema(token)
    return {SESSION_COOKIE: asyncio.run(seed())}


class _Res:
    def __init__(self, scalar_one=None, first=None):
        self._s = scalar_one
        self._f = first
    def scalar_one_or_none(self): return self._s
    def first(self): return self._f


class _FakeDB:
    """1-й execute → select(Session); 2-й → SELECT company_config_id."""
    def __init__(self, session, cfg_id="chechetov"):
        self.session = session
        self.cfg_id = cfg_id
        self.n = 0
    async def execute(self, *a, **k):
        self.n += 1
        if self.n == 1:
            return _Res(scalar_one=self.session)
        return _Res(first=(self.cfg_id,))
    async def commit(self): pass
    async def refresh(self, obj): pass
    async def scalar(self, *a, **k): return 0


@pytest.fixture(autouse=True)
def _clear():
    yield
    from app.main import app
    app.dependency_overrides.clear()


def _client(monkeypatch, fake_redis, session, cfg_id="chechetov"):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self): return ROWS
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    from app.database import get_db
    fake = _FakeDB(session, cfg_id)

    async def _ovr():
        yield fake
    app.dependency_overrides[get_db] = _ovr
    return TestClient(app, base_url="https://acme.silentqa.com")


def _session():
    return Session(id=SID, status=SessionStatus.completed,
                   created_at=_dt.datetime(2026, 6, 27, tzinfo=_dt.timezone.utc),
                   metadata_={})


def _patch_valid(monkeypatch):
    from app.routes import sessions as m
    monkeypatch.setattr(m, "valid_scenario", lambda cfg, sid: sid if sid == "general" else None)


def _capture_send(monkeypatch):
    from app.routes import sessions as m
    box = {}

    def fake(name, args=None, kwargs=None, queue=None):
        box["name"] = name
        box["kwargs"] = kwargs
    monkeypatch.setattr(m.celery_app, "send_task", fake)
    return box


def test_valid_scenario_passes_config(monkeypatch, fake_redis):
    _patch_valid(monkeypatch)
    box = _capture_send(monkeypatch)
    c = _client(monkeypatch, fake_redis, _session())
    r = c.post(f"/api/sessions/{SID}/reprocess", json={"scenario_id": "general"},
               cookies=_cookie(fake_redis))
    assert r.status_code == 200, r.text
    assert box["kwargs"]["config"] == {"scenario_id": "general"}


def test_unknown_scenario_400(monkeypatch, fake_redis):
    _patch_valid(monkeypatch)
    _capture_send(monkeypatch)
    c = _client(monkeypatch, fake_redis, _session())
    r = c.post(f"/api/sessions/{SID}/reprocess", json={"scenario_id": "nope"},
               cookies=_cookie(fake_redis))
    assert r.status_code == 400


def test_no_scenario_config_none(monkeypatch, fake_redis):
    box = _capture_send(monkeypatch)
    c = _client(monkeypatch, fake_redis, _session())
    r = c.post(f"/api/sessions/{SID}/reprocess", json={}, cookies=_cookie(fake_redis))
    assert r.status_code == 200, r.text
    assert box["kwargs"]["config"] is None


def test_viewer_403(monkeypatch, fake_redis):
    c = _client(monkeypatch, fake_redis, _session())
    r = c.post(f"/api/sessions/{SID}/reprocess", json={"scenario_id": "general"},
               cookies=_cookie(fake_redis, role="viewer"))
    assert r.status_code == 403
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `cd backend && python -m pytest tests/test_reprocess_scenario.py -q`
Expected: FAIL (`config` отсутствует в kwargs / нет 400 на неизвестном сценарии).

- [ ] **Step 3: Расширить `ReprocessBody`**

`backend/app/routes/sessions.py` @315-319:

```python
class ReprocessBody(BaseModel):
    # None → перепрогон без шаблона: заново квалити+сентимент по активному
    # профилю оценки сценария (см. routes/eval_profiles.py). С шаблоном —
    # как раньше: шаблон переопределяет протокол/критерии.
    template_id: uuid.UUID | None = None
    # None → дефолтный сценарий тенанта. Иначе — переоценка под выбранный
    # сценарий company-config (валидируется valid_scenario; невалид → 400).
    scenario_id: str | None = None
```

- [ ] **Step 4: Валидация сценария + `config` в `reprocess_session`**

В `reprocess_session`, между блоком `template_id` (после `meta.pop("template_id", None)`, строка 348) и `sess.metadata_ = meta` (строка 349) вставить:

```python
    config: dict | None = None
    if body.scenario_id is not None:
        row = (await db.execute(
            text("SELECT company_config_id FROM shared.tenants WHERE slug = :slug"),
            {"slug": require_tenant_slug()},
        )).first()
        cfg_id = row[0] if row else None
        if not valid_scenario(cfg_id, body.scenario_id):
            raise HTTPException(status_code=400, detail="Unknown scenario")
        config = {"scenario_id": body.scenario_id}

```

И в `send_task` (строки 359-364) добавить `config` в kwargs (зеркало `finish_session:560`):

```python
        lambda: celery_app.send_task(
            "pipeline.process_session",
            args=[str(session_id)],
            kwargs={"tenant_schema": tenant_schema, "config": config},
            queue="transcription",
        ),
```

- [ ] **Step 5: Запустить — зелёное**

Run: `cd backend && python -m pytest tests/test_reprocess_scenario.py -q`
Expected: PASS (4 теста).

- [ ] **Step 6: Регресс**

Run: `cd backend && python -m pytest tests/ -q`
Expected: PASS (вся backend-сюита; новый `config`-kwarg не ломает существующие reprocess-тесты — он опционален и `None` по умолчанию).

- [ ] **Step 7: Commit**

```bash
git add backend/app/routes/sessions.py backend/tests/test_reprocess_scenario.py
git commit -m "feat(sessions): reprocess принимает scenario_id → переоценка под выбранный сценарий"
```

---

## Task 2: worker — per-scenario `card_extraction` + чистка устаревшей карты

**Files:**
- Modify: `worker/tasks/card.py` (`run_card_extraction` @28 — параметр `scenario`)
- Modify: `worker/tasks/pipeline.py` (вызов @776; новый `delete_results` после `save_results` @399; ветка `else` в шаге 6b @774-779)
- Test: `worker/tests/test_card_per_scenario.py` (создать)

**Interfaces:**
- Produces: `run_card_extraction(transcript, company_config, scenario=None)` — берёт `scenario["card_extraction"]` если есть, иначе `company_config["card_extraction"]`, иначе `None`. `delete_results(session_id, key)` — идемпотентное удаление `<results>/<key>.json`.
- Consumes: `tenant_results_dir`, `require_tenant_slug`, `RESULTS_PATH` (уже в `pipeline.py`); `OpenAI` (в `card.py`).

- [ ] **Step 1: Написать падающие тесты (card)**

Создать `worker/tests/test_card_per_scenario.py`:

```python
from tasks import card


class _Resp:
    def __init__(self, text):
        self.output_text = text


class _Client:
    last_kwargs = None

    def __init__(self, text):
        self._text = text
        self.responses = self

    def create(self, **kw):
        _Client.last_kwargs = kw
        return _Resp(self._text)


def _patch(monkeypatch, text):
    monkeypatch.setattr(card, "OpenAI", lambda: _Client(text))


def test_scenario_card_wins(monkeypatch):
    _patch(monkeypatch, '{"x": 1}')
    cfg = {"card_extraction": {"prompt": "CONFIG", "json_schema": {"type": "object"}}}
    scen = {"id": "s1", "card_extraction": {"prompt": "SCENARIO", "json_schema": {"type": "object"}}}
    out = card.run_card_extraction([{"speaker": "A", "text": "hi"}], cfg, scen)
    assert out == {"x": 1}
    assert _Client.last_kwargs["input"][0]["content"] == "SCENARIO"


def test_falls_back_to_config(monkeypatch):
    _patch(monkeypatch, '{"y": 2}')
    cfg = {"card_extraction": {"prompt": "CONFIG", "json_schema": {"type": "object"}}}
    out = card.run_card_extraction([{"speaker": "A", "text": "hi"}], cfg, {"id": "s1"})
    assert out == {"y": 2}
    assert _Client.last_kwargs["input"][0]["content"] == "CONFIG"


def test_scenario_none_uses_config(monkeypatch):
    _patch(monkeypatch, '{"z": 3}')
    cfg = {"card_extraction": {"prompt": "CONFIG", "json_schema": {"type": "object"}}}
    out = card.run_card_extraction([{"speaker": "A", "text": "hi"}], cfg, None)
    assert out == {"z": 3}


def test_none_when_no_card():
    out = card.run_card_extraction([{"speaker": "A", "text": "hi"}], {}, {"id": "s1"})
    assert out is None
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `cd worker && python -m pytest tests/test_card_per_scenario.py -q`
Expected: FAIL (`run_card_extraction` пока не принимает `scenario` → TypeError, либо берёт config-level вместо scenario).

- [ ] **Step 3: Добавить `scenario` в `run_card_extraction`**

`worker/tasks/card.py` @28-31:

```python
def run_card_extraction(transcript: list[dict], company_config: dict,
                        scenario: dict | None = None) -> dict | None:
    cfg = (scenario or {}).get("card_extraction") or company_config.get("card_extraction")
    if not cfg:
        return None
```

(остальное тело без изменений)

- [ ] **Step 4: Запустить — зелёное (card)**

Run: `cd worker && python -m pytest tests/test_card_per_scenario.py -q`
Expected: PASS (4 теста).

- [ ] **Step 5: Тест чистки устаревшей карты**

Добавить в `worker/tests/test_card_per_scenario.py`:

```python
def test_delete_results_removes_card(tmp_path, monkeypatch):
    from tasks import pipeline
    from tenancy.context import set_tenant_schema, reset_tenant_schema

    monkeypatch.setattr(pipeline, "RESULTS_PATH", tmp_path)
    tok = set_tenant_schema("t_acme")
    try:
        pipeline.save_results("sess1", "card", {"a": 1})
        assert list(tmp_path.rglob("card.json")), "card.json должен был создаться"
        pipeline.delete_results("sess1", "card")
        assert not list(tmp_path.rglob("card.json"))
        pipeline.delete_results("sess1", "card")   # идемпотентно
    finally:
        reset_tenant_schema(tok)
```

Run: `cd worker && python -m pytest tests/test_card_per_scenario.py::test_delete_results_removes_card -q`
Expected: FAIL (`delete_results` не существует).

- [ ] **Step 6: Реализовать `delete_results` + прокинуть `scenario` + ветка `else`**

В `worker/tasks/pipeline.py` после `save_results` (после строки 398) добавить:

```python
def delete_results(session_id: str, key: str) -> None:
    """Удалить ранее сохранённый результат (если есть). Идемпотентно."""
    results_dir = tenant_results_dir(RESULTS_PATH, require_tenant_slug(), session_id)
    (results_dir / f"{key}.json").unlink(missing_ok=True)
```

Заменить блок шага 6b (`worker/tasks/pipeline.py:774-779`):

```python
    # === 6b. Structured card (config-gated, generic; clinical only — QA above) ===
    from tasks.card import run_card_extraction
    card = run_card_extraction(transcript_with_speakers, company_config, scenario)
    if card is not None:
        save_results(session_id, "card", card)
        logger.info(f"[{session_id}] Card extraction saved")
    else:
        # Переоценка под сценарий без карты не должна отдавать старую card.json.
        delete_results(session_id, "card")
```

- [ ] **Step 7: Запустить — зелёное (весь файл)**

Run: `cd worker && python -m pytest tests/test_card_per_scenario.py -q`
Expected: PASS (5 тестов).

- [ ] **Step 8: Регресс воркера**

Run: `cd worker && python -m pytest -q`
Expected: PASS (существующие card/pipeline-тесты зелёные; `scenario=None` по умолчанию → старое поведение).

- [ ] **Step 9: Commit**

```bash
git add worker/tasks/card.py worker/tasks/pipeline.py worker/tests/test_card_per_scenario.py
git commit -m "feat(worker): per-scenario card_extraction + чистка устаревшей card.json"
```

---

## Task 3: frontend — пикер сценариев vs RE-шаблон

**Files:**
- Modify: `backend/static/app.js` (кнопка @553; новая `reprocessScenario` рядом с `reprocessSession` @2577)

**Interfaces:**
- Consumes: `moduleOn('complexes')` (`app.js:18`), `features.scenarios` (`app.js:100`), `pickTemplateModal` (`app.js:2983`, переиспользуем для `{id,name}`), `api`, `showToast`, `isAdmin`, `reprocessSession`.
- Produces: `reprocessScenario(sessionId)` — выбор сценария (модалка при ≥2) → `POST /reprocess {scenario_id}` (или `{}` при 0/1 сценарии).

- [ ] **Step 1: Ветвление кнопки по модулю**

`backend/static/app.js` заменить строку 553 на:

```javascript
        ${isAdmin() ? (moduleOn('complexes')
          ? `<button class="btn btn-secondary btn-sm" onclick="reprocessSession('${id}')">Переоценить с другим шаблоном…</button>`
          : `<button class="btn btn-secondary btn-sm" onclick="reprocessScenario('${id}')">Переоценить</button>`) : ''}
```

- [ ] **Step 2: Функция `reprocessScenario`**

Добавить в `backend/static/app.js` сразу после `reprocessSession` (после строки 2577):

```javascript
async function reprocessScenario(sessionId) {
  const scenarios = (features && features.scenarios) || [];
  let scenarioId = null;
  if (scenarios.length >= 2) {
    const pick = await pickTemplateModal(scenarios.map(s => ({ name: s.name, id: s.id })), {
      title: 'Перепрогнать под сценарий',
      hint: 'Выберите сценарий — звонок будет переоценён по его критериям.',
      confirmLabel: 'Запустить',
    });
    if (!pick) return;
    scenarioId = pick.id;
  }
  // 0 или 1 сценарий → простой перепрогон (дефолтный сценарий).
  try {
    await api(`/api/sessions/${sessionId}/reprocess`, {
      method: 'POST',
      body: JSON.stringify(scenarioId ? { scenario_id: scenarioId } : {}),
    });
    showToast('Переоценка запущена — обновите страницу через минуту');
  } catch (err) {
    showToast('Ошибка: ' + err.message, 'error');
  }
}
```

- [ ] **Step 3: Проверить синтаксис**

Run: `node --check backend/static/app.js`
Expected: без ошибок.

- [ ] **Step 4: Ручная проверка (чек-лист)**

- Тенант без `complexes` (напр. chechetov): кнопка «Переоценить»; при одном сценарии — сразу запуск (тост); при ≥2 — модалка выбора → запуск.
- Тенант с `complexes` (realestate): кнопка «Переоценить с другим шаблоном…» работает как раньше (template-пикер).
- Невалидный сценарий не достижим из UI (берётся из `features.scenarios`); ручной POST с мусором → 400.

- [ ] **Step 5: Commit**

```bash
git add backend/static/app.js
git commit -m "feat(dashboard): «Переоценить» — пикер сценариев для не-RE тенантов (RE-шаблон за complexes)"
```

---

## Деплой-порядок (не часть код-таски)

- Task 1 (бэк) + Task 3 (фронт) — самодостаточны, **не требуют деплоя воркера** (пайплайн уже читает `config.scenario_id`).
- Task 2 (воркер) — требует ре-деплоя воркера; обратносовместима (фолбэк на config-level карту).
- Прод-ребиндинг конфига `chechetov` (`dental`→`chechetov`) — отдельный ops-шаг, см. приложение спека (запускает пользователь).
- Контент-шаг (сценарии «первичная продажа»/«ведение проекта» в `companies/chechetov.json` с критериями и per-scenario картами) — после кода, нужен доменный вход пользователя.

## Self-Review (заполнено автором плана)

- **Покрытие спека:** Часть 1 (reprocess scenario_id) → Task 1; Часть 2 (per-scenario карта + чистка) → Task 2; Часть 3 (фронт-ветвление + пикер) → Task 3. `GET /card` не трогается (label отложен) — соответствует спеку. 400 на неизвестном сценарии/нет config_id → Task 1 (`test_unknown_scenario_400`). Single-scenario UX (без модалки) → Task 3 (`scenarios.length >= 2`).
- **Плейсхолдеры:** нет — весь код дословно.
- **Согласованность имён/типов:** `scenario_id` (str|None) сквозь body→config→pipeline; `run_card_extraction(transcript, company_config, scenario=None)` и вызов с `scenario`; `delete_results(session_id, key)` определён и вызван; `reprocessScenario`/`reprocessSession` различены.
- **Изоляция:** Task 1/2/3 независимо тестируемы и коммитятся отдельно; Task 1 и 3 связаны контрактом `{scenario_id}` (зафиксирован в обоих).
