/* ============================================
   Calls Analytics Dashboard — SPA Application
   ============================================ */

const API = '';  // same origin
const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];
const app = $('#app');

// ---- State ----
let currentRoute = '';
let sessionsCache = null;
let _currentCallData = null; // {session, transcript, analysis} for export
let _currentCompanyData = null; // company config for editing
let currentUser = null; // {email, role} после логина
let features = {}; // module-флаги из /api/tenancy/features
const isAdmin = () => currentUser && currentUser.role === 'admin';
const moduleOn = (name) => features[name] !== false; // default-on, пока явно не false
const amoBase = () => features.amocrm_subdomain || 'rogovestate.amocrm.ru'; // из company-config через /features (фолбэк — дефолт realestate)

// ---- Router ----
function navigate(hash) {
  window.location.hash = hash;
}

function getRoute() {
  const hash = window.location.hash.slice(1) || 'calls';
  return hash;
}

function updateNav(route) {
  const base = route.split('/')[0];
  $$('.nav-link').forEach(link => {
    link.classList.toggle('active', link.dataset.route === base);
  });
}

async function router() {
  const route = getRoute();
  if (route === currentRoute) return;
  currentRoute = route;
  updateNav(route);

  if (route === 'calls') {
    await renderCalls();
  } else if (route.startsWith('call/')) {
    const id = route.slice(5);
    await renderCallDetail(id);
  } else if (route === 'managers') {
    renderManagers();
  } else if (route === 'upload') {
    await renderUpload();
  } else if (route === 'companies') {
    await renderCompanies();
  } else if (route.startsWith('company/')) {
    const id = route.slice(8);
    await renderCompanyDetail(id);
  } else if (route === 'reprocess') {
    if (!moduleOn('amocrm')) { navigate('#calls'); return; }
    await renderReprocess();
  } else if (route === 'templates') {
    await renderTemplates();
  } else if (route === 'template/new') {
    if (!isAdmin()) { navigate('#templates'); return; }
    await renderTemplateEdit(null);
  } else if (route.startsWith('template/')) {
    if (!isAdmin()) { navigate('#templates'); return; }
    await renderTemplateEdit(route.split('/')[1]);
  } else if (route === 'knowledge') {
    await renderKnowledge();
  } else if (route === 'complexes') {
    if (!moduleOn('complexes')) { navigate('#calls'); return; }
    await renderComplexes();
  } else if (route.startsWith('complex/')) {
    if (!moduleOn('complexes')) { navigate('#calls'); return; }
    await renderComplexDetail(route.split('/')[1]);
  } else if (route === 'team') {
    if (!isAdmin()) { navigate('#calls'); return; }
    await renderTeam();
  } else if (route === 'evaluation') {
    if (!isAdmin()) { navigate('#calls'); return; }
    await renderEvaluation();
  } else if (route === 'profile') {
    await renderProfile();
  } else {
    app.innerHTML = '<div class="empty-state"><p>Страница не найдена</p></div>';
  }
}

window.addEventListener('hashchange', router);
window.addEventListener('load', () => {
  checkHealth();
  bootAuth().then((ok) => { if (ok) { router(); } else { showLogin(); } });
});

// ---- Auth (user sessions) ----
async function bootAuth() {
  try {
    currentUser = await api('/api/user-auth/me');
    try { features = await api('/api/tenancy/features'); } catch (e) { features = {}; }
    if (features && features.display_name) {
      document.title = features.display_name;
      const logoText = document.querySelector('.logo-text');
      if (logoText) logoText.textContent = features.display_name;
    }
    document.querySelectorAll('[data-module]').forEach(el => {
      const li = el.closest('li') || el;
      if (features[el.dataset.module] === false) li.style.display = 'none';
    });
    document.querySelectorAll('[data-admin-only]').forEach(el => {
      el.style.display = isAdmin() ? '' : 'none';
    });
    const banner = document.getElementById('support-banner');
    if (currentUser.impersonated_by && !banner) {
      const b = document.createElement('div');
      b.id = 'support-banner';
      b.textContent = `Режим поддержки: ${currentUser.impersonated_by}`;
      b.style.cssText = 'background:#ff8f00;color:#fff;text-align:center;padding:4px;';
      document.body.prepend(b);
    }
    return true;
  } catch (e) {
    return false;
  }
}

function showLogin() {
  currentUser = null;
  const el = document.getElementById('app') || document.body;
  el.innerHTML = `
    <div class="login-screen">
      <form id="login-form" class="login-card">
        <h2>Вход в дашборд</h2>
        <input type="email" id="login-email" placeholder="Email" required autocomplete="username">
        <input type="password" id="login-password" placeholder="Пароль" required autocomplete="current-password">
        <button type="submit">Войти</button>
        <div id="login-error" class="login-error"></div>
      </form>
    </div>`;
  document.getElementById('login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const errEl = document.getElementById('login-error');
    errEl.textContent = '';
    try {
      const res = await fetch('/api/user-auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: document.getElementById('login-email').value.trim(),
          password: document.getElementById('login-password').value,
        }),
      });
      if (!res.ok) {
        errEl.textContent = res.status === 429
          ? 'Слишком много попыток — подождите 5 минут'
          : 'Неверный email или пароль';
        return;
      }
      currentUser = await res.json();
      window.location.reload();
    } catch (e) {
      errEl.textContent = 'Сервер недоступен';
    }
  });
}

async function logout() {
  try { await fetch('/api/user-auth/logout', { method: 'POST' }); } catch (e) {}
  showLogin();
}

// ---- API Helpers ----
async function api(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (res.status === 401) {
    let detail = '';
    try { detail = (await res.clone().json()).detail || ''; } catch (e) {}
    // Редирект на логин ТОЛЬКО для cookie-гейченных ответов (спека 5.2):
    // ошибки API-ключа/брокерского JWT сюда не относятся.
    if (detail === 'auth_required') {
      showLogin();
      throw new Error('auth required');
    }
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

async function checkHealth() {
  const el = $('#healthIndicator');
  try {
    await api('/health');
    el.className = 'health-indicator online';
  } catch {
    el.className = 'health-indicator offline';
  }
}

function showLoading() {
  app.innerHTML = '<div class="loading"><div class="spinner"></div>Загрузка...</div>';
}

function showToast(message, type = 'success') {
  let toast = $('.toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.className = `toast ${type}`;
  requestAnimationFrame(() => toast.classList.add('show'));
  setTimeout(() => toast.classList.remove('show'), 3000);
}

function formatDate(iso) {
  if (!iso) return '--';
  const d = new Date(iso);
  return d.toLocaleDateString('ru-RU', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit'
  });
}

function formatDuration(seconds) {
  if (!seconds) return '--';
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function formatSeconds(seconds) {
  if (seconds == null) return '';
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function normalizeScore(score, analysis) {
  if (analysis && analysis.score_version >= 2) return score; // already 0-10
  if (score > 10) return Math.round(score / 10); // legacy 0-100 → 0-10
  return score;
}

function scoreColor(score) {
  if (score >= 7) return '';
  if (score >= 4) return 'score-mid';
  return 'score-low';
}

function barColor(score, max = 10) {
  const pct = score / max;
  if (pct >= 0.7) return 'var(--accent)';
  if (pct >= 0.4) return 'var(--warning)';
  return 'var(--danger)';
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

const SOURCE_LABELS = {
  'amocrm': { label: 'AmoCRM', cls: 'amocrm' },
  'desktop-app': { label: 'Десктоп', cls: 'desktop' },
  'chrome-extension': { label: 'Chrome', cls: 'chrome' },
  'yandex-extension': { label: 'Яндекс', cls: 'yandex' },
  'web-recorder': { label: 'Браузер', cls: 'web' },
  'file-upload': { label: 'Загрузка', cls: 'upload' },
};

function formatSource(metadata) {
  const src = metadata && metadata.source;
  const meta = SOURCE_LABELS[src] || { label: src || '—', cls: 'unknown' };
  return `<span class="badge badge-source-${meta.cls}">${escapeHtml(meta.label)}</span>`;
}

// ============================================
// PAGE: Calls List
// ============================================
let callsFilters = { source: '', phone: '', template_id: '', kb_tag: '', kb_tag_label: '' };
let callsPhoneDebounce = null;
let _templatesCache = null;

async function _loadTemplatesOnce() {
  if (_templatesCache) return _templatesCache;
  try { _templatesCache = await api('/api/templates'); }
  catch { _templatesCache = []; }
  return _templatesCache;
}

function formatTemplate(metadata) {
  const name = metadata && metadata.template_name;
  if (!name) return '<span class="muted">—</span>';
  return `<span class="badge badge-source-desktop">${escapeHtml(name)}</span>`;
}

async function renderCalls(page = 0) {
  showLoading();
  const limit = 50;
  const offset = page * limit;
  const templates = await _loadTemplatesOnce();
  const params = new URLSearchParams({ limit, offset });
  if (callsFilters.source) params.set('source', callsFilters.source);
  if (callsFilters.phone) params.set('phone', callsFilters.phone);
  if (callsFilters.template_id) params.set('template_id', callsFilters.template_id);
  if (callsFilters.kb_tag) params.set('kb_tag', callsFilters.kb_tag);

  try {
    const data = await api(`/api/sessions?${params.toString()}`);
    const items = data.items || [];
    const total = data.total || 0;
    const totalPages = Math.ceil(total / limit);

    const sourceOptions = Object.entries(SOURCE_LABELS)
      .map(([val, m]) => `<option value="${val}" ${callsFilters.source === val ? 'selected' : ''}>${escapeHtml(m.label)}</option>`)
      .join('');
    const tplOptions = templates.map(t =>
      `<option value="${t.id}" ${callsFilters.template_id === t.id ? 'selected' : ''}>${escapeHtml(t.name)}</option>`
    ).join('');
    const filtersActive = !!(callsFilters.source || callsFilters.phone || callsFilters.template_id || callsFilters.kb_tag);

    let html = `
      <div class="page-header">
        <h1>Звонки</h1>
        <p>Список всех обработанных сессий</p>
      </div>
      <div class="stats-row">
        <div class="stat-card">
          <div class="stat-label">Всего звонков</div>
          <div class="stat-value">${total}</div>
          ${filtersActive ? '<div class="stat-sub">с учётом фильтров</div>' : ''}
        </div>
        <div class="stat-card">
          <div class="stat-label">Завершено</div>
          <div class="stat-value">${items.filter(s => s.status === 'completed').length}</div>
          <div class="stat-sub">на текущей странице</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">В обработке</div>
          <div class="stat-value">${items.filter(s => s.status === 'processing').length}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Ошибки</div>
          <div class="stat-value">${items.filter(s => s.status === 'failed').length}</div>
        </div>
      </div>
      <div class="table-container">
        <div class="table-header">
          <h2>Сессии</h2>
          <div class="table-filters">
            <select id="callsSourceFilter" class="table-filter">
              <option value="">Все источники</option>
              ${sourceOptions}
            </select>
            <select id="callsTemplateFilter" class="table-filter">
              <option value="">Все типы</option>
              <option value="none" ${callsFilters.template_id === 'none' ? 'selected' : ''}>Звонки (без шаблона)</option>
              ${tplOptions}
            </select>
            <input type="search" id="callsPhoneFilter" class="table-filter" placeholder="Телефон…" value="${escapeHtml(callsFilters.phone)}">
            ${callsFilters.kb_tag ? `<span class="kb-tag-filter-chip table-filter" title="Активен фильтр по тегу базы знаний">Тег: ${escapeHtml(callsFilters.kb_tag_label || callsFilters.kb_tag)} <button type="button" id="callsKbTagClear" title="Снять фильтр по тегу" style="margin-left:4px;cursor:pointer">×</button></span>` : ''}
            ${filtersActive ? '<button type="button" id="callsFiltersReset" class="table-filter-reset">Сбросить</button>' : ''}
            <input type="text" class="table-search" placeholder="Поиск на странице…" id="callSearch">
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th>Дата</th>
              <th>Название</th>
              <th>Источник</th>
              <th>Шаблон</th>
              <th>Телефон</th>
              <th>Менеджер</th>
              <th>Длительность</th>
              <th>Статус</th>
              <th>Чанки</th>
            </tr>
          </thead>
          <tbody id="callsBody">
            ${items.length === 0
              ? '<tr><td colspan="9" style="text-align:center;color:var(--text-muted);padding:40px;">Нет данных</td></tr>'
              : items.map(s => `
                <tr data-id="${s.id}" onclick="navigate('call/${s.id}')">
                  <td>${formatDate(s.created_at)}</td>
                  <td class="call-name-cell">${escapeHtml((s.metadata && s.metadata.title) || '--')}</td>
                  <td>${formatSource(s.metadata)}</td>
                  <td>${formatTemplate(s.metadata)}</td>
                  <td>${escapeHtml((s.metadata && s.metadata.phone) || '--')}</td>
                  <td>${escapeHtml((s.metadata && s.metadata.employee) || '--')}</td>
                  <td>${formatDuration(s.duration_seconds)}</td>
                  <td><span class="badge badge-${s.status}">${s.status}</span></td>
                  <td>${s.chunks_count}</td>
                </tr>
              `).join('')}
          </tbody>
        </table>
        ${totalPages > 1 ? `
        <div class="pagination">
          <button onclick="renderCalls(${page - 1})" ${page === 0 ? 'disabled' : ''}>Назад</button>
          <span class="page-info">${page + 1} / ${totalPages}</span>
          <button onclick="renderCalls(${page + 1})" ${page >= totalPages - 1 ? 'disabled' : ''}>Вперёд</button>
        </div>` : ''}
      </div>
    `;
    app.innerHTML = html;

    // Backend filters: source dropdown + template dropdown + phone input
    const sourceSel = $('#callsSourceFilter');
    if (sourceSel) {
      sourceSel.addEventListener('change', () => {
        callsFilters.source = sourceSel.value;
        renderCalls(0);
      });
    }
    const tplSel = $('#callsTemplateFilter');
    if (tplSel) {
      tplSel.addEventListener('change', () => {
        callsFilters.template_id = tplSel.value;
        renderCalls(0);
      });
    }
    const phoneInput = $('#callsPhoneFilter');
    if (phoneInput) {
      phoneInput.addEventListener('input', () => {
        clearTimeout(callsPhoneDebounce);
        callsPhoneDebounce = setTimeout(() => {
          callsFilters.phone = phoneInput.value.trim();
          renderCalls(0);
        }, 300);
      });
      phoneInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          clearTimeout(callsPhoneDebounce);
          callsFilters.phone = phoneInput.value.trim();
          renderCalls(0);
        }
      });
    }
    const resetBtn = $('#callsFiltersReset');
    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        callsFilters = { source: '', phone: '', template_id: '', kb_tag: '', kb_tag_label: '' };
        renderCalls(0);
      });
    }
    const kbTagClear = $('#callsKbTagClear');
    if (kbTagClear) {
      kbTagClear.addEventListener('click', () => {
        callsFilters.kb_tag = '';
        callsFilters.kb_tag_label = '';
        renderCalls(0);
      });
    }

    // Client-side search filter (visible page only)
    const searchInput = $('#callSearch');
    if (searchInput) {
      searchInput.addEventListener('input', () => {
        const q = searchInput.value.toLowerCase();
        $$('#callsBody tr').forEach(tr => {
          tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none';
        });
      });
    }
  } catch (err) {
    app.innerHTML = `<div class="empty-state"><p>Ошибка загрузки: ${escapeHtml(err.message)}</p></div>`;
  }
}

function filterByKbTag(el) {
  // Клик по KB-тегу в карточке звонка → список звонков, отфильтрованный по этому тегу.
  callsFilters = { source: '', phone: '', template_id: '',
                   kb_tag: el.dataset.entry || '', kb_tag_label: el.dataset.term || '' };
  navigate('calls');
}

