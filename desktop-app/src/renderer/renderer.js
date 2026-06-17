// UI controller for Call Recorder Desktop

// --- Global error handlers — show errors on screen ---
window.onerror = (msg, src, line) => {
  const el = document.createElement('div');
  el.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#dc2626;color:#fff;padding:10px;font-size:12px;z-index:9999;font-family:monospace';
  el.textContent = 'ERROR: ' + msg + ' (' + src + ':' + line + ')';
  document.body.appendChild(el);
};
window.onunhandledrejection = (e) => {
  const el = document.createElement('div');
  el.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#dc2626;color:#fff;padding:10px;font-size:12px;z-index:9999;font-family:monospace';
  el.textContent = 'UNHANDLED: ' + (e.reason?.message || e.reason || 'unknown');
  document.body.appendChild(el);
};
console.log('[renderer] Script loaded');

// --- Setup screen elements ---
const setupScreen = document.getElementById('setupScreen');
const loginScreen = document.getElementById('loginScreen');
const recorderScreen = document.getElementById('recorderScreen');
const setupServerUrl = document.getElementById('setupServerUrl');
const setupUsername = document.getElementById('setupUsername');
const setupPassword = document.getElementById('setupPassword');
const setupApiKey = document.getElementById('setupApiKey');
const setupEmployee = document.getElementById('setupEmployee');
const btnTestConnection = document.getElementById('btnTestConnection');
const testConnectionResult = document.getElementById('testConnectionResult');
const btnSaveSetup = document.getElementById('btnSaveSetup');

// --- Login screen elements ---
const tabLogin = document.getElementById('tabLogin');
const tabClaim = document.getElementById('tabClaim');
const loginTabPane = document.getElementById('loginTabPane');
const claimTabPane = document.getElementById('claimTabPane');
const loginEmail = document.getElementById('loginEmail');
const loginPassword = document.getElementById('loginPassword');
const btnLogin = document.getElementById('btnLogin');
const loginError = document.getElementById('loginError');
const claimPhase1 = document.getElementById('claimPhase1');
const claimPhase2 = document.getElementById('claimPhase2');
const claimEmail = document.getElementById('claimEmail');
const btnClaimStart = document.getElementById('btnClaimStart');
const claimStartError = document.getElementById('claimStartError');
const claimName = document.getElementById('claimName');
const claimMaskedEmail = document.getElementById('claimMaskedEmail');
const claimPassword = document.getElementById('claimPassword');
const claimPasswordConfirm = document.getElementById('claimPasswordConfirm');
const btnClaimComplete = document.getElementById('btnClaimComplete');
const btnClaimBack = document.getElementById('btnClaimBack');
const claimCompleteError = document.getElementById('claimCompleteError');
const brokerInfo = document.getElementById('brokerInfo');
const btnLogout = document.getElementById('btnLogout');
const appointmentTypeRow = document.getElementById('appointmentTypeRow');
const appointmentType = document.getElementById('appointmentType');

// --- Recorder screen elements ---
const btnStart = document.getElementById('btnStart');
const btnStop = document.getElementById('btnStop');
const btnReset = document.getElementById('btnReset');
const btnMinimize = document.getElementById('btnMinimize');
const btnClose = document.getElementById('btnClose');
const statusBadge = document.getElementById('statusBadge');
const statusLabel = document.getElementById('statusLabel');
const timerBlock = document.getElementById('timerBlock');
const timerText = document.getElementById('timerText');
const chunksText = document.getElementById('chunksText');
const headphoneHint = document.getElementById('headphoneHint');
const infoPanel = document.getElementById('infoPanel');
const sessionLink = document.getElementById('sessionLink');
const resultRow = document.getElementById('resultRow');
const resultLink = document.getElementById('resultLink');
const processingEta = document.getElementById('processingEta');
const errorText = document.getElementById('errorText');
const settingsToggle = document.getElementById('settingsToggle');
const settingsPanel = document.getElementById('settingsPanel');
const inputServerUrl = document.getElementById('inputServerUrl');
const inputUsername = document.getElementById('inputUsername');
const inputPassword = document.getElementById('inputPassword');
const inputApiKey = document.getElementById('inputApiKey');
const inputEmployee = document.getElementById('inputEmployee');
const btnSaveSettings = document.getElementById('btnSaveSettings');
const settingsSaved = document.getElementById('settingsSaved');

