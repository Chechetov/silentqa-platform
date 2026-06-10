const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  credentials: {
    get: () => ipcRenderer.invoke('get-credentials'),
    save: (creds) => ipcRenderer.invoke('save-credentials', creds),
  },

  // Audio loopback control (required by electron-audio-loopback)
  enableLoopbackAudio: () => ipcRenderer.invoke('enable-loopback-audio'),
  disableLoopbackAudio: () => ipcRenderer.invoke('disable-loopback-audio'),

  updateTrayStatus: (status) => ipcRenderer.send('update-tray-status', status),

  // Window controls via IPC (not window.close)
  minimizeWindow: () => ipcRenderer.send('minimize-window'),
  closeWindow: () => ipcRenderer.send('close-window'),

  // Open URL in system browser (not Electron window)
  openExternal: (url) => ipcRenderer.send('open-external', url),

  // Graceful shutdown — use .once to prevent double-fire
  onBeforeQuit: (callback) => {
    ipcRenderer.once('app-before-quit', () => callback());
  },
  sendQuitReady: () => ipcRenderer.send('quit-ready'),

  // Persistent buffer for not-yet-uploaded chunks. Every method returns
  // { ok: bool, error?: string, ... } so the renderer can fall back to its
  // in-memory queue if disk I/O is unavailable. None throw.
  chunkStore: {
    put: (sessionId, chunkNumber, arrayBuffer, meta) =>
      ipcRenderer.invoke('chunkStore:put', sessionId, chunkNumber, arrayBuffer, meta),
    delete: (sessionId, chunkNumber) =>
      ipcRenderer.invoke('chunkStore:delete', sessionId, chunkNumber),
    deleteSession: (sessionId) =>
      ipcRenderer.invoke('chunkStore:deleteSession', sessionId),
    read: (sessionId, chunkNumber) =>
      ipcRenderer.invoke('chunkStore:read', sessionId, chunkNumber),
    list: () => ipcRenderer.invoke('chunkStore:list'),
  },

  platform: process.platform,
});
