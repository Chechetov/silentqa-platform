// Recorder module — captures system audio + microphone, mixes, chunks, uploads
// System audio captured via getDisplayMedia() — intercepted by main process
// setDisplayMediaRequestHandler which auto-selects screen + loopback audio.
// IIFE — prevents global scope pollution
(function() {
'use strict';
console.log('[recorder] Script loaded');

let mediaRecorder = null;
let systemStream = null;
let micStream = null;
let audioCtx = null;
let dest = null;
let sysSource = null;
let micSource = null;
let userInitiatedStop = false;
let deviceChangeHandler = null;
let sessionId = null;
let serverUrl = '';
let authHeader = '';
let brokerToken = '';
let apiKey = '';
let chunksUploaded = 0;
let nextChunkNumber = 0;
let pendingChunks = 0;
let uploadQueue = Promise.resolve();
let onStatusUpdate = null;

// Wakes up upload retry loops the moment the OS reports we're back online,
// instead of letting them sit in their current backoff sleep.
let onlineWaiters = [];
function wakeOnlineWaiters() {
  const waiters = onlineWaiters;
  onlineWaiters = [];
  for (const w of waiters) w();
}
window.addEventListener('online', wakeOnlineWaiters);

async function waitUntilOnline() {
  if (navigator.onLine) return;
  await new Promise((resolve) => onlineWaiters.push(resolve));
}

const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 2000;
// Per-chunk retry: capped exponential, no upper bound on attempts. Step 3
// will add a persistent disk buffer so we don't hold blobs in RAM forever.
const CHUNK_BACKOFF_BASE_MS = 1000;
const CHUNK_BACKOFF_MAX_MS = 60_000;
const MIC_CONSTRAINTS = {
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
};

// Build request headers: always include Basic auth (admin); if a broker JWT
// is set via setBrokerToken(), include it alongside as X-Broker-Token. The
// optional `auth` arg lets recovery pass an explicit Basic header instead of
// the module's current one (used when a recovery scan runs alongside a live
// recording with potentially different module state).
function buildHeaders(extra = {}, auth = authHeader) {
  const h = { Authorization: auth };
  if (brokerToken) h['X-Broker-Token'] = brokerToken;
  if (apiKey) h['X-API-Key'] = apiKey;
  return Object.assign(h, extra);
}

function sendStatus(status, extra = {}) {
  if (onStatusUpdate) {
    onStatusUpdate({
      sessionId,
      status,
      chunksUploaded,
      pending: pendingChunks,
      online: navigator.onLine,
      ...extra,
    });
  }
}

async function createSession() {
  const resp = await fetchWithRetry(`${serverUrl}/api/sessions`, {
    method: 'POST',
    headers: buildHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      metadata: {
        source: 'desktop-app',
        platform: window.electronAPI.platform,
        recordedAt: new Date().toISOString(),
      },
    }),
  });
  if (!resp.ok) throw new Error(`Failed to create session: ${resp.status}`);
  const data = await resp.json();
  return data.id;
}

async function fetchWithRetry(url, options, retries = MAX_RETRIES) {
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const resp = await fetch(url, options);
      return resp;
    } catch (err) {
      if (attempt === retries) throw err;
      console.warn(`Fetch attempt ${attempt} failed, retrying in ${RETRY_DELAY_MS}ms...`, err.message);
      await new Promise((r) => setTimeout(r, RETRY_DELAY_MS));
    }
  }
}

// Sleep up to `ms`, but wake early if `online` fires (so we don't waste 60s
// of backoff once the network is back).
function backoffSleep(ms) {
  return new Promise((resolve) => {
    const t = setTimeout(() => {
      onlineWaiters = onlineWaiters.filter((w) => w !== wake);
      resolve();
    }, ms);
    const wake = () => { clearTimeout(t); resolve(); };
    onlineWaiters.push(wake);
  });
}