let timerInterval = null;
let recordingStartedAt = null;
let recordingEndedAt = null;
let currentStatus = 'idle';
let currentSessionId = null;
let currentChunks = 0;
let currentPending = 0;
let currentOnline = true;
let currentError = null;
let serverUrl = '';
let username = '';
let password = '';
let apiKey = '';
let employee = '';
let brokerJwt = '';
let brokerName = '';
let brokerEmail = '';
let claimEmailValue = '';
let recoveryStarted = false;

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function basicAuthHeader() {
  return username ? 'Basic ' + btoa(username + ':' + password) : '';
}

// API-key mode: тенант без AmoCRM-брокеров (fulldent) — ключ есть, broker-логин
// не нужен. realestate вводит username/password → broker-mode, поток не меняется.
// Реализация живёт в mode.js (подключается <script> перед renderer.js) и
// разделяется с Node-юнитом; здесь — тонкая обёртка с резервом на случай,
// если mode.js не загрузился.
function isApiKeyMode(c) {
  if (typeof window !== 'undefined' && typeof window.isApiKeyMode === 'function') {
    return window.isApiKeyMode(c);
  }
  return !!(c && c.apiKey && !c.username);
}

function showInlineError(el, msg) {
  if (!el) return;
  el.textContent = msg;
  el.className = 'test-result error';
  show(el);
}

function clearInlineError(el) {
  if (!el) return;
  hide(el);
  el.textContent = '';
}

function show(el) { if (el) el.style.display = ''; }
function hide(el) { if (el) el.style.display = 'none'; }

// ===== SETUP SCREEN =====

function showSetupScreen() {
  show(setupScreen);
  hide(loginScreen);
  hide(recorderScreen);
}

function showLoginScreen() {
  hide(setupScreen);
  show(loginScreen);
  hide(recorderScreen);
  // Default to login tab when entering screen
  switchLoginTab('login');
}

function showRecorderScreen() {
  hide(setupScreen);
  hide(loginScreen);
  show(recorderScreen);

  // API-key mode (fulldent и пр.): нет брокера — показываем «Сотрудник» вместо
  // «Брокер», шлём API-ключ + имя сотрудника, broker-токен очищаем.
  if (isApiKeyMode({ apiKey, username })) {
    hide(brokerInfo);
    if (employee) {
      brokerInfo.innerHTML = '';
      const label = document.createElement('span');
      label.textContent = 'Сотрудник: ';
      const b = document.createElement('b');
      b.textContent = employee;
      brokerInfo.appendChild(label);
      brokerInfo.appendChild(b);
      show(brokerInfo);
    }
    if (window.Recorder && window.Recorder.setBrokerToken) window.Recorder.setBrokerToken('');
    if (window.Recorder && window.Recorder.setEmployee) window.Recorder.setEmployee(employee);
    if (window.Recorder && window.Recorder.setApiKey) window.Recorder.setApiKey(apiKey);
    if (btnLogout) hide(btnLogout);
    // Per-recording appointment-type chooser — populated from the tenant's
    // scenarios. Failure-safe: never blocks recording.
    loadAppointmentTypes().catch(() => {});
    if (!recoveryStarted) {
      recoveryStarted = true;
      runRecovery().catch(() => {});
    }
    return;
  }

  if (brokerName) {
    brokerInfo.innerHTML = '';
    const label = document.createElement('span');
    label.textContent = 'Брокер: ';
    const b = document.createElement('b');
    b.textContent = brokerName;
    brokerInfo.appendChild(label);
    brokerInfo.appendChild(b);
    if (brokerEmail) {
      const email = document.createElement('span');
      email.textContent = ' · ' + brokerEmail;
      brokerInfo.appendChild(email);
    }
    show(brokerInfo);
  } else {
    hide(brokerInfo);
  }

  // Wire the recorder's broker token and API key now that we have a valid JWT.
  if (window.Recorder && typeof window.Recorder.setBrokerToken === 'function') {
    window.Recorder.setBrokerToken(brokerJwt);
  }
  if (window.Recorder && typeof window.Recorder.setApiKey === 'function') {
    window.Recorder.setApiKey(apiKey);
  }

  // Recovery only kicks in once we're past auth — see init() comment.
  if (!recoveryStarted) {
    recoveryStarted = true;
    runRecovery().catch((err) => console.warn('[recovery] failed:', err));
  }
}

