# Rogov Recorder Desktop

Electron desktop app for recording system audio + microphone. Captures all system audio (Zoom, Telegram, WhatsApp, any app) and uploads chunks to the backend for speech analytics.

## Setup

```bash
cd desktop-app
npm install
```

## Development

```bash
npm start
```

## Build

```bash
# Windows (.exe NSIS installer)
npm run build:win

# macOS (.dmg)
npm run build:mac

# Both
npm run build
```

## Architecture

- **Main Process** (`src/main/index.js`): App lifecycle, system tray, credential storage (safeStorage), IPC
- **Preload** (`src/main/preload.js`): Secure IPC bridge (contextIsolation: true, nodeIntegration: false)
- **Recorder** (`src/renderer/recorder.js`): System audio capture via electron-audio-loopback + microphone mixing + sequential chunk upload
- **UI** (`src/renderer/`): Dark theme matching the Chrome extension, frameless window with custom title bar

## Audio Pipeline

```
System Audio (electron-audio-loopback) ──┐
                                          ├── AudioContext (48kHz) → MediaRecorder (WebM/Opus, 10s chunks)
Microphone (getUserMedia) ───────────────┘                                    │
                                                                              ▼
                                                                    Sequential Upload Queue
                                                                    POST /api/sessions/{id}/chunks
```

## Platform Notes

- **Windows 10+**: Built-in WASAPI loopback through Chromium
- **macOS 12.3+**: Screen Recording permission required for system audio capture (no video is actually recorded)
- Use headphones for best quality (avoids echo from speakers being captured by microphone)