// ============================================
// PAGE: Call Detail
// ============================================
async function renderCallDetail(id) {
  showLoading();

  try {
    const session = await api(`/api/sessions/${id}`);
    let transcript = null;
    let analysis = null;

    try { transcript = await api(`/api/sessions/${id}/transcript`); } catch {}
    try { analysis = await api(`/api/sessions/${id}/analysis`); } catch {}
    let sentiment = null;
    try { sentiment = await api(`/api/sessions/${id}/sentiment`); } catch {}
    let extraction = null;
    if (moduleOn('complexes')) { try { extraction = await api(`/api/sessions/${id}/extraction`); } catch {} }
    let kbTags = [];
    if (moduleOn('knowledge_base')) { try { kbTags = await api(`/api/sessions/${id}/tags`); } catch {} }
    let card = null;
    try { card = await api(`/api/sessions/${id}/card`); } catch {}
    let asrVariants = null;  // ASR-сравнение (админ-тюнинг)
    if (isAdmin()) { try { asrVariants = await api(`/api/sessions/${id}/transcript-variants`); } catch {} }

    _currentCallData = { session, transcript, analysis, sentiment };

    // Заголовок карточки — из company-config через /features (дентал «Карта приёма»
    // vs «Итоги созвона»). Фолбэк отдаём рендереру (renderGenericCard → «Итоги»).
    const _currentCardLabel = (features && features.card_label) || null;
    const meta = session.metadata || {};
    const rawScore = analysis && analysis.overall_score != null ? analysis.overall_score : null;
    const overallScore = rawScore != null ? normalizeScore(rawScore, analysis) : null;
    const detailedSummary = analysis && analysis.detailed_summary ? analysis.detailed_summary : null;
    const summary = analysis && analysis.summary ? analysis.summary : null;

    // Extract criteria: new format (array) or build from legacy structure
    let criteria = null;
    if (analysis) {
      if (analysis.criteria) {
        criteria = analysis.criteria;
      } else if (analysis.communication_quality) {
        const cq = analysis.communication_quality;
        const pa = analysis.protocol_adherence;
        criteria = [];
        if (pa && pa.score != null) criteria.push({name:'Регламент', score: pa.score, comment: ''});
        if (cq.politeness != null) criteria.push({name:'Вежливость', score: cq.politeness, comment: ''});
        if (cq.listening != null) criteria.push({name:'Слушание', score: cq.listening, comment: ''});
        if (cq.clarity != null) criteria.push({name:'Ясность', score: cq.clarity, comment: ''});
        if (cq.empathy != null) criteria.push({name:'Эмпатия', score: cq.empathy, comment: ''});
      } else if (analysis.breakdown) {
        criteria = analysis.breakdown;
      }
    }

    const keyMoments = analysis && analysis.key_moments ? analysis.key_moments : [];
    const suggestions = analysis && analysis.improvement_suggestions ? analysis.improvement_suggestions : [];
    const sentiments = sentiment || (analysis && analysis.sentiment_timeline ? analysis.sentiment_timeline : []);

    let html = `
      <a href="#calls" class="back-link">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
        Назад к списку
      </a>

      <div class="export-buttons" style="display:flex;gap:8px;margin-bottom:12px;justify-content:flex-end">
        <button class="btn btn-secondary btn-sm" onclick="exportCallData('json')">Export JSON</button>
        <button class="btn btn-secondary btn-sm" onclick="exportCallData('csv')">Export CSV</button>
        ${moduleOn('amocrm') ? `<button class="btn btn-secondary btn-sm" onclick="linkLeadModal('${id}')">${session.metadata && session.metadata.lead_id ? 'Сменить лид AmoCRM…' : 'Привязать к лиду AmoCRM…'}</button>` : ''}
        ${isAdmin() ? (moduleOn('complexes')
          ? `<button class="btn btn-secondary btn-sm" onclick="reprocessSession('${id}')">Переоценить с другим шаблоном…</button>`
          : `<button class="btn btn-secondary btn-sm" onclick="reprocessScenario('${id}')">Переоценить</button>`) : ''}
        ${isAdmin() ? `<button class="btn btn-secondary btn-sm" onclick="compareEnginesModal('${id}')">Сравнить движки ASR…</button>` : ''}
        ${isAdmin() ? `<button class="btn btn-danger btn-sm" onclick="deleteSession('${id}')">Удалить сессию</button>` : ''}
      </div>

      <div class="call-detail-header">
        ${!extraction && overallScore != null ? `
        <div class="score-circle ${scoreColor(overallScore)}">
          <div class="score-value">${overallScore}</div>
          <div class="score-label">/ 10</div>
        </div>` : ''}
        <div class="call-detail-meta">
          <h1 class="call-title">
            <span id="callTitleText">${escapeHtml((meta.title || '').trim() || 'Сессия звонка')}</span>
            ${isAdmin() ? `<button type="button" class="btn-icon" id="callTitleEdit" title="Переименовать" onclick="editCallTitle('${id}')">✎</button>` : ''}
          </h1>
          <div class="meta-grid">
            <div class="meta-item">
              <div class="meta-label">Дата</div>
              <div class="meta-value">${formatDate(session.created_at)}</div>
            </div>
            <div class="meta-item">
              <div class="meta-label">Статус</div>
              <div class="meta-value"><span class="badge badge-${session.status}">${session.status}</span></div>
            </div>
            <div class="meta-item">
              <div class="meta-label">Длительность</div>
              <div class="meta-value">${formatDuration(session.duration_seconds)}</div>
            </div>
            <div class="meta-item">
              <div class="meta-label">Телефон</div>
              <div class="meta-value">${escapeHtml(meta.phone || '--')}</div>
            </div>
            <div class="meta-item">
              <div class="meta-label">Менеджер</div>
              <div class="meta-value">${escapeHtml(meta.employee || '--')}</div>
            </div>
            <div class="meta-item">
              <div class="meta-label">Компания</div>
              <div class="meta-value">${escapeHtml(meta.company_id || '--')}</div>
            </div>
          </div>
        </div>
      </div>

      ${extraction ? `
      <div class="extraction-section">
        <h2>Профиль ЖК — ${escapeHtml(extraction.template.name)}</h2>
        ${renderComplexProfile(extraction.raw_data)}
        <div style="margin-top:16px;display:flex;gap:8px">
          ${extraction.complex_id ? `
            <a class="btn btn-secondary btn-sm" href="#complex/${extraction.complex_id}">Открыть профиль ЖК</a>
          ` : ''}
          ${isAdmin() ? `<button class="btn btn-secondary btn-sm" onclick="relinkExtraction('${extraction.extraction_id}')">${extraction.complex_id ? 'Перепривязать к другому ЖК…' : 'Привязать к ЖК…'}</button>` : ''}
        </div>
      </div>
      ` : ''}
    `;

    if (kbTags.length) {
      html += `<div class="kb-tags-section" style="margin-bottom:16px"><h2>Теги базы знаний</h2>` +
        kbTags.map(t => `<span class="badge kb-tag-badge" style="margin-right:6px;cursor:pointer" data-entry="${escapeHtml(t.entry_id)}" data-term="${escapeHtml(t.term).replace(/"/g, '&quot;')}" onclick="filterByKbTag(this)" title="Показать звонки с этим тегом">${escapeHtml(t.term)} · ${escapeHtml(t.category)} (${t.count})</span>`).join('') +
        `</div>`;
    }

    if (!extraction) {
      html += renderCard(card, _currentCardLabel);
    // Sentiment timeline
    if (sentiments.length > 0) {
      html += `
        <div class="card">
          <h3>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
            Sentiment Timeline
          </h3>
          <div class="sentiment-bar">
            ${sentiments.map(s => {
              const cls = s.sentiment === 'positive' ? 'sentiment-positive'
                        : s.sentiment === 'negative' ? 'sentiment-negative'
                        : 'sentiment-neutral';
              return `<div class="sentiment-segment ${cls}" style="flex:1" title="${s.sentiment}${s.text ? ': ' + escapeHtml(s.text) : ''}"></div>`;
            }).join('')}
          </div>
        </div>
      `;
    }

    // Summary
    if (summary || detailedSummary) {
      html += `
        <div class="card">
          <h3>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
            Резюме
          </h3>
          ${summary ? `<p style="margin-bottom:8px">${escapeHtml(summary)}</p>` : ''}
          ${detailedSummary ? `<details style="margin-top:8px"><summary style="cursor:pointer;font-weight:600;color:var(--text-secondary)">Подробный пересказ</summary><p style="margin-top:8px;color:var(--text-secondary)">${escapeHtml(detailedSummary)}</p></details>` : ''}
        </div>
      `;
    }

    // === V3 Extended blocks ===

    // Call classification
    const callClassification = analysis && analysis.call_classification;
    if (callClassification) {
      const classColors = {
        brushoff_short: '#f85149', brushoff_with_attempt: '#d29922',
        partial: '#58a6ff', productive: '#4CAF50', meeting_scheduled: '#238636'
      };
      const classLabels = {
        brushoff_short: 'Brush-off (короткий)', brushoff_with_attempt: 'Brush-off (с попыткой)',
        partial: 'Частичный разговор', productive: 'Продуктивный', meeting_scheduled: 'Встреча назначена'
      };
      const cType = callClassification.type || 'other';
      const cColor = classColors[cType] || 'var(--text-muted)';
      const cLabel = classLabels[cType] || cType;
      html += `
        <div class="card" style="border-left:4px solid ${cColor}">
          <h3>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
            Классификация звонка
          </h3>
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
            <span class="badge" style="background:${cColor};color:#fff;padding:4px 12px;border-radius:12px;font-weight:600">${escapeHtml(cLabel)}</span>
          </div>
          <p style="color:var(--text-secondary);margin:4px 0">${escapeHtml(callClassification.description || '')}</p>
          ${callClassification.reason ? `<p style="color:var(--text-muted);font-size:13px;margin:4px 0"><em>${escapeHtml(callClassification.reason)}</em></p>` : ''}
        </div>
      `;
    }

    // Follow-through (V4 Phase 2)
    const followThrough = analysis && analysis.previous_recommendations_follow_through;
    if (followThrough && followThrough.total_recommendations > 0) {
      const icons = {yes: '✅', partial: '~', no: '❌'};
      const colors = {yes: '#4CAF50', partial: '#d29922', no: '#f85149'};
      const items = (followThrough.items || []).map(it => {
        const icon = icons[it.executed] || '—';
        const color = colors[it.executed] || 'var(--text-muted)';
        return `
          <li style="margin:6px 0">
            <span style="color:${color};font-weight:600">${icon}</span>
            ${escapeHtml(it.recommendation_text || '')}
            ${it.evidence ? `<div style="margin-left:24px;color:var(--text-secondary);font-size:13px">${escapeHtml(it.evidence)}</div>` : ''}
          </li>
        `;
      }).join('');
      html += `
        <div class="card">
          <h3>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
            Выполнение рекомендаций прошлого звонка
          </h3>
          <p style="margin-bottom:8px"><strong>${followThrough.executed_count}</strong> из <strong>${followThrough.total_recommendations}</strong></p>
          <ul style="list-style:none;padding:0;margin:0">${items}</ul>
        </div>
      `;
    }

    // Brief summary & client info summary (for V3)
    const briefSummary = analysis && analysis.brief_summary;
    const clientInfoSummary = analysis && analysis.client_info_summary;
    if (briefSummary || clientInfoSummary) {
      html += `
        <div class="card">
          <h3>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>
            Информация от клиента
          </h3>
          ${briefSummary ? `<p style="margin-bottom:8px;font-weight:500">${escapeHtml(briefSummary)}</p>` : ''}
          ${clientInfoSummary ? `<p style="color:var(--text-secondary)">${escapeHtml(clientInfoSummary)}</p>` : ''}
        </div>
      `;
    }

    // Client info details
    const clientInfo = analysis && analysis.client_info;
    if (clientInfo) {
      const infoFields = [
        {key: 'purchase_goal', label: 'Цель покупки'},
        {key: 'locations', label: 'Локации'},
        {key: 'apartment_format', label: 'Формат квартиры'},
        {key: 'timeline', label: 'Сроки'},
        {key: 'budget', label: 'Бюджет'},
        {key: 'payment_form', label: 'Форма оплаты'},
        {key: 'important_factors', label: 'Важные факторы'},
        {key: 'what_viewed', label: 'Что смотрели'},
        {key: 'current_situation', label: 'Текущая ситуация'},
        {key: 'objections_voiced', label: 'Возражения'},
        {key: 'other', label: 'Прочее'},
      ];
      const filledFields = infoFields.filter(f => clientInfo[f.key]);
      if (filledFields.length > 0) {
        html += `
          <div class="card">
            <h3>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
              Детали от клиента
            </h3>
            <div style="display:grid;grid-template-columns:140px 1fr;gap:6px 12px">
              ${filledFields.map(f => `
                <div style="color:var(--text-muted);font-size:13px">${escapeHtml(f.label)}:</div>
                <div style="font-size:13px">${escapeHtml(clientInfo[f.key])}</div>
              `).join('')}
            </div>
          </div>
        `;
      }
    }

    // Protocol checklist (V3)
    const protocolChecklist = analysis && analysis.protocol_checklist;
    if (protocolChecklist && protocolChecklist.length > 0) {
      const statusIcons = {completed: '✓', attempted: '~', not_applicable: '—', not_reached: '✗'};
      const statusColors = {completed: '#4CAF50', attempted: '#d29922', not_applicable: 'var(--text-muted)', not_reached: '#f85149'};
      const statusLabels = {completed: 'Выполнено', attempted: 'Попытка', not_applicable: 'Не применимо', not_reached: 'Не дошли'};

      // Calculate progress
      let totalItems = 0, completedItems = 0, attemptedItems = 0;
      protocolChecklist.forEach(group => {
        group.items.forEach(item => {
          if (item.status !== 'not_applicable') totalItems++;
          if (item.status === 'completed') completedItems++;
          if (item.status === 'attempted') attemptedItems++;
        });
      });
      const progressPct = totalItems > 0 ? Math.round((completedItems / totalItems) * 100) : 0;

      html += `
        <div class="card">
          <h3>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>
            Чек-лист скрипта
            <span style="font-weight:400;font-size:13px;color:var(--text-muted);margin-left:8px">${completedItems}/${totalItems} (${progressPct}%)</span>
          </h3>
          <div class="criteria-bar" style="margin-bottom:16px">
            <div class="criteria-bar-fill" style="width:${progressPct}%;background:${barColor(progressPct, 100)}"></div>
          </div>
          ${protocolChecklist.map(group => {
            const groupCompleted = group.items.filter(i => i.status === 'completed').length;
            const groupTotal = group.items.filter(i => i.status !== 'not_applicable').length;
            return `
              <div style="margin-bottom:12px">
                <div style="font-weight:600;margin-bottom:6px;font-size:14px">${escapeHtml(group.name)} <span style="font-weight:400;color:var(--text-muted);font-size:12px">${groupCompleted}/${groupTotal}</span></div>
                ${group.items.map(item => `
                  <div style="display:flex;align-items:flex-start;gap:8px;padding:3px 0;font-size:13px">
                    <span style="color:${statusColors[item.status] || 'var(--text-muted)'};font-weight:700;min-width:16px;text-align:center" title="${statusLabels[item.status] || item.status}">${statusIcons[item.status] || '?'}</span>
                    <span>${escapeHtml(item.name)}</span>
                    ${item.comment ? `<span style="color:var(--text-muted);font-size:12px;margin-left:auto">${escapeHtml(item.comment)}</span>` : ''}
                  </div>
                `).join('')}
              </div>
            `;
          }).join('')}
        </div>
      `;
    }

    // Objections (V3)
    const objections = analysis && analysis.objections;
    if (objections && objections.length > 0) {
      html += `
        <div class="card">
          <h3>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
            Возражения клиента (${objections.length})
          </h3>
          ${objections.map(obj => `
            <div style="border-left:3px solid ${obj.resolved ? '#4CAF50' : '#f85149'};padding:8px 12px;margin-bottom:8px;background:var(--bg-secondary);border-radius:0 6px 6px 0">
              <div style="font-weight:500;margin-bottom:4px">"${escapeHtml(obj.text)}"</div>
              <div style="font-size:13px;color:var(--text-secondary);margin-bottom:4px">
                <span class="badge" style="font-size:11px;padding:2px 6px">${escapeHtml(obj.category)}</span>
                ${obj.resolved ? '<span style="color:#4CAF50;margin-left:8px">Снято</span>' : '<span style="color:#f85149;margin-left:8px">Не снято</span>'}
                <span style="margin-left:8px">Отработка: ${obj.handling_quality}/10</span>
              </div>
              <div style="font-size:13px;color:var(--text-muted)">${escapeHtml(obj.broker_response)}</div>
            </div>
          `).join('')}
        </div>
      `;
    }

    // General checks (V3)
    const generalChecks = analysis && analysis.general_checks;
    if (generalChecks && generalChecks.length > 0) {
      const metCount = generalChecks.filter(c => c.met).length;
      html += `
        <div class="card">
          <h3>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>
            Общие требования
            <span style="font-weight:400;font-size:13px;color:var(--text-muted);margin-left:8px">${metCount}/${generalChecks.length}</span>
          </h3>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px">
            ${generalChecks.map(check => `
              <div style="display:flex;align-items:center;gap:6px;padding:3px 0;font-size:13px">
                <span style="color:${check.met ? '#4CAF50' : '#f85149'};font-weight:700">${check.met ? '✓' : '✗'}</span>
                <span>${escapeHtml(check.name)}</span>
                ${check.count != null ? `<span style="color:var(--text-muted);font-size:11px">(${check.count})</span>` : ''}
              </div>
            `).join('')}
          </div>
        </div>
      `;
    }

    html += '<div class="card-grid">';

    // Criteria breakdown
    if (criteria) {
      const entries = Array.isArray(criteria) ? criteria : Object.entries(criteria).map(([k,v]) => ({name:k, score: typeof v === 'number' ? v : v.score, max: v.max || 10, comment: (v && v.comment) || ''}));
      html += `
        <div class="card">
          <h3>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20V10M18 20V4M6 20v-4"/></svg>
            Оценка по критериям
          </h3>
          ${entries.map(c => {
            const name = c.name || c.criterion || '';
            const rawScore = typeof c === 'number' ? c : (c.score != null ? c.score : c);
            const score = normalizeScore(rawScore, analysis);
            const max = c.max || 10;
            const pct = Math.round((score / max) * 100);
            const comment = c.comment || '';
            return `
              <div class="criteria-item">
                <div class="criteria-label">
                  <span>${escapeHtml(name)}</span>
                  <span>${score} / ${max}</span>
                </div>
                <div class="criteria-bar">
                  <div class="criteria-bar-fill" style="width:${pct}%;background:${barColor(score, max)}"></div>
                </div>
                ${comment ? `<div class="criteria-comment">${escapeHtml(comment)}</div>` : ''}
              </div>
            `;
          }).join('')}
        </div>
      `;
    }

    // Key moments
    if (keyMoments.length > 0) {
      html += `
        <div class="card">
          <h3>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
            Ключевые моменты
          </h3>
          ${keyMoments.map(m => {
            const text = typeof m === 'string' ? m : (m.text || m.description || JSON.stringify(m));
            const time = m.time != null ? formatSeconds(m.time) : (m.timestamp ? m.timestamp : null);
            return `
              <div class="moment-item">
                ${time ? `<div class="moment-time">${time}</div>` : ''}
                ${escapeHtml(text)}
              </div>
            `;
          }).join('')}
        </div>
      `;
    }

    html += '</div>'; // close card-grid

    // Suggestions
    if (suggestions.length > 0) {
      html += `
        <div class="card">
          <h3>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
            Рекомендации по улучшению
          </h3>
          ${suggestions.map(s => {
            const text = typeof s === 'string' ? s : (s.text || s.suggestion || JSON.stringify(s));
            return `<div class="suggestion-item">${escapeHtml(text)}</div>`;
          }).join('')}
        </div>
      `;
    }

    // Transcript
    if (transcript) {
      const segments = Array.isArray(transcript) ? transcript : (transcript.segments || transcript.utterances || []);
      html += `
        <div class="card">
          <h3>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>
            Транскрипт
          </h3>
          <audio id="audioPlayer" controls preload="metadata"
                 src="/api/sessions/${id}/audio"
                 style="width:100%; margin-bottom:16px; border-radius:8px;">
          </audio>
          ${segments.length === 0 ? '' : `
          <div class="transcript-search-box">
            <input type="text" id="transcriptSearch" placeholder="Поиск по диалогу…" autocomplete="off">
            <span class="transcript-search-count" id="transcriptSearchCount"></span>
            <button type="button" class="btn-icon" id="transcriptSearchPrev" title="Предыдущее">↑</button>
            <button type="button" class="btn-icon" id="transcriptSearchNext" title="Следующее">↓</button>
          </div>`}
          <div class="transcript-container" id="transcriptContainer">
            ${segments.length === 0
              ? '<p style="color:var(--text-muted)">Транскрипт пуст</p>'
              : segments.map(seg => {
                  const speaker = seg.speaker || 'SPEAKER_00';
                  const spkNum = parseInt(speaker.replace(/\D/g, '')) || 0;
                  const cls = `speaker-${spkNum % 3}`;
                  const time = seg.start != null ? formatSeconds(seg.start) : '';
                  const text = seg.text || seg.content || '';
                  const displayName = getSpeakerDisplay(speaker);
                  return `
                    <div class="transcript-line" data-start="${seg.start != null ? seg.start : ''}" data-end="${seg.end != null ? seg.end : ''}">
                      ${time ? `<span class="transcript-time">${time}</span>` : ''}
                      <span class="speaker-tag ${cls}" data-speaker="${escapeHtml(speaker)}"${isAdmin() ? ` onclick="event.stopPropagation(); editSpeakerName('${escapeHtml(speaker)}')" style="cursor:pointer" title="Нажмите чтобы переименовать"` : ''}>${escapeHtml(displayName)}</span>
                      <span class="transcript-text">${escapeHtml(text)}</span>
                    </div>
                  `;
                }).join('')}
          </div>
        </div>
      `;
    }

    html += renderAsrVariantsSection(asrVariants);

    } // end if (!extraction) — legacy analysis blocks

    app.innerHTML = html;

    // ASR-варианты: переключение вкладок движков
    document.querySelectorAll('.asr-variant-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        const eng = btn.dataset.engine;
        document.querySelectorAll('.asr-variant-tab').forEach(b => { b.style.fontWeight = b.dataset.engine === eng ? '700' : ''; });
        document.querySelectorAll('.asr-variant-panel').forEach(p => { p.style.display = p.dataset.engine === eng ? '' : 'none'; });
      });
    });

    // Audio player: click-to-seek on transcript lines
    const transcriptContainer = $('#transcriptContainer');
    const audioPlayer = $('#audioPlayer');
    if (transcriptContainer && audioPlayer) {
      transcriptContainer.addEventListener('click', (e) => {
        const line = e.target.closest('.transcript-line');
        if (!line) return;
        // Don't seek if clicking on speaker tag (that triggers rename)
        if (e.target.closest('.speaker-tag')) return;
        const start = parseFloat(line.dataset.start);
        if (!isNaN(start)) {
          audioPlayer.currentTime = start;
          audioPlayer.play();
        }
      });

      // Highlight active transcript segment during playback
      audioPlayer.addEventListener('timeupdate', () => {
        const currentTime = audioPlayer.currentTime;
        const lines = $$('.transcript-line', transcriptContainer);
        let activeLine = null;
        for (const line of lines) {
          const start = parseFloat(line.dataset.start);
          const end = parseFloat(line.dataset.end);
          line.classList.remove('transcript-line--active');
          if (!isNaN(start) && !isNaN(end) && currentTime >= start && currentTime < end) {
            activeLine = line;
          } else if (!isNaN(start) && isNaN(end) && currentTime >= start) {
            activeLine = line;
          }
        }
        if (activeLine) {
          activeLine.classList.add('transcript-line--active');
          activeLine.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }
      });
    }

    initTranscriptSearch();
  } catch (err) {
    app.innerHTML = `
      <a href="#calls" class="back-link">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
        Назад к списку
      </a>
      <div class="empty-state"><p>Ошибка загрузки: ${escapeHtml(err.message)}</p></div>
    `;
  }
}

