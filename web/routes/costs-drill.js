// costs-drill.js — per-tool drill-down: #/costs/<tool>?range=30d&group=auto
import { api, fmt } from '/web/app.js';
import { fetchJson, chartScope, loadingHtml, setBusy, groupSelect, topBar, dayBars, sparkCard } from '/web/routes/costs-drill-ui.js';

const SEV_COLOR = { high: '#E5484D', warn: '#F5A623', info: '#4A9EFF' };
const Q_COLOR = { frequent_expensive: '#E5484D', frequent_cheap: '#E8A23B', rare_expensive: '#7C5CFF', rare_cheap: '#3FB68B' };
const SORTS = {
  est_cost_usd: 'est. cost', calls: 'calls', cost_per_call: 'cost / call', avg_result_tokens: 'avg result tok',
};
const esc = fmt.htmlSafe;

function param(name) {
  const m = new RegExp('(?:^|&)' + name + '=([^&]+)').exec(location.hash.split('?')[1] || '');
  return m ? decodeURIComponent(m[1]) : '';
}
function hashFor(tool, range, group) {
  return '#/costs/' + encodeURIComponent(tool) + '?range=' + encodeURIComponent(range)
    + (group ? '&group=' + encodeURIComponent(group) : '');
}

export function scatterChart(scope, el, rows, labelOf, th) {
  const c = scope.init(el);
  const axis = name => ({
    type: 'log', name, nameLocation: 'middle', nameGap: 28, nameTextStyle: { color: '#8B98A6' },
    axisLabel: { color: '#8B98A6' }, axisLine: { lineStyle: { color: '#1F2630' } },
    splitLine: { lineStyle: { color: '#1F2630' } },
  });
  const pts = rows.filter(r => r.calls > 0 && r.cost_per_call > 0);
  c.setOption({
    textStyle: { color: '#E6EDF3', fontFamily: 'Inter' },
    grid: { left: 56, right: 16, top: 16, bottom: 44, containLabel: true },
    tooltip: {
      backgroundColor: '#0F1419', borderColor: '#283040', textStyle: { color: '#E6EDF3', fontSize: 12 },
      formatter: p => `${esc(labelOf(p.data[2]))}<br>${fmt.int(p.data[0])} calls, ${fmt.usd4(p.data[1])} / call<br>${esc(p.data[2].quadrant_label)}`,
    },
    xAxis: axis('calls (log)'), yAxis: axis('cost per call, USD (log)'),
    series: [{
      type: 'scatter', symbolSize: d => Math.max(8, Math.min(34, 8 + Math.log10(1 + d[3] * 1e4) * 4)),
      data: pts.map(r => ({ value: [r.calls, r.cost_per_call, r, r.est_cost_usd], itemStyle: { color: Q_COLOR[r.quadrant], opacity: 0.8 } })),
      markLine: {
        silent: true, symbol: 'none', lineStyle: { color: '#8B98A6', type: 'dashed' }, label: { color: '#8B98A6', fontSize: 10 },
        data: [{ xAxis: Math.max(th.calls_threshold, 1), name: 'frequent from' }, { yAxis: th.cost_per_call_threshold, name: 'tool avg cost/call' }]
          .filter(m => (m.xAxis || m.yAxis) > 0),
      },
    }],
  });
  return c;
}

function summary(d) {
  const t = d.totals, rp = d.repeat;
  const kpi = (l, v, s) => `<div class="card kpi"><div class="label">${l}</div><div class="value">${v}</div>${s ? `<div class="sub">${s}</div>` : ''}</div>`;
  return `<div class="grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px">
    ${kpi('est. cost', fmt.usd(t.est_cost_usd), `carry ${fmt.usd(t.carry_cost_usd)}`)}
    ${kpi('calls', fmt.int(t.calls), `${fmt.int(t.unattributed_calls)} unattributed (not ranked)`)}
    ${kpi('sessions', fmt.int(t.sessions), '')}
    ${kpi('error rate', fmt.pct(t.error_rate), '')}
    ${kpi('repeat calls', fmt.int(rp.repeat_calls), `${fmt.pct(rp.share_of_calls)} of calls, ${fmt.usd(rp.repeat_cost_usd)} (${fmt.pct(rp.share_of_cost)} of cost)`)}
  </div>`;
}

