const assert = require('assert');

// Чистая логика конфига вынесена в config.js без chrome.*/DOM,
// чтобы гоняться node-юнитом (паттерн desktop-app/src/renderer/mode.js).
const { normalizeServerUrl, validateConfig } = require('../config.js');

// --- normalizeServerUrl (семантика = desktop mode.js) ---
assert.strictEqual(normalizeServerUrl('acme.silentqa.com'), 'https://acme.silentqa.com');
assert.strictEqual(normalizeServerUrl('https://x.silentqa.com'), 'https://x.silentqa.com');
assert.strictEqual(normalizeServerUrl('HTTP://x.local'), 'HTTP://x.local');
assert.strictEqual(normalizeServerUrl('https://x.silentqa.com///'), 'https://x.silentqa.com');
assert.strictEqual(normalizeServerUrl('  acme.silentqa.com  '), 'https://acme.silentqa.com');
assert.strictEqual(normalizeServerUrl(''), '');
assert.strictEqual(normalizeServerUrl(null), '');

// --- validateConfig ---
assert.deepStrictEqual(
  validateConfig({ serverUrl: 'acme.silentqa.com', apiKey: ' sqa_abc ' }),
  { ok: true, serverUrl: 'https://acme.silentqa.com', apiKey: 'sqa_abc' });
assert.strictEqual(validateConfig({ serverUrl: '', apiKey: 'k' }).ok, false);
assert.ok(validateConfig({ serverUrl: '', apiKey: 'k' }).error.length > 0);
assert.strictEqual(validateConfig({ serverUrl: 'x.com', apiKey: '' }).ok, false);
assert.strictEqual(validateConfig({ serverUrl: 'x.com', apiKey: '   ' }).ok, false);
assert.strictEqual(validateConfig(null).ok, false);
assert.strictEqual(validateConfig(undefined).ok, false);

console.log('config OK');
