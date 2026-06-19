const { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, safeStorage, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const fsp = require('fs/promises');
const crypto = require('crypto');

// Initialize electron-audio-loopback (sets up setDisplayMediaRequestHandler + Chromium flags)
const { initMain } = require('electron-audio-loopback');
initMain();

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
    height: 580,
    frame: true, // native frame — enables menu bar + DevTools access
    resizable: true,
    backgroundColor: '#111827',
    show: false,
    title: 'SilentQA Recorder',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));

  // Open DevTools automatically for debugging
  mainWindow.webContents.openDevTools({ mode: 'detach' });

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