const labelOf = r => r.label || r.key;
function keyCell(r) {
  const text = esc(fmt.short(labelOf(r), 90));
  return r.session_id ? `<a href="#/sessions/${encodeURIComponent(r.session_id)}">${text}</a>` : text;
}

function table(d, sort) {
  const rows = [...d.rows].sort((a, b) => b[sort] - a[sort]);
  const th = (k, l) => `<th class="num" aria-sort="${k === sort ? 'descending' : 'none'}"><button class="sortbtn${k === sort ? ' active' : ''}" data-sort="${k}" aria-label="sort by ${l}">${l}${k === sort ? ' ▼' : ''}</button></th>`;
  const gl = (d.available_groups.find(g => g.key === d.group) || { label: d.group }).label;
  return `<table><thead><tr><th>${esc(gl)}</th>${th('calls', 'calls')}${th('est_cost_usd', 'est. cost')}
    <th class="num">share</th><th class="num">cum.</th><th class="num">carry</th>${th('cost_per_call', 'cost / call')}
    ${th('avg_result_tokens', 'avg tok')}<th class="num">max tok</th><th class="num">err</th><th class="num">sess.</th>
    <th class="num">per sess.</th><th>first / last</th><th>quadrant</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td class="mono" style="font-size:11px;word-break:break-all" title="${esc(labelOf(r) === r.key ? r.key : labelOf(r) + ' (' + r.key + ')')}">${keyCell(r)}</td>
      <td class="num">${fmt.int(r.calls)}</td><td class="num">${fmt.usd4(r.est_cost_usd)}</td>
      <td class="num">${fmt.pct(r.share_of_tool_cost)}</td><td class="num">${fmt.pct(r.cumulative_share)}</td>
      <td class="num">${fmt.usd4(r.carry_cost_usd)}</td><td class="num">${fmt.usd4(r.cost_per_call)}</td>
      <td class="num">${fmt.int(Math.round(r.avg_result_tokens))}</td><td class="num">${fmt.int(r.max_result_tokens)}</td>
      <td class="num">${fmt.pct(r.error_rate)}</td><td class="num">${fmt.int(r.sessions)}</td>
      <td class="num">${r.frequency_per_session}</td>
      <td class="mono">${fmt.ts(r.first_seen)}<br>${fmt.ts(r.last_seen)}</td>
      <td><span class="badge" style="color:${Q_COLOR[r.quadrant]};border-color:${Q_COLOR[r.quadrant]}">${esc(r.quadrant_label)}</span></td></tr>`).join('')
      || '<tr><td colspan="14" class="muted">no attributed calls in this range</td></tr>'}
    </tbody></table>`;
}

function diags(list) {
  return `<div class="card" style="margin-top:16px"><h3>Diagnostics</h3>${list.length
    ? `<table><tbody>${list.map(x => `<tr><td style="width:70px"><span class="badge" style="color:${SEV_COLOR[x.severity] || '#888'};border-color:${SEV_COLOR[x.severity] || '#888'}">${esc(x.severity)}</span></td>
      <td>${esc(x.text)}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">Nothing suspicious found.</p>'}</div>`;
}

const DIM_NOTE = {
  result_size: 'Cost by result size: many small results or a few huge ones?',
  day: 'Cost per day (UTC)', hour_of_day: 'Cost by hour of day (UTC)',
};