// ============================================
// PAGE: Managers
// ============================================
async function renderManagers() {
  showLoading();
  try {
    const managers = await api('/api/managers');

    app.innerHTML = `
      <div class="page-header">
        <h1>Менеджеры</h1>
        <p>Статистика по менеджерам</p>
      </div>
      <div class="stats-row">
        <div class="stat-card">
          <div class="stat-label">Всего менеджеров</div>
          <div class="stat-value">${managers.length}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Всего звонков</div>
          <div class="stat-value">${managers.reduce((s, m) => s + m.total_calls, 0)}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Средний балл</div>
          <div class="stat-value">${managers.length ? (managers.reduce((s, m) => s + m.avg_score, 0) / managers.length).toFixed(1) : '--'}</div>
        </div>
      </div>
      <div class="table-container">
        <div class="table-header">
          <h2>Рейтинг менеджеров</h2>
        </div>
        <table>
          <thead>
            <tr>
              <th>Менеджер</th>
              <th>Кол-во звонков</th>
              <th>Средний балл</th>
              <th>Последний звонок</th>
            </tr>
          </thead>
          <tbody>
            ${managers.length === 0
              ? `<tr>
                  <td colspan="4" style="text-align:center;color:var(--text-muted);padding:60px 20px;">
                    Нет данных о менеджерах
                  </td>
                </tr>`
              : managers.map(m => `
                <tr>
                  <td><strong>${escapeHtml(m.name)}</strong></td>
                  <td>${m.total_calls}</td>
                  <td>${m.avg_score ? `<span style="color:${m.avg_score >= 7 ? 'var(--accent)' : m.avg_score >= 4 ? 'var(--warning)' : 'var(--danger)'};font-weight:600">${m.avg_score}</span>` : '--'}</td>
                  <td>${formatDate(m.last_call_date)}</td>
                </tr>
              `).join('')}
          </tbody>
        </table>
      </div>
    `;
  } catch (err) {
    app.innerHTML = `<div class="empty-state"><p>Ошибка загрузки: ${escapeHtml(err.message)}</p></div>`;
  }
}

// ============================================
// PAGE: Companies List
// ============================================
async function renderCompanies() {
  showLoading();
  let companies;
  try {
    companies = await api('/api/companies');
  } catch (err) {
    if (/not found|404/i.test(err.message)) {
      app.innerHTML = '<div class="empty-state"><p>Раздел доступен только платформенному администратору</p></div>';
    } else {
      app.innerHTML = `<div class="empty-state"><p>Ошибка загрузки: ${escapeHtml(err.message)}</p></div>`;
    }
    return;
  }
  try {
    app.innerHTML = `
      <div class="page-header">
        <h1>Компании</h1>
        <p>Управление конфигурациями компаний</p>
      </div>
      ${companies.length === 0
        ? '<div class="empty-state"><p>Нет зарегистрированных компаний</p></div>'
        : `<div class="company-list">
            ${companies.map(c => `
              <div class="company-card" onclick="navigate('company/${c.id}')">
                <h3>${escapeHtml(c.name || c.id)}</h3>
                <div class="company-meta">
                  ID: ${escapeHtml(c.id)} &middot; Word boost: ${c.word_boost_count || 0} слов
                </div>
              </div>
            `).join('')}
          </div>`}
    `;
  } catch (err) {
    app.innerHTML = `<div class="empty-state"><p>Ошибка загрузки: ${escapeHtml(err.message)}</p></div>`;
  }
}

// ============================================
// PAGE: Company Detail / Edit
// ============================================
async function renderCompanyDetail(id) {
  showLoading();
  try {
    const company = await api(`/api/companies/${id}`);
    _currentCompanyData = company;
    const wordBoost = (company.asr && company.asr.word_boost) || [];
    const protocol = (company.quality && company.quality.protocol) || '';
    const engine = (company.asr && company.asr.engine) || 'default';
    const scenarios = company.scenarios || [];

    app.innerHTML = `
      <a href="#companies" class="back-link">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
        Назад к компаниям
      </a>

      <div class="page-header">
        <h1>${escapeHtml(company.name || id)}</h1>
        <p>Конфигурация компании: ${escapeHtml(id)}</p>
      </div>

      <div class="card">
        <h3>Основные настройки</h3>
        <div class="form-group">
          <label>Название компании</label>
          <input type="text" id="companyName" value="${escapeHtml(company.name || '')}">
        </div>
      </div>

      <div class="card">
        <h3>Word Boost</h3>
        <div class="tags-container" id="wordBoostTags">
          ${wordBoost.map(w => `
            <span class="tag">
              ${escapeHtml(w)}
              <span class="tag-remove" onclick="removeWord(this, '${escapeHtml(w)}')">&times;</span>
            </span>
          `).join('')}
        </div>
        <div class="tag-input-row">
          <input type="text" id="newWordInput" placeholder="Добавить слово..." onkeydown="if(event.key==='Enter')addWord()">
          <button class="btn btn-secondary btn-sm" onclick="addWord()">Добавить</button>
        </div>
      </div>

      <div class="card">
        <h3>Протокол по умолчанию</h3>
        <p style="font-size:12px;color:var(--text-muted);margin-bottom:8px">Используется, если для сессии не выбран конкретный сценарий</p>
        <div class="form-group">
          <textarea id="companyProtocol" placeholder="Опишите протокол оценки качества...">${escapeHtml(protocol)}</textarea>
        </div>
      </div>

      <div class="card">
        <h3>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
          Сценарии оценки
          <button class="btn btn-secondary btn-sm" style="margin-left:auto" onclick="addScenario()">+ Добавить сценарий</button>
        </h3>
        <p style="font-size:12px;color:var(--text-muted);margin-bottom:12px">Разные сценарии для разных типов разговоров. Каждый сценарий имеет свой протокол, критерии и промпт.</p>
        <div id="scenariosContainer">
          ${scenarios.length === 0
            ? '<p style="color:var(--text-muted);font-size:13px">Нет сценариев. Будет использоваться протокол по умолчанию.</p>'
            : scenarios.map((s, i) => renderScenarioCard(s, i)).join('')}
        </div>
      </div>

      <div class="card">
        <h3>ASR движок</h3>
        <div class="form-group">
          <select id="companyEngine">
            <option value="default" ${engine === 'default' ? 'selected' : ''}>Default (системный)</option>
            <option value="whisper" ${engine === 'whisper' ? 'selected' : ''}>Whisper</option>
            <option value="assemblyai" ${engine === 'assemblyai' ? 'selected' : ''}>AssemblyAI</option>
          </select>
        </div>
      </div>

      <button class="btn btn-primary" onclick="saveCompany('${escapeHtml(id)}')">Сохранить</button>
    `;
  } catch (err) {
    app.innerHTML = `
      <a href="#companies" class="back-link">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
        Назад к компаниям
      </a>
      <div class="empty-state"><p>Ошибка загрузки: ${escapeHtml(err.message)}</p></div>
    `;
  }
}

// ---- Company editing helpers ----

function getWordBoostWords() {
  return [...$$('#wordBoostTags .tag')].map(tag => {
    // Get text content excluding the remove button
    const clone = tag.cloneNode(true);
    const removeBtn = clone.querySelector('.tag-remove');
    if (removeBtn) removeBtn.remove();
    return clone.textContent.trim();
  });
}

function addWord() {
  const input = $('#newWordInput');
  const word = input.value.trim();
  if (!word) return;

  const existing = getWordBoostWords();
  if (existing.includes(word)) {
    showToast('Слово уже добавлено', 'error');
    return;
  }

  const container = $('#wordBoostTags');
  const tag = document.createElement('span');
  tag.className = 'tag';
  tag.innerHTML = `${escapeHtml(word)}<span class="tag-remove" onclick="removeWord(this, '${escapeHtml(word)}')">&times;</span>`;
  container.appendChild(tag);
  input.value = '';
  input.focus();
}

function removeWord(btn, word) {
  btn.parentElement.remove();
}

async function saveCompany(id) {
  const name = $('#companyName').value.trim();
  const words = getWordBoostWords();
  const protocol = $('#companyProtocol').value;
  const engine = $('#companyEngine').value;
  const scenarios = collectScenariosFromDOM();

  try {
    const current = await api(`/api/companies/${id}`);
    current.name = name || current.name;
    if (!current.asr) current.asr = {};
    current.asr.word_boost = words;
    if (engine !== 'default') {
      current.asr.engine = engine;
    } else {
      delete current.asr.engine;
    }
    if (!current.quality) current.quality = {};
    current.quality.protocol = protocol;
    current.scenarios = scenarios;

    await api(`/api/companies/${id}`, {
      method: 'PUT',
      body: JSON.stringify(current),
    });

    showToast('Компания сохранена');
  } catch (err) {
    showToast('Ошибка: ' + err.message, 'error');
  }
}

// ============================================
// Scenarios Management
// ============================================

function renderScenarioCard(scenario, index) {
  const s = scenario;
  const criteria = s.criteria || [];
  const typeBadge = s.type === 'in_person'
    ? '<span class="badge" style="background:rgba(163,113,247,0.15);color:#a371f7;font-size:10px">Очно</span>'
    : '<span class="badge" style="background:rgba(88,166,255,0.15);color:#58a6ff;font-size:10px">Звонок</span>';

  return `
    <div class="scenario-card" data-scenario-index="${index}">
      <div class="scenario-header" onclick="toggleScenario(${index})">
        <div style="display:flex;align-items:center;gap:8px;flex:1">
          <svg class="scenario-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
          <strong>${escapeHtml(s.name || 'Без названия')}</strong>
          ${typeBadge}
          <span style="color:var(--text-muted);font-size:12px">${criteria.length} критери${criteria.length === 1 ? 'й' : criteria.length < 5 ? 'я' : 'ев'}</span>
        </div>
        <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); removeScenario(${index})" title="Удалить сценарий" style="color:var(--danger);padding:4px 8px">&times;</button>
      </div>
      <div class="scenario-body" id="scenarioBody_${index}" style="display:none">
        <div class="form-group">
          <label>ID сценария</label>
          <input type="text" class="scenario-id" value="${escapeHtml(s.id || '')}" placeholder="например: primary_call">
        </div>
        <div class="form-group">
          <label>Название</label>
          <input type="text" class="scenario-name" value="${escapeHtml(s.name || '')}">
        </div>
        <div class="form-group">
          <label>Тип</label>
          <select class="scenario-type">
            <option value="phone" ${s.type !== 'in_person' ? 'selected' : ''}>Телефонный звонок</option>
            <option value="in_person" ${s.type === 'in_person' ? 'selected' : ''}>Очная встреча / кабинет</option>
          </select>
        </div>
        <div class="form-group">
          <label>Описание</label>
          <input type="text" class="scenario-description" value="${escapeHtml(s.description || '')}" placeholder="Краткое описание сценария">
        </div>
        <div class="form-group">
          <label>Протокол (шаги разговора)</label>
          <textarea class="scenario-protocol" rows="6" placeholder="1. Приветствие&#10;2. Выяснение потребности&#10;...">${escapeHtml(s.protocol || '')}</textarea>
        </div>
        <div class="form-group">
          <label>Критерии оценки</label>
          <div class="criteria-list" id="criteriaList_${index}">
            ${criteria.map((c, ci) => renderCriterionRow(c, index, ci)).join('')}
          </div>
          <button class="btn btn-secondary btn-sm" onclick="addCriterion(${index})" style="margin-top:6px">+ Добавить критерий</button>
        </div>
        <div class="form-group">
          <label>Дополнительные инструкции для оценки (промпт)</label>
          <textarea class="scenario-prompt" rows="3" placeholder="Необязательно. Дополнительные указания для AI при оценке...">${escapeHtml(s.prompt || '')}</textarea>
        </div>
      </div>
    </div>
  `;
}

