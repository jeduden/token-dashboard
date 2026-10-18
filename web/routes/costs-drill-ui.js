// costs-drill-ui.js — helpers for the per-tool drill-down: chart lifecycle, fetch with error body,
// loading state, group selector and the time-based charts.
import { fmt } from '/web/app.js';

const esc = fmt.htmlSafe;
const AXIS_TXT = '#8B98A6';
const GRID = '#1F2630';
const TOOLTIP = {
  trigger: 'axis', backgroundColor: '#0F1419', borderColor: '#283040', borderWidth: 1,
  textStyle: { color: '#E6EDF3', fontFamily: 'Inter', fontSize: 12 }, padding: [8, 12],
};

// GET json; unlike app.js api() it keeps the server's error text (HTTP 400 carries {error}).
export async function fetchJson(url) {
  try {
    const r = await fetch(url);
    let data = null;
    try { data = await r.json(); } catch (e) { /* non-json body */ }
    return { ok: r.ok, status: r.status, data };
  } catch (e) {
    return { ok: false, status: 0, data: { error: String(e && e.message || e) } };
  }
}

// Shared wording for the expiry component of the cost model (tooltips / legends).
export const EXPIRY_HELP = 'expiry = re-write after idle gap: when the thread sat idle longer than the cache lifetime '
  + '(5 min, or 1 h for 1h writes), the next turn wrote everything carried (this content included) to the cache again '
  + 'instead of reading it';
export const expiryTh = (label = 'expiry') => `<th class="num" title="${esc(EXPIRY_HELP)}">${esc(label)}</th>`;

let current = null;
// One scope per render: a single resize listener for all its charts, removed (and the charts
// disposed) on the next route change (hashchange) or when a newer scope replaces it.
export function chartScope() {
  if (current) current.dispose();
  const charts = [];
  const onResize = () => charts.forEach(c => { if (!c.isDisposed()) c.resize(); });
  const scope = {
    init(el) {
      const c = echarts.init(el, null, { renderer: 'svg' });
      charts.push(c);
      return c;
    },
    dispose() {
      window.removeEventListener('resize', onResize);
      window.removeEventListener('hashchange', scope.dispose);
      charts.forEach(c => { if (!c.isDisposed()) c.dispose(); });
      charts.length = 0;
      if (current === scope) current = null;
    },
  };
  window.addEventListener('resize', onResize);
  window.addEventListener('hashchange', scope.dispose);
  current = scope;
  return scope;
}

export function loadingHtml(tool) {
  return `<div class="card drill-loading-card" role="status" aria-live="polite"><div class="drill-bar"></div>
    <p class="muted">Loading ${esc(tool)} drill-down&hellip; (large ranges can take a couple of seconds)</p></div>`;
}

// Dim the content that is already on screen while a new group loads.
export function setBusy(root, on) {
  root.classList.toggle('drill-dim', on);
  root.setAttribute('aria-busy', on ? 'true' : 'false');
}

export function groupSelect(d) {
  const opt = g => `<option value="${esc(g.key)}" ${g.key === d.group ? 'selected' : ''}>${esc(g.label)}</option>`;
  const by = kind => d.available_groups.filter(g => g.kind === kind).map(opt).join('');
  return `<label class="muted" style="font-size:12px">group by
    <select id="grp"><optgroup label="By parameter">${by('parameter')}</optgroup>
      <optgroup label="By dimension">${by('dimension')}</optgroup></select></label>`;
}

const usdFmt = v => fmt.usd4(v);
function barOption(categories, values, color, rotate) {
  return {
    textStyle: { color: '#E6EDF3', fontFamily: 'Inter' },
    grid: { left: 36, right: 12, top: 24, bottom: 24, containLabel: true },
    tooltip: { ...TOOLTIP, axisPointer: { type: 'shadow' }, valueFormatter: usdFmt },
    xAxis: { type: 'category', data: categories, axisLine: { lineStyle: { color: GRID } }, axisTick: { show: false },
      axisLabel: { color: AXIS_TXT, interval: categories.length > 24 ? 'auto' : 0, rotate } },
    yAxis: { type: 'value', axisLine: { show: false }, axisTick: { show: false }, splitLine: { lineStyle: { color: GRID } },
      axisLabel: { color: AXIS_TXT } },
    series: [{ type: 'bar', data: values, itemStyle: { color, borderRadius: [4, 4, 0, 0] }, barMaxWidth: 32 }],
  };
}

export function topBar(scope, el, rows, labelOf) {
  const c = scope.init(el);
  const top = rows.slice(0, 15);
  c.setOption(barOption(top.map(r => fmt.short(labelOf(r), 26)), top.map(r => r.est_cost_usd), '#E5A23B', top.length > 5 ? 25 : 0));
  return c;
}

// Cost over time: days ascending (from the day group rows or the `time` section).
export function dayBars(scope, el, days) {
  const c = scope.init(el);
  const ds = [...days].sort((a, b) => (a.day < b.day ? -1 : 1));
  c.setOption(barOption(ds.map(x => x.day), ds.map(x => x.est_cost_usd), '#4A9EFF', ds.length > 12 ? 45 : 0));
  return c;
}

export function sparkCard(scope, root, d) {
  const days = d.time && d.time.days || [];
  const el = root.querySelector('#ch-time');
  if (!el) return;
  if (days.length < 2) { el.innerHTML = '<p class="muted">Not enough days in this range for a trend.</p>'; el.style.height = 'auto'; return; }
  dayBars(scope, el, days);
}
