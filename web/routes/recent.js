import { api, fmt, $, $$ } from '/web/app.js';
import { stackedBarChart } from '/web/charts.js';

const esc = fmt.htmlSafe;
const RANGES = [
  { key: '24h', label: '24h', hours: 24 },
  { key: '7d',  label: '7d',  hours: 24 * 7 },
  { key: '30d', label: '30d', hours: 24 * 30 },
  { key: '90d', label: '90d', hours: 24 * 90 },
  { key: 'all', label: 'All', hours: null },
];
const METRICS = {
  cost:     { label: 'cost',     get: s => s.cost_usd, fmt: fmt.usd },
  tokens:   { label: 'tokens',   get: s => s.tokens,   fmt: fmt.compact },
  messages: { label: 'messages', get: s => s.messages, fmt: fmt.int },
};
const TOP = 6, OTHER = '#8B98A6';

function params() {
  const q = new URLSearchParams(location.hash.split('?')[1] || '');
  const range = RANGES.find(r => r.key === q.get('range')) || RANGES[1];
  return {
    range, metric: METRICS[q.get('metric')] ? q.get('metric') : 'cost',
    bucket: ['day', 'hour'].includes(q.get('bucket')) ? q.get('bucket') : (range.hours && range.hours <= 24 * 30 ? 'hour' : 'day'),
    sel: (q.get('sessions') || '').split(',').filter(Boolean),
    at: q.get('at') || '',
  };
}

function go(p) {
  const q = new URLSearchParams({ range: p.range.key, metric: p.metric, bucket: p.bucket });
  if (p.sel.length) q.set('sessions', p.sel.join(','));
  if (p.at) q.set('at', p.at);
  location.hash = '#/recent?' + q.toString();
}

// Window [since, until) for one bucket key such as 2026-01-01 or 2026-01-01T10 (UTC).
function bucketWindow(t, bucket) {
  const d = new Date(bucket === 'hour' ? t + ':00:00Z' : t + 'T00:00:00Z');
  const next = new Date(d.getTime() + (bucket === 'hour' ? 3600e3 : 86400e3));
  const cut = bucket === 'hour' ? 13 : 10;
  return { since: t, until: next.toISOString().slice(0, cut) };
}

function spark(vals, w = 120, h = 26) {
  if (!vals.length) return '';
  const max = Math.max(...vals, 1e-12);
  const step = vals.length > 1 ? w / (vals.length - 1) : 0;
  const pts = vals.map((v, i) => `${(i * step).toFixed(1)},${(h - 2 - (v / max) * (h - 4)).toFixed(1)}`).join(' ');
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${pts}" fill="none" stroke="#4A9EFF" stroke-width="1.5"/></svg>`;
}

const seg = (items, active, attr) => `<div class="range-tabs">${items.map(([k, l]) =>
  `<button type="button" data-${attr}="${k}" class="${k === active ? 'active' : ''}">${l}</button>`).join('')}</div>`;

