const { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, safeStorage, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const fsp = require('fs/promises');
const crypto = require('crypto');

// macOS system-audio loopback backend selection (ported from the working v3 build).
// The available capture backend depends on the macOS VERSION, not the CPU arch:
//   • CoreAudio process taps (CATap, "MacCatapSystemAudioLoopbackCapture")  — macOS 14.2+
//   • ScreenCaptureKit       ("MacSckSystemAudioLoopbackOverride")          — macOS 13.0+
// Forcing CATap on an unsupported system makes the audio source fail to start.
function macOSSupportsCoreAudioTap() {
  if (process.platform !== 'darwin') return false;
  const ver = typeof process.getSystemVersion === 'function' ? process.getSystemVersion() : '0';
  const [major, minor = 0] = ver.split('.').map((n) => parseInt(n, 10) || 0);
  return major > 14 || (major === 14 && minor >= 2);
}

const isAppleSilicon = process.arch === 'arm64';
// Manual override for A/B testing: AUDIO_BACKEND=sck | catap
let useCoreAudioTap;
if (process.env.AUDIO_BACKEND === 'catap') useCoreAudioTap = true;
else if (process.env.AUDIO_BACKEND === 'sck') useCoreAudioTap = false;
// Apple Silicon: ScreenCaptureKit is the proven path. Intel: CATap only on macOS 14.2+.
else useCoreAudioTap = !isAppleSilicon && macOSSupportsCoreAudioTap();

// Enable macOS system-audio loopback via Chromium flags — MUST be before app.ready.
// Without these, getDisplayMedia({ audio: true }) fails with "Not supported".
app.commandLine.appendSwitch('enable-features',
  useCoreAudioTap
    ? 'MacLoopbackAudioForScreenShare,MacCatapSystemAudioLoopbackCapture'
    : 'MacLoopbackAudioForScreenShare,MacSckSystemAudioLoopbackOverride');

// electron-audio-loopback registers an IPC handler + merges flags. The ACTIVE
// getDisplayMedia handler is the manual one set in createWindow() below.
try {
  const { initMain } = require('electron-audio-loopback');
  initMain({ forceCoreAudioTap: useCoreAudioTap });
} catch (err) {
  console.warn('electron-audio-loopback not available, using manual setup:', err.message);
}

// Single instance lock
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
}

let mainWindow = null;
let tray = null;
let isQuitting = false;

const credentialsPath = path.join(app.getPath('userData'), 'credentials.enc');

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 420,
    height: 700,
    frame: true, // native frame — enables menu bar + DevTools access
    resizable: true,
    backgroundColor: '#FAF8F3', // warm light theme bg (was dark #111827 → flash)
    show: false,
    title: 'SilentQA Recorder',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));

  // DevTools: auto-open only in dev (npm start). In a packaged .dmg it is NEVER
  // auto-opened for end users — still reachable on demand via the shortcut below.
  if (!app.isPackaged) {
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }
  // (on-demand F12 / Cmd+Opt+I toggle handler already exists below)

  // System-audio capture: auto-answer getDisplayMedia with the screen + 'loopback'
  // audio (no picker dialog shown to the user). This ACTIVE handler overrides the
  // library's; without it getDisplayMedia({ audio: true }) fails with "Not supported".
  try {
    const { desktopCapturer, session } = require('electron');
    session.defaultSession.setDisplayMediaRequestHandler((_request, callback) => {
      desktopCapturer.getSources({ types: ['screen'] }).then((sources) => {
        if (sources.length === 0) { callback({}); return; }
        callback({ video: sources[0], audio: 'loopback' });
      }).catch(() => callback({}));
    });
  } catch (err) {
    console.warn('Failed to set display media handler:', err.message);
  }

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  // Close event → graceful quit
  mainWindow.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault();
      gracefulQuit();
    }
  });

  // DevTools shortcut: F12 or Cmd+Option+I
  mainWindow.webContents.on('before-input-event', (_event, input) => {
    if (input.key === 'F12' ||
        (input.meta && input.alt && input.key.toLowerCase() === 'i') ||
        (input.control && input.shift && input.key.toLowerCase() === 'i')) {
      mainWindow.webContents.toggleDevTools();
    }
  });

}

function getTrayIcon(status) {
  const iconName = status === 'recording' ? 'tray-recording.png' : 'tray-idle.png';
  const iconPath = path.join(__dirname, '..', 'assets', iconName);
  try {
    return nativeImage.createFromPath(iconPath).resize({ width: 16, height: 16 });
  } catch {
    return nativeImage.createEmpty();
  }
}

