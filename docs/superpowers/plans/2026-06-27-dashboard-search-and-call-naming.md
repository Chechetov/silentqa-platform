# Dashboard: поиск по диалогу + нейминг звонков — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в дашборд (а) клиентский поиск по транскрипту звонка и (б) переименование звонка (custom title) с показом в списке.

**Architecture:** #4 — новый `PATCH /api/sessions/{id}/title` (admin-only) пишет `metadata.title`; `title` добавляется в `_SERVER_OWNED_METADATA`, чтобы PATCH был единственным путём записи; фронт рендерит редактируемый заголовок в карточке и колонку в списке. #3 — чистый клиент: highlight-only поиск над `#transcriptContainer`, XSS-safe пересборка через `escapeHtml`+`<mark>`, не ломает аудио-синк.

**Tech Stack:** FastAPI + SQLAlchemy (async) backend; vanilla JS SPA (`backend/static/`, без сборки); pytest + Starlette TestClient + FakeRedis.

**Spec:** `docs/superpowers/specs/2026-06-26-dashboard-search-and-call-naming-design.md`.

## Global Constraints

- Рабочая директория для backend-тестов: `cd backend && python -m pytest …` (импорты завязаны на cwd).
- Тесты не требуют Postgres/Redis: `FakeRedis` из `backend/tests/conftest.py` (фикстура `fake_redis`); `get_db` каждый тест-файл переопределяет сам через локальный `_FakeDB` + `app.dependency_overrides[get_db]` (паттерн — `backend/tests/test_desktop_template_gate.py`, куки — `backend/tests/test_team_users.py:_cookie`).
- Frontend без сборки: проверка JS = `node --check backend/static/app.js`.
- Русскоязычный UI/комментарии — match существующий код.
- `escapeHtml` (`app.js:265-268`) — обязателен на любом пользовательском тексте в DOM.
- RBAC: `require_admin` (`backend/app/auth_user.py`) — допускает только роль `admin`; `viewer`/`manager` → 403.

---

## Task 1: #4 backend — `PATCH /sessions/{id}/title` + server-owned strip

**Files:**
- Modify: `backend/app/routes/sessions.py` (добавить `"title"` в `_SERVER_OWNED_METADATA` @127-132; новый `TitleUpdate` + эндпоинт рядом со `speaker-map` @571)
- Test: `backend/tests/test_session_title.py` (создать)

