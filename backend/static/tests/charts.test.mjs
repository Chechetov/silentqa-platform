import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { svgLineChart, svgSparkline, svgBarRow, computeTalkMetrics } = require('../charts.js');

test('svgLineChart возвращает svg с polyline и пропускает null-точки', () => {
  const svg = svgLineChart([
    { label: '2026-07-01', value: 5 },
    { label: '2026-07-02', value: null },
    { label: '2026-07-03', value: 8 },
  ], { yMax: 10 });
  assert.ok(svg.startsWith('<svg'));
  assert.ok(svg.includes('<polyline'));
  assert.ok(svg.includes('2026-07-01') && svg.includes('2026-07-03'));
});

test('svgLineChart пустые данные → заглушка без polyline', () => {
  const svg = svgLineChart([], {});
  assert.ok(svg.startsWith('<svg') && !svg.includes('<polyline'));
});

test('svgSparkline рисует и терпит null', () => {
  const svg = svgSparkline([7, null, 8, 6]);
  assert.ok(svg.startsWith('<svg') && svg.includes('<polyline'));
});

test('svgBarRow ширина пропорциональна', () => {
  const half = svgBarRow(0.5, 1);
  const full = svgBarRow(1, 1);
  assert.ok(half.startsWith('<svg') && full.startsWith('<svg'));
  assert.notEqual(half, full);
});

test('computeTalkMetrics — паритет с worker-версией', () => {
  const segs = [
    { speaker: 'A', start: 0, end: 10 },
    { speaker: 'B', start: 10, end: 14 },
    { speaker: 'A', start: 14, end: 20 },
    { speaker: 'A', start: 20, end: 25 },
  ];
  const m = computeTalkMetrics(segs, { A: 'manager', B: 'client' });
  assert.equal(m.manager_sec, 21);
  assert.equal(m.client_sec, 4);
  assert.ok(Math.abs(m.talk_ratio - 21 / 25) < 1e-9);
  assert.equal(m.longest_monologue_sec, 11);
  assert.equal(m.unattributed, false);
  assert.equal(computeTalkMetrics([], null), null);
  assert.equal(computeTalkMetrics(segs, null).unattributed, true);
});

test('computeTalkMetrics — object-shaped speaker_map паритет с плоской формой', () => {
  const segs = [
    { speaker: 'A', start: 0, end: 10 },
    { speaker: 'B', start: 10, end: 14 },
    { speaker: 'A', start: 14, end: 20 },
    { speaker: 'A', start: 20, end: 25 },
  ];
  // реальная форма session.metadata.speaker_map: {spk: {name, role}}
  const obj = computeTalkMetrics(segs, {
    A: { name: 'Иван', role: 'manager' },
    B: { name: null, role: 'client' },
  });
  const flat = computeTalkMetrics(segs, { A: 'manager', B: 'client' });
  assert.deepEqual(obj, flat);
  assert.equal(obj.manager_sec, 21);
  assert.equal(obj.client_sec, 4);
  assert.equal(obj.unattributed, false);
});
