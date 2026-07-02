const btnStart = document.getElementById("btnStart");
const btnStop = document.getElementById("btnStop");
const btnReset = document.getElementById("btnReset");
const btnMic = document.getElementById("btnMic");
const statusBadge = document.getElementById("statusBadge");
const statusLabel = document.getElementById("statusLabel");
const timerBlock = document.getElementById("timerBlock");
const timerText = document.getElementById("timerText");
const chunksText = document.getElementById("chunksText");
const micBlock = document.getElementById("micBlock");
const micStatusText = document.getElementById("micStatusText");
const infoPanel = document.getElementById("infoPanel");
const sessionRow = document.getElementById("sessionRow");
const sessionLink = document.getElementById("sessionLink");
const resultRow = document.getElementById("resultRow");
const resultLink = document.getElementById("resultLink");
const processingEta = document.getElementById("processingEta");
const errorText = document.getElementById("errorText");
const serverUrlInput = document.getElementById("serverUrl");

// Конфиг живёт в chrome.storage (см. settings-блок ниже), не в исходнике.
const apiKeyInput = document.getElementById("apiKey");
const employeeInput = document.getElementById("employee");
const btnSaveSettings = document.getElementById("btnSaveSettings");
const settingsSaved = document.getElementById("settingsSaved");
const settingsBlock = document.getElementById("settingsBlock");

let timerInterval = null;

function show(el) { if (el) el.style.display = ""; }
function hide(el) { if (el) el.style.display = "none"; }

// --- Microphone permission ---
// Extension popup and offscreen share origin chrome-extension://ID
// getUserMedia in popup grants permission for that origin
// so offscreen can then use it without a prompt

async function checkMicPermission() {
  try {
    const result = await navigator.permissions.query({ name: "microphone" });
    return result.state; // "granted", "denied", "prompt"
  } catch {
    return "prompt";
  }
}

async function updateMicUI() {
  const state = await checkMicPermission();
  if (state === "granted") {
    micBlock.className = "mic-block mic-ok";
    micStatusText.textContent = "Microphone: allowed";
    hide(btnMic);
    show(micBlock);
    chrome.storage.local.set({ micDenied: false });
  } else if (state === "denied") {
    micBlock.className = "mic-block mic-denied";
    micStatusText.textContent = "Microphone blocked. Click below to fix.";
    btnMic.textContent = "Open mic settings page";
    show(btnMic);
    show(micBlock);
  } else {
    micBlock.className = "mic-block mic-denied";
    micStatusText.textContent = "Microphone not granted. Click below to allow.";
    btnMic.textContent = "Grant microphone access";
    show(btnMic);
    show(micBlock);
  }
  return state;
}

btnMic.addEventListener("click", async () => {
  const state = await checkMicPermission();

  if (state === "denied") {
    // Permission permanently denied — open dedicated page where user can
    // click the lock icon in address bar to change permission
    chrome.tabs.create({ url: chrome.runtime.getURL("mic-permission.html") });
    return;
  }

  // State is "prompt" — try requesting directly from popup
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach(t => t.stop());
    chrome.storage.local.set({ micDenied: false });
    await updateMicUI();
  } catch (err) {
    // If denied, open the dedicated page as fallback
    chrome.storage.local.set({ micDenied: true });
    chrome.tabs.create({ url: chrome.runtime.getURL("mic-permission.html") });
  }
});

// Check mic on popup open (only in idle state)
chrome.storage.local.get(["recording", "status"], (data) => {
  const status = data.status || "";
  if (!data.recording && status !== "processing" && status !== "completed") {
    updateMicUI();
  }
});

// Load saved state
chrome.storage.local.get(
  ["serverUrl", "recording", "sessionId", "status", "chunksUploaded", "error", "recordingStartedAt", "recordingEndedAt", "micDenied"],
  (data) => {
    updateUI(data);
  }
);

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

// Listen for real-time updates
chrome.storage.onChanged.addListener(() => {
  chrome.storage.local.get(
    ["recording", "sessionId", "status", "chunksUploaded", "error", "serverUrl", "recordingStartedAt", "recordingEndedAt", "micDenied"],
    (data) => updateUI(data)
  );
});

function formatTimer(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}

function startTimer(startedAt) {
  stopTimer();
  function tick() {
    const elapsed = Math.floor((Date.now() - startedAt) / 1000);
    timerText.textContent = formatTimer(elapsed);
  }
  tick();
  timerInterval = setInterval(tick, 1000);
}

function stopTimer() {
  if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
}

