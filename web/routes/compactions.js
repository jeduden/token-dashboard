import { fmt } from '/web/app.js';
import { fetchJson, chartScope } from '/web/routes/costs-drill-ui.js';

const esc = fmt.htmlSafe;
const RANGES = [
  { key: '7d',  label: '7d',  days: 7 },
  { key: '30d', label: '30d', days: 30 },
  { key: '90d', label: '90d', days: 90 },
  { key: 'all', label: 'All', days: null },
];
const AUTO = '#E8A23B', MANUAL = '#4A9EFF', OTHER = '#8B98A6';
const AXIS_TXT = '#8B98A6', GRID = '#1F2630';

function readRange() {
  const m = /(?:^|&)range=([^&]+)/.exec(location.hash.split('?')[1] || '');
  const k = m && decodeURIComponent(m[1]);
  return RANGES.find(r => r.key === k) || RANGES[1];
}
function writeRange(key) {
  location.hash = '#/compactions?range=' + encodeURIComponent(key);
}

const secs = s => s == null ? '—' : (s >= 60 ? (s / 60).toFixed(1) + ' min' : Math.round(s) + ' s');
const tok = n => n == null ? '—' : fmt.compact(n);

// One-sentence plain-English reading of the idle-gap chart.
export function gapSentence(d) {
  const known = d.summary.count - d.gap_unknown;
  if (!known) return 'No compaction in this range has a known idle gap before it.';
  const a = d.after_expiry;
  const long = d.gap_buckets.filter(b => ['30_60m', '1_2h', 'gt2h'].includes(b.key)).reduce((n, b) => n + b.count, 0);
  const short = d.gap_buckets.find(b => b.key === 'lt5m').count;
  return `${fmt.int(short)} of ${fmt.int(known)} compactions came within 5 minutes of the last assistant turn (mid-task), `
    + `${fmt.int(long)} came after more than 30 minutes away from the keyboard, and ${fmt.int(a.count)} came after an idle gap `
    + 'long enough for the prompt cache to expire, so the whole context had to be processed at the full price.';
}

