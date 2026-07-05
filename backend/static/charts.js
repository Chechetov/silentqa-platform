/* ============================================
   charts.js — SVG-графики дашборда (без зависимостей).
   Чистые функции: данные → SVG-строка. Node-тестируемо.
   Подключать в index.html ДО app.js.
   ============================================ */

function _esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _pts(values, w, h, yMax, pad) {
  // индексы с null пропускаем; x равномерно, y инвертирован
  const n = values.length;
  const out = [];
  values.forEach((v, i) => {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return;
    const x = n === 1 ? w / 2 : pad + (i * (w - 2 * pad)) / (n - 1);
    const y = h - pad - (Math.min(Number(v), yMax) / yMax) * (h - 2 * pad);
    out.push([Math.round(x * 10) / 10, Math.round(y * 10) / 10]);
  });
  return out;
}

function svgLineChart(points, opts) {
  const o = Object.assign({ width: 560, height: 160, yMax: 10 }, opts || {});
  const w = Number(o.width), h = Number(o.height), yMax = Number(o.yMax) || 10;
  const head = `<svg class="chart-line" viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img">`;
  if (!points || !points.length) {
    return head + `<text x="${w / 2}" y="${h / 2}" text-anchor="middle" class="chart-empty">нет данных</text></svg>`;
  }
  const vals = points.map(p => (p && p.value !== undefined ? p.value : null));
  const pts = _pts(vals, w, h, yMax, 18);
  const poly = pts.length
    ? `<polyline fill="none" stroke="var(--accent, #b45309)" stroke-width="2" points="${pts.map(p => p.join(',')).join(' ')}"/>`
    : '';
  const dots = pts.map(p => `<circle cx="${p[0]}" cy="${p[1]}" r="2.5" fill="var(--accent, #b45309)"/>`).join('');
  const first = points[0], last = points[points.length - 1];
  const labels = `<text x="18" y="${h - 4}" class="chart-axis">${_esc(first.label || '')}</text>` +
    `<text x="${w - 18}" y="${h - 4}" text-anchor="end" class="chart-axis">${_esc(last.label || '')}</text>`;
  const grid = [0.25, 0.5, 0.75].map(f => {
    const y = Math.round((h - 18 - f * (h - 36)) * 10) / 10;
    return `<line x1="18" x2="${w - 18}" y1="${y}" y2="${y}" class="chart-grid"/>`;
  }).join('');
  return head + grid + poly + dots + labels + '</svg>';
}

function svgSparkline(values, opts) {
  const o = Object.assign({ width: 90, height: 24, yMax: 10 }, opts || {});
  const w = Number(o.width), h = Number(o.height);
  const pts = _pts(values || [], w, h, Number(o.yMax) || 10, 2);
  const poly = pts.length > 1
    ? `<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${pts.map(p => p.join(',')).join(' ')}"/>`
    : (pts.length === 1 ? `<circle cx="${pts[0][0]}" cy="${pts[0][1]}" r="2" fill="currentColor"/>` : '');
  return `<svg class="sparkline" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">${poly}</svg>`;
}

function svgBarRow(value, max, opts) {
  const o = Object.assign({ width: 120, height: 10 }, opts || {});
  const w = Number(o.width), h = Number(o.height);
  const frac = max > 0 ? Math.max(0, Math.min(1, Number(value) / Number(max))) : 0;
  const fw = Math.round(frac * w * 10) / 10;
  return `<svg class="bar-row" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">` +
    `<rect x="0" y="0" width="${w}" height="${h}" rx="3" class="bar-bg"/>` +
    `<rect x="0" y="0" width="${fw}" height="${h}" rx="3" class="bar-fill"/></svg>`;
}

function computeTalkMetrics(segments, speakerMap) {
  const segs = (segments || []).filter(s => s && typeof s.start === 'number'
    && typeof s.end === 'number' && s.end > s.start);
  if (!segs.length) return null;

  const perSpeaker = {};
  segs.forEach(s => {
    const sp = String(s.speaker || 'UNKNOWN');
    perSpeaker[sp] = (perSpeaker[sp] || 0) + (s.end - s.start);
  });

  let roles = {};
  Object.entries(speakerMap || {}).forEach(([k, v]) => { roles[k] = String(v); });
  const unattributed = !Object.values(roles).some(v => v === 'manager');
  if (unattributed) {
    const ranked = Object.keys(perSpeaker).sort((a, b) => perSpeaker[b] - perSpeaker[a]);
    roles = {};
    if (ranked[0]) roles[ranked[0]] = 'manager';
    if (ranked[1]) roles[ranked[1]] = 'client';
  }

  let manager_sec = 0, client_sec = 0, other_sec = 0;
  Object.entries(perSpeaker).forEach(([sp, sec]) => {
    if (roles[sp] === 'manager') manager_sec += sec;
    else if (roles[sp] === 'client') client_sec += sec;
    else other_sec += sec;
  });

  let longest = 0, cur = 0, curSp = null;
  segs.forEach(s => {
    const sp = String(s.speaker || 'UNKNOWN');
    cur = sp === curSp ? cur + (s.end - s.start) : (s.end - s.start);
    curSp = sp;
    if (cur > longest) longest = cur;
  });

  const talked = manager_sec + client_sec;
  const r2 = x => Math.round(x * 100) / 100;
  return {
    manager_sec: r2(manager_sec), client_sec: r2(client_sec), other_sec: r2(other_sec),
    talk_ratio: talked > 0 ? Math.round((manager_sec / talked) * 10000) / 10000 : null,
    longest_monologue_sec: r2(longest),
    unattributed,
  };
}

if (typeof module !== 'undefined') {
  module.exports = { svgLineChart, svgSparkline, svgBarRow, computeTalkMetrics };
}
