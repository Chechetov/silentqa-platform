// Service Worker — manages tab capture and offscreen document

let recording = false;

// Ensure offscreen document exists
async function ensureOffscreen() {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
  });
  if (contexts.length > 0) return;

  await chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: ["USER_MEDIA"],
    justification: "Recording tab audio for speech analytics",
  });
}

// Start recording the active tab
async function startRecording(tabId) {
  await ensureOffscreen();

  const streamId = await chrome.tabCapture.getMediaStreamId({
    targetTabId: tabId,
  });

  // Get server URL and auth credentials from storage
  const { serverUrl = "http://localhost:8000", authUsername = "", authPassword = "", apiKey = "" } =
    await chrome.storage.local.get(["serverUrl", "authUsername", "authPassword", "apiKey"]);

  // Send to offscreen document to start recording
  chrome.runtime.sendMessage({
    type: "start-recording",
    streamId,
    serverUrl,
    authUsername,
    authPassword,
    apiKey,
    tabId,
  });

  recording = true;
  await chrome.storage.local.set({ recording: true, tabId });
}

// Stop recording
async function stopRecording() {
  chrome.runtime.sendMessage({ type: "stop-recording" });
  recording = false;
  await chrome.storage.local.set({ recording: false, tabId: null });
}

// Listen for messages from popup and offscreen
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "start-capture") {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) {
        startRecording(tabs[0].id)
          .then(() => sendResponse({ success: true }))
          .catch((err) => sendResponse({ success: false, error: err.message }));
      } else {
        sendResponse({ success: false, error: "No active tab" });
      }
    });
    return true; // async response
  }

  if (message.type === "stop-capture") {
    stopRecording()
      .then(() => sendResponse({ success: true }))
      .catch((err) => sendResponse({ success: false, error: err.message }));
    return true;
  }

  if (message.type === "recording-status") {
    // Forward status updates from offscreen to storage for popup
    chrome.storage.local.set({
      sessionId: message.sessionId,
      status: message.status,
      chunksUploaded: message.chunksUploaded,
      error: message.error,
    });
  }

  if (message.type === "recording-stopped") {
    recording = false;
    // Preserve recordingEndedAt if already set by popup stop button
    chrome.storage.local.get(["recordingEndedAt"], (data) => {
      const updates = {
        recording: false,
        status: message.status,
        sessionId: message.sessionId,
      };
      if (!data.recordingEndedAt) {
        updates.recordingEndedAt = Date.now();
      }
      chrome.storage.local.set(updates);
    });
  }
});