function renderCriterionRow(criterion, scenarioIndex, criterionIndex) {
  const c = criterion || {};
  return `
    <div class="criterion-row">
      <input type="text" class="criterion-id" value="${escapeHtml(c.id || '')}" placeholder="ID (лат.)" style="width:120px">
      <input type="text" class="criterion-name" value="${escapeHtml(c.name || '')}" placeholder="Название" style="flex:1">
      <input type="text" class="criterion-desc" value="${escapeHtml(c.description || '')}" placeholder="Описание критерия" style="flex:2">
      <button class="btn btn-secondary btn-sm" onclick="this.closest('.criterion-row').remove()" style="color:var(--danger);padding:4px 8px">&times;</button>
    </div>
  `;
}

function toggleScenario(index) {
  const body = $(`#scenarioBody_${index}`);
  const card = body.closest('.scenario-card');
  if (body.style.display === 'none') {
    body.style.display = 'block';
    card.classList.add('scenario-card--open');
  } else {
    body.style.display = 'none';
    card.classList.remove('scenario-card--open');
  }
}

function addScenario() {
  const container = $('#scenariosContainer');
  // Remove "no scenarios" message if present
  const emptyMsg = container.querySelector('p');
  if (emptyMsg) emptyMsg.remove();

  const index = container.querySelectorAll('.scenario-card').length;
  const newScenario = { id: '', name: 'Новый сценарий', type: 'phone', description: '', protocol: '', criteria: [], prompt: '' };
  const html = renderScenarioCard(newScenario, index);
  container.insertAdjacentHTML('beforeend', html);
  // Auto-expand the new scenario
  toggleScenario(index);
}

function removeScenario(index) {
  const card = $(`.scenario-card[data-scenario-index="${index}"]`);
  if (card && confirm('Удалить сценарий?')) {
    card.remove();
    // Re-index remaining scenarios
    $$('.scenario-card').forEach((card, i) => {
      card.dataset.scenarioIndex = i;
      const body = card.querySelector('.scenario-body');
      if (body) body.id = `scenarioBody_${i}`;
      // Update onclick handlers
      const header = card.querySelector('.scenario-header');
      if (header) header.setAttribute('onclick', `toggleScenario(${i})`);
      const removeBtn = header.querySelector('button');
      if (removeBtn) removeBtn.setAttribute('onclick', `event.stopPropagation(); removeScenario(${i})`);
    });
  }
}

function addCriterion(scenarioIndex) {
  const list = $(`#criteriaList_${scenarioIndex}`);
  if (!list) return;
  const html = renderCriterionRow({}, scenarioIndex, list.children.length);
  list.insertAdjacentHTML('beforeend', html);
}

function collectScenariosFromDOM() {
  const scenarios = [];
  $$('.scenario-card').forEach(card => {
    const body = card.querySelector('.scenario-body');
    if (!body) return;
    const criteria = [];
    body.querySelectorAll('.criterion-row').forEach(row => {
      const id = row.querySelector('.criterion-id').value.trim();
      const name = row.querySelector('.criterion-name').value.trim();
      const desc = row.querySelector('.criterion-desc').value.trim();
      if (id || name) {
        criteria.push({ id: id || name.toLowerCase().replace(/\s+/g, '_'), name, description: desc });
      }
    });
    scenarios.push({
      id: body.querySelector('.scenario-id').value.trim(),
      name: body.querySelector('.scenario-name').value.trim(),
      type: body.querySelector('.scenario-type').value,
      description: body.querySelector('.scenario-description').value.trim(),
      protocol: body.querySelector('.scenario-protocol').value,
      criteria,
      prompt: body.querySelector('.scenario-prompt').value.trim() || null,
    });
  });
  return scenarios;
}

// ============================================
// PAGE: Upload Audio File
// ============================================
async function renderUpload() {
  let templates = [];
  try { templates = await api('/api/templates'); } catch {}

  app.innerHTML = `
    <div class="page-header">
      <h1>Загрузка аудио</h1>
      <p>Загрузите готовый аудиофайл для анализа</p>
    </div>

    <div class="card">
      <h3>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        Аудиофайл
      </h3>
      <div class="upload-dropzone" id="dropzone">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" stroke-width="1.5">
          <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>
          <polyline points="17 8 12 3 7 8"/>
          <line x1="12" y1="3" x2="12" y2="15"/>
        </svg>
        <p>Перетащите аудиофайл сюда</p>
        <p style="font-size:12px;color:var(--text-muted)">или нажмите для выбора файла</p>
        <p style="font-size:11px;color:var(--text-muted);margin-top:8px">MP3, WAV, M4A, WebM, OGG, AAC — до 500 МБ</p>
        <input type="file" id="fileInput" accept="audio/*,.mp3,.wav,.m4a,.webm,.ogg,.aac,.flac,.opus" style="display:none">
      </div>
      <div id="fileInfo" style="display:none;margin-top:12px"></div>
    </div>

    <div class="card">
      <h3>Метаданные</h3>
      <div class="form-group">
        <label>Шаблон обработки</label>
        <select id="uploadTemplateId">
          <option value="">— Без шаблона (legacy-протокол) —</option>
          ${templates.map(t => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.name)} (${escapeHtml(t.kind)})</option>`).join('')}
        </select>
        ${moduleOn('complexes') ? '<p style="font-size:12px;color:var(--text-muted);margin-top:4px">Выбери «Презентация ЖК» для извлечения структуры из записи презентации</p>' : ''}
      </div>
      <div class="form-group">
        <label>Сотрудник</label>
        <input type="text" id="uploadEmployee" placeholder="Имя сотрудника">
      </div>
      <div class="form-group">
        <label>Комментарий</label>
        <input type="text" id="uploadComment" placeholder="Необязательно">
      </div>
    </div>

    <button class="btn btn-primary" id="uploadBtn" disabled onclick="startUpload()">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
      Загрузить и обработать
    </button>

    <div id="uploadProgress" style="display:none;margin-top:16px"></div>
  `;

  // Dropzone interactions
  const dropzone = $('#dropzone');
  const fileInput = $('#fileInput');
  let selectedFile = null;

  dropzone.addEventListener('click', () => fileInput.click());

  dropzone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropzone.classList.add('upload-dropzone--active');
  });

  dropzone.addEventListener('dragleave', () => {
    dropzone.classList.remove('upload-dropzone--active');
  });

  dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('upload-dropzone--active');
    if (e.dataTransfer.files.length > 0) {
      selectFile(e.dataTransfer.files[0]);
    }
  });

  fileInput.addEventListener('change', () => {
    if (fileInput.files.length > 0) {
      selectFile(fileInput.files[0]);
    }
  });

  function selectFile(file) {
    const validTypes = ['audio/', 'video/webm'];
    if (!validTypes.some(t => file.type.startsWith(t)) && !file.name.match(/\.(mp3|wav|m4a|webm|ogg|aac|flac|opus|wma)$/i)) {
      showToast('Неподдерживаемый формат файла', 'error');
      return;
    }
    selectedFile = file;
    window._uploadFile = file;
    const sizeMB = (file.size / 1024 / 1024).toFixed(1);
    dropzone.innerHTML = `
      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
      <p style="font-weight:600">${escapeHtml(file.name)}</p>
      <p style="font-size:12px;color:var(--text-muted)">${sizeMB} МБ &middot; ${file.type || 'audio'}</p>
      <p style="font-size:11px;color:var(--text-secondary);margin-top:4px">Нажмите чтобы заменить</p>
    `;
    dropzone.classList.add('upload-dropzone--selected');
    $('#uploadBtn').disabled = false;
  }
}

async function startUpload() {
  const file = window._uploadFile;
  if (!file) return;

  const employee = $('#uploadEmployee').value.trim();
  const comment = $('#uploadComment').value.trim();

  const btn = $('#uploadBtn');
  const progress = $('#uploadProgress');
  btn.disabled = true;
  btn.textContent = 'Загрузка...';

  const metadata = { source: 'file-upload', uploadedAt: new Date().toISOString() };
  if (employee) metadata.employee = employee;
  if (comment) metadata.comment = comment;

  try {
    // 1. Create session
    progress.style.display = 'block';
    progress.innerHTML = '<div class="upload-step active">Создание сессии...</div>';
    const session = await api('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({ metadata }),
    });
    const sessionId = session.id;

    // 2. Upload audio file
    progress.innerHTML += '<div class="upload-step active">Загрузка файла...</div>';
    const formData = new FormData();
    formData.append('file', file);
    const tplId = $('#uploadTemplateId') && $('#uploadTemplateId').value;
    if (tplId) formData.append('template_id', tplId);
    const uploadRes = await fetch(`/api/sessions/${sessionId}/upload-audio`, {
      method: 'POST',
      body: formData,
    });
    if (!uploadRes.ok) {
      const err = await uploadRes.json().catch(() => ({}));
      throw new Error(err.detail || `Upload failed: ${uploadRes.status}`);
    }

    // 3. Finish session (triggers pipeline)
    progress.innerHTML += '<div class="upload-step active">Запуск обработки...</div>';
    await api(`/api/sessions/${sessionId}/finish`, { method: 'POST' });

    // 4. Done
    progress.innerHTML += `
      <div class="upload-step success">
        Готово! Файл загружен и отправлен на обработку.
        <a href="#call/${sessionId}" style="color:var(--accent);margin-left:8px">Перейти к сессии</a>
      </div>
    `;
    btn.textContent = 'Загружено';
    showToast('Файл загружен и обрабатывается');

  } catch (err) {
    progress.innerHTML += `<div class="upload-step error">Ошибка: ${escapeHtml(err.message)}</div>`;
    btn.disabled = false;
    btn.textContent = 'Повторить загрузку';
    showToast('Ошибка загрузки: ' + err.message, 'error');
  }
}

// ============================================
// Export Functions
// ============================================
function downloadFile(content, filename, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

async function deleteSession(id) {
  const phrase = prompt('Это удалит запись, аудио, транскрипт и анализ — необратимо.\n\nВведите слово удалить для подтверждения:');
  if (phrase === null) return;
  if (phrase.trim().toLowerCase() !== 'удалить') {
    showToast('Удаление отменено: фраза не совпадает', 'error');
    return;
  }
  try {
    await api(`/api/sessions/${id}`, {
      method: 'DELETE',
    });
    showToast('Сессия удалена');
    navigate('calls');
  } catch (err) {
    showToast(`Не удалось удалить: ${err.message}`, 'error');
  }
}

function exportCallData(format) {
  if (!_currentCallData) {
    showToast('Нет данных для экспорта', 'error');
    return;
  }
  const { session, transcript, analysis } = _currentCallData;
  const id = session.id.slice(0, 8);
  const date = (session.created_at || '').slice(0, 10);

  if (format === 'json') {
    const data = { session, transcript, analysis };
    downloadFile(JSON.stringify(data, null, 2), `call_${id}_${date}.json`, 'application/json');
    showToast('JSON экспортирован');
  } else if (format === 'csv') {
    const segments = Array.isArray(transcript) ? transcript : (transcript && (transcript.segments || transcript.utterances)) || [];
    const speakerMap = _getSpeakerMap();
    let csv = 'time_start,time_end,speaker,role,text\n';
    for (const seg of segments) {
      const speaker = seg.speaker || 'SPEAKER_00';
      const mapped = speakerMap[speaker];
      const displayName = (mapped && mapped.name) || speaker;
      const role = (mapped && mapped.role) || '';
      const text = (seg.text || seg.content || '').replace(/"/g, '""');
      csv += `${seg.start || ''},${seg.end || ''},"${displayName}","${role}","${text}"\n`;
    }
    if (analysis) {
      csv += `\n"overall_score","${analysis.overall_score || ''}"\n`;
      csv += `"detailed_summary","${(analysis.detailed_summary || '').replace(/"/g, '""')}"\n`;
      csv += `"summary","${(analysis.summary || '').replace(/"/g, '""')}"\n`;
    }
    downloadFile(csv, `call_${id}_${date}.csv`, 'text/csv');
    showToast('CSV экспортирован');
  }
}

// ============================================
// Speaker Mapping
// ============================================
function _getSpeakerMap() {
  if (!_currentCallData) return {};
  const meta = _currentCallData.session.metadata || {};
  const analysis = _currentCallData.analysis || {};
  const manualMap = meta.speaker_map || {};
  const autoMap = analysis.speaker_roles || {};
  // Manual override > auto-detected
  const merged = { ...autoMap };
  for (const [k, v] of Object.entries(manualMap)) {
    merged[k] = { ...merged[k], ...v };
  }
  return merged;
}

function getSpeakerDisplay(speakerId) {
  const map = _getSpeakerMap();
  const info = map[speakerId];
  if (info && info.name) return info.name;
  if (info && info.role) return `${speakerId} (${info.role})`;
  return speakerId;
}

function _getUniqueSpeakers() {
  if (!_currentCallData) return [];
  const transcript = _currentCallData.transcript;
  const segments = Array.isArray(transcript) ? transcript : (transcript && (transcript.segments || transcript.utterances)) || [];
  const speakers = new Set();
  for (const seg of segments) {
    if (seg.speaker) speakers.add(seg.speaker);
  }
  return [...speakers].sort();
}