**Interfaces:**
- Produces: `PATCH /api/sessions/{session_id}/title`, body `{"title": str | null}` → `SessionResponse` (200); 400/401/403/404. Пишет/чистит `session.metadata_["title"]` (strip, лимит 200).
- Consumes: существующие `require_admin`, `_to_response`, `build_session_metadata`, модели `Session`/`Chunk`, `select`/`func`/`text` (уже импортированы в `sessions.py`).

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/tests/test_session_title.py`:

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
SID = uuid.UUID("00000000-0000-0000-0000-000000000010")


def _cookie(fake_redis, role="admin", employee=None, email="a@x.io"):
    from tenancy.context import reset_tenant_schema, set_tenant_schema
    from app import auth_sessions

    async def seed():
        token = set_tenant_schema("t_acme")
        try:
            return await auth_sessions.create_session("u1", email, role, employee_name=employee)
        finally:
            reset_tenant_schema(token)
    return {SESSION_COOKIE: asyncio.run(seed())}


class _Result:
    def __init__(self, val): self._val = val
    def scalar_one_or_none(self): return self._val


class _FakeDB:
    def __init__(self, session): self.session = session
    async def execute(self, *a, **k): return _Result(self.session)
    async def commit(self): pass
    async def refresh(self, obj): pass
    async def scalar(self, *a, **k): return 0


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app
    app.dependency_overrides.clear()


def _client(monkeypatch, fake_redis, session):
    from app.tenancy_http import TenantRegistry

    async def fake_all(self): return ROWS
    monkeypatch.setattr(TenantRegistry, "all_tenants", fake_all)
    from app.main import app
    from app.database import get_db
    fake = _FakeDB(session)

    async def _ovr():
        yield fake
    app.dependency_overrides[get_db] = _ovr
    return TestClient(app, base_url="https://acme.silentqa.com"), fake


def _session(meta=None):
    return Session(id=SID, status=SessionStatus.completed,
                   created_at=_dt.datetime(2026, 6, 27, tzinfo=_dt.timezone.utc),
                   metadata_=meta)


def test_admin_rename_ok(monkeypatch, fake_redis):
    s = _session({"phone": "+700"})
    c, _ = _client(monkeypatch, fake_redis, s)
    r = c.patch(f"/api/sessions/{SID}/title", json={"title": "  Демо-созвон  "},
                cookies=_cookie(fake_redis))
    assert r.status_code == 200, r.text
    assert r.json()["metadata"]["title"] == "Демо-созвон"
    assert s.metadata_["phone"] == "+700"


@pytest.mark.parametrize("body", [{"title": None}, {"title": ""}, {"title": "   "}])
def test_clear_title(monkeypatch, fake_redis, body):
    s = _session({"title": "old", "phone": "+700"})
    c, _ = _client(monkeypatch, fake_redis, s)
    r = c.patch(f"/api/sessions/{SID}/title", json=body, cookies=_cookie(fake_redis))
    assert r.status_code == 200, r.text
    assert "title" not in r.json()["metadata"]
    assert s.metadata_["phone"] == "+700"


def test_truncate_200(monkeypatch, fake_redis):
    c, _ = _client(monkeypatch, fake_redis, _session())
    r = c.patch(f"/api/sessions/{SID}/title", json={"title": "x" * 500},
                cookies=_cookie(fake_redis))
    assert r.status_code == 200
    assert len(r.json()["metadata"]["title"]) == 200


def test_unauthorized_401(monkeypatch, fake_redis):
    c, _ = _client(monkeypatch, fake_redis, _session())
    r = c.patch(f"/api/sessions/{SID}/title", json={"title": "x"})
    assert r.status_code == 401


def test_viewer_403(monkeypatch, fake_redis):
    c, _ = _client(monkeypatch, fake_redis, _session())
    r = c.patch(f"/api/sessions/{SID}/title", json={"title": "x"},
                cookies=_cookie(fake_redis, role="viewer"))
    assert r.status_code == 403


def test_manager_403(monkeypatch, fake_redis):
    c, _ = _client(monkeypatch, fake_redis, _session())
    r = c.patch(f"/api/sessions/{SID}/title", json={"title": "x"},
                cookies=_cookie(fake_redis, role="manager", employee="Иванов"))
    assert r.status_code == 403


def test_not_found_404(monkeypatch, fake_redis):
    c, _ = _client(monkeypatch, fake_redis, None)
    r = c.patch(f"/api/sessions/{SID}/title", json={"title": "x"},
                cookies=_cookie(fake_redis))
    assert r.status_code == 404


def test_title_stripped_on_create():
    from app.routes.sessions import build_session_metadata
    meta = build_session_metadata({"title": "Hacked", "employee": "Иван"}, None)
    assert "title" not in meta
    assert meta["employee"] == "Иван"
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `cd backend && python -m pytest tests/test_session_title.py -q`
Expected: FAIL (404/422 на несуществующем эндпоинте; `test_title_stripped_on_create` — `title` ещё не в server-owned).

- [ ] **Step 3: Добавить `"title"` в `_SERVER_OWNED_METADATA`**

В `backend/app/routes/sessions.py` @127-132, дополнить кортеж:

```python
_SERVER_OWNED_METADATA = (
    "broker_id", "amocrm_user_id", "broker_name", "responsible_user_id",
    # company_id/scenario_id — выбор конфига оценки принадлежит серверу
    # (берётся из shared.tenants.company_config_id), клиент подменить не может.
    "company_id", "scenario_id",
    # title — кастомное имя звонка задаётся ТОЛЬКО через PATCH /title (admin-only),
    # не при создании сессии клиентом.
    "title",
)
```

- [ ] **Step 4: Реализовать эндпоинт**

В `backend/app/routes/sessions.py`, сразу после `update_speaker_map` (после строки 592):

```python
class TitleUpdate(BaseModel):
    title: str | None = None


_TITLE_MAX_LEN = 200