// Best-effort disk persistence: write the chunk to userData before we try
// uploading it, so a crash or power loss doesn't lose audio. Falls back to
// pure in-memory operation if the IPC handler is missing (older main process)
// or disk I/O fails (read-only home, antivirus lock, full disk).
async function persistChunk(sid, chunkNumber, blob) {
  const cs = window.electronAPI && window.electronAPI.chunkStore;
  if (!cs || typeof cs.put !== 'function') return false;
  try {
    const buf = await blob.arrayBuffer();
    const result = await cs.put(sid, chunkNumber, buf, {
      serverUrl,
      // Don't persist auth — credentials live in the OS keychain (safeStorage).
    });
    return !!(result && result.ok);
  } catch (err) {
    console.warn('[recorder] persistChunk failed, will keep in memory only:', err.message);
    return false;
  }
}

async function forgetPersistedChunk(sid, chunkNumber) {
  const cs = window.electronAPI && window.electronAPI.chunkStore;
  if (!cs || typeof cs.delete !== 'function') return;
  try {
    await cs.delete(sid, chunkNumber);
  } catch (_) {
    // Non-critical: next launch's recovery scan will see it was already
    // uploaded (server idempotency) and clean up.
  }
}

// Performs the actual POST with backoff. `getBody()` is a thunk so we can
// rebuild the FormData on each retry — FormData with a Blob can't be reused
// across fetches in some Chromium versions.
//
// `target` carries serverUrl + authHeader explicitly (NOT module state), so
// this function works correctly when called from recovery in parallel with a
// live recording that may be mutating the module's serverUrl/authHeader.
//
// `maxAttempts`: 0 = retry forever (live recording); >0 = give up after N
// transient failures so a wedged session can't block recovery of healthier
// ones.
async function postChunkWithBackoff(target, sid, chunkNumber, getBody, opts = {}) {
  const { label = 'chunk', maxAttempts = 0 } = opts;
  let attempt = 0;
  while (true) {
    if (!navigator.onLine) {
      sendStatus('recording', { note: 'offline' });
      await waitUntilOnline();
    }

    let resp;
    try {
      resp = await fetch(
        `${target.serverUrl}/api/sessions/${sid}/chunks`,
        { method: 'POST', body: getBody(), headers: buildHeaders({}, target.authHeader) },
      );
    } catch (err) {
      attempt++;
      if (maxAttempts && attempt > maxAttempts) {
        return { ok: false, status: 0, terminal: false, gaveUp: true };
      }
      const delay = Math.min(CHUNK_BACKOFF_MAX_MS, CHUNK_BACKOFF_BASE_MS * 2 ** (attempt - 1));
      console.warn(`[recorder] ${label} ${chunkNumber} network error attempt ${attempt}, retry in ${delay}ms:`, err.message);
      await backoffSleep(delay);
      continue;
    }

    if (resp.ok) return { ok: true };

    const retryable = resp.status >= 500 || resp.status === 408 || resp.status === 429;
    if (!retryable) {
      console.error(`[recorder] ${label} ${chunkNumber} permanently rejected: ${resp.status}`);
      return { ok: false, status: resp.status, terminal: true };
    }

    attempt++;
    if (maxAttempts && attempt > maxAttempts) {
      return { ok: false, status: resp.status, terminal: false, gaveUp: true };
    }
    const delay = Math.min(CHUNK_BACKOFF_MAX_MS, CHUNK_BACKOFF_BASE_MS * 2 ** (attempt - 1));
    console.warn(`[recorder] ${label} ${chunkNumber} got ${resp.status} attempt ${attempt}, retry in ${delay}ms`);
    await backoffSleep(delay);
  }
}