export default async function (root) {
  const p = params();
  const m = METRICS[p.metric];
  const q = new URLSearchParams({ bucket: p.bucket });
  if (p.range.hours) q.set('since', new Date(Date.now() - p.range.hours * 3600e3).toISOString());
  if (p.sel.length) q.set('sessions', p.sel.join(','));

  let data;
  try { data = await api('/api/recent/buckets?' + q.toString()); }
  catch (e) {
    root.innerHTML = `<div class="card"><h2>Recent</h2><p class="muted">${esc(String(e.message))}</p>
      <p><a href="#/recent?range=${p.range.key}&bucket=day">Show per day instead</a></p></div>`;
    return;
  }
  const { buckets, sessions } = data;
  const label = s => `${s.project_name} · ${s.session_id.slice(0, 6)}`;
  const top = sessions.slice(0, TOP);
  const topIds = new Set(top.map(s => s.session_id));
  const series = top.map(s => ({ name: label(s), values: buckets.map(b => (b.sessions[s.session_id] ? m.get(b.sessions[s.session_id]) : 0)) }));
  if (sessions.length > TOP) {
    series.push({ name: 'other', color: OTHER, values: buckets.map(b =>
      Object.entries(b.sessions).filter(([id]) => !topIds.has(id)).reduce((n, [, v]) => n + m.get(v), 0)) });
  }
  const byId = Object.fromEntries(sessions.map(s => [s.session_id, s]));
  const at = buckets.find(b => b.t === p.at);
  // With a bucket selected, show each session's numbers for that bucket only.
  const shown = (at
    ? Object.entries(at.sessions).map(([id, v]) => ({ ...byId[id], ...v }))
    : sessions).sort((x, y) => y.cost_usd - x.cost_usd || y.messages - x.messages);
  const totals = buckets.reduce((a, b) => ({ messages: a.messages + b.messages, tokens: a.tokens + b.tokens, cost_usd: a.cost_usd + b.cost_usd }),
    { messages: 0, tokens: 0, cost_usd: 0 });

  root.innerHTML = `
    <div class="card">
      <div class="flex" style="margin-bottom:10px;flex-wrap:wrap;gap:10px">
        <h2 style="margin:0">Recent activity</h2>
        <span class="muted" style="font-size:12px">${fmt.int(totals.messages)} messages · ${fmt.compact(totals.tokens)} tokens · ${fmt.usd(totals.cost_usd)} · ${sessions.length} sessions · UTC</span>
        <div class="spacer"></div>
        ${seg(Object.entries(METRICS).map(([k, v]) => [k, v.label]), p.metric, 'metric')}
        ${seg([['hour', 'per hour'], ['day', 'per day']], p.bucket, 'bucket')}
        ${seg(RANGES.map(r => [r.key, r.label]), p.range.key, 'range')}
      </div>
      <div id="chart" style="height:260px"></div>
      <p class="muted" style="font-size:12px;margin:6px 0 0">Click a bar to list that ${p.bucket}'s messages below.</p>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Per ${p.bucket}</h3>
      <p class="muted" style="font-size:12px">What happened in each ${p.bucket}, newest first (empty ones skipped). Click a row to select it.</p>
      <div style="max-height:320px;overflow:auto">
      <table id="bkt">
        <thead><tr><th>${p.bucket}</th><th class="num">messages</th><th class="num">tokens</th><th class="num">cost</th><th class="num">sessions</th><th>biggest session</th></tr></thead>
        <tbody>
          ${buckets.filter(b => b.messages).reverse().map(b => {
            const [id, v] = Object.entries(b.sessions).sort((x, y) => y[1].cost_usd - x[1].cost_usd || y[1].messages - x[1].messages)[0];
            const bs = byId[id];
            return `<tr data-t="${esc(b.t)}" style="cursor:pointer" class="${b.t === p.at ? 'active' : ''}">
              <td class="mono">${esc(b.t.replace('T', ' '))}${p.bucket === 'hour' ? ':00' : ''}</td>
              <td class="num">${fmt.int(b.messages)}</td>
              <td class="num">${fmt.compact(b.tokens)}</td>
              <td class="num">${fmt.usd(b.cost_usd)}</td>
              <td class="num">${fmt.int(Object.keys(b.sessions).length)}</td>
              <td class="mono"><a href="#/sessions/${encodeURIComponent(id)}">${esc(bs ? label(bs) : id.slice(0, 8))}</a> <span class="muted">${fmt.usd(v.cost_usd)}</span></td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Sessions ${at ? `in ${esc(p.at.replace('T', ' '))}${p.bucket === 'hour' ? ':00' : ''}` : 'in this range'}</h3>
      <p class="muted" style="font-size:12px">${at ? `Numbers are for this ${p.bucket} only, not the session total.` : 'Numbers are totals over the selected range.'}
        Sorted by cost. Click a row to filter chart and feed to it; select several to compare. The sparkline is the ${m.label} per ${p.bucket}.</p>
      <table id="cmp">
        <thead><tr><th>session</th><th>first in range</th><th>last in range</th><th class="num">messages</th><th class="num">tokens</th><th class="num">cost</th><th>${m.label} / ${p.bucket}</th></tr></thead>
        <tbody>
          ${shown.map(s => `
            <tr data-sid="${esc(s.session_id)}" style="cursor:pointer" class="${p.sel.includes(s.session_id) ? 'active' : ''}">
              <td><a href="#/sessions/${encodeURIComponent(s.session_id)}" class="mono" title="open session">${esc(label(s))}</a></td>
              <td class="mono">${esc(s.first.replace('T', ' '))}</td>
              <td class="mono">${esc(s.last.replace('T', ' '))}</td>
              <td class="num">${fmt.int(s.messages)}</td>
              <td class="num">${fmt.compact(s.tokens)}</td>
              <td class="num">${fmt.usd(s.cost_usd)}</td>
              <td>${spark(buckets.map(b => (b.sessions[s.session_id] ? m.get(b.sessions[s.session_id]) : 0)))}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>

    <div class="card" style="margin-top:16px" id="feed-card"><h3>Messages</h3><p class="muted">Loading…</p></div>`;

  $$('[data-metric]', root).forEach(b => b.addEventListener('click', () => go({ ...p, metric: b.dataset.metric })));
  $$('[data-bucket]', root).forEach(b => b.addEventListener('click', () => go({ ...p, bucket: b.dataset.bucket, at: '' })));
  $$('[data-range]', root).forEach(b => b.addEventListener('click', () => {
    const range = RANGES.find(r => r.key === b.dataset.range);
    go({ ...p, range, at: '', bucket: range.hours === null || range.hours > 24 * 30 ? 'day' : p.bucket });
  }));
  $$('#bkt tbody tr', root).forEach(tr => tr.addEventListener('click', e => {
    if (e.target.closest('a')) return;
    go({ ...p, at: p.at === tr.dataset.t ? '' : tr.dataset.t });
  }));
  $$('#cmp tbody tr', root).forEach(tr => tr.addEventListener('click', e => {
    if (e.target.closest('a')) return;
    const id = tr.dataset.sid;
    go({ ...p, sel: p.sel.includes(id) ? p.sel.filter(x => x !== id) : [...p.sel, id] });
  }));

  const chart = stackedBarChart($('#chart', root), { categories: buckets.map(b => b.t.replace('T', ' ') + (p.bucket === 'hour' ? ':00' : '')), series, formatter: v => m.fmt(v) });
  chart.on('click', ev => go({ ...p, at: p.at === buckets[ev.dataIndex].t ? '' : buckets[ev.dataIndex].t }));

  // Feed: the selected bucket, or the newest messages in the range.
  const fq = new URLSearchParams({ limit: p.at ? 500 : 100 });
  if (p.at) { const w = bucketWindow(p.at, p.bucket); fq.set('since', w.since); fq.set('until', w.until); }
  else if (p.range.hours) fq.set('since', q.get('since'));
  if (p.sel.length) fq.set('sessions', p.sel.join(','));
  const feed = (await api('/api/recent?' + fq.toString())).messages;
  $('#feed-card', root).innerHTML = `
    <h3>Messages ${p.at ? `in ${esc(p.at.replace('T', ' '))}${p.bucket === 'hour' ? ':00' : ''} <a href="#" id="clear-at" class="muted" style="font-size:12px">clear</a>` : '(newest 100)'}</h3>
    <table>
      <thead><tr><th>time</th><th>session</th><th>type</th><th>model</th><th class="blur-sensitive">prompt / tools</th>
        <th class="num">ctx</th><th class="num">out</th><th class="num">cost</th></tr></thead>
      <tbody>
        ${feed.map(x => `<tr>
          <td class="mono">${fmt.ts(x.timestamp)}</td>
          <td class="mono" title="${esc(x.session_id)}"><a href="#/sessions/${encodeURIComponent(x.session_id)}">${esc(byId[x.session_id] ? label(byId[x.session_id]) : x.session_id.slice(0, 8))}</a></td>
          <td>${x.type}${x.is_sidechain ? ' <span class="badge">side</span>' : ''}</td>
          <td>${x.model ? `<span class="badge ${fmt.modelClass(x.model)}">${esc(fmt.modelShort(x.model))}</span>` : ''}</td>
          <td class="blur-sensitive">${esc(x.text ? fmt.short(x.text, 110) : (x.tools || []).join(' · '))}</td>
          <td class="num">${x.context_tokens == null ? '' : fmt.compact(x.context_tokens)}</td>
          <td class="num">${x.type === 'assistant' ? fmt.int(x.output_tokens) : ''}</td>
          <td class="num">${x.cost_usd == null ? '' : fmt.usd4(x.cost_usd)}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
  const clear = $('#clear-at', root);
  if (clear) clear.addEventListener('click', e => { e.preventDefault(); go({ ...p, at: '' }); });
}