function kpi(name, val, sub, title = '') {
  return `<div class="card kpi"><div class="label">${esc(name)}</div>
    <div class="value" title="${esc(title)}">${val}</div>${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;
}

function stackedOption(cats, defs, rot) {
  return {
    textStyle: { color: '#E6EDF3', fontFamily: 'Inter' },
    grid: { left: 36, right: 12, top: 28, bottom: 24, containLabel: true },
    legend: { textStyle: { color: AXIS_TXT }, top: 0, right: 0, icon: 'roundRect', itemWidth: 8, itemHeight: 8 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: '#0F1419', borderColor: '#283040',
      borderWidth: 1, textStyle: { color: '#E6EDF3', fontFamily: 'Inter', fontSize: 12 }, padding: [8, 12] },
    xAxis: { type: 'category', data: cats, axisLine: { lineStyle: { color: GRID } }, axisTick: { show: false },
      axisLabel: { color: AXIS_TXT, interval: cats.length > 20 ? 'auto' : 0, rotate: rot } },
    yAxis: { type: 'value', minInterval: 1, axisLine: { show: false }, axisTick: { show: false },
      splitLine: { lineStyle: { color: GRID } }, axisLabel: { color: AXIS_TXT } },
    series: defs.map(([name, values, color]) => ({ name, type: 'bar', stack: 'n', data: values,
      itemStyle: { color }, barMaxWidth: 28 })),
  };
}

function splitSeries(rows) {
  const defs = [['auto', rows.map(r => r.auto), AUTO], ['manual', rows.map(r => r.manual), MANUAL]];
  if (rows.some(r => r.other)) defs.push(['unknown trigger', rows.map(r => r.other), OTHER]);
  return defs;
}

function table(head, rows, empty) {
  return `<table><thead><tr>${head.map(([h, num]) => `<th${num ? ' class="num"' : ''}>${esc(h)}</th>`).join('')}</tr></thead>
    <tbody>${rows.join('') || `<tr><td colspan="${head.length}" class="muted">${esc(empty)}</td></tr>`}</tbody></table>`;
}

function expiryCard(d) {
  const a = d.after_expiry;
  const t = d.thresholds;
  if (!a.count) {
    return `<div class="card" style="margin-top:16px"><h3>Compactions after an expired cache</h3>
      <p class="muted">None in this range: no compaction followed an idle gap longer than the cache lifetime
      (${t.ttl_5m_s / 60} min, or ${t.ttl_1h_s / 60} min after a 1h cache write).</p></div>`;
  }
  return `<div class="card" style="margin-top:16px"><h3>Compactions after an expired cache</h3>
    <p style="margin:0 0 8px"><b>${fmt.int(a.count)}</b> of ${fmt.int(d.summary.count)} compactions (${fmt.int(a.auto)} auto, ${fmt.int(a.manual)} manual)
      started after the thread had been idle longer than the prompt-cache lifetime
      (${t.ttl_5m_s / 60} min, or ${t.ttl_1h_s / 60} min after a 1h cache write). Their ${tok(a.pre_tokens_total)} tokens of context
      were processed with a cold cache.</p>
    <p style="margin:0 0 8px"><b>What-if, upper bound: up to ${fmt.usd(a.avoidable_usd_upper_bound)}</b> could have been avoided by
      compacting before the pause instead of after it${a.unpriced ? ` (${fmt.int(a.unpriced)} unpriced, not included)` : ''}.</p>
    <p class="muted" style="margin:0;font-size:12px">${esc(a.note)}
      See also the <a href="#/cache">Cache</a> tab for the idle-gap breakdown of expired turns.</p></div>`;
}

function body(d, range) {
  const s = d.summary;
  const days = range.days ? `last ${range.days} days` : 'all time';
  return `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Compactions</h2>
      <span class="muted" style="font-size:12px">${days}</span>
      <div class="spacer"></div>
      <div class="range-tabs" role="tablist">
        ${RANGES.map(r => `<button type="button" data-range="${r.key}" class="${r.key === range.key ? 'active' : ''}">${r.label}</button>`).join('')}
      </div>
    </div>
    <p class="muted" style="font-size:12px;margin:0 0 14px">Real compaction events recorded by Claude Code (not inferred from cache counts).
      Context sizes are Claude Code's own token counts. Compactions are only listed once the transcript that holds them has been scanned.</p>
    <div class="row cols-4">
      ${kpi('Compactions', fmt.int(s.count), `median ${s.median_turns_between == null ? '—' : fmt.int(s.median_turns_between)} turns apart in a session`)}
      ${kpi('Auto vs manual', `${fmt.int(s.auto)} / ${fmt.int(s.manual)}`, s.other ? `${fmt.int(s.other)} with no recorded trigger` : 'automatic / by hand')}
      ${kpi('Context before → after', `${tok(s.median_pre_tokens)} → ${tok(s.median_post_tokens)}`, `median; 90th percentile before: ${tok(s.p90_pre_tokens)}`,
            'median preTokens → postTokens')}
      ${kpi('Median duration', secs(s.median_duration_s), `${tok(s.total_dropped_tokens)} tokens dropped in total`)}
    </div>
    <div class="row cols-2" style="margin-top:16px">
      <div class="card"><h3>Compactions per day</h3>
        <div id="ch-comp-day" style="height:240px" role="img" aria-label="Compactions per day, auto and manual"></div></div>
      <div class="card"><h3>Context size before compacting</h3>
        <div id="ch-comp-pre" style="height:240px" role="img" aria-label="Compactions by context size before compacting"></div></div>
    </div>
    <div class="card" style="margin-top:16px"><h3>Idle gap before the compaction</h3>
      <p class="muted" style="margin:-4px 0 10px;font-size:12px">Time from the thread's last assistant turn until you came back (or the compaction started).</p>
      <div id="ch-comp-gap" style="height:240px" role="img" aria-label="Compactions by idle gap before them, auto and manual"></div>
      <p style="margin:10px 0 0">${esc(gapSentence(d))}</p></div>
    ${expiryCard(d)}
    <div class="row cols-2" style="margin-top:16px">
      <div class="card"><h3>By repo</h3>${table([['repo'], ['compactions', 1], ['median before', 1]],
        d.by_repo.slice(0, 15).map(r => `<tr><td>${esc(r.repo_name || r.repo)}</td><td class="num">${fmt.int(r.count)}</td><td class="num">${tok(r.median_pre_tokens)}</td></tr>`),
        'No compactions in this range.')}</div>
      <div class="card"><h3>By project</h3>${table([['project'], ['compactions', 1], ['median before', 1]],
        d.by_project.slice(0, 15).map(r => `<tr><td title="${esc(r.project_slug)}">${esc(r.project_name || r.project_slug)}</td><td class="num">${fmt.int(r.count)}</td><td class="num">${tok(r.median_pre_tokens)}</td></tr>`),
        'No compactions in this range.')}</div>
    </div>
    <div class="card" style="margin-top:16px"><h3>Sessions with the most compactions</h3>${table(
      [['session'], ['project'], ['compactions', 1], ['largest context before', 1]],
      d.worst_sessions.map(w => `<tr><td><a href="#/sessions/${encodeURIComponent(w.session_id)}" class="mono">${esc(String(w.session_id).slice(0, 8))}…</a></td>
        <td>${esc(w.project_name || w.project_slug)}</td><td class="num">${fmt.int(w.compactions)}</td><td class="num">${tok(w.max_pre_tokens)}</td></tr>`),
      'No compactions in this range.')}</div>`;
}

export default async function (root) {
  const range = readRange();
  const scope = chartScope();                     // disposes charts + window listeners on the next route change
  root.innerHTML = '<div class="card" role="status" aria-live="polite"><p class="muted">Loading compactions&hellip;</p></div>';
  const since = range.days ? new Date(Date.now() - range.days * 86400 * 1000).toISOString() : null;
  const r = await fetchJson('/api/compactions' + (since ? '?since=' + encodeURIComponent(since) : ''));
  // Navigated away (or picked another range) while loading: a newer render owns the page.
  if (!location.hash.startsWith('#/compactions') || readRange().key !== range.key) return;
  if (!r.ok) {
    const msg = (r.data && r.data.error) || ('HTTP ' + r.status);
    root.innerHTML = `<div class="card" role="alert"><h2>Compactions</h2><p>Could not load compactions: ${esc(msg)}</p></div>`;
    return;
  }
  const d = r.data;
  root.innerHTML = body(d, range);
  root.querySelectorAll('.range-tabs button').forEach(b => b.addEventListener('click', () => writeRange(b.dataset.range)));
  const draw = (id, cats, defs, rot) => {
    const el = root.querySelector('#' + id);
    if (el && cats.length) scope.init(el).setOption(stackedOption(cats, defs, rot));
    else if (el) { el.style.height = 'auto'; el.innerHTML = '<p class="muted">No data in this range.</p>'; }
  };
  draw('ch-comp-day', d.by_day.map(x => x.day.slice(5)), splitSeries(d.by_day), d.by_day.length > 12 ? 45 : 0);
  draw('ch-comp-pre', d.pre_token_buckets.map(x => x.label), splitSeries(d.pre_token_buckets), 0);
  draw('ch-comp-gap', d.gap_buckets.map(x => x.label), splitSeries(d.gap_buckets), 0);
}