async function uploadChunk(blob, chunkNumber) {
  if (!sessionId || blob.size === 0) {
    pendingChunks = Math.max(0, pendingChunks - 1);
    return;
  }

  // Persist to disk BEFORE upload so a crash mid-upload doesn't lose audio.
  // If persistence fails, we still proceed in-memory — better to risk a
  // restart-loss than to drop the chunk now.
  const sid = sessionId;
  const persisted = await persistChunk(sid, chunkNumber, blob);

  const result = await postChunkWithBackoff(
    { serverUrl, authHeader },
    sid,
    chunkNumber,
    () => {
      const fd = new FormData();
      fd.append('file', blob, `chunk_${String(chunkNumber).padStart(6, '0')}.webm`);
      fd.append('chunk_number', String(chunkNumber));
      return fd;
    },
  );

  pendingChunks = Math.max(0, pendingChunks - 1);

  if (result.ok) {
    chunksUploaded++;
    if (persisted) await forgetPersistedChunk(sid, chunkNumber);
    sendStatus('recording');
  } else if (result.status === 401 || result.status === 403) {
    // Auth failure mid-recording — broker JWT expired or revoked. Don't
    // delete the persisted chunk: after re-login, recovery scan will find
    // it and re-upload with the fresh JWT. Signal the renderer to bounce
    // the user to the login screen.
    sendStatus('error', {
      error: 'Сессия брокера истекла. Войди заново — несохранённые чанки на диске.',
      authExpired: true,
    });
  } else {
    // Terminal failure: keep on disk so user can inspect / retry manually.
    sendStatus('error', { error: `Server rejected chunk ${chunkNumber}: ${result.status}` });
  }
}

// Resume uploads for chunks that were persisted to disk in a prior session
// but never made it to the server. Called on app startup, before the user can
// begin a new recording. Skips chunks whose stored serverUrl differs from the
// current one (user switched servers — leave files alone) and chunks whose
// session no longer exists on the server.
async function recoverPendingChunks(currentServerUrl, currentAuthHeader, statusCallback) {
  const cs = window.electronAPI && window.electronAPI.chunkStore;
  if (!cs || typeof cs.list !== 'function') return { recovered: 0, skipped: 0 };

  let listResult;
  try {
    listResult = await cs.list();
  } catch (err) {
    console.warn('[recovery] list failed:', err.message);
    return { recovered: 0, skipped: 0 };
  }
  if (!listResult || !listResult.ok) return { recovered: 0, skipped: 0 };

  const items = listResult.items || [];
  if (items.length === 0) return { recovered: 0, skipped: 0 };

  // Group by session
  const grouped = {};
  for (const it of items) {
    (grouped[it.sessionId] = grouped[it.sessionId] || []).push(it);
  }

  let recovered = 0;
  let skipped = 0;

  for (const [sid, chunks] of Object.entries(grouped)) {
    const sample = chunks[0];
    const sidServer = (sample.meta && sample.meta.serverUrl) || '';
    if (sidServer && sidServer !== currentServerUrl) {
      // Cross-server orphan — leave it alone, user might switch back.
      skipped += chunks.length;
      continue;
    }

    if (statusCallback) statusCallback({ phase: 'check', sessionId: sid, total: chunks.length });

    // Ask server which chunks are missing for this session.
    let missingSet = null;
    try {
      const r = await fetch(`${currentServerUrl}/api/sessions/${sid}/missing-chunks`, {
        headers: buildHeaders({}, currentAuthHeader),
      });
      if (r.status === 404) {
        // Session deleted on server — drop local files.
        await cs.deleteSession(sid);
        skipped += chunks.length;
        continue;
      }
      if (r.ok) {
        const data = await r.json();
        const missingArr = Array.isArray(data.missing) ? data.missing : [];
        const maxNum = typeof data.max === 'number' ? data.max : 0;
        // Treat as "missing" anything in the explicit list, plus anything
        // numbered above the server's current max (chunks the server never saw).
        missingSet = new Set(missingArr);
        for (const c of chunks) {
          if (c.chunkNumber > maxNum) missingSet.add(c.chunkNumber);
        }
      }
    } catch (_) {
      // Server unreachable — try again next launch.
      skipped += chunks.length;
      continue;
    }

    // If we couldn't determine missing list (server down), skip this session.
    if (missingSet === null) { skipped += chunks.length; continue; }

    // Sort numerically to upload in order — same reason as merge_chunks.
    chunks.sort((a, b) => a.chunkNumber - b.chunkNumber);

    // Recovery uses an explicit target; we deliberately don't touch the
    // module's serverUrl/authHeader so a concurrent live recording isn't
    // affected. Per-chunk attempts are capped so a single wedged session
    // can't block recovery of healthier sessions.
    const target = { serverUrl: currentServerUrl, authHeader: currentAuthHeader };
    for (const c of chunks) {
      if (!missingSet.has(c.chunkNumber)) {
        // Server already has it — nothing to do, just clean up the file.
        await cs.delete(sid, c.chunkNumber);
        continue;
      }
      if (statusCallback) statusCallback({ phase: 'upload', sessionId: sid, chunkNumber: c.chunkNumber });

      const readResult = await cs.read(sid, c.chunkNumber);
      if (!readResult || !readResult.ok || !readResult.data) {
        console.warn('[recovery] read failed for', sid, c.chunkNumber);
        continue;
      }
      const blob = new Blob([readResult.data], { type: 'audio/webm' });

      const result = await postChunkWithBackoff(
        target,
        sid,
        c.chunkNumber,
        () => {
          const fd = new FormData();
          fd.append('file', blob, `chunk_${String(c.chunkNumber).padStart(6, '0')}.webm`);
          fd.append('chunk_number', String(c.chunkNumber));
          return fd;
        },
        { label: 'recover', maxAttempts: 8 },
      );

      if (result.ok) {
        recovered++;
        await cs.delete(sid, c.chunkNumber);
      } else if (result.terminal) {
        // Server-side terminal reject (e.g. session is failed/completed) —
        // delete local file so we don't loop on it forever.
        await cs.delete(sid, c.chunkNumber);
      }
      // gaveUp / other transient: leave on disk for next launch.
    }
  }

  return { recovered, skipped };
}