// Populate the per-recording appointment-type chooser from the tenant's
// scenarios. Generic — scenario ids/names come entirely from the server
// (GET /api/tenancy/features, resolved by Host, no auth). Called only in
// API-key mode. Failure-safe: any error / missing scenarios leaves the row
// hidden and sets no appointment type (backend falls back to default
// scenario — RE-safe, unchanged behavior).
async function loadAppointmentTypes() {
  if (!appointmentTypeRow || !appointmentType || !serverUrl) return;
  let scenarios = null;
  try {
    const resp = await fetch(serverUrl + '/api/tenancy/features');
    if (!resp.ok) return;
    const features = await resp.json();
    scenarios = features && features.scenarios;
  } catch (_) {
    // Network error / server down — leave row hidden, record without a type.
    return;
  }
  if (!Array.isArray(scenarios) || scenarios.length === 0) return;

  appointmentType.innerHTML = '';
  for (const s of scenarios) {
    if (!s || !s.id) continue;
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.name || s.id;
    appointmentType.appendChild(opt);
  }
  if (appointmentType.options.length === 0) return;

  show(appointmentTypeRow);
  appointmentType.value = appointmentType.options[0].value;
  if (window.Recorder && window.Recorder.setAppointmentType) {
    window.Recorder.setAppointmentType(appointmentType.value);
  }
}

// Bound once (not inside loadAppointmentTypes) so re-entry can't stack
// duplicate listeners.
if (appointmentType) {
  appointmentType.addEventListener('change', () => {
    if (window.Recorder && window.Recorder.setAppointmentType) {
      window.Recorder.setAppointmentType(appointmentType.value);
    }
  });
}

// Test Connection — direct fetch from renderer (no IPC, avoids main process issues)
btnTestConnection.addEventListener('click', async () => {
  try {
    const url = setupServerUrl.value.replace(/\/+$/, '');
    const user = setupUsername.value;
    const pass = setupPassword.value;

    if (!url) {
      testConnectionResult.textContent = 'Please enter a server URL';
      testConnectionResult.className = 'test-result error';
      show(testConnectionResult);
      return;
    }

    btnTestConnection.disabled = true;
    btnTestConnection.textContent = 'Testing...';
    hide(testConnectionResult);
    hide(btnSaveSetup);

    const authHeader = user ? 'Basic ' + btoa(user + ':' + pass) : '';
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);

    const resp = await fetch(url + '/api/sessions', {
      method: 'GET',
      headers: { Authorization: authHeader },
      signal: controller.signal,
    });
    clearTimeout(timeout);

    btnTestConnection.disabled = false;
    btnTestConnection.textContent = 'Test Connection';

    if (resp.ok) {
      testConnectionResult.textContent = 'Connection successful!';
      testConnectionResult.className = 'test-result success';
      show(btnSaveSetup);
    } else if (resp.status === 401 || resp.status === 403) {
      testConnectionResult.textContent = 'Invalid credentials (HTTP ' + resp.status + ')';
      testConnectionResult.className = 'test-result error';
    } else {
      testConnectionResult.textContent = 'Server returned HTTP ' + resp.status;
      testConnectionResult.className = 'test-result error';
    }
    show(testConnectionResult);

  } catch (err) {
    btnTestConnection.disabled = false;
    btnTestConnection.textContent = 'Test Connection';
    testConnectionResult.textContent = 'Error: ' + (err.name === 'AbortError' ? 'Connection timeout (10s)' : err.message);
    testConnectionResult.className = 'test-result error';
    show(testConnectionResult);
  }
});