async function editSpeakerName(speakerId) {
  if (!_currentCallData) return;
  const current = _getSpeakerMap()[speakerId] || {};
  const allSpeakers = _getUniqueSpeakers().filter(s => s !== speakerId);

  // Build modal
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal-content">
      <h3 style="margin:0 0 16px">Настройки спикера: ${speakerId}</h3>
      <div style="margin-bottom:12px">
        <label style="display:block;margin-bottom:4px;color:var(--text-muted);font-size:13px">Имя</label>
        <input id="modalSpeakerName" type="text" value="${escapeHtml(current.name || '')}" placeholder="Имя спикера" style="width:100%;padding:8px 12px;background:var(--bg-primary);border:1px solid var(--border);border-radius:6px;color:var(--text-primary);font-size:14px">
      </div>
      <div style="margin-bottom:16px">
        <label style="display:block;margin-bottom:4px;color:var(--text-muted);font-size:13px">Роль</label>
        <select id="modalSpeakerRole" style="width:100%;padding:8px 12px;background:var(--bg-primary);border:1px solid var(--border);border-radius:6px;color:var(--text-primary);font-size:14px">
          <option value="">— не указана —</option>
          <option value="manager" ${current.role === 'manager' ? 'selected' : ''}>Менеджер</option>
          <option value="client" ${current.role === 'client' ? 'selected' : ''}>Клиент</option>
          <option value="doctor" ${current.role === 'doctor' ? 'selected' : ''}>Врач</option>
          <option value="other" ${current.role === 'other' ? 'selected' : ''}>Другой</option>
        </select>
      </div>
      ${allSpeakers.length > 0 ? `
      <div style="border-top:1px solid var(--border);padding-top:16px;margin-bottom:16px">
        <label style="display:block;margin-bottom:4px;color:var(--text-muted);font-size:13px">Объединить с другим спикером</label>
        <p style="font-size:12px;color:var(--text-muted);margin:0 0 8px">Все реплики ${speakerId} будут переназначены выбранному спикеру</p>
        <select id="modalMergeTarget" style="width:100%;padding:8px 12px;background:var(--bg-primary);border:1px solid var(--border);border-radius:6px;color:var(--text-primary);font-size:14px">
          <option value="">— не объединять —</option>
          ${allSpeakers.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(getSpeakerDisplay(s))} (${s})</option>`).join('')}
        </select>
      </div>
      ` : ''}
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button id="modalCancel" style="padding:8px 16px;background:var(--bg-tertiary);border:1px solid var(--border);border-radius:6px;color:var(--text-primary);cursor:pointer">Отмена</button>
        <button id="modalSave" style="padding:8px 16px;background:var(--accent);border:none;border-radius:6px;color:#fff;cursor:pointer;font-weight:500">Сохранить</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  // Focus name input
  const nameInput = overlay.querySelector('#modalSpeakerName');
  nameInput.focus();
  nameInput.select();

  return new Promise((resolve) => {
    overlay.querySelector('#modalCancel').onclick = () => { overlay.remove(); resolve(); };
    overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve(); } });

    overlay.querySelector('#modalSave').onclick = async () => {
      const name = nameInput.value.trim();
      const role = overlay.querySelector('#modalSpeakerRole').value;
      const mergeTarget = overlay.querySelector('#modalMergeTarget')?.value || '';
      const sessionId = _currentCallData.session.id;

      overlay.remove();

      // If merging speakers
      if (mergeTarget) {
        try {
          await api(`/api/sessions/${sessionId}/reassign-speaker`, {
            method: 'PATCH',
            body: JSON.stringify({ old_speaker: speakerId, new_speaker: mergeTarget }),
          });
          // Update local transcript data
          const segments = Array.isArray(_currentCallData.transcript)
            ? _currentCallData.transcript
            : (_currentCallData.transcript.segments || _currentCallData.transcript.utterances || []);
          for (const seg of segments) {
            if (seg.speaker === speakerId) seg.speaker = mergeTarget;
          }
          showToast(`${speakerId} объединён с ${getSpeakerDisplay(mergeTarget)}`);
          // Re-render the whole call detail
          renderCallDetail(_currentCallData.session.id);
          resolve();
          return;
        } catch (err) {
          showToast('Ошибка объединения: ' + err.message, 'error');
          resolve();
          return;
        }
      }

      // Just rename/role update
      const meta = _currentCallData.session.metadata || {};
      const speakerMap = { ...(meta.speaker_map || {}) };
      speakerMap[speakerId] = { name: name || null, role: role || null };

      try {
        await api(`/api/sessions/${sessionId}/speaker-map`, {
          method: 'PATCH',
          body: JSON.stringify({ speaker_map: speakerMap }),
        });
        _currentCallData.session.metadata = { ...meta, speaker_map: speakerMap };
        $$('.speaker-tag').forEach(tag => {
          const sid = tag.dataset.speaker;
          if (sid) tag.textContent = getSpeakerDisplay(sid);
        });
        showToast('Спикер обновлён');
      } catch (err) {
        showToast('Ошибка: ' + err.message, 'error');
      }
      resolve();
    };
  });
}

async function renderReprocess() {
  const app = document.getElementById('app');
  app.innerHTML = `
    <div class="page-header">
      <h1>Повторный прогон звонков</h1>
      <p class="page-subtitle">Укажите ID сделки — подтянем все записанные звонки с её контактов и прогоним через оценку.</p>
    </div>
    <div class="card" style="max-width:600px">
      <form id="reprocessForm" style="display:flex;flex-direction:column;gap:12px">
        <label style="display:flex;flex-direction:column;gap:6px">
          <span>ID сделки AmoCRM</span>
          <input type="number" id="leadIdInput" required placeholder="11425275" style="padding:8px;border-radius:6px;border:1px solid var(--border)">
        </label>
        <label style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="forceInput">
          <span>Перезапустить уже обработанные</span>
        </label>
        <button type="submit" class="btn btn-primary" style="align-self:flex-start">Запустить</button>
      </form>
      <div id="reprocessResult" style="margin-top:16px"></div>
    </div>
  `;

  document.getElementById('reprocessForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const leadId = parseInt(document.getElementById('leadIdInput').value, 10);
    const force = document.getElementById('forceInput').checked;
    const resultEl = document.getElementById('reprocessResult');
    resultEl.innerHTML = '<em>Отправляем запрос…</em>';
    try {
      const resp = await fetch('/api/amocrm/reprocess', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({lead_id: leadId, force}),
      });
      if (!resp.ok) {
        const errText = await resp.text();
        resultEl.innerHTML = `<div style="color:#f85149">Ошибка ${resp.status}: ${escapeHtml(errText.slice(0, 200))}</div>`;
        return;
      }
      const data = await resp.json();
      const rows = (title, items, color) => {
        if (!items || items.length === 0) return '';
        const listItems = items.map(it => `<li>note ${it.note_id}${it.duration != null ? ` (${it.duration}s)` : ''}${it.call_id != null ? ` → call_id ${it.call_id}` : ''}</li>`).join('');
        return `<div style="margin-top:8px"><strong style="color:${color}">${title} (${items.length}):</strong><ul>${listItems}</ul></div>`;
      };
      resultEl.innerHTML =
        rows('В очередь поставлено', data.queued, '#4CAF50') +
        rows('Уже были обработаны', data.already_processed, '#58a6ff') +
        rows('Без записи (скипнуто)', data.missing_recording, '#d29922');
    } catch (err) {
      resultEl.innerHTML = `<div style="color:#f85149">Сбой: ${escapeHtml(err.message)}</div>`;
    }
  });
}

// ============================================
// PAGE: Templates
// ============================================
// База знаний (#knowledge)
// ============================================
async function renderKnowledge() {
  showLoading();
  try {
    const cats = await api('/api/knowledge/categories?include_entries=true');
    let html = `
      <div class="page-header">
        <div>
          <h1>База знаний</h1>
          <p>Термины, бренды, сущности: коррекция транскрипта, глоссарий для LLM, теги звонков</p>
        </div>
        ${isAdmin() ? `<button class="btn btn-primary" onclick="kbNewCategory()">+ Категория</button>` : ''}
      </div>`;
    html += cats.length
      ? cats.map(c => kbCategoryCard(c)).join('')
      : `<div class="empty-state"><p>Пока нет категорий${isAdmin() ? ' — создайте первую' : ''}</p></div>`;
    app.innerHTML = html;
  } catch (err) {
    app.innerHTML = `<div class="empty-state"><p>Ошибка: ${escapeHtml(err.message)}</p></div>`;
  }
}

function kbFlagBadge(cat, key, label) {
  const on = !!cat[key];
  const attr = isAdmin()
    ? `onclick="kbToggleFlag('${cat.id}','${key}',${!on})" style="cursor:pointer"`
    : '';
  return `<span class="badge ${on ? 'badge-completed' : ''}" ${attr} title="${label}">${label}: ${on ? 'да' : 'нет'}</span>`;
}

function kbCategoryCard(cat) {
  const entries = cat.entries || [];
  return `
    <div class="card" style="margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <h3 style="margin:0">${escapeHtml(cat.name)} <span class="muted" style="font-weight:400">(${escapeHtml(cat.slug)})</span></h3>
        ${isAdmin() ? `<button class="btn btn-danger btn-sm" title="Удалить категорию" onclick="kbDeleteCategory('${cat.id}', ${JSON.stringify(cat.name)})">✕</button>` : ''}
      </div>
      <div style="display:flex;gap:8px;margin:8px 0;flex-wrap:wrap">
        ${kbFlagBadge(cat, 'feeds_asr', 'ASR')}
        ${kbFlagBadge(cat, 'feeds_llm', 'Глоссарий')}
        ${kbFlagBadge(cat, 'is_taxonomy', 'Теги')}
      </div>
      ${cat.description ? `<p class="muted">${escapeHtml(cat.description)}</p>` : ''}
      <table class="data-table" style="width:100%;margin-top:8px">
        <thead><tr><th>Термин</th><th>Синонимы</th><th>Описание</th><th></th></tr></thead>
        <tbody>
          ${entries.length ? entries.map(e => `
            <tr>
              <td>${escapeHtml(e.term)}</td>
              <td>${escapeHtml((e.aliases || []).join(', '))}</td>
              <td>${escapeHtml(e.description || '')}</td>
              <td style="white-space:nowrap;text-align:right">
                <button class="btn btn-secondary btn-sm" onclick="kbShowMentions('${e.id}', ${JSON.stringify(e.term)})">Упоминания</button>
                ${isAdmin() ? `<button class="btn btn-danger btn-sm" onclick="kbDeleteEntry('${e.id}', ${JSON.stringify(e.term)})">✕</button>` : ''}
              </td>
            </tr>`).join('') : `<tr><td colspan="4" class="muted">Записей пока нет</td></tr>`}
        </tbody>
      </table>
      ${isAdmin() ? `
        <form onsubmit="event.preventDefault(); kbAddEntry('${cat.id}', this)" style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
          <input type="text" name="term" placeholder="термин" required style="flex:1;min-width:120px">
          <input type="text" name="aliases" placeholder="синонимы через запятую" style="flex:1;min-width:120px">
          <input type="text" name="description" placeholder="описание" style="flex:2;min-width:120px">
          <button type="submit" class="btn btn-secondary btn-sm">+ Запись</button>
        </form>
        <details style="margin-top:8px">
          <summary class="muted" style="cursor:pointer">Импорт списком</summary>
          <form onsubmit="event.preventDefault(); kbImport('${cat.id}', this)" style="margin-top:8px">
            <textarea name="rows" rows="5" placeholder="Один термин на строку, синонимы после двоеточия: Эталон: etalon, эталон" style="width:100%"></textarea>
            <button type="submit" class="btn btn-secondary btn-sm" style="margin-top:6px">Импортировать</button>
          </form>
        </details>
      ` : ''}
    </div>`;
}

async function kbNewCategory() {
  const name = prompt('Название категории (например, «Бренды»):');
  if (!name) return;
  const slug = (prompt('Slug (латиница, напр. brands):', '') || '').trim();
  if (!slug) return;
  try {
    await api('/api/knowledge/categories', {
      method: 'POST',
      body: JSON.stringify({ name: name.trim(), slug }),
    });
    showToast('Категория создана');
    renderKnowledge();
  } catch (err) {
    showToast('Не удалось создать: ' + err.message, 'error');
  }
}

async function kbToggleFlag(catId, key, value) {
  try {
    await api(`/api/knowledge/categories/${catId}`, {
      method: 'PATCH',
      body: JSON.stringify({ [key]: value }),
    });
    renderKnowledge();
  } catch (err) {
    showToast('Не удалось изменить: ' + err.message, 'error');
  }
}

async function kbDeleteCategory(catId, name) {
  if (!confirm(`Удалить категорию "${name}" со всеми записями?`)) return;
  try {
    await api(`/api/knowledge/categories/${catId}`, { method: 'DELETE' });
    showToast('Категория удалена');
    renderKnowledge();
  } catch (err) {
    showToast('Не удалось удалить: ' + err.message, 'error');
  }
}

async function kbAddEntry(catId, form) {
  const aliases = form.aliases.value.split(',').map(s => s.trim()).filter(Boolean);
  try {
    await api('/api/knowledge/entries', {
      method: 'POST',
      body: JSON.stringify({
        category_id: catId,
        term: form.term.value.trim(),
        aliases,
        description: form.description.value.trim() || null,
      }),
    });
    renderKnowledge();
  } catch (err) {
    showToast('Не удалось добавить: ' + err.message, 'error');
  }
}

async function kbDeleteEntry(entryId, term) {
  if (!confirm(`Удалить запись "${term}"?`)) return;
  try {
    await api(`/api/knowledge/entries/${entryId}`, { method: 'DELETE' });
    renderKnowledge();
  } catch (err) {
    showToast('Не удалось удалить: ' + err.message, 'error');
  }
}

async function kbImport(catId, form) {
  const rows = form.rows.value.split('\n').map(line => {
    const t = line.trim();
    if (!t) return null;
    const idx = t.indexOf(':');
    const term = (idx >= 0 ? t.slice(0, idx) : t).trim();
    const aliases = idx >= 0 ? t.slice(idx + 1).split(',').map(s => s.trim()).filter(Boolean) : [];
    return term ? { term, aliases } : null;
  }).filter(Boolean);
  if (!rows.length) return;
  try {
    const res = await api('/api/knowledge/import', {
      method: 'POST',
      body: JSON.stringify({ category_id: catId, rows }),
    });
    showToast(`Импортировано: ${res.inserted} из ${res.received}`);
    renderKnowledge();
  } catch (err) {
    showToast('Импорт не удался: ' + err.message, 'error');
  }
}

async function kbShowMentions(entryId, term) {
  try {
    const rows = await api(`/api/knowledge/entries/${entryId}/mentions`);
    if (!rows.length) { showToast(`«${term}»: упоминаний нет`); return; }
    const total = rows.reduce((s, r) => s + (r.count || 0), 0);
    const lines = rows.map(r => `${formatDate(r.created_at)} — ${r.count}`).join('\n');
    alert(`«${term}»: ${total} упоминаний в ${rows.length} звонках\n\n${lines}`);
  } catch (err) {
    showToast('Не удалось загрузить упоминания: ' + err.message, 'error');
  }
}


// ============================================
function _kindLabel(kind) {
  if (kind === 'extraction') return 'Извлечение фактов';
  if (kind === 'evaluation') return 'Оценка звонка';
  return kind;
}

async function renderTemplates() {
  showLoading();
  try {
    const items = await api('/api/templates');
    let html = `
      <div class="page-header">
        <div>
          <h1>Шаблоны</h1>
          <p>Что и как анализировать в записи: извлекать факты или оценивать звонок</p>
        </div>
        ${isAdmin() ? `<button class="btn btn-primary" onclick="navigate('template/new')">+ Новый шаблон</button>` : ''}
      </div>
      ${items.length === 0
        ? '<div class="empty-state"><p>Пока нет шаблонов</p></div>'
        : `<div class="cards-grid">${items.map(t => `
            <a class="tpl-card" href="#template/${t.id}">
              <div class="tpl-card-head">
                <span class="tpl-kind tpl-kind-${escapeHtml(t.kind)}">${escapeHtml(_kindLabel(t.kind))}</span>
                ${isAdmin() ? `<button class="icon-btn icon-btn--danger tpl-card-del" title="Удалить шаблон" aria-label="Удалить" onclick="event.preventDefault();event.stopPropagation();deleteTemplate('${t.id}', ${JSON.stringify(t.name)})">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>
                </button>` : ''}
              </div>
              <div class="tpl-card-name">${escapeHtml(t.name)}</div>
              <div class="tpl-card-desc">${escapeHtml(t.description || '—')}</div>
              <div class="tpl-card-foot">Изменён ${formatDate(t.updated_at)}</div>
            </a>
          `).join('')}</div>`
      }
    `;
    app.innerHTML = html;
  } catch (err) {
    app.innerHTML = `<div class="empty-state"><p>Ошибка: ${escapeHtml(err.message)}</p></div>`;
  }
}

async function renderTemplateEdit(id) {
  showLoading();
  let tpl = { name: '', description: '', kind: 'extraction', prompt: '', json_schema: {} };
  try {
    if (id) tpl = await api(`/api/templates/${id}`);
    const schemaText = JSON.stringify(tpl.json_schema, null, 2);
    const schemaLines = Math.max(20, Math.min(40, schemaText.split('\n').length + 1));
    app.innerHTML = `
      <a href="#templates" class="back-link">← Назад к шаблонам</a>
      <div class="page-header">
        <div>
          <h1>${id ? 'Редактирование шаблона' : 'Новый шаблон'}</h1>
          <p>${id ? escapeHtml(tpl.name) : 'Опиши что и как извлекать или оценивать'}</p>
        </div>
      </div>

      <form id="tplForm" onsubmit="event.preventDefault(); saveTemplate('${id || ''}')" class="tpl-form">

        <div class="card">
          <h3>Основное</h3>
          <div class="form-group">
            <label>Название</label>
            <input type="text" id="tplName" value="${escapeHtml(tpl.name)}" placeholder="например: Извлечение структуры" required>
          </div>
          <div class="form-group">
            <label>Тип шаблона</label>
            <select id="tplKind"${id ? ' disabled' : ''}>
              <option value="extraction"${tpl.kind === 'extraction' ? ' selected' : ''}>Извлечение фактов (транскрипт → структура)</option>
              <option value="evaluation"${tpl.kind === 'evaluation' ? ' selected' : ''}>Оценка звонка (звонок → скрипт + критерии)</option>
            </select>
            ${id ? '<p class="form-hint">Тип нельзя менять у существующего шаблона</p>' : ''}
          </div>
          <div class="form-group">
            <label>Описание</label>
            <textarea id="tplDesc" rows="2" placeholder="Короткое описание для списка шаблонов">${escapeHtml(tpl.description || '')}</textarea>
          </div>
        </div>

        <div class="card">
          <h3>Промпт для LLM</h3>
          <p class="form-hint">Инструкции, которые получит модель. Для извлечения — что искать. Для оценки — регламент, критерии, особые правила.</p>
          <textarea id="tplPrompt" class="tpl-prompt" rows="14" placeholder="Ты извлекаешь / оцениваешь..." required>${escapeHtml(tpl.prompt)}</textarea>
        </div>

        <div class="card">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <h3 style="margin:0">JSON Schema</h3>
            <button type="button" class="btn btn-secondary btn-sm" onclick="validateSchemaInline()">Проверить</button>
          </div>
          <p class="form-hint">Структура ответа модели. Strict-режим OpenAI: каждый object должен иметь <code>additionalProperties: false</code> и <code>required</code> со всеми ключами.</p>
          <textarea id="tplSchema" class="tpl-schema" rows="${schemaLines}" required>${escapeHtml(schemaText)}</textarea>
        </div>

        <div class="tpl-form-actions">
          <button type="submit" class="btn btn-primary">Сохранить</button>
          <a href="#templates" class="btn btn-secondary">Отмена</a>
        </div>
      </form>
    `;
  } catch (err) {
    app.innerHTML = `<div class="empty-state"><p>Ошибка: ${escapeHtml(err.message)}</p></div>`;
  }
}

async function saveTemplate(id) {
  let schema;
  try { schema = JSON.parse($('#tplSchema').value); }
  catch (e) { return showToast('JSON Schema is not valid JSON: ' + e.message, 'error'); }
  const body = {
    name: $('#tplName').value.trim(),
    description: $('#tplDesc').value.trim() || null,
    prompt: $('#tplPrompt').value,
    json_schema: schema,
  };
  if (!id) body.kind = $('#tplKind').value;

  try {
    if (id) {
      await api(`/api/templates/${id}`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      });
    } else {
      await api('/api/templates', {
        method: 'POST',
        body: JSON.stringify(body),
      });
    }
    showToast('Шаблон сохранён');
    navigate('templates');
  } catch (err) {
    showToast('Не удалось сохранить: ' + err.message, 'error');
  }
}

function validateSchemaInline() {
  try {
    JSON.parse($('#tplSchema').value);
    showToast('JSON синтаксически корректен (серверная валидация JSON Schema на сохранении)');
  } catch (e) {
    showToast('Ошибка JSON: ' + e.message, 'error');
  }
}

async function deleteTemplate(id, name) {
  if (!confirm(`Удалить шаблон "${name}"?`)) return;
  try {
    await api(`/api/templates/${id}`, {
      method: 'DELETE',
    });
    showToast('Шаблон удалён');
    renderTemplates();
  } catch (err) {
    showToast('Не удалось удалить: ' + err.message, 'error');
  }
}

// ============================================
// Extraction helpers (Task 14)
// ============================================

function renderExtractionFields(data, depth = 0) {
  if (data === null || data === undefined) return '<em class="muted">—</em>';
  if (Array.isArray(data)) {
    if (data.length === 0) return '<em class="muted">пусто</em>';
    if (data.every(v => typeof v === 'string')) {
      return '<ul>' + data.map(v => `<li>${escapeHtml(v)}</li>`).join('') + '</ul>';
    }
    return '<ul>' + data.map(v => `<li>${typeof v === 'object' ? renderExtractionFields(v, depth + 1) : escapeHtml(String(v))}</li>`).join('') + '</ul>';
  }
  if (typeof data === 'object') {
    return Object.entries(data).map(([k, v]) =>
      `<div class="ext-field"><div class="ext-key">${escapeHtml(k)}</div><div class="ext-val">${renderExtractionFields(v, depth + 1)}</div></div>`
    ).join('');
  }
  if (typeof data === 'boolean') return data ? '✓ да' : '✗ нет';
  return escapeHtml(String(data));
}

// ============================================
// «Презентация ЖК» — структурированный профиль на русском
// ============================================

const CX_LABELS = {
  location: 'Локация', architecture: 'Архитектура и корпуса',
  delivery: 'Сроки сдачи', finishes: 'Отделка', pricing: 'Цены',
  amenities: 'Комфорт и инфраструктура', additional_info: 'Заметки с презентаций',
  // location
  district: 'Район', address: 'Адрес', parks: 'Парки', embankments: 'Набережные',
  transport: 'Транспорт', malls: 'Торговые центры', venues: 'Места рядом',
  future_plans: 'Что появится в будущем',
  // architecture
  style: 'Стиль', materials: 'Материалы фасада', phases: 'Очередей',
  buildings: 'Корпусов', floors: 'Этажность', layouts_note: 'Планировки',
  // amenities
  lobby: 'Лобби', concierge: 'Консьерж', meeting_rooms: 'Переговорные',
  coworking: 'Коворкинг', guest_entrance: 'Гостевой вход',
  observation_deck: 'Видовая терраса', fitness: 'Фитнес',
  parking: 'Паркинг', engineering: 'Инженерия', yard: 'Двор',
  // pricing
  cash: 'При 100% оплате', mortgage: 'Ипотека',
  installment: 'Рассрочка', min_price: 'Цена от',
  // finishes
  options: 'Варианты отделки', design_styles: 'Стили дизайна',
};

const CX_ROMAN_QUARTER = { 1: 'I', 2: 'II', 3: 'III', 4: 'IV' };

function _cxIsEmpty(v) {
  if (v === null || v === undefined) return true;
  if (typeof v === 'string') return v.trim() === '';
  if (Array.isArray(v)) return v.length === 0;
  if (typeof v === 'object') return Object.values(v).every(_cxIsEmpty);
  return false;
}

function _cxFmtPrice(num, currency) {
  if (num === null || num === undefined || num === '') return null;
  const n = Number(num);
  if (!isFinite(n)) return String(num);
  const formatted = n.toLocaleString('ru-RU');
  const sym = !currency || /^rub|руб|₽$/i.test(String(currency)) ? '₽'
            : /^usd|\$$/i.test(String(currency)) ? '$'
            : /^eur|€$/i.test(String(currency)) ? '€'
            : String(currency);
  return `${formatted} ${sym}`;
}

function _cxFmtQuarter(year, quarter) {
  const q = quarter ? (CX_ROMAN_QUARTER[quarter] || quarter) : null;
  if (year && q) return `${q} кв. ${year}`;
  if (year) return String(year);
  if (q) return `${q} кв.`;
  return null;
}

function _cxValueHtml(v) {
  if (Array.isArray(v)) {
    if (v.length === 0) return '';
    if (v.every(x => typeof x === 'string')) {
      return '<ul class="cx-list">' + v.map(x => `<li>${escapeHtml(x)}</li>`).join('') + '</ul>';
    }
    return '<ul class="cx-list">' + v.map(x =>
      `<li>${typeof x === 'object' ? renderExtractionFields(x) : escapeHtml(String(x))}</li>`
    ).join('') + '</ul>';
  }
  if (typeof v === 'boolean') {
    return v
      ? '<span class="cx-badge cx-badge--yes">да</span>'
      : '<span class="cx-badge cx-badge--no">нет</span>';
  }
  if (v === null || v === undefined) return '';
  return escapeHtml(String(v));
}

function _cxRow(label, valueHtml) {
  if (!valueHtml) return '';
  return `<div class="cx-row"><div class="cx-key">${escapeHtml(label)}</div><div class="cx-val">${valueHtml}</div></div>`;
}

function _cxSection(title, rowsHtml, extraClass = '') {
  if (!rowsHtml) return '';
  return `<section class="cx-section ${extraClass}"><h3 class="cx-section-title">${escapeHtml(title)}</h3><div class="cx-grid">${rowsHtml}</div></section>`;
}

function _cxSectionByKeys(title, src, keys) {
  if (!src || _cxIsEmpty(src)) return '';
  const rows = keys
    .filter(k => !_cxIsEmpty(src[k]))
    .map(k => _cxRow(CX_LABELS[k] || k, _cxValueHtml(src[k])))
    .join('');
  return _cxSection(title, rows);
}

function _cxDeliverySection(d) {
  if (!d || _cxIsEmpty(d)) return '';
  const rows = [];
  const overall = _cxFmtQuarter(d.overall_year, d.overall_quarter);
  if (overall) rows.push(_cxRow('Срок сдачи', escapeHtml(overall)));
  if (Array.isArray(d.by_phase) && d.by_phase.length) {
    const items = d.by_phase.map(p => {
      const q = _cxFmtQuarter(p.year, p.quarter);
      const phase = p.phase ? escapeHtml(p.phase) : '—';
      return `<li>${phase}${q ? ' · ' + escapeHtml(q) : ''}</li>`;
    }).join('');
    rows.push(_cxRow('По очередям', `<ul class="cx-list">${items}</ul>`));
  }
  return _cxSection(CX_LABELS.delivery, rows.join(''));
}

function _cxPricingSection(p) {
  if (!p || _cxIsEmpty(p)) return '';
  const rows = [];
  const minP = _cxFmtPrice(p.min_price, p.currency);
  if (minP) rows.push(_cxRow(CX_LABELS.min_price, `<span class="cx-price">${escapeHtml(minP)}</span>`));
  for (const k of ['cash', 'installment', 'mortgage']) {
    if (!_cxIsEmpty(p[k])) rows.push(_cxRow(CX_LABELS[k], _cxValueHtml(p[k])));
  }
  return _cxSection(CX_LABELS.pricing, rows.join(''));
}

function _cxAmenitiesSection(a) {
  if (!a || _cxIsEmpty(a)) return '';
  const rows = [];
  const boolKeys = ['lobby', 'concierge', 'meeting_rooms', 'coworking',
                    'guest_entrance', 'observation_deck', 'fitness'];
  const yes = boolKeys.filter(k => a[k] === true);
  const no  = boolKeys.filter(k => a[k] === false);
  if (yes.length) {
    rows.push(_cxRow('Есть', yes.map(k =>
      `<span class="cx-chip cx-chip--yes">${escapeHtml(CX_LABELS[k])}</span>`
    ).join('')));
  }
  if (no.length) {
    rows.push(_cxRow('Нет', no.map(k =>
      `<span class="cx-chip cx-chip--no">${escapeHtml(CX_LABELS[k])}</span>`
    ).join('')));
  }
  if (!_cxIsEmpty(a.parking)) rows.push(_cxRow(CX_LABELS.parking, _cxValueHtml(a.parking)));
  if (!_cxIsEmpty(a.engineering)) rows.push(_cxRow(CX_LABELS.engineering, _cxValueHtml(a.engineering)));
  if (a.yard && !_cxIsEmpty(a.yard)) {
    const parts = [];
    if (a.yard.area_ha) parts.push(`<span class="cx-chip">${escapeHtml(String(a.yard.area_ha))} га</span>`);
    if (Array.isArray(a.yard.zones) && a.yard.zones.length) parts.push(_cxValueHtml(a.yard.zones));
    if (parts.length) rows.push(_cxRow(CX_LABELS.yard, parts.join('')));
  }
  return _cxSection(CX_LABELS.amenities, rows.join(''));
}

function _cxNotesSection(notes) {
  if (!Array.isArray(notes) || notes.length === 0) return '';
  const cards = notes.map(n => {
    if (typeof n === 'string') {
      if (!n.trim()) return '';
      return `<div class="cx-note"><div class="cx-note-text">${escapeHtml(n)}</div></div>`;
    }
    const text = n && typeof n === 'object' ? n.note : String(n);
    if (!text || !String(text).trim()) return '';
    const sid = n && n.session_id;
    const rec = n && n.recorded_at;
    let metaHtml = '';
    if (sid) {
      const dateStr = rec ? formatDate(rec) : 'Открыть запись';
      metaHtml = `<a class="cx-note-link" href="#call/${escapeHtml(sid)}">${rec ? 'Запись от ' + escapeHtml(dateStr) : escapeHtml(dateStr)}</a>`;
    } else if (rec) {
      metaHtml = escapeHtml(formatDate(rec));
    }
    return `<div class="cx-note"><div class="cx-note-text">${escapeHtml(text)}</div>${metaHtml ? `<div class="cx-note-meta">${metaHtml}</div>` : ''}</div>`;
  }).filter(Boolean).join('');
  if (!cards) return '';
  return `<section class="cx-section cx-section--notes"><h3 class="cx-section-title">${escapeHtml(CX_LABELS.additional_info)} <span class="cx-section-count">${notes.length}</span></h3><div class="cx-notes">${cards}</div></section>`;
}

const CX_KNOWN_TOP_KEYS = new Set([
  'name', 'developer', 'class',
  'location', 'architecture', 'amenities', 'delivery', 'finishes', 'pricing',
  'additional_info',
]);

function renderComplexProfile(data) {
  if (!data || typeof data !== 'object') {
    return '<p class="muted">Профиль пуст.</p>';
  }
  let html = '';
  html += _cxSectionByKeys(CX_LABELS.location, data.location,
    ['address', 'district', 'parks', 'embankments', 'transport', 'malls', 'venues', 'future_plans']);
  html += _cxSectionByKeys(CX_LABELS.architecture, data.architecture,
    ['style', 'materials', 'phases', 'buildings', 'floors', 'layouts_note']);
  html += _cxDeliverySection(data.delivery);
  html += _cxSectionByKeys(CX_LABELS.finishes, data.finishes, ['options', 'design_styles']);
  html += _cxPricingSection(data.pricing);
  html += _cxAmenitiesSection(data.amenities);
  html += _cxNotesSection(data.additional_info);

  // Любые незнакомые ключи (на случай других шаблонов) — через старый рендер.
  const unknown = {};
  for (const [k, v] of Object.entries(data)) {
    if (!CX_KNOWN_TOP_KEYS.has(k) && !_cxIsEmpty(v)) unknown[k] = v;
  }
  if (Object.keys(unknown).length) {
    html += `<section class="cx-section"><h3 class="cx-section-title">Прочее</h3>${renderExtractionFields(unknown)}</section>`;
  }

  if (!html) {
    return '<p class="muted">Информация ещё не извлечена. Обработай звонок через шаблон извлечения.</p>';
  }
  return html;
}

// Dispatcher: dental cards keep their bespoke layout; everything else (e.g.
// chechetov «Итоги созвона») renders through the generic key/value renderer.
function renderCard(card, label) {
  if (!card) return '';
  const isDental = card.dental_status !== undefined ||
    (card.patient && typeof card.patient === 'object');
  return isDental ? renderDentalCard(card, label) : renderGenericCard(card, label);
}

// --- Generic card renderer (domain-agnostic) -------------------------------
const _CARD_LABELS = {
  summary: 'Итог', participants: 'Участники', topics: 'Темы', points: 'Тезисы',
  agreements: 'Договорённости', next_steps: 'Следующие шаги', action: 'Действие',
  owner: 'Ответственный', due: 'Срок', objections: 'Возражения',
  objection: 'Возражение', raised_by: 'Кто высказал', handled: 'Отработано',
  handling: 'Как отработано', improvement: 'Как можно лучше',
  sale_context: 'Контекст продажи', speaker: 'Спикер', name: 'Имя',
  role: 'Роль', topic: 'Тема',
};
function _cardLabel(k) {
  return _CARD_LABELS[k] || k.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}
function _cardObjInline(o) {
  return Object.entries(o)
    .filter(([, v]) => v !== null && v !== '' && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `<b>${escapeHtml(_cardLabel(k))}:</b> ${_cardVal(v)}`)
    .join(' · ') || '—';
}
function _cardVal(v) {
  const esc = escapeHtml;
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'boolean') return v ? 'да' : 'нет';
  if (typeof v === 'string' || typeof v === 'number') return esc(String(v));
  if (Array.isArray(v)) {
    if (!v.length) return '—';
    const items = v.map((x) =>
      (x && typeof x === 'object') ? _cardObjInline(x) : esc(String(x)));
    return `<ul class="card-ul">${items.map((i) => `<li>${i}</li>`).join('')}</ul>`;
  }
  if (typeof v === 'object') return _cardObjInline(v);
  return esc(String(v));
}
function renderGenericCard(card, label) {
  const esc = escapeHtml;
  const rows = Object.entries(card)
    .filter(([k]) => k !== 'summary')
    .map(([k, v]) => `<dt>${esc(_cardLabel(k))}</dt><dd>${_cardVal(v)}</dd>`)
    .join('');
  return `
    <div class="card">
      <h3>📋 ${esc(label || 'Итоги')}</h3>
      <dl class="card-dl">${rows}</dl>
      ${card.summary ? `<blockquote>${esc(card.summary)}</blockquote>` : ''}
    </div>`;
}

// --- Dental card renderer (unchanged) --------------------------------------
function renderDentalCard(card, label) {
  if (!card) return '';
  const esc = escapeHtml;
  const list = (a) => (a && a.length) ? a.map(esc).join(', ') : '—';
  const cl = card; // card.json IS the clinical object
  const p = cl.patient || {};
  const an = cl.anamnesis || {};
  const tp = cl.treatment_plan || {};
  const teeth = (cl.dental_status || []).map(t =>
    `<tr><td>${esc(t.tooth)}</td><td>${esc(t.status)}</td><td>${esc(t.note || '—')}</td></tr>`).join('');
  const stages = (tp.stages || []).map(s =>
    `<tr><td>${esc(s.procedure)}</td><td>${esc(s.teeth || '—')}</td><td>${esc(s.priority)}</td><td>${esc(s.timeline || '—')}</td><td>${esc(s.price || '—')}</td></tr>`).join('');
  const row = (k, v) => `<dt>${k}</dt><dd>${v}</dd>`;
  return `
    <div class="card">
      <h3>🦷 ${esc(label || 'Карта приёма')}</h3>
      <dl class="card-dl">
        ${row('Пациент', `${esc(p.gender || '—')}; имя ${esc(p.name || '—')}; возраст ${esc(p.age || '—')}; тел. ${esc(p.phone || '—')}`)}
        ${row('Повод', esc(cl.chief_complaint || '—'))}
        ${row('Жалобы', list(an.complaints))}
        ${row('Анамнез', esc(an.history || '—'))}
        ${row('Хронические', list(an.chronic_conditions))}
        ${row('Аллергии', list(an.allergies))}
        ${row('Препараты', list(an.medications))}
        ${row('Осмотр', list(cl.examination))}
        ${row('Диагноз', list(cl.diagnosis))}
      </dl>
      ${teeth ? `<h4>Зубная формула</h4><table class="card-table"><tr><th>Зуб</th><th>Статус</th><th>Заметка</th></tr>${teeth}</table>` : ''}
      ${stages ? `<h4>План лечения</h4><table class="card-table"><tr><th>Процедура</th><th>Зубы</th><th>Приоритет</th><th>Срок</th><th>Цена</th></tr>${stages}</table>
        <p><b>Итого:</b> ${esc(tp.total_cost || '—')} · <b>Оплата:</b> ${list(tp.payment_options)}</p>` : '<h4>План лечения</h4><p>—</p>'}
      ${(cl.recommendations && cl.recommendations.length) ? `<h4>Рекомендации</h4><ul>${cl.recommendations.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}
      ${cl.visit_outcome ? `<p><b>Итог визита:</b> ${esc(cl.visit_outcome.result)} — ${esc(cl.visit_outcome.next_step || '—')}</p>` : ''}
      ${cl.summary ? `<blockquote>${esc(cl.summary)}</blockquote>` : ''}
    </div>`;
}

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