function createTray() {
  tray = new Tray(getTrayIcon('idle'));

  const contextMenu = Menu.buildFromTemplate([
    {
      label: 'Show Window',
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        }
      },
    },
    { type: 'separator' },
    {
      label: 'Quit',
      click: () => gracefulQuit(),
    },
  ]);

  tray.setToolTip('SilentQA Recorder');
  tray.setContextMenu(contextMenu);

  tray.on('click', () => {
    if (mainWindow) {
      if (mainWindow.isVisible()) {
        mainWindow.hide();
      } else {
        mainWindow.show();
        mainWindow.focus();
      }
    }
  });
}

// Graceful shutdown
function gracefulQuit() {
  if (isQuitting) return;
  isQuitting = true;

  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('app-before-quit');

    const timeout = setTimeout(() => {
      console.warn('Graceful shutdown timeout — force quitting');
      app.exit(0);
    }, 10000);

    ipcMain.once('quit-ready', () => {
      clearTimeout(timeout);
      app.exit(0);
    });
  } else {
    app.exit(0);
  }
}

// --- IPC Handlers ---

ipcMain.handle('get-credentials', () => {
  try {
    if (fs.existsSync(credentialsPath)) {
      if (safeStorage.isEncryptionAvailable()) {
        const encrypted = fs.readFileSync(credentialsPath);
        const decrypted = safeStorage.decryptString(encrypted);
        return JSON.parse(decrypted);
      }
      return JSON.parse(fs.readFileSync(credentialsPath, 'utf-8'));
    }
  } catch (err) {
    console.error('Failed to read credentials:', err.message);
  }
  return null;
});

ipcMain.handle('save-credentials', (_event, credentials) => {
  try {
    if (safeStorage.isEncryptionAvailable()) {
      const encrypted = safeStorage.encryptString(JSON.stringify(credentials));
      fs.writeFileSync(credentialsPath, encrypted);
    } else {
      fs.writeFileSync(credentialsPath, JSON.stringify(credentials), 'utf-8');
    }
    return { success: true };
  } catch (err) {
    console.error('Failed to save credentials:', err.message);
    return { success: false, error: err.message };
  }
});

ipcMain.on('update-tray-status', (_event, status) => {
  if (tray && !tray.isDestroyed()) {
    tray.setImage(getTrayIcon(status));
  }
});

ipcMain.on('minimize-window', () => {
  if (mainWindow) mainWindow.hide();
});

ipcMain.on('close-window', () => {
  if (mainWindow) mainWindow.close();
});

ipcMain.on('open-external', (_event, url) => {
  if (url && (url.startsWith('https://') || url.startsWith('http://'))) {
    shell.openExternal(url);
  }
});

// --- chunkStore: persistent buffer for not-yet-uploaded chunks ---
//
// Layout:  <userData>/pending-chunks/<sessionId>/<NNNNNN>.webm  (binary)
//          <userData>/pending-chunks/<sessionId>/<NNNNNN>.json  (sidecar)
//
// The renderer writes a chunk to disk BEFORE attempting upload. On a
// successful upload it asks us to delete that chunk. If the app crashes / OS
// sleeps / power dies between write and upload, the chunk survives — on next
// launch we scan and resume uploads.
//
// Every handler is best-effort: it returns { ok: bool, error?: string } so
// the renderer can fall back to in-memory upload if the disk path is broken
// (read-only $HOME, full disk, antivirus lock, etc.). We never throw across
// the IPC boundary — exceptions on the renderer side from `invoke` would
// take down the upload loop.

const chunkStoreRoot = path.join(app.getPath('userData'), 'pending-chunks');

function chunkPaths(sessionId, chunkNumber) {
  // sessionId is a UUID from the server; basic sanitization rejects path
  // traversal but accepts hex/uuid forms. chunkNumber is integer.
  const safeSid = String(sessionId).replace(/[^a-zA-Z0-9-]/g, '');
  const num = String(chunkNumber).padStart(6, '0');
  if (!safeSid || !/^[0-9]+$/.test(num)) {
    throw new Error('invalid sessionId/chunkNumber');
  }
  const dir = path.join(chunkStoreRoot, safeSid);
  return {
    dir,
    binPath: path.join(dir, `${num}.webm`),
    metaPath: path.join(dir, `${num}.json`),
  };
}

async function atomicWriteBinary(target, buffer) {
  const tmp = `${target}.tmp.${crypto.randomBytes(4).toString('hex')}`;
  try {
    await fsp.writeFile(tmp, buffer);
    await fsp.rename(tmp, target);
  } catch (err) {
    try { await fsp.unlink(tmp); } catch (_) {}
    throw err;
  }
}