// Save & Start
btnSaveSetup.addEventListener('click', async () => {
  try {
    serverUrl = setupServerUrl.value.replace(/\/+$/, '');
    username = setupUsername.value;
    password = setupPassword.value;
    apiKey = setupApiKey.value;
    if (setupEmployee) employee = setupEmployee.value;

    await persistCredentials();

    // Sync to settings inputs
    inputServerUrl.value = serverUrl;
    inputUsername.value = username;
    inputPassword.value = password;
    if (inputApiKey) inputApiKey.value = apiKey;
    if (inputEmployee) inputEmployee.value = employee;

    if (isApiKeyMode({ apiKey, username })) {
      // API-key mode: брокер не нужен — сразу к записи.
      showRecorderScreen();
    } else {
      // Server is reachable, but the broker still needs to log in / activate.
      showLoginScreen();
    }
  } catch (err) {
    testConnectionResult.textContent = 'Save error: ' + err.message;
    testConnectionResult.className = 'test-result error';
    show(testConnectionResult);
  }
});

// ===== LOGIN SCREEN =====

async function persistCredentials() {
  await window.electronAPI.credentials.save({
    serverUrl,
    username,
    password,
    apiKey,
    employee,
    brokerJwt,
    brokerName,
    brokerEmail,
  });
}

function switchLoginTab(which) {
  if (which === 'claim') {
    tabClaim.classList.add('active');
    tabLogin.classList.remove('active');
    show(claimTabPane);
    hide(loginTabPane);
  } else {
    tabLogin.classList.add('active');
    tabClaim.classList.remove('active');
    show(loginTabPane);
    hide(claimTabPane);
  }
  // Clear stale errors when switching
  clearInlineError(loginError);
  clearInlineError(claimStartError);
  clearInlineError(claimCompleteError);
}

tabLogin.addEventListener('click', () => switchLoginTab('login'));
tabClaim.addEventListener('click', () => switchLoginTab('claim'));

function showClaimPhase1() {
  show(claimPhase1);
  hide(claimPhase2);
  clearInlineError(claimCompleteError);
  claimPassword.value = '';
  claimPasswordConfirm.value = '';
}

function showClaimPhase2() {
  hide(claimPhase1);
  show(claimPhase2);
  clearInlineError(claimStartError);
}

btnClaimBack.addEventListener('click', () => {
  showClaimPhase1();
});

// --- Login ---
btnLogin.addEventListener('click', async () => {
  clearInlineError(loginError);
  const email = (loginEmail.value || '').trim();
  const pwd = loginPassword.value || '';

  if (!EMAIL_RE.test(email)) {
    showInlineError(loginError, 'Введите корректный email');
    return;
  }
  if (!pwd) {
    showInlineError(loginError, 'Введите пароль');
    return;
  }

  btnLogin.disabled = true;
  try {
    const resp = await fetch(serverUrl + '/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: basicAuthHeader() },
      body: JSON.stringify({ email, password: pwd }),
    });
    if (resp.status === 401) {
      showInlineError(loginError, 'Неверный email или пароль');
      return;
    }
    if (!resp.ok) {
      showInlineError(loginError, 'Ошибка сервера: ' + resp.status);
      return;
    }
    const data = await resp.json();
    await applyBrokerSession(data);
  } catch (err) {
    showInlineError(loginError, 'Ошибка сети: ' + err.message);
  } finally {
    btnLogin.disabled = false;
  }
});

// --- Claim: start (lookup) ---
btnClaimStart.addEventListener('click', async () => {
  clearInlineError(claimStartError);
  const email = (claimEmail.value || '').trim();

  if (!EMAIL_RE.test(email)) {
    showInlineError(claimStartError, 'Введите корректный email');
    return;
  }

  btnClaimStart.disabled = true;
  try {
    const resp = await fetch(serverUrl + '/api/auth/claim/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: basicAuthHeader() },
      body: JSON.stringify({ email }),
    });
    if (resp.status === 404) {
      showInlineError(claimStartError, 'Брокер с таким email не найден. Уточните у руководителя.');
      return;
    }
    if (resp.status === 409) {
      showInlineError(claimStartError, 'Аккаунт уже активирован. Войдите через вкладку «Войти».');
      return;
    }
    if (!resp.ok) {
      showInlineError(claimStartError, 'Ошибка сервера: ' + resp.status);
      return;
    }
    const data = await resp.json();
    claimEmailValue = email;
    claimName.textContent = data.name || '';
    claimMaskedEmail.textContent = data.masked_email || '';
    showClaimPhase2();
  } catch (err) {
    showInlineError(claimStartError, 'Ошибка сети: ' + err.message);
  } finally {
    btnClaimStart.disabled = false;
  }
});

