# Multi-Platform Recorder: Yandex Browser Extension + Electron Desktop App

**Date:** 2026-03-21
**Status:** Reviewed

## 1. Overview

Extend the existing Chrome extension to two new platforms:

1. **Yandex Browser Extension** — port of the Chrome extension (minimal changes)
2. **Electron Desktop App** — universal system audio + microphone recorder for Windows and macOS

The Electron app is the primary deliverable. It captures all system audio (Zoom desktop, Telegram, WhatsApp, any app) plus microphone input, mixes them, and uploads chunks to the backend — invisibly to the call participants.

### 1.1 White-Label Architecture

The product is designed to be **server-agnostic**. The app does not contain hardcoded server URLs, credentials, or branding. On first launch, the user configures:
- Server URL (the backend instance)
- Credentials (username/password)

This allows the same app binary to connect to any compatible backend deployment — different clients, different servers. The backend API contract (`/api/sessions`, `/chunks`, `/finish`) is the integration interface.

The app name is **"Call Recorder"** (neutral). Branding can be customized per deployment via config or rebuild.

## 2. Yandex Browser Extension

### 2.1 Approach

Yandex Browser is Chromium-based and supports Chrome extension APIs including `tabCapture`, `offscreen`, and Manifest V3. The existing extension should work with minimal modifications.

### 2.2 Required Changes

| File | Change | Reason |
|------|--------|--------|
| `manifest.json` | No changes needed | MV3 is supported, all APIs are compatible |
| `background.js` | No changes needed | `chrome.*` namespace works in Yandex |
| `offscreen.js` | No changes needed | Same Chromium audio stack |
| `popup.js` | No changes needed | Pure DOM/JS |
| `popup.html` | No changes needed | Standard HTML/CSS |

### 2.3 Distribution Changes

- Yandex Browser installs extensions from Chrome Web Store or Opera Addons
- Alternative: sideload via `browser://extensions/` in developer mode (for internal use)
- Create a separate ZIP package with identical code

### 2.4 Testing Checklist

- [ ] Install extension via developer mode in Yandex Browser
- [ ] Verify `tabCapture` works (start recording on a tab with audio)
- [ ] Verify microphone permission flow
- [ ] Verify chunk upload to backend
- [ ] Verify session creation and finish endpoints
- [ ] Test on Zoom Web, Google Meet, Telegram Web

### 2.5 Potential Issues

- Yandex may lag behind Chrome's Chromium version — if `offscreen` API is unavailable in older versions, fallback to background page approach
- `host_permissions` may need testing — verify CORS bypass works identically

## 3. Electron Desktop App

### 3.1 Architecture Overview

```
┌─────────────────────────────────────────────┐
│              Electron App                    │
│  ┌────────────────┐  ┌───────────────────┐  │
│  │  Main Process   │  │ Renderer (UI)     │  │
│  │                 │  │                   │  │
│  │ - App lifecycle │  │ - Start/Stop btn  │  │
│  │ - Tray icon     │  │ - Timer display   │  │
│  │ - Audio init    │  │ - Status badge    │  │
│  │ - Auto-start    │  │ - Session info    │  │
│  │                 │  │ - Chunk counter   │  │
│  └───────┬─────────┘  └───────┬───────────┘  │
│          │                    │               │
│  ┌───────▼────────────────────▼───────────┐  │
│  │         Audio Capture Layer             │  │
│  │                                         │  │
│  │  System Audio (electron-audio-loopback) │  │
│  │  + Microphone (getUserMedia)            │  │
│  │  → Web Audio API mixer                  │  │
│  │  → MediaRecorder (WebM/Opus)            │  │
│  │  → 10s chunks → HTTP upload             │  │
│  └─────────────────────────────────────────┘  │
└──────────────────────────┬────────────────────┘
                           │ HTTPS POST
                           ▼
                 ┌─────────────────┐
                 │  Existing Backend │
                 │  (FastAPI)        │
                 │  /api/sessions    │
                 │  /api/chunks      │
                 │  /api/finish      │
                 └─────────────────┘
```

### 3.2 Technology Stack

