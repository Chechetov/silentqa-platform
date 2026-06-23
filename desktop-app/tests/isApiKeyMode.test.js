const assert = require('assert');

// renderer.js трогает DOM на верхнем уровне и не импортируется напрямую в Node.
// Поэтому чистая логика isApiKeyMode вынесена в src/renderer/mode.js (preferred
// «extractable pure logic» путь из спеки §6) — тестируем РЕАЛЬНУЮ реализацию,
// которую грузит и Electron (<script src="mode.js">), не копию.
const { isApiKeyMode } = require('../src/renderer/mode.js');

// API-key mode = ключ есть И username пуст.
assert.strictEqual(isApiKeyMode({ apiKey: 'k', username: '' }), true);
assert.strictEqual(isApiKeyMode({ apiKey: 'k' }), true);
// username присутствует → broker-mode (realestate), НЕ api-key mode.
assert.strictEqual(isApiKeyMode({ apiKey: 'k', username: 'u' }), false);
// нет ключа → не api-key mode.
assert.strictEqual(isApiKeyMode({ apiKey: '', username: '' }), false);
assert.strictEqual(isApiKeyMode({ username: 'u' }), false);
assert.strictEqual(isApiKeyMode({}), false);
// защита от null/undefined.
assert.strictEqual(isApiKeyMode(null), false);
assert.strictEqual(isApiKeyMode(undefined), false);

console.log('isApiKeyMode OK');