// --- Claim: complete (set password) ---
btnClaimComplete.addEventListener('click', async () => {
  clearInlineError(claimCompleteError);
  const pwd = claimPassword.value || '';
  const pwd2 = claimPasswordConfirm.value || '';

  if (pwd.length < 8) {
    showInlineError(claimCompleteError, 'Пароль должен быть не короче 8 символов');
    return;
  }
  if (pwd !== pwd2) {
    showInlineError(claimCompleteError, 'Пароли не совпадают');
    return;
  }

  btnClaimComplete.disabled = true;
  try {
    const resp = await fetch(serverUrl + '/api/auth/claim/complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: basicAuthHeader() },
      body: JSON.stringify({ email: claimEmailValue, password: pwd }),
    });
    if (resp.status === 409) {
      showInlineError(claimCompleteError, 'Аккаунт уже активирован. Войдите через вкладку «Войти».');
      return;
    }
    if (resp.status === 404) {
      showInlineError(claimCompleteError, 'Брокер не найден или неактивен');
      return;
    }
    if (!resp.ok) {
      showInlineError(claimCompleteError, 'Ошибка сервера: ' + resp.status);
      return;
    }
    const data = await resp.json();
    await applyBrokerSession(data);
  } catch (err) {
    showInlineError(claimCompleteError, 'Ошибка сети: ' + err.message);
  } finally {
    btnClaimComplete.disabled = false;
  }
});

async function applyBrokerSession(data) {
  brokerJwt = data.token || '';
  const broker = data.broker || {};
  brokerName = broker.name || '';
  brokerEmail = broker.email || '';
  await persistCredentials();
  // Reset login form fields so they aren't sitting around in DOM
  loginPassword.value = '';
  claimPassword.value = '';
  claimPasswordConfirm.value = '';
  showRecorderScreen();
}

async function logout({ force = false } = {}) {
  // Refuse to log out mid-recording with chunks pending — they'd lose
  // attribution. The user can stop the recording first. `force=true` is the
  // server-initiated path (401/403 mid-recording) where we have no choice.
  if (!force) {
    const isRec = window.Recorder && window.Recorder.isRecording && window.Recorder.isRecording();
    if (isRec || currentPending > 0) {
      showInlineError(errorText, 'Останови запись прежде чем выйти из аккаунта.');
      return false;
    }
  }
  brokerJwt = '';
  brokerName = '';
  brokerEmail = '';
  if (window.Recorder && typeof window.Recorder.setBrokerToken === 'function') {
    window.Recorder.setBrokerToken('');
  }
  // Allow recovery to run again after the next successful login.
  recoveryStarted = false;
  await persistCredentials();
  hide(settingsPanel);
  showLoginScreen();
  return true;
}

if (btnLogout) {
  btnLogout.addEventListener('click', (e) => {
    e.preventDefault();
    logout().catch((err) => console.warn('logout failed:', err));
  });
}

// ===== RECORDER SCREEN =====