| Component | Technology | Version |
|-----------|-----------|---------|
| Framework | Electron | >= 31.0.1 |
| System Audio | electron-audio-loopback | latest |
| Microphone | navigator.mediaDevices.getUserMedia | Web API |
| Audio Mixing | Web Audio API (AudioContext) | Web API |
| Recording | MediaRecorder | Web API |
| Codec | WebM/Opus | Same as extension |
| HTTP Client | fetch (renderer) or node-fetch (main) | Built-in |
| UI | Vanilla HTML/CSS/JS | No framework |
| Packaging | electron-builder | latest |

### 3.3 System Audio Capture

Uses `electron-audio-loopback` npm package which leverages hidden Chromium flags:

- **Windows 10+**: Built-in WASAPI loopback through Chromium
- **macOS 12.3+**: `MacLoopbackAudioForScreenShare` + `MacSckSystemAudioLoopbackOverride` flags
- **Linux**: `PulseaudioLoopbackForScreenShare` (bonus — not primary target)

No third-party drivers needed. No BlackHole. No virtual audio devices.

**Important**: The library works by intercepting `getDisplayMedia()` and suppressing the screen-share picker. In secure Electron mode (`contextIsolation: true`, `nodeIntegration: false`), the "manual mode" with IPC is required:

```javascript
// Main process (index.js)
const { initMain } = require('electron-audio-loopback');
initMain();

// Preload script (preload.js) — expose IPC channels
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('audioLoopback', {
  enable: () => ipcRenderer.invoke('enable-loopback-audio'),
  disable: () => ipcRenderer.invoke('disable-loopback-audio'),
});

// Renderer process (recorder.js) — get system audio
await window.audioLoopback.enable();
// Must request video: true — library requirement, video tracks stripped after
const displayStream = await navigator.mediaDevices.getDisplayMedia({
  video: true,
  audio: true,
});
// Strip video tracks — we only need audio
displayStream.getVideoTracks().forEach(track => {
  track.stop();
  displayStream.removeTrack(track);
});
const systemStream = displayStream; // now audio-only
```

**macOS note**: On first launch, macOS will show a "Screen Recording" permission dialog even though we only capture audio. The app should show an explanatory dialog before triggering the system prompt: "The app needs Screen Recording permission to capture system audio. No video is recorded."

### 3.4 Audio Pipeline (reuses extension logic)

```
System Audio Stream ──┐
                      ├──► AudioContext (48kHz) ──► MediaStreamDestination
Microphone Stream ────┘                                      │
                                                             ▼
                                                      MediaRecorder
                                                    (webm/opus, 10s chunks)
                                                             │
                                                             ▼
                                                    Sequential Upload Queue
                                                    POST /api/sessions/{id}/chunks
```

Key difference from extension: system audio comes from `electron-audio-loopback` instead of `tabCapture`. The rest of the pipeline (mixing, recording, chunking, upload) is **identical**.

### 3.5 UI Design

The app window reuses the same dark theme as the extension popup, adapted for a desktop window:

**Window:**
- Size: 380 x 500 px (slightly larger than popup)
- Always-on-top option
- Minimize to system tray
- frameless window with custom title bar

**System Tray:**
- Tray icon with recording status indicator (green = ready, red = recording)
- Right-click menu: Start/Stop, Show Window, Quit
- Left-click: toggle window visibility

**UI Components (same as extension):**
- Header: "Call Recorder" + version badge
- Status badge with animated dot (Ready / Recording / Processing / Completed / Error)
- Timer (MM:SS) with chunk counter
- Start/Stop/Reset buttons
- Session info panel with links to dashboard
- Error display
- Settings panel (first launch or gear icon): Server URL, Username, Password

### 3.6 Key Differences from Extension

| Aspect | Chrome Extension | Electron App |
|--------|-----------------|--------------|
| Audio source | Tab audio (tabCapture) | System audio (loopback) |
| Scope | Single browser tab | All system audio (any app) |
| Microphone | getUserMedia | getUserMedia (same) |
| Mixing | Web Audio API | Web Audio API (same) |
| Chunking | MediaRecorder 10s | MediaRecorder 10s (same) |
| Upload | fetch + Basic Auth | fetch + Basic Auth (same) |
| UI framework | Extension popup | Electron BrowserWindow |
| Distribution | Chrome Web Store / ZIP | Installer (exe/dmg) |
| Persistence | chrome.storage.local | electron-store or localStorage |
| Tray | N/A | System tray with status |

### 3.7 File Structure

