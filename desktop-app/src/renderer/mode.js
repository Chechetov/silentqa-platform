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

// Браузер (Electron renderer): кладём в window для использования из renderer.js.
if (typeof window !== 'undefined') {
  window.isApiKeyMode = isApiKeyMode;
}

// Node (юнит-тест): экспортируем через CommonJS.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { isApiKeyMode };
}