// --- Timer ---
function formatTimer(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

function startTimer() {
  if (timerInterval) return; // already running, don't restart
  function tick() {
    const elapsed = Math.floor((Date.now() - recordingStartedAt) / 1000);
    timerText.textContent = formatTimer(elapsed);
  }
  tick();
  timerInterval = setInterval(tick, 1000);
}

function stopTimer() {
  if (timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
}

function estimateProcessingTime(recordingSeconds) {
  if (recordingSeconds < 60) return '~1-2 min';
  if (recordingSeconds < 180) return '~2-3 min';
  if (recordingSeconds < 600) return '~3-5 min';
  return '~5-10 min';
}

// --- UI Update ---
function updateUI() {
  const isRec = currentStatus === 'recording' || currentStatus === 'connecting';
  const isDone = currentStatus === 'processing' || currentStatus === 'completed';
  const isError = currentStatus === 'error';

  // Buttons
  if (isRec) {
    hide(btnStart);
    show(btnStop);
    hide(btnReset);
    hide(headphoneHint);
  } else if (isDone || isError) {
    hide(btnStart);
    hide(btnStop);
    show(btnReset);
    hide(headphoneHint);
  } else {
    show(btnStart);
    hide(btnStop);
    hide(btnReset);
    show(headphoneHint);
  }

  // Timer
  if (isRec || isDone) {
    show(timerBlock);
    chunksText.textContent = currentChunks || '0';
    if (isRec && recordingStartedAt) {
      startTimer();
    } else {
      stopTimer();
      if (recordingStartedAt) {
        const endTime = recordingEndedAt || Date.now();
        const elapsed = Math.floor((endTime - recordingStartedAt) / 1000);
        timerText.textContent = formatTimer(elapsed);
      }
    }
  } else {
    hide(timerBlock);
    stopTimer();
  }

  // Status badge
  statusBadge.className = 'status-badge';
  if (isRec) {
    if (!currentOnline) {
      statusBadge.classList.add('status-offline');
      statusLabel.textContent = `Offline • ${currentPending} в очереди`;
    } else if (currentPending > 0) {
      statusBadge.classList.add('status-recording');
      statusLabel.textContent = `Recording • ${currentPending} ↑`;
    } else {
      statusBadge.classList.add('status-recording');
      statusLabel.textContent = 'Recording';
    }
  } else if (currentStatus === 'processing') {
    statusBadge.classList.add('status-processing');
    if (recordingStartedAt && recordingEndedAt) {
      const recSeconds = Math.floor((recordingEndedAt - recordingStartedAt) / 1000);
      statusLabel.textContent = 'Rec: ' + formatTimer(recSeconds) + ' \u2022 Processing...';
    } else {
      statusLabel.textContent = 'Processing...';
    }
  } else if (currentStatus === 'completed') {
    statusBadge.classList.add('status-done');
    statusLabel.textContent = 'Completed';
  } else if (isError) {
    statusBadge.classList.add('status-error');
    statusLabel.textContent = 'Error';
  } else {
    statusBadge.classList.add('status-idle');
    statusLabel.textContent = 'Ready';
  }

  // Info panel
  if (currentSessionId) {
    show(infoPanel);
    sessionLink.textContent = currentSessionId.substring(0, 8) + '...';
    sessionLink.href = serverUrl + '/api/sessions/' + currentSessionId;
    if (isDone) {
      show(resultRow);
      resultLink.href = serverUrl + '/#call/' + currentSessionId;
      if (currentStatus === 'processing' && recordingStartedAt && recordingEndedAt) {
        const recSec = Math.floor((recordingEndedAt - recordingStartedAt) / 1000);
        processingEta.textContent = 'Estimated processing time: ' + estimateProcessingTime(recSec);
        show(processingEta);
      } else {
        hide(processingEta);
      }
    } else {
      hide(resultRow);
      hide(processingEta);
    }
  } else {
    hide(infoPanel);
  }

  // Error
  if (currentError) {
    show(errorText);
    errorText.textContent = currentError;
  } else {
    hide(errorText);
  }

  // Hide settings when recording
  if (isRec) {
    hide(settingsPanel);
    hide(settingsToggle);
  } else {
    show(settingsToggle);
  }
}

// --- Status callback from recorder ---
function onRecorderStatus(data) {
  currentSessionId = data.sessionId || currentSessionId;
  if (typeof data.chunksUploaded === 'number') currentChunks = data.chunksUploaded;
  if (typeof data.pending === 'number') currentPending = data.pending;
  if (typeof data.online === 'boolean') currentOnline = data.online;
  if (data.error) {
    currentError = data.error;
    currentStatus = 'error';
    recordingEndedAt = recordingEndedAt || Date.now();
  } else {
    currentStatus = data.status;
    currentError = null;
  }
  updateUI();

  // Server told us the broker JWT is no longer valid (expired / disabled).
  // Force-logout the broker but keep persisted chunks on disk so they'll be
  // re-uploaded after the broker re-logs in. Recovery will pick them up.
  if (data.authExpired) {
    logout({ force: true }).catch(() => {});
  }
}

// --- Start Recording ---
btnStart.addEventListener('click', async () => {
  try {
    btnStart.disabled = true;
    currentError = null;
    currentChunks = 0;
    currentSessionId = null;
    recordingStartedAt = Date.now();
    recordingEndedAt = null;
    currentStatus = 'connecting';
    updateUI();

    await window.Recorder.start(serverUrl, username, password, onRecorderStatus);
  } catch (err) {
    currentError = err.message;
    currentStatus = 'error';
    updateUI();
  } finally {
    btnStart.disabled = false;
  }
});

// --- Stop Recording ---
btnStop.addEventListener('click', async () => {
  btnStop.disabled = true;
  recordingEndedAt = Date.now();
  currentStatus = 'processing';
  updateUI();

  await window.Recorder.stop();
  btnStop.disabled = false;
});

// --- Reset ---
btnReset.addEventListener('click', () => {
  currentStatus = 'idle';
  currentSessionId = null;
  currentChunks = 0;
  currentPending = 0;
  currentError = null;
  recordingStartedAt = null;
  recordingEndedAt = null;
  timerText.textContent = '00:00';
  updateUI();
});

// --- Title bar buttons ---
btnMinimize.addEventListener('click', () => {
  window.electronAPI.minimizeWindow(); // hide to tray
});

btnClose.addEventListener('click', () => {
  window.electronAPI.closeWindow(); // graceful quit via IPC
});

// --- Settings ---
settingsToggle.addEventListener('click', () => {
  if (settingsPanel.style.display === 'none') {
    show(settingsPanel);
  } else {
    hide(settingsPanel);
  }
});

btnSaveSettings.addEventListener('click', async () => {
  serverUrl = inputServerUrl.value.replace(/\/+$/, '');
  username = inputUsername.value;
  password = inputPassword.value;
  if (inputApiKey) apiKey = inputApiKey.value;
  if (inputEmployee) employee = inputEmployee.value;

  await persistCredentials();

  settingsSaved.style.display = 'block';
  setTimeout(() => {
    settingsSaved.style.display = 'none';
  }, 2000);
});

// --- External links → system browser ---
document.addEventListener('click', (e) => {
  const link = e.target.closest('a[href]');
  if (link && link.href && link.href.startsWith('http')) {
    e.preventDefault();
    window.electronAPI.openExternal(link.href);
  }
});

// --- Load credentials on startup ---
async function init() {
  const creds = await window.electronAPI.credentials.get();

  // Достаточны ли креды для запуска? Два режима:
  //  • broker-mode (realestate): serverUrl + username + password;
  //  • API-key mode (fulldent): serverUrl + apiKey (username пуст).
  // Иначе (первый запуск / частичные креды) — экран настройки.
  const hasServer = !!(creds && creds.serverUrl);
  const hasBrokerCreds = !!(creds && creds.username && creds.password);
  const hasApiKeyCreds = !!(creds && isApiKeyMode(creds));
  if (!hasServer || (!hasBrokerCreds && !hasApiKeyCreds)) {
    // First launch (or partial creds) — show setup screen
    showSetupScreen();
    return;
  }

  serverUrl = creds.serverUrl || '';
  username = creds.username || '';
  password = creds.password || '';
  apiKey = creds.apiKey || '';
  employee = creds.employee || '';
  brokerJwt = creds.brokerJwt || '';
  brokerName = creds.brokerName || '';
  brokerEmail = creds.brokerEmail || '';
  inputServerUrl.value = serverUrl;
  inputUsername.value = username;
  inputPassword.value = password;
  if (inputApiKey) inputApiKey.value = apiKey;
  if (inputEmployee) inputEmployee.value = employee;
  if (window.Recorder && window.Recorder.setApiKey) window.Recorder.setApiKey(apiKey);

  // API-key mode (fulldent): нет брокера — пропускаем broker-JWT-гейт и
  // /api/auth/me, сразу к записи. realestate (с username/password) идёт ниже.
  if (isApiKeyMode(creds)) {
    showRecorderScreen();
    return;
  }

  if (!brokerJwt) {
    // Setup done, but no broker session — show login.
    showLoginScreen();
    return;
  }

  // We have a token. Validate it via /api/auth/me. On 401 we kick to login;
  // on a network error we trust the token (don't punish flaky networks).
  try {
    const resp = await fetch(serverUrl + '/api/auth/me', {
      headers: {
        Authorization: basicAuthHeader(),
        'X-Broker-Token': brokerJwt,
      },
    });
    // Treat any explicit auth-failure status the same: token gone for good.
    // Network errors / 5xx fall through and keep the cached session.
    if (resp.status === 401 || resp.status === 403 || resp.status === 410) {
      brokerJwt = '';
      brokerName = '';
      brokerEmail = '';
      await persistCredentials();
      showLoginScreen();
      return;
    }
    if (resp.ok) {
      // Refresh local broker info from server (name may have changed)
      try {
        const me = await resp.json();
        brokerName = me.name || brokerName;
        brokerEmail = me.email || brokerEmail;
        await persistCredentials();
      } catch (_) { /* keep cached values */ }
    }
    // Any non-401 response (200, 5xx, network blip) — proceed with cached session.
  } catch (err) {
    console.warn('[auth/me] network error, keeping cached session:', err.message);
  }

  showRecorderScreen();
}

async function runRecovery() {
  if (!window.Recorder || typeof window.Recorder.recoverPendingChunks !== 'function') return;
  const authHeader = username ? 'Basic ' + btoa(username + ':' + password) : '';

  const banner = document.createElement('div');
  banner.style.cssText = 'position:fixed;top:0;left:0;right:0;background:rgba(245,158,11,0.95);color:#000;padding:8px 12px;font-size:12px;z-index:9998;font-family:system-ui';
  banner.textContent = 'Проверяю несохранённые записи…';

  let progressShown = false;
  const onStatus = (info) => {
    if (info.phase === 'check') {
      if (!progressShown) { document.body.appendChild(banner); progressShown = true; }
      banner.textContent = `Восстанавливаю запись ${info.sessionId.slice(0, 8)}… (${info.total} чанков)`;
      // Block Start while we're actively uploading old data — prevents the
      // user from creating a new session that races with recovery.
      btnStart.disabled = true;
      btnStart.title = 'Идёт восстановление прошлой записи';
    } else if (info.phase === 'upload') {
      banner.textContent = `Дозагружаю чанк #${info.chunkNumber}…`;
    }
  };

  try {
    const result = await window.Recorder.recoverPendingChunks(serverUrl, authHeader, onStatus);
    if (progressShown) {
      if (result.recovered > 0) {
        banner.style.background = 'rgba(16,185,129,0.95)';
        banner.textContent = `Восстановлено ${result.recovered} чанков`;
      } else {
        banner.textContent = 'Все записи актуальны';
      }
      setTimeout(() => banner.remove(), 3000);
    }
  } catch (err) {
    if (progressShown) {
      banner.style.background = 'rgba(239,68,68,0.95)';
      banner.style.color = '#fff';
      banner.textContent = 'Восстановление не удалось: ' + err.message;
      setTimeout(() => banner.remove(), 5000);
    }
  } finally {
    btnStart.disabled = false;
    btnStart.title = '';
  }
}

init();

// --- Graceful shutdown handler ---
window.electronAPI.onBeforeQuit(async () => {
  if (window.Recorder.isRecording()) {
    recordingEndedAt = Date.now();
    try {
      await window.Recorder.stop();
    } catch (err) {
      console.warn('Error during graceful shutdown:', err.message);
    }
  }
  window.electronAPI.sendQuitReady();
});