function pageHtml(d, range, notice) {
  const isDay = d.group === 'day';
  const th = d.thresholds;
  return `
    <div class="flex" style="margin-bottom:14px">
      <a href="#/costs?range=${esc(range)}">&larr; Costs</a>
      <h2 style="margin:0;font-size:16px"><span class="badge">${esc(d.tool)}</span> drill-down</h2>
      ${groupSelect(d)}
      <div class="spacer"></div>
      <span class="muted" style="font-size:12px">range ${esc(range)} (change on the Costs tab)</span>
    </div>
    ${notice ? `<div class="card drill-notice" role="alert">${esc(notice)}</div>` : ''}
    ${summary(d)}
    ${d.truncated ? `<div class="card" style="margin-bottom:16px"><span class="muted">More than ${fmt.int(d.meta.key_cap)} distinct keys: the rest are merged into one "other" row.</span></div>` : ''}
    <div class="card"><h3>${isDay ? 'Cost per day (UTC)' : 'Top 15 by estimated cost'}</h3>
      ${DIM_NOTE[d.group] && !isDay ? `<p class="muted" style="font-size:12px">${esc(DIM_NOTE[d.group])}</p>` : ''}
      <div id="ch-bar" style="height:280px"></div></div>
    ${isDay ? '' : `<div class="card" style="margin-top:16px"><h3>Cost per day (UTC)</h3><div id="ch-time" style="height:160px"></div></div>`}
    <div class="card" style="margin-top:16px"><h3>Frequency vs cost per call</h3>
      <p class="muted" style="font-size:12px">Thresholds: high volume = at least ${fmt.int(th.calls_threshold)} calls, expensive = at least ${fmt.usd4(th.cost_per_call_threshold)} per call (this tool's average). "High volume" is relative to this tool's heaviest keys, not to other tools. <span title="${esc(th.method)}">Method: ${esc(th.method)}</span>
        Right = more calls, up = costs more per call. Colours: ${Object.entries(d.quadrant_labels).map(([k, l]) => `<span style="color:${Q_COLOR[k]}">&#9679; ${esc(l)}</span>`).join(' &nbsp; ')}</p>
      <div id="ch-sc" style="height:320px"></div></div>
    <div class="card" style="margin-top:16px"><h3>Breakdown</h3><div id="tbl" style="overflow-x:auto"></div></div>
    ${diags(d.diagnostics)}`;
}

function paint(root, d, tool, range, notice, onGroup) {
  const scope = chartScope();
  root.innerHTML = pageHtml(d, range, notice);
  let sort = 'est_cost_usd';
  const tbl = root.querySelector('#tbl');
  const draw = () => {
    tbl.innerHTML = table(d, sort);
    tbl.querySelectorAll('button.sortbtn').forEach(b => b.addEventListener('click', () => {
      sort = b.dataset.sort; draw();
      const again = tbl.querySelector('button[data-sort="' + sort + '"]');
      if (again) again.focus();
    }));
  };
  draw();
  root.querySelector('#grp').addEventListener('change', e => onGroup(e.target.value));
  if (d.group === 'day') dayBars(scope, root.querySelector('#ch-bar'), d.rows.map(r => ({ day: r.key, est_cost_usd: r.est_cost_usd })));
  else topBar(scope, root.querySelector('#ch-bar'), d.rows, labelOf);
  sparkCard(scope, root, d);
  scatterChart(scope, root.querySelector('#ch-sc'), d.rows, labelOf, d.thresholds);
}

export default async function (root, tool) {
  const range = param('range') || '30d';
  const days = { '7d': 7, '30d': 30, '90d': 90 }[range] || null;
  const since = days ? new Date(Date.now() - days * 86400e3).toISOString() : null;
  const urlFor = group => '/api/tool-costs/params?limit=500&tool=' + encodeURIComponent(tool) + '&group=' + encodeURIComponent(group)
    + (since ? '&since=' + encodeURIComponent(since) : '');
  let seq = 0;

  // Load a group; on HTTP 400 (stale/invalid group) fall back to auto and say why.
  async function load(group, firstLoad) {
    const mine = ++seq;
    if (firstLoad) root.innerHTML = loadingHtml(tool); else setBusy(root, true);
    let res = await fetchJson(urlFor(group)), notice = '';
    if (res.status === 400 && group !== 'auto') {
      const why = (res.data && res.data.error) || 'invalid group';
      res = await fetchJson(urlFor('auto'));
      notice = `Group "${group}" is not available (${why}); showing the automatic grouping instead.`;
    }
    if (mine !== seq) return;               // a newer request superseded this one
    setBusy(root, false);
    if (!res.ok) {
      const why = (res.data && res.data.error) || ('HTTP ' + res.status);
      root.innerHTML = `<div class="card"><a href="#/costs?range=${esc(range)}">&larr; Costs</a><p class="muted">Could not load tool ${esc(tool)}: ${esc(why)}</p></div>`;
      return;
    }
    const d = res.data;
    if (notice || !firstLoad) history.replaceState(null, '', hashFor(tool, range, notice ? 'auto' : d.group));
    paint(root, d, tool, range, notice, g => {
      history.pushState(null, '', hashFor(tool, range, g));   // no hashchange: keep content, dim it while loading
      load(g, false);
    });
  }
  await load(param('group') || 'auto', true);
}