async function reprocessSession(sessionId) {
  let templates = [];
  try { templates = await api('/api/templates'); } catch (e) { return showToast('Не удалось загрузить шаблоны', 'error'); }
  if (!templates.length) return showToast('Нет доступных шаблонов', 'error');
  const tpl = await pickTemplateModal(templates, {
    title: 'Перепрогнать сессию',
    hint: 'Выберите шаблон — анализ будет запущен заново с его промптом и схемой.',
    confirmLabel: 'Запустить',
  });
  if (!tpl) return;
  try {
    await api(`/api/sessions/${sessionId}/reprocess`, {
      method: 'POST',
      body: JSON.stringify({ template_id: tpl.id }),
    });
    showToast('Переоценка запущена — обновите страницу через минуту');
  } catch (err) {
    showToast('Ошибка: ' + err.message, 'error');
  }
}

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

// ── ASR-сравнение (админ-тюнинг) ────────────────────────────────
const ASR_ENGINE_LABELS = { whisper: 'Whisper (локальный)', assemblyai: 'AssemblyAI', elevenlabs: 'ElevenLabs Scribe' };

function _asrVariantStat(s) {
  if (!s) return 'нет данных';
  if (s.status === 'error') return `ошибка: ${escapeHtml(s.error || '')}`;
  const spk = s.has_speakers ? `${(s.speakers || []).length} спикер(ов)` : 'без спикеров';
  return `${s.segments} сегм. · ${s.chars} симв. · ${spk}${s.elapsed_s != null ? ` · ${s.elapsed_s}s` : ''}`;
}

