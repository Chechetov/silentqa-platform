// Чистая логика дискриминатора режима записи — без зависимостей от DOM,
// чтобы её можно было импортировать в Node-юните (renderer.js трогает DOM
// на верхнем уровне и не импортируется напрямую). В Electron подключается
// <script src="mode.js"> ПЕРЕД renderer.js и кладёт isApiKeyMode в window.

// API-key mode: тенант без AmoCRM-брокеров (fulldent) — API-ключ есть,
// username пуст → broker-логин не нужен, сразу экран записи.
// realestate вводит username/password → broker-mode, поток не меняется.
function isApiKeyMode(c) {
  return !!(c && c.apiKey && !c.username);
}

// Нормализация адреса сервера: добавляет https:// если схема не указана и срезает
// хвостовые слэши. Без этого fetch(url + '/api/...') при URL без схемы (напр.
// "host.com") резолвится относительно file:// и падает с "failed to fetch".
function normalizeServerUrl(raw) {
  let s = String(raw == null ? '' : raw).trim();
  if (!s) return '';
  s = s.replace(/\/+$/, '');
  if (!/^https?:\/\//i.test(s)) s = 'https://' + s;
  return s;
}

// Браузер (Electron renderer): кладём в window для использования из renderer.js.
if (typeof window !== 'undefined') {
  window.isApiKeyMode = isApiKeyMode;
  window.normalizeServerUrl = normalizeServerUrl;
}

// Node (юнит-тест): экспортируем через CommonJS.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { isApiKeyMode, normalizeServerUrl };
}