async function finishSession() {
  if (!sessionId) return;
  const sid = sessionId;
  try {
    await fetchWithRetry(`${serverUrl}/api/sessions/${sid}/finish`, {
      method: 'POST',
      headers: buildHeaders(),
    });
    sendStatus('processing');
    pollSessionStatus();
    // Server has accepted the session — purge the local persistent buffer.
    // If this fails, the next recovery scan will see the chunks, ask
    // /missing-chunks (server returns nothing missing) and clean up then.
    const cs = window.electronAPI && window.electronAPI.chunkStore;
    if (cs && typeof cs.deleteSession === 'function') {
      cs.deleteSession(sid).catch(() => {});
    }
  } catch (err) {
    console.error('Finish session error:', err);
    sendStatus('error', { error: err.message });
  }
}

let pollTimer = null;

function pollSessionStatus() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const resp = await fetch(`${serverUrl}/api/sessions/${sessionId}`, {
        headers: buildHeaders(),
      });
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.status === 'completed' || data.status === 'done') {
        clearInterval(pollTimer);
        pollTimer = null;
        sendStatus('completed');
      } else if (data.status === 'error' || data.status === 'failed') {
        clearInterval(pollTimer);
        pollTimer = null;
        sendStatus('error', { error: data.error || 'Processing failed' });
      }
    } catch (err) {
      console.warn('Poll status error:', err.message);
    }
  }, 5000);
}

