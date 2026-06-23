const assert = require('assert');

// Чистая логика нормализации URL вынесена в src/renderer/mode.js (как isApiKeyMode),
// чтобы тестировать РЕАЛЬНУЮ реализацию, которую грузит Electron (<script src="mode.js">).
const { normalizeServerUrl } = require('../src/renderer/mode.js');

// Без схемы → подставляем https:// (иначе fetch падает "failed to fetch").
assert.strictEqual(normalizeServerUrl('chechetov.silentqa.com'), 'https://chechetov.silentqa.com');
// Уже со схемой → не трогаем.
assert.strictEqual(normalizeServerUrl('https://x.silentqa.com'), 'https://x.silentqa.com');
assert.strictEqual(normalizeServerUrl('http://x.local'), 'http://x.local');
// Схема в верхнем регистре распознаётся (не дублируем https://).
assert.strictEqual(normalizeServerUrl('HTTPS://x.com'), 'HTTPS://x.com');
// Хвостовые слэши срезаются.
assert.strictEqual(normalizeServerUrl('https://x.silentqa.com/'), 'https://x.silentqa.com');
assert.strictEqual(normalizeServerUrl('chechetov.silentqa.com///'), 'https://chechetov.silentqa.com');
// Пробелы обрезаются.
assert.strictEqual(normalizeServerUrl('  chechetov.silentqa.com  '), 'https://chechetov.silentqa.com');
// Пустое / мусор / null-safety.
assert.strictEqual(normalizeServerUrl(''), '');
assert.strictEqual(normalizeServerUrl('   '), '');
assert.strictEqual(normalizeServerUrl(null), '');
assert.strictEqual(normalizeServerUrl(undefined), '');

console.log('normalizeServerUrl OK');
