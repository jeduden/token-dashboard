import { api, fmt } from '/web/app.js';
import { barChart, stackedBarChart } from '/web/charts.js';

const RANGES = [
  { key: '7d',  label: '7d',  days: 7 },
  { key: '30d', label: '30d', days: 30 },
  { key: '90d', label: '90d', days: 90 },
  { key: 'all', label: 'All', days: null },
];

const CAUSE_LABEL = {
  ttl_expired_5m: 'TTL expired (5m)',
  ttl_expired_1h: 'TTL expired (1h)',
  model_switch:   'Model switch',
  prefix_changed: 'Prefix changed',
  new_content:    'Large new content',
  unknown:        'Unknown',
};

function readRange() {
  const q = (location.hash.split('?')[1] || '');
  const m = /(?:^|&)range=([^&]+)/.exec(q);
  const k = m && decodeURIComponent(m[1]);
  return RANGES.find(r => r.key === k) || RANGES[1];
}

function writeRange(key) {
  const base = (location.hash.replace(/^#/, '').split('?')[0]) || '/cache';
  location.hash = '#' + base + '?range=' + encodeURIComponent(key);
}

export default async function (root) {
  const range = readRange();
  const since = range.days ? new Date(Date.now() - range.days * 86400 * 1000).toISOString() : null;
  const d = await api('/api/cache' + (since ? '?since=' + encodeURIComponent(since) : ''));
  const s = d.summary;
  const label = c => CAUSE_LABEL[c] || c;

  const kpi = (name, val, title, sub = '') => `
    <div class="card kpi">
      <div class="label">${name}</div>
      <div class="value" title="${fmt.htmlSafe(title)}">${val}</div>
      ${sub ? `<div class="sub">${sub}</div>` : ''}
    </div>`;

  const causes = d.by_cause.filter(c => c.turns > 0);
  const t = d.thresholds;

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Cache misses</h2>
      <span class="muted" style="font-size:12px">${range.days ? `last ${range.days} days` : 'all time'}</span>
      <div class="spacer"></div>
      <div class="range-tabs" role="tablist">
        ${RANGES.map(r => `<button data-range="${r.key}" class="${r.key === range.key ? 'active' : ''}">${r.label}</button>`).join('')}
      </div>
    </div>

    <p class="muted" style="font-size:12px;margin:0 0 14px">
      Heuristic estimates. A <b>miss</b> is a non-first assistant turn where at least
      ${fmt.pct(t.miss_share)} of the prompt (and ${fmt.int(t.min_miss_tokens)}+ tokens) was written to the
      cache or sent uncached instead of read. First turns of a session or subagent are
      <b>cold starts</b>, not misses. Causes are guessed from time gaps and token shapes;
      the transcripts never say why a cache was missed.
    </p>

    <div class="row cols-4">
      ${kpi('Cache hit ratio', fmt.pct(s.hit_ratio), 'read / (read + created + uncached input)', `${fmt.compact(s.tokens_read)} read of ${fmt.compact(s.tokens_read + s.tokens_created + s.tokens_input)}`)}
      ${kpi('Miss turns', fmt.int(s.miss_turns), fmt.int(s.miss_turns) + ' of ' + fmt.int(s.turns) + ' turns', `${fmt.pct(s.miss_rate)} of ${fmt.int(s.turns)} turns`)}
      ${kpi('Cold starts', fmt.int(s.cold_starts), 'first turn of each session / subagent thread')}
      <div class="card kpi cost">
        <div class="label">Est. wasted</div>
        <div class="value" title="${fmt.usd4(s.wasted_usd)}">${fmt.usd(s.wasted_usd)}</div>
        <div class="sub">extra vs. reading from cache${s.estimated_pricing ? ' (tier-fallback pricing)' : ''}${s.unpriced_miss_turns ? `; ${fmt.int(s.unpriced_miss_turns)} unpriced` : ''}</div>
      </div>
    </div>

    <div class="row cols-2" style="margin-top:16px">
      <div class="card">
        <h3>Daily cache read vs. created</h3>
        <p class="muted" style="margin:-4px 0 10px;font-size:12px">Tokens read from cache (cheap) vs. written to it (expensive). A growing <b>created</b> share means more misses.</p>
        <div id="ch-cache-day" style="height:260px"></div>
      </div>
      <div class="card">
        <h3>Estimated waste by cause (USD)</h3>
        <div id="ch-cache-cause" style="height:260px"></div>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>What the causes mean</h3>
      <table>
        <thead><tr><th>cause</th><th class="num">miss turns</th><th class="num">est. wasted</th><th>explanation (heuristic)</th></tr></thead>
        <tbody>
          ${d.by_cause.map(c => `
            <tr><td>${fmt.htmlSafe(label(c.cause))}</td>
                <td class="num">${fmt.int(c.turns)}</td>
                <td class="num">${fmt.usd(c.wasted_usd)}</td>
                <td class="muted">${fmt.htmlSafe(c.help)}</td></tr>`).join('')}
        </tbody>
      </table>
    </div>

    <div class="row cols-2" style="margin-top:16px">
      <div class="card">
        <h3>By project</h3>
        <table>
          <thead><tr><th>project</th><th class="num">hit ratio</th><th class="num">misses</th><th class="num">wasted</th></tr></thead>
          <tbody>${d.by_project.slice(0, 15).map(r => `
            <tr><td title="${fmt.htmlSafe(r.project_slug)}">${fmt.htmlSafe(r.project_name || r.project_slug)}</td>
                <td class="num">${fmt.pct(r.hit_ratio)}</td>
                <td class="num">${fmt.int(r.miss_turns)}</td>
                <td class="num">${fmt.usd(r.wasted_usd)}</td></tr>`).join('')}</tbody>
        </table>
      </div>
      <div class="card">
        <h3>By model</h3>
        <table>
          <thead><tr><th>model</th><th class="num">hit ratio</th><th class="num">misses</th><th class="num">wasted</th></tr></thead>
          <tbody>${d.by_model.map(r => `
            <tr><td><span class="badge ${fmt.modelClass(r.model)}">${fmt.htmlSafe(fmt.modelShort(r.model))}</span></td>
                <td class="num">${fmt.pct(r.hit_ratio)}</td>
                <td class="num">${fmt.int(r.miss_turns)}</td>
                <td class="num">${fmt.usd(r.wasted_usd)}</td></tr>`).join('')}</tbody>
        </table>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Worst sessions</h3>
      <table>
        <thead><tr><th>session</th><th>project</th><th class="num">misses</th><th class="num">est. wasted</th><th>dominant cause</th></tr></thead>
        <tbody>${d.worst_sessions.map(w => `
          <tr><td><a href="#/sessions/${encodeURIComponent(w.session_id)}" class="mono">${fmt.htmlSafe(w.session_id.slice(0, 8))}…</a></td>
              <td>${fmt.htmlSafe(w.project_name || w.project_slug)}</td>
              <td class="num">${fmt.int(w.miss_count)}</td>
              <td class="num">${fmt.usd(w.wasted_usd)}</td>
              <td>${fmt.htmlSafe(label(w.dominant_cause))}</td></tr>`).join('')
          || '<tr><td colspan="5" class="muted">No misses in this range.</td></tr>'}</tbody>
      </table>
    </div>`;

  root.querySelectorAll('.range-tabs button').forEach(b =>
    b.addEventListener('click', () => writeRange(b.dataset.range)));

  stackedBarChart(document.getElementById('ch-cache-day'), {
    categories: d.by_day.map(r => r.day.slice(5)),
    series: [
      { name: 'cache read',    values: d.by_day.map(r => r.tokens_read),    color: '#3FB68B' },
      { name: 'cache created', values: d.by_day.map(r => r.tokens_created), color: '#E8A23B' },
    ],
    formatter: v => Number(v).toLocaleString() + ' tokens',
  });
  barChart(document.getElementById('ch-cache-cause'), {
    categories: causes.map(c => label(c.cause)),
    values: causes.map(c => c.wasted_usd),
    color: '#E5484D',
  });
}