// Acquire system audio via getDisplayMedia. The main process'
// setDisplayMediaRequestHandler auto-selects the screen + loopback audio
// — no picker dialog shown to the user.
async function acquireSystemStream() {
  const displayStream = await navigator.mediaDevices.getDisplayMedia({
    video: true,   // required by Chromium, video track stripped below
    audio: true,   // system audio via loopback
  });
  displayStream.getVideoTracks().forEach((track) => {
    track.stop();
    displayStream.removeTrack(track);
  });
  if (displayStream.getAudioTracks().length === 0) {
    throw new Error('No system audio available. On macOS, grant Screen Recording permission in System Preferences > Privacy & Security > Screen Recording.');
  }
  return displayStream;
}

// Hot-swap a dead microphone track for a fresh one without stopping
// MediaRecorder. The dest stream consumed by MediaRecorder stays alive;
// we just rewire which source feeds it.
async function swapMic(reason) {
  if (userInitiatedStop || !audioCtx || !dest) return;
  console.log('[recorder] swapMic triggered:', reason);
  try {
    const newStream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
    const newSource = audioCtx.createMediaStreamSource(newStream);
    newSource.connect(dest);

    // Tear down the old graph node + tracks AFTER the new one is wired in,
    // so there's no gap longer than one audio frame.
    if (micSource) {
      try { micSource.disconnect(); } catch (_) {}
    }
    if (micStream) {
      micStream.getTracks().forEach((t) => t.stop());
    }

    micStream = newStream;
    micSource = newSource;
    attachTrackHandlers(newStream.getAudioTracks()[0], 'mic');
    sendStatus('recording', { note: 'mic-swapped:' + reason });
  } catch (err) {
    console.warn('[recorder] mic swap failed:', err.message);
    // Mic is optional — we keep going on system audio alone.
    micStream = null;
    micSource = null;
    sendStatus('recording', { note: 'mic-lost:' + err.message });
  }
}

async function swapSystem(reason) {
  if (userInitiatedStop || !audioCtx || !dest) return;
  console.log('[recorder] swapSystem triggered:', reason);
  try {
    const newStream = await acquireSystemStream();
    const newSource = audioCtx.createMediaStreamSource(newStream);
    newSource.connect(dest);

    if (sysSource) {
      try { sysSource.disconnect(); } catch (_) {}
    }
    if (systemStream) {
      systemStream.getTracks().forEach((t) => t.stop());
    }

    systemStream = newStream;
    sysSource = newSource;
    attachTrackHandlers(newStream.getAudioTracks()[0], 'system');
    sendStatus('recording', { note: 'system-swapped:' + reason });
  } catch (err) {
    console.error('[recorder] system swap failed:', err.message);
    sendStatus('error', { error: 'System audio lost: ' + err.message });
  }
}

function attachTrackHandlers(track, kind) {
  if (!track) return;
  track.addEventListener('ended', () => {
    if (kind === 'mic') swapMic('track-ended');
    else swapSystem('track-ended');
  });
}

