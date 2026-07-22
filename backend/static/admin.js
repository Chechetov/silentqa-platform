// Админ-SPA платформы. Все запросы — на свой origin (admin.silentqa.com).
const $ = (sel) => document.querySelector(sel);

function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...opts,
  });
  if (res.status === 401) {
    const body = await res.clone().json().catch(() => ({}));
    if (body.detail === 'platform_auth_required') { showLogin(); throw new Error('auth'); }
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

function showLogin() {
  $('#pa-login').classList.remove('pa-hidden');
  $('#pa-main').classList.add('pa-hidden');
}

function showMain(email) {
  $('#pa-login').classList.add('pa-hidden');
  $('#pa-main').classList.remove('pa-hidden');
  $('#pa-whoami').textContent = email;
}

async function boot() {
  try {
    const me = await api('/api/platform/auth/me');
    showMain(me.email);
  } catch (e) { return; /* showLogin уже вызван внутри api() при 401 */ }
  await loadTenants();
}

$('#pa-login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('#pa-login-error').textContent = '';
  try {
    const me = await api('/api/platform/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email: $('#pa-email').value, password: $('#pa-password').value }),
    });
    showMain(me.email);
    await loadTenants();
  } catch (e) {
    $('#pa-login-error').textContent = e.message === 'too_many_attempts'
      ? 'Слишком много попыток — подожди' : 'Неверный email или пароль';
  }
});

$('#pa-logout').addEventListener('click', async () => {
  await api('/api/platform/auth/logout', { method: 'POST' }).catch(() => {});
  showLogin();
});

async function loadTenants() {
  const tenants = await api('/api/platform/tenants');
  const rows = tenants.map((t) => `
    <tr>
      <td><a href="#" data-slug="${escapeHtml(t.slug)}" class="pa-open">${escapeHtml(t.slug)}</a></td>
      <td>${escapeHtml(t.display_name || '')}</td>
      <td>${t.status === 'suspended' ? '<span class="pa-badge-suspended">suspended</span>' : 'active'}</td>
      <td>${t.sessions_count}</td>
      <td>${t.minutes} мин</td>
      <td>${t.last_activity ? t.last_activity.slice(0, 16).replace('T', ' ') : '—'}</td>
    </tr>`).join('');
  $('#pa-tenants').innerHTML = `
    <tr><th>Клиент</th><th>Название</th><th>Статус</th>
        <th>Сессий (месяц)</th><th>Минут</th><th>Активность</th></tr>${rows}`;
  document.querySelectorAll('.pa-open').forEach((a) =>
    a.addEventListener('click', (ev) => { ev.preventDefault(); openTenant(a.dataset.slug); }));
}

$('#pa-show-create').addEventListener('click', () =>
  $('#pa-create-form').classList.toggle('pa-hidden'));

$('#pa-create-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('#pa-create-error').textContent = '';
  try {
    const res = await api('/api/platform/tenants', {
      method: 'POST',
      body: JSON.stringify({
        slug: $('#pa-new-slug').value.trim(),
        display_name: $('#pa-new-name').value.trim(),
        admin_email: $('#pa-new-email').value.trim(),
      }),
    });
    $('#pa-res-password').textContent = res.admin_password;
    $('#pa-res-key').textContent = res.api_key;
    $('#pa-create-result').classList.remove('pa-hidden');
    await loadTenants();
  } catch (e) { $('#pa-create-error').textContent = e.message; }
});

async function openTenant(slug) {
  const users = await api(`/api/platform/tenants/${slug}/users`);
  const userRows = users.map((u) => `
    <tr>
      <td>${escapeHtml(u.email)}</td><td>${escapeHtml(u.role)}</td><td>${escapeHtml(u.employee_name || '—')}</td>
      <td class="pa-row-actions">
        <button data-act="reset" data-id="${u.id}">Сбросить пароль</button>
        <button data-act="del" data-id="${u.id}" class="pa-danger">Удалить</button>
      </td>
    </tr>`).join('');
  const card = $('#pa-tenant-card');
  card.classList.remove('pa-hidden');
  card.innerHTML = `
    <h3>${escapeHtml(slug)}</h3>
    <div class="pa-row-actions">
      <button id="pa-imp">Открыть дашборд (режим поддержки)</button>
      <button id="pa-suspend">Suspend</button>
      <button id="pa-activate">Activate</button>
      <button id="pa-rotate" class="pa-danger">Ротация API-ключа</button>
    </div>
    <div id="pa-card-secret"></div>
    <h4>Юзеры</h4>
    <table class="pa-table">${userRows}</table>`;
  $('#pa-imp').onclick = async () => {
    const r = await api(`/api/platform/tenants/${slug}/impersonate`, { method: 'POST' });
    window.open(r.url, '_blank');
  };
  $('#pa-suspend').onclick = async () => {
    if (!confirm(`Приостановить ${slug}? Дашборд и приём записей перестанут работать.`)) return;
    await api(`/api/platform/tenants/${slug}`, { method: 'PATCH', body: JSON.stringify({ status: 'suspended' }) });
    await loadTenants(); await openTenant(slug);
  };
  $('#pa-activate').onclick = async () => {
    await api(`/api/platform/tenants/${slug}`, { method: 'PATCH', body: JSON.stringify({ status: 'active' }) });
    await loadTenants(); await openTenant(slug);
  };
  $('#pa-rotate').onclick = async () => {
    if (!confirm('Старый ключ перестанет работать во ВСЕХ рекордерах клиента. Продолжить?')) return;
    const r = await api(`/api/platform/tenants/${slug}/rotate-key`, { method: 'POST' });
    $('#pa-card-secret').innerHTML =
      `Новый API-ключ (сохрани, больше не покажем): <span class="pa-secret">${escapeHtml(r.api_key)}</span>`;
  };
  card.querySelectorAll('button[data-act]').forEach((b) => {
    b.onclick = async () => {
      if (b.dataset.act === 'reset') {
        const r = await api(`/api/platform/tenants/${slug}/users/${b.dataset.id}/reset-password`, { method: 'POST' });
        $('#pa-card-secret').innerHTML =
          `Новый пароль (сохрани, больше не покажем): <span class="pa-secret">${escapeHtml(r.password)}</span>`;
      } else if (b.dataset.act === 'del') {
        if (!confirm('Удалить юзера?')) return;
        await api(`/api/platform/tenants/${slug}/users/${b.dataset.id}`, { method: 'DELETE' });
        await openTenant(slug);
      }
    };
  });
}

boot();