@router.patch("/{session_id}/title", response_model=SessionResponse,
              dependencies=[Depends(require_admin)])
async def update_session_title(
    session_id: uuid.UUID,
    body: TitleUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Set or clear a human-friendly custom title for a session (metadata.title)."""
    session = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    meta = dict(session.metadata_ or {})
    title = (body.title or "").strip()
    if title:
        meta["title"] = title[:_TITLE_MAX_LEN]
    else:
        meta.pop("title", None)
    session.metadata_ = meta
    await db.commit()
    await db.refresh(session)

    chunks_count = await db.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.session_id == session_id)
    )
    return _to_response(session, chunks_count or 0)
```

- [ ] **Step 5: Запустить — убедиться, что зелёное**

Run: `cd backend && python -m pytest tests/test_session_title.py -q`
Expected: PASS (9 тестов, включая 3 параметризации `test_clear_title`).

- [ ] **Step 6: Регресс — соседние тесты сессий не сломаны**

Run: `cd backend && python -m pytest tests/test_desktop_template_gate.py tests/test_manager_scope.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/routes/sessions.py backend/tests/test_session_title.py
git commit -m "feat(sessions): PATCH /sessions/{id}/title — кастомное имя звонка (admin-only, metadata.title)"
```

---

## Task 2: #4 frontend — редактируемый заголовок + колонка «Название»

**Files:**
- Modify: `backend/static/app.js` (шапка карточки @565; список @378/@390/@393; новая функция `editCallTitle`)

**Interfaces:**
- Consumes: `PATCH /api/sessions/{id}/title` (Task 1); `api()`, `escapeHtml()`, `showToast()`, `isAdmin()`, переменная `meta` (= `session.metadata_`, в scope в `renderCallDetail`, см. `app.js:581`).

- [ ] **Step 1: Заголовок в шапке карточки**

В `backend/static/app.js` заменить строку 565 `<h1>Сессия звонка</h1>` на:

```javascript
          <h1 class="call-title">
            <span id="callTitleText">${escapeHtml((meta.title || '').trim() || 'Сессия звонка')}</span>
            ${isAdmin() ? `<button type="button" class="btn-icon" id="callTitleEdit" title="Переименовать" onclick="editCallTitle('${id}')">✎</button>` : ''}
          </h1>
```

(Остаётся внутри template-literal `html += \`…\``; не выносить за конкатенацию.)

- [ ] **Step 2: Колонка «Название» в списке**

В `backend/static/app.js`:
- после `<th>Дата</th>` (строка 378) добавить `<th>Название</th>`;
- в пустом состоянии (строка 390) поднять `colspan="8"` → `colspan="9"`;
- после `<td>${formatDate(s.created_at)}</td>` (строка 393) добавить:

```javascript
                  <td class="call-name-cell">${escapeHtml((s.metadata && s.metadata.title) || '--')}</td>
```

- [ ] **Step 3: Функция `editCallTitle` + CSS**

Добавить в `backend/static/app.js` (рядом с `editSpeakerName`/другими хелперами карточки):

```javascript
function editCallTitle(sessionId) {
  const span = document.getElementById('callTitleText');
  if (!span || span.dataset.editing) return;
  const editBtn = document.getElementById('callTitleEdit');
  const current = span.textContent === 'Сессия звонка' ? '' : span.textContent;
  span.dataset.editing = '1';
  if (editBtn) editBtn.style.display = 'none';

  const input = document.createElement('input');
  input.type = 'text';
  input.maxLength = 200;
  input.value = current;
  input.className = 'call-title-input';
  span.replaceWith(input);
  input.focus();

  let done = false;
  const restore = (text) => {
    const s = document.createElement('span');
    s.id = 'callTitleText';
    s.textContent = (text || '').trim() || 'Сессия звонка';
    input.replaceWith(s);
    if (editBtn) editBtn.style.display = '';
  };
  const save = async () => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    try {
      await api(`/api/sessions/${sessionId}/title`, {
        method: 'PATCH',
        body: JSON.stringify({ title: value }),
      });
      restore(value);
    } catch (err) {
      showToast('Не удалось переименовать: ' + err.message, 'error');
      restore(current);
    }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    else if (e.key === 'Escape') { done = true; restore(current); }
  });
  input.addEventListener('blur', save);
}
```

В `backend/static/styles.css` добавить:

```css
.call-title { display: flex; align-items: center; gap: 10px; }
.call-title-input { font-size: inherit; padding: 4px 8px; max-width: 480px; }
.call-name-cell { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.btn-icon { background: transparent; border: 1px solid var(--border); color: var(--text-secondary);
            border-radius: 6px; cursor: pointer; padding: 2px 8px; line-height: 1.4; }
.btn-icon:hover { background: var(--bg-card-hover); }
```

- [ ] **Step 4: Проверить синтаксис**

Run: `node --check backend/static/app.js`
Expected: без ошибок (нет вывода).

- [ ] **Step 5: Ручная проверка (чек-лист)**

Запустить дашборд (`./run.sh backend`, тенант-контур), открыть карточку звонка:
- карандаш → инпут → ввод → Enter → заголовок обновился; перезагрузка карточки сохраняет имя;
- Escape отменяет; ошибка сети → тост + откат;
- в списке звонков появилась колонка «Название» (или `--`); поиск `#callSearch` находит по названию.

- [ ] **Step 6: Commit**

```bash
git add backend/static/app.js backend/static/styles.css
git commit -m "feat(dashboard): редактируемое имя звонка в карточке + колонка «Название» в списке"
```

---

## Task 3: #3 frontend — поиск по транскрипту (highlight-only)

**Files:**
- Modify: `backend/static/app.js` (UI-блок поиска в рендере транскрипта @945; инициализация после аудио-хендлеров @1019; функция `initTranscriptSearch`)
- Modify: `backend/static/styles.css` (стили поиска/подсветки)

**Interfaces:**
- Consumes: `escapeHtml()`, хелперы `$`/`$$` (`querySelector`/`All`, см. `app.js:1002`), DOM-структура транскрипта (`.transcript-line`/`.transcript-text`, `app.js:956-959`).
- Не трогает: клик-перемотку и `.transcript-line--active` (аудио-синк, `app.js:983-1018`).

- [ ] **Step 1: UI-блок поиска**

В `backend/static/app.js`, в рендере транскрипта вставить строку поиска ПЕРЕД `<div class="transcript-container" id="transcriptContainer">` (строка 945), только когда есть сегменты:

```javascript
          ${segments.length === 0 ? '' : `
          <div class="transcript-search-box">
            <input type="text" id="transcriptSearch" placeholder="Поиск по диалогу…" autocomplete="off">
            <span class="transcript-search-count" id="transcriptSearchCount"></span>
            <button type="button" class="btn-icon" id="transcriptSearchPrev" title="Предыдущее">↑</button>
            <button type="button" class="btn-icon" id="transcriptSearchNext" title="Следующее">↓</button>
          </div>`}
          <div class="transcript-container" id="transcriptContainer">
```

- [ ] **Step 2: Функция `initTranscriptSearch`**

Добавить в `backend/static/app.js` (рядом с рендером карточки):

```javascript
function initTranscriptSearch() {
  const container = document.getElementById('transcriptContainer');
  const input = document.getElementById('transcriptSearch');
  const countEl = document.getElementById('transcriptSearchCount');
  const prevBtn = document.getElementById('transcriptSearchPrev');
  const nextBtn = document.getElementById('transcriptSearchNext');
  if (!container || !input) return;

  const texts = $$('.transcript-text', container);
  texts.forEach((el) => { el.dataset.raw = el.textContent; });  // эагерный снимок исходника

  let matches = [];
  let cur = -1;
  let timer = null;

  const clearMarks = () => {
    texts.forEach((el) => { el.textContent = el.dataset.raw; });  // textContent, НЕ innerHTML
    matches = []; cur = -1;
  };

  const focusMatch = () => {
    matches.forEach((m) => m.classList.remove('transcript-match--current'));
    if (cur < 0 || cur >= matches.length) return;
    const m = matches[cur];
    m.classList.add('transcript-match--current');
    m.scrollIntoView({ block: 'center', behavior: 'smooth' });
  };

  const run = (q) => {
    clearMarks();
    const needle = (q || '').trim().toLowerCase();
    if (!needle) { countEl.textContent = ''; return; }
    texts.forEach((el) => {
      const raw = el.dataset.raw;
      const low = raw.toLowerCase();
      let i = low.indexOf(needle);
      if (i === -1) return;
      let out = '';
      let pos = 0;
      while (i !== -1) {
        out += escapeHtml(raw.slice(pos, i));
        out += '<mark class="transcript-match">' + escapeHtml(raw.slice(i, i + needle.length)) + '</mark>';
        pos = i + needle.length;
        i = low.indexOf(needle, pos);
      }
      out += escapeHtml(raw.slice(pos));
      el.innerHTML = out;
    });
    matches = $$('.transcript-match', container);
    countEl.textContent = matches.length ? `${matches.length} совпадений` : 'нет совпадений';
    if (matches.length) { cur = 0; focusMatch(); }
  };

  const step = (delta) => {
    if (!matches.length) return;
    cur = (cur + delta + matches.length) % matches.length;
    focusMatch();
  };

  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => run(input.value), 200);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); step(e.shiftKey ? -1 : 1); }
  });
  if (prevBtn) prevBtn.addEventListener('click', () => step(-1));
  if (nextBtn) nextBtn.addEventListener('click', () => step(1));
}
```

- [ ] **Step 3: Вызвать инициализацию**

В `backend/static/app.js`, после блока аудио-хендлеров (после строки 1019, закрывающей `if (transcriptContainer && audioPlayer) { … }`), добавить:

```javascript
    initTranscriptSearch();
```

- [ ] **Step 4: CSS подсветки**

В `backend/static/styles.css` добавить (рядом с транскрипт-стилями ~652):

```css
.transcript-search-box { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.transcript-search-box input { flex: 1; }
.transcript-search-count { color: var(--text-secondary); font-size: 13px; white-space: nowrap; }
mark.transcript-match { background: rgba(210,153,34,0.30); color: inherit; border-radius: 2px; }
mark.transcript-match--current { background: rgba(210,153,34,0.65); box-shadow: 0 0 0 2px rgba(210,153,34,0.5); }
```

(`.btn-icon` уже добавлен в Task 2; если Task 3 идёт раньше — добавить его сюда.)

- [ ] **Step 5: Проверить синтаксис**

Run: `node --check backend/static/app.js`
Expected: без ошибок.

- [ ] **Step 6: Ручная проверка (чек-лист)**

Открыть карточку звонка с транскриптом:
- ввод запроса → подсветка всех совпадений, счётчик «N совпадений», ↑/↓ и Enter/Shift+Enter прыгают по совпадениям со скроллом, текущее — ярче;
- пустой запрос → подсветка снята, счётчик пуст;
- спецсимволы (`(`, `.`) ищутся буквально; `ё`/`е` различаются;
- клик по подсвеченной строке → перемотка аудио работает; проигрывание подсвечивает активную строку (синяя рамка) поверх жёлтых меток — без конфликта;
- очистка возвращает исходный текст (нет вложенных `<mark>`).

- [ ] **Step 7: Commit**

```bash
git add backend/static/app.js backend/static/styles.css
git commit -m "feat(dashboard): поиск по диалогу в транскрипте (highlight-only, XSS-safe)"
```

---

## Self-Review (заполнено автором плана)

- **Покрытие спека:** #3 поиск → Task 3; #4 нейминг (бэк+strip) → Task 1, (фронт карточка+список) → Task 2. RBAC admin-only + server-owned strip → Task 1 (тесты viewer/manager/strip). XSS-safe подсветка/восстановление → Task 3 (escapeHtml + textContent-restore). Все требования спека покрыты.
- **Плейсхолдеры:** нет — весь код приведён дословно.
- **Согласованность имён:** `editCallTitle`, `initTranscriptSearch`, `_TITLE_MAX_LEN`, `TitleUpdate`, классы `.transcript-match`/`--current`, `callTitleText`/`callTitleEdit` — единообразны между задачами.
- **Порядок:** Task 1 (бэк) → Task 2/Task 3 (фронт, независимы между собой). `.btn-icon` определяется в первой из фронт-задач, что попадёт в работу.