function renderAsrVariantsSection(variants) {
  if (!variants) return '';
  const engines = Object.keys(variants.engines || {});
  if (!engines.length) return '';
  const tabs = engines.map((e, i) =>
    `<button class="btn btn-secondary btn-sm asr-variant-tab" data-engine="${e}" style="${i === 0 ? 'font-weight:700' : ''}">${escapeHtml(ASR_ENGINE_LABELS[e] || e)} <span style="opacity:.6">(${escapeHtml((variants.engines[e] || {}).status || '?')})</span></button>`
  ).join('');
  const panels = engines.map((e, i) => {
    const segs = (variants.variants || {})[e] || [];
    const body = segs.length
      ? segs.map(seg => {
          const speaker = seg.speaker || '';
          const time = seg.start != null ? formatSeconds(seg.start) : '';
          return `<div class="transcript-line">${time ? `<span class="transcript-time">${time}</span>` : ''}${speaker ? `<span class="speaker-tag">${escapeHtml(speaker)}</span>` : ''}<span class="transcript-text">${escapeHtml(seg.text || '')}</span></div>`;
        }).join('')
      : '<p style="color:var(--text-muted)">пусто</p>';
    return `<div class="asr-variant-panel" data-engine="${e}" style="${i === 0 ? '' : 'display:none'}">
        <div style="color:var(--text-muted);font-size:13px;margin-bottom:8px">${escapeHtml(_asrVariantStat(variants.engines[e]))}</div>
        <div class="transcript-container">${body}</div>
      </div>`;
  }).join('');
  return `
    <div class="card">
      <h3>Варианты транскрипта (ASR-сравнение)</h3>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">${tabs}</div>
      ${panels}
    </div>`;
}

async function compareEnginesModal(sessionId) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal-content">
      <div class="modal-head"><h3>Сравнить движки ASR</h3></div>
      <p style="color:var(--text-muted);font-size:13px">Прогнать аудио этого звонка выбранными движками для сравнения качества. Облачные движки (AssemblyAI, ElevenLabs) — платные вызовы.</p>
      <div style="display:flex;flex-direction:column;gap:8px;margin:12px 0">
        ${Object.entries(ASR_ENGINE_LABELS).map(([id, label]) => `<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" value="${id}" checked> ${escapeHtml(label)}</label>`).join('')}
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="btn btn-secondary" id="asrCancel">Отмена</button>
        <button class="btn btn-primary" id="asrRun">Запустить</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  overlay.querySelector('#asrCancel').addEventListener('click', close);
  overlay.querySelector('#asrRun').addEventListener('click', async () => {
    const engines = Array.from(overlay.querySelectorAll('input[type=checkbox]:checked')).map(c => c.value);
    if (!engines.length) return showToast('Выберите хотя бы один движок', 'error');
    try {
      await api(`/api/sessions/${sessionId}/transcribe-compare`, { method: 'POST', body: JSON.stringify({ engines }) });
      showToast('Сравнение запущено — обновите страницу через минуту');
      close();
    } catch (err) {
      showToast('Ошибка: ' + err.message, 'error');
    }
  });
}

// ── Критерии оценки (#evaluation, пер-сценарные профили) ─────────
let _evalState = { scenarioId: null, data: null };

function evalSelectScenario(sid) { _evalState.scenarioId = sid; renderEvaluation(); }

function _evalCriterionRow(c) {
  return `<div class="eval-crit-row" style="display:grid;grid-template-columns:1fr 2fr auto;gap:8px;margin-bottom:8px">
    <input class="eval-crit-name" placeholder="Название" value="${escapeHtml(c.name || '')}">
    <input class="eval-crit-desc" placeholder="Описание (что оцениваем)" value="${escapeHtml(c.description || '')}">
    <button class="btn btn-danger btn-sm" onclick="this.closest('.eval-crit-row').remove()">✕</button>
  </div>`;
}

function evalAddCriterion() {
  document.getElementById('evalCriteria').insertAdjacentHTML('beforeend', _evalCriterionRow({}));
}

function _evalCollectCriteria() {
  return Array.from(document.querySelectorAll('#evalCriteria .eval-crit-row')).map(row => ({
    name: row.querySelector('.eval-crit-name').value.trim(),
    description: row.querySelector('.eval-crit-desc').value.trim(),
  })).filter(c => c.name);
}

function _evalHistory(history) {
  if (!history || !history.length) return '<p style="color:var(--text-muted)">Версий пока нет — действует дефолт из конфига.</p>';
  return history.map(v => `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 0;border-bottom:1px solid var(--border,#eee)">
      <div><b>v${v.version}</b> · ${escapeHtml(v.source)} · ${escapeHtml(formatDate(v.created_at))}
        ${v.is_active ? '<span style="background:#2e7d32;color:#fff;padding:2px 6px;border-radius:4px;font-size:11px">активна</span>' : ''}</div>
      <div style="display:flex;gap:6px">
        ${v.is_active ? '' : `<button class="btn btn-secondary btn-sm" onclick="evalActivate('${v.id}')">Активировать</button>`}
        ${v.is_active ? '' : `<button class="btn btn-danger btn-sm" onclick="evalDeleteVersion('${v.id}')">Удалить</button>`}
      </div>
    </div>`).join('');
}

async function renderEvaluation() {
  const scenarios = (features && features.scenarios) || [];
  if (!scenarios.length) {
    app.innerHTML = `<div class="empty-state"><h2>Критерии оценки</h2><p>У аккаунта нет сценариев созвонов. Критерии оценки настраиваются по сценарию.</p></div>`;
    return;
  }
  if (!_evalState.scenarioId || !scenarios.some(s => s.id === _evalState.scenarioId)) {
    _evalState.scenarioId = scenarios[0].id;
  }
  const sid = _evalState.scenarioId;
  showLoading();
  let data;
  try { data = await api(`/api/eval-profiles?scenario_id=${encodeURIComponent(sid)}`); }
  catch (err) { app.innerHTML = `<div class="empty-state"><p>Ошибка: ${escapeHtml(err.message)}</p></div>`; return; }
  _evalState.data = data;

  const active = data.active;
  const fb = data.file_fallback || {};
  const criteria = (active && active.criteria && active.criteria.length) ? active.criteria : (fb.criteria || []);
  const prompt = (active && active.prompt) || fb.prompt || '';
  const srcLabel = active ? `активная версия v${active.version}` : 'дефолт из конфига (профиль не задан)';
  const opts = scenarios.map(s => `<option value="${escapeHtml(s.id)}" ${s.id === sid ? 'selected' : ''}>${escapeHtml(s.name)}</option>`).join('');

  app.innerHTML = `
    <div class="page-header"><h1>Критерии оценки</h1></div>
    <div class="card">
      <label>Сценарий созвона</label>
      <select id="evalScenario" onchange="evalSelectScenario(this.value)" style="max-width:360px">${opts}</select>
      <p style="color:var(--text-muted);font-size:13px;margin-top:6px">Источник: ${escapeHtml(srcLabel)}</p>
    </div>
    <div class="card">
      <h3>Критерии</h3>
      <div id="evalCriteria">${criteria.map(c => _evalCriterionRow(c)).join('') || ''}</div>
      <button class="btn btn-secondary btn-sm" onclick="evalAddCriterion()">+ Критерий</button>
      <div style="margin-top:12px"><button class="btn btn-primary" onclick="evalSaveCriteria()">Сохранить как версию</button></div>
    </div>
    <div class="card">
      <h3>Промпт оценки</h3>
      <textarea id="evalPromptView" rows="8" readonly style="width:100%;font-family:monospace">${escapeHtml(prompt)}</textarea>
      <h4 style="margin-top:16px">Пожелания → переработать промпт</h4>
      <p style="color:var(--text-muted);font-size:13px">Опишите, что важно при оценке. LLM переработает промпт; результат сохранится как новая версия — можно активировать или отклонить.</p>
      <textarea id="evalWishes" rows="4" placeholder="Напр.: строже оценивай отработку возражений; учитывай фиксацию следующих шагов" style="width:100%"></textarea>
      <div style="margin-top:8px"><button class="btn btn-primary" id="evalRewriteBtn" onclick="evalRewrite()">Переработать промпт</button></div>
      <div id="evalRewritePreview"></div>
    </div>
    <div class="card">
      <h3>История версий</h3>
      ${_evalHistory(data.history)}
    </div>`;
}

async function evalSaveCriteria() {
  const criteria = _evalCollectCriteria();
  const d = _evalState.data || {};
  const prompt = (d.active && d.active.prompt) || (d.file_fallback || {}).prompt || null;
  try {
    await api(`/api/eval-profiles/${encodeURIComponent(_evalState.scenarioId)}/versions`, {
      method: 'POST', body: JSON.stringify({ criteria, prompt, source: 'manual', activate: true }),
    });
    showToast('Критерии сохранены как новая активная версия');
    renderEvaluation();
  } catch (err) { showToast('Ошибка: ' + err.message, 'error'); }
}

async function evalRewrite() {
  const wishes = document.getElementById('evalWishes').value.trim();
  if (!wishes) return showToast('Опишите пожелания', 'error');
  const btn = document.getElementById('evalRewriteBtn');
  btn.disabled = true; btn.textContent = 'Думаю…';
  try {
    const v = await api(`/api/eval-profiles/${encodeURIComponent(_evalState.scenarioId)}/rewrite`, {
      method: 'POST', body: JSON.stringify({ wishes }),
    });
    const meta = v.rewrite_meta || {};
    document.getElementById('evalRewritePreview').innerHTML = `
      <div class="card" style="margin-top:12px;border:1px solid var(--accent,#888)">
        <h4>Предложение (v${v.version}, ещё не активно)</h4>
        ${meta.rationale ? `<p style="color:var(--text-muted)">${escapeHtml(meta.rationale)}</p>` : ''}
        <textarea rows="8" readonly style="width:100%;font-family:monospace">${escapeHtml(v.prompt || '')}</textarea>
        ${(v.criteria && v.criteria.length) ? `<p style="margin-top:8px"><b>Критерии:</b> ${v.criteria.map(c => escapeHtml(c.name)).join(', ')}</p>` : ''}
        <div style="display:flex;gap:8px;margin-top:8px">
          <button class="btn btn-primary" onclick="evalActivate('${v.id}')">Активировать</button>
          <button class="btn btn-secondary" onclick="evalDeleteVersion('${v.id}')">Отклонить</button>
        </div>
      </div>`;
  } catch (err) {
    showToast('Ошибка: ' + err.message, 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Переработать промпт';
  }
}

async function evalActivate(vid) {
  try {
    await api(`/api/eval-profiles/versions/${vid}/activate`, { method: 'PATCH' });
    showToast('Версия активирована');
    renderEvaluation();
  } catch (err) { showToast('Ошибка: ' + err.message, 'error'); }
}

async function evalDeleteVersion(vid) {
  if (!confirm('Удалить версию?')) return;
  try {
    await api(`/api/eval-profiles/versions/${vid}`, { method: 'DELETE' });
    showToast('Версия удалена');
    renderEvaluation();
  } catch (err) { showToast('Ошибка: ' + err.message, 'error'); }
}

// Pull a numeric AmoCRM lead id out of URL or pasted digits.
// Recognised: amocrm.ru/leads/detail/<id>, /leads/<id>, ?id=<id>, or a bare digit string ≥5 chars.
function _extractLeadIdFromQuery(q) {
  if (!q) return null;
  const s = String(q).trim();
  const url = s.match(/\/leads\/(?:detail\/)?(\d+)/i);
  if (url) return parseInt(url[1], 10);
  const idParam = s.match(/[?&]id=(\d+)/i);
  if (idParam) return parseInt(idParam[1], 10);
  if (/^\d{5,}$/.test(s)) return parseInt(s, 10);
  return null;
}

async function _postLinkLead(sessionId, leadId) {
  await api(`/api/sessions/${sessionId}/link-lead`, {
    method: 'POST',
    body: JSON.stringify({ lead_id: leadId }),
  });
}

async function linkLeadModal(sessionId) {
  if (!moduleOn('amocrm')) return;  // AmoCRM-only; кнопка скрыта при выключенном модуле (defense-in-depth)
  // Look up current lead from cached call data, if any.
  let currentLeadId = null;
  if (_currentCallData && _currentCallData.session && _currentCallData.session.id === sessionId) {
    currentLeadId = (_currentCallData.session.metadata || {}).lead_id || null;
  }

  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal-content modal-content--wide">
        <div class="modal-head">
          <h3>Привязка к лиду AmoCRM</h3>
          <p class="modal-hint">Вставьте ссылку на сделку (https://…amocrm.ru/leads/detail/12345) или ищите по имени / телефону / email. После привязки оценка будет пересчитана с учётом истории по клиенту, а в карточку сделки уйдут заметки со ссылкой, выжимкой и планом.</p>
          ${currentLeadId ? `
            <div class="link-current">
              Текущая привязка: ${moduleOn('amocrm') ? `<a href="https://${amoBase()}/leads/detail/${currentLeadId}" target="_blank" rel="noopener">сделка #${currentLeadId}</a>` : `сделка #${currentLeadId}`}
              <button type="button" id="leadUnlink" class="link-action">Отвязать</button>
            </div>
          ` : ''}
        </div>
        <div style="padding:8px 24px 0">
          <input id="leadSearchInput" type="text" placeholder="Ссылка на сделку, имя, телефон или email…" autocomplete="off"
                 style="width:100%;padding:10px 12px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text-primary);font-size:14px;outline:none">
        </div>
        <div class="lead-search-list" id="leadSearchResults">
          <div class="muted" style="padding:24px;text-align:center;font-size:13px">Начните вводить запрос…</div>
        </div>
        <div class="modal-foot">
          <button type="button" id="leadCancel" class="btn btn-secondary">Отмена</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    const input = overlay.querySelector('#leadSearchInput');
    const results = overlay.querySelector('#leadSearchResults');
    let searchSeq = 0;
    let debounceTimer = null;
    const close = (val) => { overlay.remove(); resolve(val); };

    function escapeAttr(s) { return String(s).replace(/"/g, '&quot;'); }

    async function linkAndClose(leadId, label) {
      if (!confirm(`Привязать сессию к ${label} и переоценить с учётом истории?`)) return;
      try {
        await _postLinkLead(sessionId, leadId);
        showToast('Привязано — переоценка запущена (~1 мин). Обновите страницу позже.');
        close({ lead_id: leadId });
      } catch (err) {
        showToast('Не удалось привязать: ' + err.message, 'error');
      }
    }

    function renderDirectId(leadId) {
      results.innerHTML = `
        <button type="button" class="lead-result" data-lead-id="${leadId}">
          <div class="lead-result-main">
            <span class="lead-result-name">Сделка по ссылке</span>
            <span class="lead-result-id">#${leadId}</span>
          </div>
          <div class="lead-result-sub">
            <span>Открыть в AmoCRM:&nbsp;</span>
            ${moduleOn('amocrm') ? `<a href="https://${amoBase()}/leads/detail/${leadId}" target="_blank" rel="noopener" onclick="event.stopPropagation()">https://${amoBase()}/leads/detail/${leadId}</a>` : `#${leadId}`}
          </div>
        </button>`;
      results.querySelector('.lead-result').addEventListener('click', () => linkAndClose(leadId, `сделке #${leadId}`));
    }

    function renderItems(items) {
      if (!items.length) {
        results.innerHTML = '<div class="muted" style="padding:24px;text-align:center;font-size:13px">Ничего не нашлось</div>';
        return;
      }
      results.innerHTML = items.map(it => `
        <button type="button" class="lead-result" data-lead-id="${it.lead_id}" data-lead-name="${escapeAttr(it.lead_name)}">
          <div class="lead-result-main">
            <span class="lead-result-name">${escapeHtml(it.lead_name || '(без имени)')}</span>
            <span class="lead-result-id">#${it.lead_id}</span>
          </div>
          <div class="lead-result-sub">
            ${it.contact_name ? `<span>${escapeHtml(it.contact_name)}</span>` : ''}
            ${it.phone ? `<span class="lead-result-phone">${escapeHtml(it.phone)}</span>` : ''}
            ${it.price != null ? `<span class="lead-result-price">${Number(it.price).toLocaleString('ru-RU')} ₽</span>` : ''}
          </div>
        </button>
      `).join('');
      results.querySelectorAll('.lead-result').forEach(el => {
        el.addEventListener('click', () => {
          const leadId = parseInt(el.dataset.leadId, 10);
          const leadName = el.dataset.leadName || `#${leadId}`;
          linkAndClose(leadId, `лиду «${leadName}» (#${leadId})`);
        });
      });
    }

    async function runSearch(q) {
      const trimmed = (q || '').trim();
      // Direct lead id from URL or digits — skip the search call.
      const directId = _extractLeadIdFromQuery(trimmed);
      if (directId) {
        renderDirectId(directId);
        return;
      }
      if (trimmed.length < 2) {
        results.innerHTML = '<div class="muted" style="padding:24px;text-align:center;font-size:13px">Введите минимум 2 символа или вставьте ссылку на сделку</div>';
        return;
      }
      const seq = ++searchSeq;
      results.innerHTML = '<div class="muted" style="padding:24px;text-align:center;font-size:13px">Ищу…</div>';
      try {
        const data = await api('/api/amocrm/search-leads?q=' + encodeURIComponent(trimmed));
        if (seq !== searchSeq) return; // stale response
        renderItems(data.items || []);
      } catch (err) {
        if (seq !== searchSeq) return;
        results.innerHTML = `<div class="muted" style="padding:24px;text-align:center;font-size:13px;color:var(--danger)">Ошибка: ${escapeHtml(err.message)}</div>`;
      }
    }

    input.addEventListener('input', () => {
      clearTimeout(debounceTimer);
      // Direct id resolves instantly without waiting for debounce.
      const directId = _extractLeadIdFromQuery(input.value);
      if (directId) {
        renderDirectId(directId);
        return;
      }
      debounceTimer = setTimeout(() => runSearch(input.value), 300);
    });
    input.focus();

    const unlinkBtn = overlay.querySelector('#leadUnlink');
    if (unlinkBtn) {
      unlinkBtn.onclick = async () => {
        if (!confirm('Отвязать лид? Заметки в AmoCRM будут удалены.')) return;
        try {
          await api(`/api/sessions/${sessionId}/link-lead`, {
            method: 'POST',
            body: JSON.stringify({ lead_id: null }),
          });
          showToast('Отвязано, заметки в AmoCRM удалены.');
          close({ lead_id: null });
        } catch (err) {
          showToast('Не удалось отвязать: ' + err.message, 'error');
        }
      };
    }

    overlay.querySelector('#leadCancel').onclick = () => close(null);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(null); });
    document.addEventListener('keydown', function onKey(e) {
      if (e.key === 'Escape') {
        document.removeEventListener('keydown', onKey);
        close(null);
      }
    });
  });
}