```
desktop-app/
├── package.json
├── electron-builder.yml        # Build config for Windows/macOS
├── src/
│   ├── main/
│   │   ├── index.js            # Main process: app lifecycle, tray, window
│   │   └── preload.js          # Preload script: IPC for loopback, credentials, tray
│   ├── renderer/
│   │   ├── index.html          # UI (adapted from popup.html)
│   │   ├── renderer.js         # UI logic (adapted from popup.js)
│   │   ├── recorder.js         # Audio capture + upload (adapted from offscreen.js)
│   │   └── styles.css          # Styles (extracted from popup.html)
│   └── assets/
│       ├── icon.png            # App icon
│       ├── tray-idle.png       # Tray icon (ready)
│       ├── tray-recording.png  # Tray icon (recording)
│       └── icon.ico            # Windows icon
├── build/                      # electron-builder output
└── README.md
```

### 3.7.1 Preload Script IPC Channels

The preload script (`preload.js`) exposes the following IPC channels to the renderer:

| Channel | Direction | Purpose |
|---------|-----------|---------|
| `enable-loopback-audio` | renderer → main | Enable system audio loopback capture |
| `disable-loopback-audio` | renderer → main | Disable system audio loopback |
| `get-credentials` | renderer → main | Read server URL + credentials from secure storage |
| `save-credentials` | renderer → main | Save server URL + credentials to secure storage |
| `update-tray-status` | renderer → main | Update tray icon (idle/recording/error) |
| `recording-state` | main → renderer | Notify renderer of tray-initiated start/stop |
| `app-before-quit` | main → renderer | Signal graceful shutdown — renderer must finish uploads |

### 3.8 Main Process (src/main/index.js)

Responsibilities:
- Initialize `electron-audio-loopback` via `initMain()`
- Call `app.requestSingleInstanceLock()` to prevent multiple instances
- Create BrowserWindow (380x500, frameless, dark background, `contextIsolation: true`, `nodeIntegration: false`)
- Create system tray with context menu
- Handle window show/hide on tray click
- Handle `app-before-quit`: send signal to renderer, wait up to 10s for upload queue drain, then force quit
- Credential storage via `safeStorage` API (OS-level encryption, not `electron-store` obfuscation)
- Optional: auto-start with OS (user preference)

**Graceful shutdown flow:**
1. User clicks Quit (or Cmd+Q / Alt+F4)
2. Main process sends `app-before-quit` to renderer via IPC
3. Renderer stops MediaRecorder, waits for final chunk upload, calls `/finish`
4. Renderer sends `quit-ready` back to main
5. Main calls `app.quit()` — timeout: 10 seconds, then force quit
6. If network is down: log warning, abandon partial session (backend handles orphaned sessions)

### 3.9 Renderer Process (src/renderer/recorder.js)

Core recording logic — adapted from `offscreen.js`:

```javascript
// 1. Get system audio via electron-audio-loopback (secure mode)
await window.audioLoopback.enable();
const displayStream = await navigator.mediaDevices.getDisplayMedia({
  video: true,  // required by library — will be stripped
  audio: true,
});
// Strip video tracks — audio only
displayStream.getVideoTracks().forEach(track => {
  track.stop();
  displayStream.removeTrack(track);
});

// 2. Get microphone
const micStream = await navigator.mediaDevices.getUserMedia({
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
});

// 3. Mix using Web Audio API (identical to extension)
const audioCtx = new AudioContext({ sampleRate: 48000 });
const dest = audioCtx.createMediaStreamDestination();

const sysSource = audioCtx.createMediaStreamSource(displayStream);
sysSource.connect(dest);
// No playback needed — user already hears system audio natively

const micSource = audioCtx.createMediaStreamSource(micStream);
micSource.connect(dest);

// 4. Record mixed stream (identical to extension)
const recorder = new MediaRecorder(dest.stream, {
  mimeType: "audio/webm;codecs=opus"
});
recorder.start(10000); // 10-second chunks

// 5. Upload chunks (identical to extension)
recorder.ondataavailable = (e) => {
  if (e.data.size > 0) {
    uploadQueue = uploadQueue.then(() => uploadChunk(e.data));
  }
};
```

**Note on headphones**: If the user is not wearing headphones, the loopback captures the microphone output played through speakers, creating duplicate audio in the recording. The UI should recommend headphones for best quality, but this does not break functionality — it just adds echo to the recording.

