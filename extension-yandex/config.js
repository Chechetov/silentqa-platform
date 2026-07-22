// Чистая конфиг-логика расширения — без chrome.* и DOM, чтобы гоняться
// node-юнитами (паттерн desktop-app/src/renderer/mode.js). В popup
// подключается <script src="config.js"> ПЕРЕД popup.js.

// Нормализация адреса сервера: https:// по умолчанию, без хвостовых слэшей.
// Семантика 1-в-1 с desktop-app/src/renderer/mode.js (осознанный дубль:
// общего модуля между расширением и десктопом нет).
function normalizeServerUrl(raw) {
  let s = String(raw == null ? '' : raw).trim();
  if (!s) return '';
  s = s.replace(/\/+$/, '');
  if (!/^https?:\/\//i.test(s)) s = 'https://' + s;
  return s;
}

// Готовность к записи: нужен и сервер, и API-ключ тенанта.
function validateConfig(c) {
  const serverUrl = normalizeServerUrl(c && c.serverUrl);
  const apiKey = String((c && c.apiKey) || '').trim();
  if (!serverUrl) return { ok: false, error: 'Укажите адрес сервера в настройках' };
  if (!apiKey) return { ok: false, error: 'Укажите API-ключ в настройках' };
  return { ok: true, serverUrl, apiKey };
}

// Node (юнит-тест): CommonJS-экспорт. В браузере функции и так глобальные.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { normalizeServerUrl, validateConfig };
}
