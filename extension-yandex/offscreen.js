// Offscreen document — handles MediaRecorder and chunk uploads
// Captures BOTH tab audio (other participants) AND microphone (user)

let mediaRecorder = null;
let tabStream = null;
let micStream = null;
let audioCtx = null;
let sessionId = null;
let serverUrl = "";
let authHeader = "";
let apiKey = "";
let chunksUploaded = 0;
let uploadQueue = Promise.resolve();

// Send status back to background/popup
function sendStatus(status, extra = {}) {
  chrome.runtime.sendMessage({
    type: "recording-status",
    sessionId,
    status,
    chunksUploaded,
    ...extra,
  });
}

// Create a session on the server
async function createSession(tabId) {
  const resp = await fetch(`${serverUrl}/api/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Authorization": authHeader, ...(apiKey ? { "X-API-Key": apiKey } : {}) },
    body: JSON.stringify({
      metadata: {
        source: "chrome-extension",
        tabId,
        recordedAt: new Date().toISOString(),
      },
    }),
  });
  if (!resp.ok) throw new Error(`Failed to create session: ${resp.status}`);
  const data = await resp.json();
  return data.id;
}

// Upload a chunk to the server
async function uploadChunk(blob) {
  if (!sessionId || blob.size === 0) return;

  const formData = new FormData();
  formData.append("file", blob, `chunk_${String(chunksUploaded + 1).padStart(3, "0")}.webm`);

  try {
    const resp = await fetch(
      `${serverUrl}/api/sessions/${sessionId}/chunks`,
      { method: "POST", body: formData, headers: { "Authorization": authHeader, ...(apiKey ? { "X-API-Key": apiKey } : {}) } }
    );
    if (resp.ok) {
      chunksUploaded++;
      sendStatus("recording");
    } else {
      console.error("Chunk upload failed:", resp.status);
      sendStatus("error", { error: `Upload failed: ${resp.status}` });
    }
  } catch (err) {
    console.error("Chunk upload error:", err);
    sendStatus("error", { error: err.message });
  }
}

// Finish session — trigger processing
async function finishSession() {
  if (!sessionId) return;
  try {
    await fetch(`${serverUrl}/api/sessions/${sessionId}/finish`, {
      method: "POST",
      headers: { "Authorization": authHeader, ...(apiKey ? { "X-API-Key": apiKey } : {}) },
    });
    sendStatus("processing");
    chrome.runtime.sendMessage({
      type: "recording-stopped",
      status: "processing",
      sessionId,
    });
  } catch (err) {
    console.error("Finish session error:", err);
    sendStatus("error", { error: err.message });
  }
}

// Start recording — capture tab audio + microphone, mix together
async function startRecording(streamId, url, tabId, username, password, key) {
  serverUrl = url;
  authHeader = username ? "Basic " + btoa(username + ":" + password) : "";
  apiKey = key || "";
  chunksUploaded = 0;
  uploadQueue = Promise.resolve();

  try {
    // Create session on server
    sessionId = await createSession(tabId);
    sendStatus("connecting");

    // 1. Get tab audio stream (what others say)
    tabStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: {
          chromeMediaSource: "tab",
          chromeMediaSourceId: streamId,
        },
      },
    });

    // 2. Get microphone stream (what user says)
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (micErr) {
      console.warn("Microphone access denied, recording tab audio only:", micErr.message);
      micStream = null;
    }

    // 3. Mix streams using Web Audio API
    audioCtx = new AudioContext({ sampleRate: 48000 });
    const dest = audioCtx.createMediaStreamDestination();

    // Tab audio → mixer + playback (so user can still hear the call)
    const tabSource = audioCtx.createMediaStreamSource(tabStream);
    tabSource.connect(dest);
    tabSource.connect(audioCtx.destination); // playback to user

    // Microphone → mixer (no playback to avoid echo)
    if (micStream) {
      const micSource = audioCtx.createMediaStreamSource(micStream);
      micSource.connect(dest);
    }

    // 4. Record the mixed stream
    const mixedStream = dest.stream;
    mediaRecorder = new MediaRecorder(mixedStream, {
      mimeType: "audio/webm;codecs=opus",
    });

    // Upload each chunk directly via sequential queue (no race conditions)
    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        uploadQueue = uploadQueue.then(() => uploadChunk(event.data));
      }
    };

    mediaRecorder.start(10000);
    sendStatus("recording");
  } catch (err) {
    console.error("Start recording error:", err);
    sendStatus("error", { error: err.message });
  }
}

// Stop recording
async function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    // stop() triggers one final ondataavailable with remaining data
    await new Promise((resolve) => {
      mediaRecorder.onstop = resolve;
      mediaRecorder.stop();
    });

    // Wait for all queued uploads (including the final chunk) to complete
    await uploadQueue;
  }

  // Stop all tracks
  if (tabStream) {
    tabStream.getTracks().forEach((t) => t.stop());
    tabStream = null;
  }
  if (micStream) {
    micStream.getTracks().forEach((t) => t.stop());
    micStream = null;
  }
  if (audioCtx) {
    audioCtx.close().catch(() => {});
    audioCtx = null;
  }

  mediaRecorder = null;

  await finishSession();
}

// Listen for messages from the background service worker
chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "start-recording") {
    startRecording(message.streamId, message.serverUrl, message.tabId, message.authUsername, message.authPassword, message.apiKey);
  }
  if (message.type === "stop-recording") {
    stopRecording();
  }
});