### 3.10 API Integration

Uses the **exact same backend API** as the extension:

1. `POST /api/sessions` — create session with metadata `{ source: "desktop-app", platform: "windows|macos" }`
2. `POST /api/sessions/{id}/chunks` — upload audio chunk
3. `POST /api/sessions/{id}/finish` — trigger transcription pipeline

Authentication: HTTP Basic Auth (same credentials).

### 3.11 Packaging & Distribution

**Windows:**
- Format: NSIS installer (.exe) via electron-builder
- Code signing: optional (Windows SmartScreen warning without it)
- Auto-update: electron-updater (optional, future)

**macOS:**
- Format: DMG with .app bundle via electron-builder
- Code signing: Apple Developer certificate required for Gatekeeper
- Without signing: users must allow in System Preferences > Security
- ScreenCaptureKit requires macOS permission prompt on first use

### 3.12 First Launch & Configuration

On first launch (no saved credentials), the app shows a setup screen instead of the recorder UI:

1. **Server URL** — required, e.g. `https://analytics.example.com`
2. **Username** — required
3. **Password** — required
4. **Test Connection** button — calls `GET /api/sessions` to verify URL + credentials
5. On success → save to `safeStorage`, show recorder UI
6. Settings accessible later via gear icon in header

### 3.13 Metadata Source Differentiation

The backend stores `metadata.source` per session. The desktop app sends:
```json
{
  "source": "desktop-app",
  "platform": "windows",  // or "macos"
  "recordedAt": "2026-03-21T10:30:00Z"
}
```

This allows the dashboard to distinguish between browser extension and desktop app recordings.

## 4. Security Considerations

- **No hardcoded credentials or server URLs** — everything configured by the user on first launch
- Credentials stored via Electron `safeStorage` API (OS-level keychain on macOS, DPAPI on Windows)
- **Note**: The existing Chrome extension still has hardcoded credentials in `popup.js` (lines 21-23). Should be moved to a settings flow in a future update.
- HTTPS enforced for all API calls
- No recording indicator visible to other call participants
- System tray icon visible only to the user running the app
- `contextIsolation: true`, `nodeIntegration: false` — secure Electron defaults
- Single instance lock prevents multiple app copies running simultaneously

## 5. Implementation Order

1. **Phase 1: Yandex Browser Extension** (1-2 hours)
   - Copy extension directory
   - Test in Yandex Browser
   - Document any differences

2. **Phase 2: Electron App — Core** (1-2 days)
   - Set up Electron project with electron-audio-loopback
   - Implement audio capture + mixing + chunking
   - Implement upload to backend
   - Basic UI (start/stop/timer)

3. **Phase 3: Electron App — Polish** (1 day)
   - System tray integration
   - Frameless window with custom title bar
   - Error handling and reconnection
   - Settings (server URL, credentials)

4. **Phase 4: Packaging** (0.5 day)
   - electron-builder config for Windows (.exe)
   - electron-builder config for macOS (.dmg)
   - Test installers on both platforms

## 6. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| `electron-audio-loopback` doesn't work on specific OS version | High | Fall back to `navigator.mediaDevices.getDisplayMedia()` with manual screen share picker (worse UX but functional) |
| macOS Screen Recording permission denied | Medium | Show explanatory dialog before system prompt; provide instructions for System Preferences > Privacy > Screen Recording |
| Yandex Browser lacks `offscreen` API | Low | Check `typeof chrome.offscreen` on install; if missing, use background page approach |
| No headphones — echo in recording | Low | Show recommendation in UI; does not break recording, only degrades quality |
| Electron app size (~150MB) | Low | Acceptable for desktop app; compress with ASAR |
| Network failure mid-recording | Medium | Chunks continue recording locally; retry upload on reconnect; log warning on app quit with unsent chunks |
| WebM/Opus not supported in some Electron version | Very Low | Chromium supports it natively; pinned to Electron >= 31 |

## 7. Out of Scope

- Linux packaging (may work via PulseAudio loopback, but not a target platform)
- Auto-update mechanism (Phase 5, future)
- Backend changes for source filtering/differentiation (dashboard update, separate task)
- Fixing hardcoded credentials in existing Chrome extension (separate task)
