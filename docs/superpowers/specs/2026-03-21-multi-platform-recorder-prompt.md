# Implementation Prompt: Multi-Platform Recorder

Use this prompt in a new Claude Code session with Dangerously Skip Permissions enabled.

---

## Prompt

Реализуй проект по спецификации `docs/superpowers/specs/2026-03-21-multi-platform-recorder-design.md`.

### Контекст

У нас есть работающее Chrome-расширение в `extension/`. Оно записывает аудио вкладки браузера (tabCapture) + микрофон, миксует через Web Audio API, нарезает на 10-секундные чанки (WebM/Opus) и загружает на бэкенд (FastAPI).

**Важно: продукт должен быть white-label / server-agnostic.** Никаких захардкоженных URL, credentials или брендинга. Приложение при первом запуске спрашивает у пользователя Server URL + логин/пароль. Одна и та же сборка работает с любым совместимым бэкендом.

### Что нужно сделать

**Фаза 1: Yandex Browser Extension**
- Скопируй `extension/` → `extension-yandex/`
- Код менять не нужно — Яндекс.Браузер совместим с Chrome API (tabCapture, offscreen, MV3)
- Создай `extension-yandex/README.md` с инструкцией по установке в Яндекс.Браузер через developer mode (`browser://extensions/`)

**Фаза 2: Electron Desktop App — Core**
- Создай `desktop-app/` со структурой из спецификации (секция 3.7)
- `npm init` + установи зависимости: `electron@latest`, `electron-audio-loopback`, `electron-builder`
- Реализуй Main Process (`src/main/index.js`):
  - `initMain()` из electron-audio-loopback
  - `app.requestSingleInstanceLock()`
  - BrowserWindow: 380x500, frameless, dark background, `contextIsolation: true`, `nodeIntegration: false`
  - System tray с иконками (idle/recording) и контекстным меню (Start/Stop, Show, Quit)
  - Graceful shutdown: сигнал renderer → ждать 10с → force quit
- Реализуй Preload (`src/main/preload.js`):
  - IPC каналы для loopback enable/disable, credentials, tray status, quit signal (см. секцию 3.7.1)
- Реализуй Recorder (`src/renderer/recorder.js`):
  - Системный звук через `getDisplayMedia` + electron-audio-loopback (secure mode)
  - Микрофон через `getUserMedia`
  - Миксование через Web Audio API (AudioContext 48kHz)
  - MediaRecorder webm/opus, чанки 10 секунд
  - Sequential upload queue (как в `extension/offscreen.js`)
  - API: POST /api/sessions, POST /api/sessions/{id}/chunks, POST /api/sessions/{id}/finish
  - Auth: HTTP Basic Auth
  - Metadata source: `"desktop-app"`, platform: `process.platform`
- Реализуй UI (`src/renderer/index.html` + `renderer.js` + `styles.css`):
  - Адаптируй из `extension/popup.html` и `extension/popup.js`
  - Название приложения: **"Call Recorder"** (нейтральное, без брендинга)
  - Тёмная тема (та же что в расширении)
  - **Экран первого запуска (Setup)**: Server URL + Username + Password + кнопка "Test Connection" (GET /api/sessions). При успехе — сохранить в safeStorage, перейти к рекордеру. При ошибке — показать сообщение.
  - Кнопки Start/Stop/Reset, таймер MM:SS, счётчик чанков, статус-бейдж
  - Иконка шестерёнки в header — открывает настройки (Server URL, Username, Password)
  - Рекомендация использовать наушники (маленький текст под кнопкой Start)
  - Предупреждение не мьютить системный звук во время записи

**Фаза 3: Electron App — Polish**
- Настрой `electron-builder.yml` для сборки Windows (.exe NSIS) и macOS (.dmg)
- Добавь иконки приложения (сгенерируй простые placeholder SVG → PNG)
- На macOS: покажи объяснительный диалог перед запросом Screen Recording permission
- Обработка ошибок сети: если upload failed — retry 3 раза с 2с паузой, потом error status

### Важные детали

- **НЕ используй** `nodeIntegration: true` — только secure mode через preload + IPC
- `getDisplayMedia` требует `video: true` — strip video tracks после получения stream
- Код загрузки чанков переиспользуй из `extension/offscreen.js` — та же логика sequential queue
- **Никаких захардкоженных URL и credentials в коде** — всё через настройки при первом запуске. Для тестирования можешь использовать `https://rogov.automate-it.fun`, admin / rogov2025***, но эти значения НЕ должны быть в исходниках, только вводятся руками в UI
- Используй Context7 MCP сервер для получения актуальной документации по Electron и electron-audio-loopback

### Чего НЕ делать

- Не трогай существующее расширение в `extension/`
- Не трогай бэкенд — API уже готов
- Не делай auto-update (будущая фаза)
- Не делай Linux-сборку