function estimateProcessingTime(recordingSeconds) {
  if (recordingSeconds < 60) return "~1-2 min";
  if (recordingSeconds < 180) return "~2-3 min";
  if (recordingSeconds < 600) return "~3-5 min";
  return "~5-10 min";
}

function updateUI(data) {
  const isRecording = !!data.recording;
  const status = data.status || "";
  const hasSession = !!data.sessionId;
  const isDone = status === "processing" || status === "completed";
  const isError = status === "error" || !!data.error;

  // --- Buttons ---
  if (isRecording) {
    hide(btnStart);
    show(btnStop);
    hide(btnReset);
  } else if (isDone || isError) {
    hide(btnStart);
    hide(btnStop);
    show(btnReset);
  } else {
    show(btnStart);
    hide(btnStop);
    hide(btnReset);
  }
  btnStart.disabled = false;
  btnStop.disabled = false;

  // --- Mic block: show only in idle ---
  if (isRecording || isDone) {
    hide(micBlock);
  }

  // --- Timer ---
  if (isRecording || isDone) {
    show(timerBlock);
    chunksText.textContent = data.chunksUploaded || "0";
    if (isRecording && data.recordingStartedAt) {
      startTimer(data.recordingStartedAt);
    } else {
      stopTimer();
      if (data.recordingStartedAt) {
        const endTime = data.recordingEndedAt || Date.now();
        const elapsed = Math.floor((endTime - data.recordingStartedAt) / 1000);
        timerText.textContent = formatTimer(elapsed);
      }
    }
  } else {
    hide(timerBlock);
    stopTimer();
  }

  // --- Status badge ---
  statusBadge.className = "status-badge";
  if (isRecording) {
    statusBadge.classList.add("status-recording");
    statusLabel.textContent = data.micDenied ? "Recording (tab only)" : "Recording";
  } else if (status === "processing") {
    statusBadge.classList.add("status-processing");
    if (data.recordingStartedAt && data.recordingEndedAt) {
      const recSeconds = Math.floor((data.recordingEndedAt - data.recordingStartedAt) / 1000);
      statusLabel.textContent = "Rec: " + formatTimer(recSeconds) + " \u2022 Processing...";
    } else {
      statusLabel.textContent = "Processing...";
    }
  } else if (status === "completed") {
    statusBadge.classList.add("status-done");
    statusLabel.textContent = "Completed";
  } else if (isError) {
    statusBadge.classList.add("status-error");
    statusLabel.textContent = "Error";
  } else {
    statusBadge.classList.add("status-idle");
    statusLabel.textContent = "Ready";
  }

  // --- Info panel ---
  if (hasSession) {
    show(infoPanel);
    const url = data.serverUrl || "";
    sessionLink.textContent = data.sessionId.substring(0, 8) + "...";
    sessionLink.href = url + "/api/sessions/" + data.sessionId;
    if (isDone) {
      show(resultRow);
      resultLink.href = url + "/#call/" + data.sessionId;

      if (status === "processing" && data.recordingStartedAt && data.recordingEndedAt) {
        const recSec = Math.floor((data.recordingEndedAt - data.recordingStartedAt) / 1000);
        processingEta.textContent = "Estimated processing time: " + estimateProcessingTime(recSec);
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

  // --- Error ---
  if (data.error) {
    show(errorText);
    errorText.textContent = data.error;
  } else {
    hide(errorText);
  }
}

// Start
btnStart.addEventListener("click", async () => {
  btnStart.disabled = true;

  // Check mic — try to get permission before starting
  let micDenied = false;
  const micState = await checkMicPermission();
  if (micState === "granted") {
    micDenied = false;
  } else if (micState === "prompt") {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach(t => t.stop());
    } catch {
      micDenied = true;
    }
  } else {
    micDenied = true;
  }

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

  chrome.runtime.sendMessage({ type: "start-capture" }, (response) => {
    btnStart.disabled = false;
    if (response && !response.success) {
      chrome.storage.local.set({
        recording: false,
        status: "error",
        error: response.error || "Failed to start recording",
      });
    }
  });
});

// Stop
btnStop.addEventListener("click", () => {
  btnStop.disabled = true;
  chrome.storage.local.set({ recordingEndedAt: Date.now() });
  chrome.runtime.sendMessage({ type: "stop-capture" }, () => {
    btnStop.disabled = false;
  });
});

// Reset
btnReset.addEventListener("click", () => {
  chrome.storage.local.set({
    recording: false,
    sessionId: null,
    status: null,
    chunksUploaded: 0,
    error: null,
    recordingStartedAt: null,
    recordingEndedAt: null,
    micDenied: null,
  });
  updateMicUI();
});