ipcMain.handle('chunkStore:put', async (_event, sessionId, chunkNumber, arrayBuffer, meta) => {
  try {
    const { dir, binPath, metaPath } = chunkPaths(sessionId, chunkNumber);
    await fsp.mkdir(dir, { recursive: true });
    const buf = Buffer.from(arrayBuffer);
    await atomicWriteBinary(binPath, buf);
    const sidecar = JSON.stringify({
      sessionId,
      chunkNumber,
      size: buf.length,
      savedAt: new Date().toISOString(),
      ...(meta || {}),
    });
    await atomicWriteBinary(metaPath, Buffer.from(sidecar, 'utf-8'));
    return { ok: true };
  } catch (err) {
    console.error('[chunkStore:put]', err.message);
    return { ok: false, error: err.message };
  }
});

ipcMain.handle('chunkStore:delete', async (_event, sessionId, chunkNumber) => {
  try {
    const { binPath, metaPath } = chunkPaths(sessionId, chunkNumber);
    await fsp.unlink(binPath).catch((e) => { if (e.code !== 'ENOENT') throw e; });
    await fsp.unlink(metaPath).catch((e) => { if (e.code !== 'ENOENT') throw e; });
    return { ok: true };
  } catch (err) {
    console.error('[chunkStore:delete]', err.message);
    return { ok: false, error: err.message };
  }
});

ipcMain.handle('chunkStore:deleteSession', async (_event, sessionId) => {
  try {
    const safeSid = String(sessionId).replace(/[^a-zA-Z0-9-]/g, '');
    if (!safeSid) return { ok: false, error: 'invalid sessionId' };
    const dir = path.join(chunkStoreRoot, safeSid);
    await fsp.rm(dir, { recursive: true, force: true });
    return { ok: true };
  } catch (err) {
    console.error('[chunkStore:deleteSession]', err.message);
    return { ok: false, error: err.message };
  }
});

ipcMain.handle('chunkStore:read', async (_event, sessionId, chunkNumber) => {
  try {
    const { binPath } = chunkPaths(sessionId, chunkNumber);
    const buf = await fsp.readFile(binPath);
    // Return a transferable ArrayBuffer slice.
    return { ok: true, data: buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) };
  } catch (err) {
    if (err.code === 'ENOENT') return { ok: false, error: 'not found' };
    console.error('[chunkStore:read]', err.message);
    return { ok: false, error: err.message };
  }
});

ipcMain.handle('chunkStore:list', async () => {
  try {
    let sessionDirs;
    try {
      sessionDirs = await fsp.readdir(chunkStoreRoot, { withFileTypes: true });
    } catch (err) {
      if (err.code === 'ENOENT') return { ok: true, items: [] };
      throw err;
    }
    const items = [];
    for (const sd of sessionDirs) {
      if (!sd.isDirectory()) continue;
      const sessionId = sd.name;
      const sdir = path.join(chunkStoreRoot, sessionId);
      const files = await fsp.readdir(sdir).catch(() => []);
      for (const f of files) {
        if (!f.endsWith('.webm')) continue;
        const num = parseInt(f.replace('.webm', ''), 10);
        if (!Number.isFinite(num)) continue;
        const metaFile = path.join(sdir, f.replace('.webm', '.json'));
        let meta = null;
        try {
          meta = JSON.parse(await fsp.readFile(metaFile, 'utf-8'));
        } catch (_) {
          // Sidecar missing/corrupt — fall back to filename info.
        }
        const stat = await fsp.stat(path.join(sdir, f)).catch(() => null);
        items.push({
          sessionId,
          chunkNumber: num,
          size: stat ? stat.size : (meta && meta.size) || 0,
          savedAt: (meta && meta.savedAt) || (stat && stat.mtime.toISOString()) || null,
          meta: meta || {},
        });
      }
    }
    return { ok: true, items };
  } catch (err) {
    console.error('[chunkStore:list]', err.message);
    return { ok: false, error: err.message, items: [] };
  }
});

// --- App Lifecycle ---

app.on('ready', () => {
  createWindow();
  createTray();
});

app.on('second-instance', () => {
  if (mainWindow) {
    mainWindow.show();
    mainWindow.focus();
  }
});

app.on('before-quit', (e) => {
  if (!isQuitting) {
    e.preventDefault();
    gracefulQuit();
  }
});

app.on('window-all-closed', () => {});

app.on('activate', () => {
  if (mainWindow) mainWindow.show();
});