// Start recording — system audio + microphone
async function startRecording(url, username, password, statusCallback) {
  serverUrl = url;
  authHeader = username ? 'Basic ' + btoa(username + ':' + password) : '';
  chunksUploaded = 0;
  nextChunkNumber = 0;
  pendingChunks = 0;
  uploadQueue = Promise.resolve();
  onStatusUpdate = statusCallback;
  userInitiatedStop = false;

  try {
    // 1. Create session on server
    sessionId = await createSession();
    sendStatus('connecting');

    // 2. System audio (loopback)
    systemStream = await acquireSystemStream();

    // 3. Microphone (optional — recording works without it)
    try {
      micStream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
    } catch (micErr) {
      console.warn('Microphone access denied, recording system audio only:', micErr.message);
      micStream = null;
    }

    // 4. Mix streams using Web Audio API. Sources can be hot-swapped under
    // the same dest without restarting MediaRecorder.
    audioCtx = new AudioContext({ sampleRate: 48000 });
    dest = audioCtx.createMediaStreamDestination();

    sysSource = audioCtx.createMediaStreamSource(systemStream);
    sysSource.connect(dest);
    attachTrackHandlers(systemStream.getAudioTracks()[0], 'system');

    if (micStream) {
      micSource = audioCtx.createMediaStreamSource(micStream);
      micSource.connect(dest);
      attachTrackHandlers(micStream.getAudioTracks()[0], 'mic');
    }

    // On macOS the mic track sometimes stays "live" but routed to a stale
    // device when AirPods (dis)connect — devicechange is the safety net.
    deviceChangeHandler = () => {
      if (userInitiatedStop || !mediaRecorder || mediaRecorder.state !== 'recording') return;
      const micTrack = micStream && micStream.getAudioTracks()[0];
      if (!micTrack || micTrack.readyState !== 'live' || micTrack.muted) {
        swapMic('devicechange');
      }
    };
    navigator.mediaDevices.addEventListener('devicechange', deviceChangeHandler);

    // 5. Record the mixed stream
    mediaRecorder = new MediaRecorder(dest.stream, {
      mimeType: 'audio/webm;codecs=opus',
    });

    // Sequential upload queue — chunks finish in order. Step 3 will replace
    // the in-memory blob hand-off with a disk-backed handoff for crash safety.
    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        nextChunkNumber++;
        pendingChunks++;
        const num = nextChunkNumber;
        const blob = event.data;
        sendStatus('recording');
        uploadQueue = uploadQueue.then(() => uploadChunk(blob, num));
      }
    };

    // External stop (track failure, browser quirk) — finalize the session
    // so the server doesn't sit in `uploading` forever.
    mediaRecorder.onstop = () => {
      if (!userInitiatedStop) {
        console.warn('[recorder] MediaRecorder stopped externally — finalizing');
        uploadQueue.then(() => finishSession()).catch(() => {});
      }
    };
    mediaRecorder.onerror = (event) => {
      console.error('[recorder] MediaRecorder error:', event.error);
      sendStatus('error', { error: 'Recorder error: ' + (event.error?.message || 'unknown') });
    };

    mediaRecorder.start(10000); // 10-second chunks
    sendStatus('recording');
    window.electronAPI.updateTrayStatus('recording');

  } catch (err) {
    console.error('Start recording error:', err);
    sendStatus('error', { error: err.message });
  }
}

// Stop recording
async function stopRecording() {
  userInitiatedStop = true;

  if (deviceChangeHandler) {
    navigator.mediaDevices.removeEventListener('devicechange', deviceChangeHandler);
    deviceChangeHandler = null;
  }

  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    await new Promise((resolve) => {
      mediaRecorder.onstop = resolve;
      mediaRecorder.stop();
    });
    // Wait for all queued uploads, but cap at 30s. If we're offline with a
    // long tail of pending chunks, hanging the UI forever is worse than
    // calling /finish early — the server-side watchdog (15min) already
    // handles abandoned `uploading` sessions, and step 3's persistent disk
    // buffer will let the next launch finish the upload.
    await Promise.race([
      uploadQueue,
      new Promise((resolve) => setTimeout(resolve, 30_000)),
    ]);
  }

  // Stop all tracks
  if (systemStream) {
    systemStream.getTracks().forEach((t) => t.stop());
    systemStream = null;
  }
  if (micStream) {
    micStream.getTracks().forEach((t) => t.stop());
    micStream = null;
  }
  if (audioCtx) {
    audioCtx.close().catch(() => {});
    audioCtx = null;
  }
  dest = null;
  sysSource = null;
  micSource = null;

  mediaRecorder = null;
  window.electronAPI.updateTrayStatus('idle');
  await finishSession();
}

function getSessionId() {
  return sessionId;
}

function isRecording() {
  return mediaRecorder !== null && mediaRecorder.state === 'recording';
}

window.Recorder = {
  start: startRecording,
  stop: stopRecording,
  getSessionId,
  isRecording,
  recoverPendingChunks,
  setBrokerToken: (token) => { brokerToken = token || ''; },
  setApiKey: (key) => { apiKey = key || ''; },
};
})();