const KIND_BADGE = {
  evaluation: { label: 'Оценка', cls: 'tpl-kind-evaluation' },
  extraction: { label: 'Извлечение', cls: 'tpl-kind-extraction' },
};

function pickTemplateModal(templates, opts = {}) {
  const title = opts.title || 'Выберите шаблон';
  const hint = opts.hint || '';
  const confirmLabel = opts.confirmLabel || 'Подтвердить';

  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    const cards = templates.map((t, i) => {
      const badge = KIND_BADGE[t.kind] || { label: t.kind || '', cls: '' };
      const desc = t.description ? `<div class="tpl-card-desc">${escapeHtml(t.description)}</div>` : '';
      return `
        <button type="button" class="tpl-card tpl-card--pick" data-index="${i}">
          <div class="tpl-card-head">
            <span class="tpl-card-name">${escapeHtml(t.name)}</span>
            <span class="tpl-kind ${badge.cls}">${escapeHtml(badge.label)}</span>
          </div>
          ${desc}
        </button>`;
    }).join('');

    overlay.innerHTML = `
      <div class="modal-content modal-content--wide">
        <div class="modal-head">
          <h3>${escapeHtml(title)}</h3>
          ${hint ? `<p class="modal-hint">${escapeHtml(hint)}</p>` : ''}
        </div>
        <div class="tpl-card-list">${cards}</div>
        <div class="modal-foot">
          <button type="button" id="tplCancel" class="btn btn-secondary">Отмена</button>
          <button type="button" id="tplConfirm" class="btn btn-primary" disabled>${escapeHtml(confirmLabel)}</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    let selected = -1;
    const confirmBtn = overlay.querySelector('#tplConfirm');
    const close = (val) => { overlay.remove(); resolve(val); };

    overlay.querySelectorAll('.tpl-card').forEach((el) => {
      el.addEventListener('click', () => {
        selected = parseInt(el.dataset.index, 10);
        overlay.querySelectorAll('.tpl-card').forEach((x) => x.classList.remove('selected'));
        el.classList.add('selected');
        confirmBtn.disabled = false;
      });
      el.addEventListener('dblclick', () => {
        const i = parseInt(el.dataset.index, 10);
        close(templates[i] || null);
      });
    });

    overlay.querySelector('#tplCancel').onclick = () => close(null);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(null); });
    confirmBtn.onclick = () => close(selected >= 0 ? templates[selected] : null);
    document.addEventListener('keydown', function onKey(e) {
      if (e.key === 'Escape') {
        document.removeEventListener('keydown', onKey);
        close(null);
      }
    });
  });
}

async function relinkExtraction(extractionId) {
  let items = [];
  try { items = await api('/api/complexes?limit=200'); } catch { return showToast('Не удалось загрузить список ЖК', 'error'); }
  if (!items.length) return showToast('В базе нет ЖК — пока некуда перепривязывать', 'error');
  const choice = prompt('Привязать к ЖК — введите имя:\n\n' + items.map(c => `• ${c.name} (${c.developer || '?'})`).join('\n'));
  if (!choice) return;
  const target = items.find(c => c.name === choice.trim());
  if (!target) return showToast('ЖК не найден', 'error');
  try {
    await api(`/api/extractions/${extractionId}/relink`, {
      method: 'POST',
      body: JSON.stringify({ complex_id: target.id }),
    });
    showToast('Перепривязано');
    location.reload();
  } catch (err) {
    showToast('Ошибка: ' + err.message, 'error');
  }
}

// ============================================
// PAGE: Complexes (knowledge base)
// ============================================
let complexFilters = { developer: '', class_: '', district: '', q: '' };

async function renderComplexes() {
  showLoading();
  try {
    const params = new URLSearchParams();
    if (complexFilters.developer) params.set('developer', complexFilters.developer);
    if (complexFilters.class_) params.set('class_', complexFilters.class_);
    if (complexFilters.district) params.set('district', complexFilters.district);
    if (complexFilters.q) params.set('q', complexFilters.q);
    const items = await api('/api/complexes?' + params.toString());

    app.innerHTML = `
      <div class="page-header"><h1>База ЖК</h1><p>${items.length} профилей</p></div>
      <div class="table-filters" style="margin-bottom:16px">
        <input class="table-filter" id="cxQ" placeholder="Поиск по имени" value="${escapeHtml(complexFilters.q)}">
        <input class="table-filter" id="cxDev" placeholder="Застройщик" value="${escapeHtml(complexFilters.developer)}">
        <input class="table-filter" id="cxCls" placeholder="Класс (бизнес/комфорт/...)" value="${escapeHtml(complexFilters.class_)}">
        <input class="table-filter" id="cxDist" placeholder="Район" value="${escapeHtml(complexFilters.district)}">
        <button class="btn btn-secondary btn-sm" onclick="applyComplexFilters()">Применить</button>
      </div>
      ${items.length === 0
        ? '<div class="empty-state"><p>Профилей пока нет — обработай презентацию через шаблон «Презентация ЖК».</p></div>'
        : '<div class="cards-grid">' + items.map(c => `
          <a class="complex-card" href="#complex/${c.id}">
            <h3>${escapeHtml(c.name)}</h3>
            <div class="complex-card-sub">
              ${c.class ? `<span class="badge badge-source-amocrm">${escapeHtml(c.class)}</span>` : ''}
              <span class="muted">${escapeHtml(c.developer || '')}</span>
            </div>
            <div class="muted">${escapeHtml(c.district || '')}</div>
            <div class="muted" style="margin-top:8px">${c.sources_count} источник${c.sources_count === 1 ? '' : (c.sources_count >= 2 && c.sources_count <= 4 ? 'а' : 'ов')}</div>
          </a>
        `).join('') + '</div>'}
    `;

    const apply = () => applyComplexFilters();
    ['cxQ','cxDev','cxCls','cxDist'].forEach(id => {
      const el = $(`#${id}`);
      if (el) el.addEventListener('keydown', e => { if (e.key === 'Enter') apply(); });
    });
  } catch (err) {
    app.innerHTML = `<div class="empty-state"><p>Ошибка: ${escapeHtml(err.message)}</p></div>`;
  }
}

function applyComplexFilters() {
  complexFilters = {
    q:        $('#cxQ').value.trim(),
    developer: $('#cxDev').value.trim(),
    class_:   $('#cxCls').value.trim(),
    district: $('#cxDist').value.trim(),
  };
  renderComplexes();
}

async function renderComplexDetail(id) {
  showLoading();
  try {
    const c = await api(`/api/complexes/${id}`);
    app.innerHTML = `
      <a class="back-link" href="#complexes">← К списку ЖК</a>
      <div class="page-header">
        <h1>${escapeHtml(c.name)}</h1>
        <p>${escapeHtml(c.developer || '')} ${c.class ? '· ' + escapeHtml(c.class) : ''} ${c.district ? '· ' + escapeHtml(c.district) : ''}</p>
      </div>
      ${isAdmin() ? `
      <div style="margin-bottom:16px;display:flex;gap:8px">
        <button class="btn btn-secondary btn-sm" onclick="renameComplex('${c.id}', ${JSON.stringify(c.name)})">Переименовать</button>
        <button class="btn btn-danger btn-sm" onclick="deleteComplex('${c.id}', ${JSON.stringify(c.name)})">Удалить профиль</button>
      </div>` : ''}
      <div class="cx-profile">
        ${renderComplexProfile(c.aggregated_data)}
      </div>
      <h2 style="margin-top:24px">Источники</h2>
      <ul>
        ${c.sources.map(s => `<li><a href="#call/${s.session_id}">Запись от ${formatDate(s.created_at)}</a></li>`).join('')}
      </ul>
    `;
  } catch (err) {
    app.innerHTML = `<div class="empty-state"><p>Ошибка: ${escapeHtml(err.message)}</p></div>`;
  }
}

async function renameComplex(id, currentName) {
  const newName = prompt('Новое имя ЖК:', currentName);
  if (!newName || newName === currentName) return;
  try {
    await api(`/api/complexes/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ name: newName }),
    });
    showToast('Переименовано');
    renderComplexDetail(id);
  } catch (err) {
    showToast('Ошибка: ' + err.message, 'error');
  }
}

async function deleteComplex(id, name) {
  if (!confirm(`Удалить профиль "${name}"? Сами записи останутся, но будут отвязаны.`)) return;
  try {
    await api(`/api/complexes/${id}`, { method: 'DELETE' });
    showToast('Удалено');
    navigate('complexes');
  } catch (err) {
    showToast('Ошибка: ' + err.message, 'error');
  }
}

// ============================================
// PAGE: Team (admin only)
// ============================================
async function renderTeam() {
  showLoading();
  const appEl = document.getElementById('app');
  let users;
  try {
    users = await api('/api/users');
  } catch (err) {
    appEl.innerHTML = `<div class="empty-state"><p>Ошибка загрузки: ${escapeHtml(err.message)}</p></div>`;
    return;
  }
  let knownNames = [];
  try { knownNames = (await api('/api/managers')).map(m => m.name); } catch (e) {}
  const options = knownNames.map(n => `<option value="${escapeHtml(n)}">`).join('');
  appEl.innerHTML = `
    <h2>Команда</h2>
    <datalist id="known-employees">${options}</datalist>
    <table>
      <tr><th>Email</th><th>Роль</th><th>Сотрудник (из рекордера)</th><th></th></tr>
      ${users.map(u => `
        <tr>
          <td>${escapeHtml(u.email)}</td>
          <td>
            <select data-role-for="${u.id}">
              ${['admin','viewer','manager'].map(r =>
                `<option value="${r}" ${u.role===r?'selected':''}>${r}</option>`).join('')}
            </select>
          </td>
          <td><input list="known-employees" data-emp-for="${u.id}"
                     value="${escapeHtml(u.employee_name||'')}" placeholder="—"></td>
          <td>
            <button data-save="${u.id}">Сохранить</button>
            <button data-reset="${u.id}">Сбросить пароль</button>
            <button data-del="${u.id}">Удалить</button>
          </td>
        </tr>`).join('')}
    </table>
    <h3>Добавить юзера</h3>
    <form id="team-add">
      <input type="email" id="team-email" placeholder="Email" required>
      <select id="team-role">
        <option value="viewer">viewer</option>
        <option value="admin">admin</option>
        <option value="manager">manager</option>
      </select>
      <input list="known-employees" id="team-emp" placeholder="Сотрудник (для manager)">
      <button type="submit">Создать</button>
    </form>
    <div id="team-secret"></div>`;
  appEl.querySelector('#team-add').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = { email: appEl.querySelector('#team-email').value,
                   role: appEl.querySelector('#team-role').value };
    const emp = appEl.querySelector('#team-emp').value.trim();
    if (emp) body.employee_name = emp;
    try {
      const r = await api('/api/users', { method: 'POST', body: JSON.stringify(body) });
      const newEmail = r.email, newPwd = r.password;
      await renderTeam();
      document.getElementById('app').querySelector('#team-secret').innerHTML =
        `Пароль для ${escapeHtml(newEmail)} (покажем один раз): <code>${escapeHtml(newPwd)}</code>`;
    } catch (e) { showToast(e.message, 'error'); }
  });
  appEl.querySelectorAll('button[data-save]').forEach(b => b.onclick = async () => {
    const id = b.dataset.save;
    try {
      await api(`/api/users/${id}`, { method: 'PATCH', body: JSON.stringify({
        role: appEl.querySelector(`[data-role-for="${id}"]`).value,
        employee_name: appEl.querySelector(`[data-emp-for="${id}"]`).value.trim(),
      })});
      showToast('Сохранено');
    } catch (e) { showToast(e.message, 'error'); }
  });
  appEl.querySelectorAll('button[data-reset]').forEach(b => b.onclick = async () => {
    try {
      const r = await api(`/api/users/${b.dataset.reset}/reset-password`, { method: 'POST' });
      appEl.querySelector('#team-secret').innerHTML =
        `Новый пароль (покажем один раз): <code>${escapeHtml(r.password)}</code>`;
    } catch (e) { showToast(e.message, 'error'); }
  });
  appEl.querySelectorAll('button[data-del]').forEach(b => b.onclick = async () => {
    if (!confirm('Удалить юзера?')) return;
    try { await api(`/api/users/${b.dataset.del}`, { method: 'DELETE' }); await renderTeam(); }
    catch (e) { showToast(e.message, 'error'); }
  });
}

// ============================================
// PAGE: Profile (all roles)
// ============================================
async function renderProfile() {
  const appEl = document.getElementById('app');
  appEl.innerHTML = `
    <h2>Профиль</h2>
    <div>${escapeHtml(currentUser.email)} (${escapeHtml(currentUser.role)})</div>
    <h3>Сменить пароль</h3>
    <form id="pw-form">
      <input type="password" id="pw-old" placeholder="Текущий пароль" required>
      <input type="password" id="pw-new" placeholder="Новый пароль (мин. 8)" required minlength="8">
      <button type="submit">Сменить</button>
    </form>`;
  appEl.querySelector('#pw-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
      await api('/api/user-auth/change-password', { method: 'POST', body: JSON.stringify({
        old_password: appEl.querySelector('#pw-old').value,
        new_password: appEl.querySelector('#pw-new').value,
      })});
      showToast('Пароль изменён; остальные сессии разлогинены');
      ev.target.reset();
    } catch (e) {
      showToast(e.message === 'wrong_password' ? 'Неверный текущий пароль' : e.message, 'error');
    }
  });
}
