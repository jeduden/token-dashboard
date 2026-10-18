// costs-skill.js — per-skill drill-down: #/costs/skill/<name>?range=30d&group=project
import { fmt } from '/web/app.js';

const SEV_COLOR = { high: '#E5484D', warn: '#F5A623', info: '#4A9EFF' };
const esc = fmt.htmlSafe;
const COLS = [
  ['est_cost_usd', 'est. cost'], ['invocations', 'invocations'], ['cost_per_invocation', 'cost / invocation'],
  ['avg_carry_turns', 'avg carry turns'], ['load_cost_usd', 'load'], ['carry_cost_usd', 'carry'],
];

function param(name) {
  const m = new RegExp('(?:^|&)' + name + '=([^&]+)').exec(location.hash.split('?')[1] || '');
  return m ? decodeURIComponent(m[1]) : '';
}
const go = (skill, range, group) => {
  location.hash = '#/costs/skill/' + encodeURIComponent(skill) + '?range=' + encodeURIComponent(range)
    + (group ? '&group=' + encodeURIComponent(group) : '');
};

// One resize listener per visit; removed (and charts disposed) when the route changes.
let teardown = null;
function trackCharts(charts) {
  if (teardown) teardown();
  const onResize = () => charts.forEach(c => c.resize());
  const off = () => {
    window.removeEventListener('resize', onResize);
    window.removeEventListener('hashchange', off);
    charts.forEach(c => { try { c.dispose(); } catch (e) { /* already gone */ } });
    if (teardown === off) teardown = null;
  };
  window.addEventListener('resize', onResize);
  window.addEventListener('hashchange', off);
  teardown = off;
}

function chart(el, opt) {
  const c = echarts.init(el, null, { renderer: 'svg' });
  const axis = { axisLine: { lineStyle: { color: '#1F2630' } }, axisLabel: { color: '#8B98A6' }, axisTick: { show: false } };
  c.setOption({
    textStyle: { color: '#E6EDF3', fontFamily: 'Inter' },
    grid: { left: 36, right: 12, top: 24, bottom: 24, containLabel: true },
    tooltip: { trigger: 'axis', backgroundColor: '#0F1419', borderColor: '#283040', textStyle: { color: '#E6EDF3', fontSize: 12 } },
    xAxis: { ...axis, type: 'category', data: opt.categories, axisLabel: { ...axis.axisLabel, interval: 0, rotate: opt.categories.length > 5 ? 25 : 0 } },
    yAxis: [{ ...axis, type: 'value', axisLine: { show: false }, splitLine: { lineStyle: { color: '#1F2630' } } }]
      .concat(opt.y2 ? [{ ...axis, type: 'value', axisLine: { show: false }, splitLine: { show: false }, minInterval: 1 }] : []),
    series: opt.series,
  });
  return c;
}

const kpi = (l, v, s) => `<div class="card kpi"><div class="label">${l}</div><div class="value">${v}</div>${s ? `<div class="sub">${s}</div>` : ''}</div>`;

function summary(d) {
  const t = d.totals;
  const loadShare = t.est_cost_usd ? t.load_cost_usd / t.est_cost_usd : null;
  const cat = d.catalog.installed ? `catalog: ${esc(d.catalog.root)}, ${fmt.int(d.catalog.tokens)} tok` : 'not in catalog';
  return `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px">
    ${kpi('invocations', fmt.int(t.invocations), `${fmt.int(t.unattributed_invocations)} unattributed (not costed)`)}
    ${kpi('sessions', fmt.int(t.sessions), `first ${esc(fmt.ts(t.first_used))}<br>last ${esc(fmt.ts(t.last_used))}`)}
    ${kpi('est. cost', fmt.usd(t.est_cost_usd), `rank ${t.rank} of ${t.skills_total}, ${fmt.pct(t.cost_percentile)} percentile` + (t.vs_average == null ? '' : `, ${t.vs_average}x avg skill`))}
    ${kpi('cost / invocation', fmt.usd4(t.cost_per_invocation), `avg ${fmt.int(Math.round(d.avg_carry_turns))} carry turns`)}
    ${kpi('load vs carry', `${fmt.usd(t.load_cost_usd)} / ${fmt.usd(t.carry_cost_usd)}`, loadShare == null ? '' : `${fmt.pct(loadShare)} load, ${fmt.pct(1 - loadShare)} carry`)}
    ${kpi('load size', `${fmt.int(t.est_load_tokens)}${t.estimated ? '~' : ''} tok`, (t.estimated ? 'estimated; ' : '') + cat)}
  </div>`;
}

function table(d, sort, asc) {
  const rows = [...d.rows].sort((a, b) => (asc ? 1 : -1) * (a[sort] - b[sort]));
  const named = d.group === 'project' || d.group === 'repo' || d.group === 'session';
  const th = ([k, l]) => `<th class="num" aria-sort="${k === sort ? (asc ? 'ascending' : 'descending') : 'none'}">
    <button class="sort-btn" data-sort="${k}" aria-label="sort by ${l}" style="${k === sort ? 'color:var(--text)' : ''}">${l}${k === sort ? (asc ? ' ▲' : ' ▼') : ''}</button></th>`;
  const label = r => (d.group === 'project' || d.group === 'session') && r.project_name
    ? `${esc(r.project_name)}<div class="muted mono" style="font-size:10px">${esc(fmt.short(r.key, 60))}</div>`
    : d.group === 'repo' ? esc(r.repo_name || r.key) : esc(fmt.short(r.key, 60));
  return `<table><thead><tr><th>${esc(d.group)}</th>${COLS.map(th).join('')}
    <th class="num">sessions</th><th class="num">share</th><th class="num">cum.</th><th>first / last</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td class="mono" style="font-size:11px;word-break:break-all" title="${esc(r.key)}">${label(r)}${named && r.repo_name && d.group !== 'repo' ? `<span class="muted" style="font-size:10px"> · ${esc(r.repo_name)}</span>` : ''}</td>
      <td class="num">${fmt.usd4(r.est_cost_usd)}</td><td class="num">${fmt.int(r.invocations)}</td>
      <td class="num">${fmt.usd4(r.cost_per_invocation)}</td><td class="num">${r.avg_carry_turns}</td>
      <td class="num">${fmt.usd4(r.load_cost_usd)}</td><td class="num">${fmt.usd4(r.carry_cost_usd)}</td>
      <td class="num">${fmt.int(r.sessions)}</td><td class="num">${fmt.pct(r.share)}</td><td class="num">${fmt.pct(r.cumulative_share)}</td>
      <td class="mono">${esc(fmt.ts(r.first_seen))}<br>${esc(fmt.ts(r.last_seen))}</td></tr>`).join('')
      || '<tr><td colspan="11" class="muted">no costed invocations in this range</td></tr>'}
    </tbody></table>`;
}

function repeatCard(d) {
  const r = d.repeat, sp = r.carry_split;
  const ws = r.worst_session;
  const cell = (n, l) => `<td>${l}</td><td class="num">${fmt.int(sp[n].invocations)}</td><td class="num">${fmt.usd4(sp[n].est_cost_usd)}</td><td class="num">${fmt.usd4(sp[n].carry_cost_usd)}</td>`;
  return `<div class="card" style="margin-top:16px"><h3>Reloads and carry length</h3>
    <p>${fmt.int(r.repeat_invocations)} re-invocation(s) of this skill within the same session
      (${fmt.int(r.sessions_with_repeats)} session(s)), costing ${fmt.usd(r.repeat_cost_usd)}
      (${fmt.pct(r.share_of_cost)} of its cost).
      ${ws ? `Worst: <a href="#/sessions/${encodeURIComponent(ws.session_id)}">${esc(ws.session_id.slice(0, 8))}</a> (${fmt.int(ws.invocations)} loads).` : ''}</p>
    <table><thead><tr><th>kind</th><th class="num">invocations</th><th class="num">est. cost</th><th class="num">carry</th></tr></thead><tbody>
      <tr>${cell('short', 'loaded, session ended soon')}</tr><tr>${cell('medium', 'carried a while')}</tr><tr>${cell('long', 'carried long')}</tr>
    </tbody></table><p class="muted" style="font-size:12px">${esc(r.carry_split_note)}</p></div>`;
}

function sessionsCard(d) {
  return `<div class="card" style="margin-top:16px"><h3>Top sessions for this skill</h3>
    <table><thead><tr><th>session</th><th>project</th><th class="num">invocations</th><th class="num">carry turns</th><th class="num">est. cost</th></tr></thead><tbody>
    ${d.top_sessions.map(s => `<tr><td class="mono"><a href="#/sessions/${encodeURIComponent(s.session_id)}">${esc(s.session_id.slice(0, 8))}</a></td>
      <td>${esc(s.project_name || s.project_slug || '')}</td><td class="num">${fmt.int(s.invocations)}</td>
      <td class="num">${fmt.int(s.carry_turns)}</td><td class="num">${fmt.usd4(s.est_cost_usd)}</td></tr>`).join('')
      || '<tr><td colspan="5" class="muted">no sessions</td></tr>'}</tbody></table></div>`;
}

function diagCard(list) {
  const ev = e => e && e.session_id ? `session ${e.session_id.slice(0, 8)}` : '';
  return `<div class="card" style="margin-top:16px"><h3>Diagnostics</h3>${list.length
    ? `<table><tbody>${list.map(x => `<tr><td style="width:70px"><span class="badge sev-badge" style="color:${SEV_COLOR[x.severity] || '#888'};border-color:${SEV_COLOR[x.severity] || '#888'}">${esc(x.severity)}</span></td>
      <td><b>${esc(x.rule)}</b>: ${esc(x.text)}
        ${x.evidence && x.evidence.session_id ? `<div class="muted mono" style="font-size:11px">evidence: <a href="#/sessions/${encodeURIComponent(x.evidence.session_id)}">${esc(ev(x.evidence))}</a></div>` : ''}
        ${x.suggestion ? `<div class="muted" style="font-size:12px">Suggestion: ${esc(x.suggestion)}</div>` : ''}</td></tr>`).join('')}</tbody></table>`
    : '<p class="muted">Nothing suspicious found.</p>'}</div>`;
}

export default async function (root, skill) {
  const range = param('range') || '30d';
  const days = { '7d': 7, '30d': 30, '90d': 90 }[range] || null;
  const group = param('group') || 'project';
  const since = days ? new Date(Date.now() - days * 86400e3).toISOString() : null;
  const back = `<a href="#/costs?range=${esc(range)}">&larr; Costs</a>`;
  root.innerHTML = `<div class="card">${back}<p class="muted">Loading ${esc(skill)}…</p></div>`;
  const url = '/api/skill-costs/detail?limit=500&skill=' + encodeURIComponent(skill) + '&group=' + encodeURIComponent(group)
    + (since ? '&since=' + encodeURIComponent(since) : '');
  let d;
  try {
    const r = await fetch(url);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || ('request failed (' + r.status + ')'));
    d = body;
  } catch (e) {
    root.innerHTML = `<div class="card">${back}<p>${esc(String(e.message || e))}</p>
      <p><a href="#/costs?range=${esc(range)}">Back to the Costs tab</a></p></div>`;
    return;
  }
  let sort = 'est_cost_usd', asc = false;
  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      ${back}
      <h2 style="margin:0;font-size:16px"><span class="badge">${esc(d.skill)}</span> skill drill-down</h2>
      <label class="muted" style="font-size:12px">group by
        <select id="skill-grp">${d.available_groups.map(g => `<option value="${esc(g.key)}" ${g.key === d.group ? 'selected' : ''}>${esc(g.label)}</option>`).join('')}</select></label>
      <div class="spacer"></div>
      <span class="muted" style="font-size:12px">range ${esc(range)} (change on the Costs tab)</span>
    </div>
    ${summary(d)}
    ${d.truncated ? `<div class="card" style="margin-bottom:16px"><span class="muted">More than ${fmt.int(d.meta.key_cap)} distinct keys: the rest are merged into one "other" row.</span></div>` : ''}
    <div class="card"><h3>Top 15 by estimated cost (${esc(d.group)})</h3><div id="skill-bar" style="height:280px"></div></div>
    <div class="card" style="margin-top:16px"><h3>Invocations and cost per day</h3><div id="skill-tl" style="height:220px"></div></div>
    <div class="card" style="margin-top:16px"><h3>Breakdown</h3><div id="skill-tbl" style="overflow-x:auto"></div></div>
    ${repeatCard(d)}${sessionsCard(d)}${diagCard(d.diagnostics)}`;
  const tbl = root.querySelector('#skill-tbl');
  const draw = () => {
    tbl.innerHTML = table(d, sort, asc);
    tbl.querySelectorAll('button.sort-btn').forEach(b => b.addEventListener('click', () => {
      asc = b.dataset.sort === sort ? !asc : false;
      sort = b.dataset.sort;
      draw();
      const again = tbl.querySelector(`button[data-sort="${sort}"]`);
      if (again) again.focus();
    }));
  };
  draw();
  root.querySelector('#skill-grp').addEventListener('change', e => go(skill, range, e.target.value));
  const top = d.rows.slice(0, 15);
  const tl = d.invocation_timeline;
  trackCharts([
    chart(root.querySelector('#skill-bar'), {
      categories: top.map(r => fmt.short(r.project_name || r.repo_name || r.key, 26)),
      series: [{ type: 'bar', data: top.map(r => r.est_cost_usd), itemStyle: { color: '#E5A23B', borderRadius: [4, 4, 0, 0] }, barMaxWidth: 32 }],
    }),
    chart(root.querySelector('#skill-tl'), {
      y2: true,
      categories: tl.map(x => x.day),
      series: [{ name: 'est. cost', type: 'bar', data: tl.map(x => x.est_cost_usd), itemStyle: { color: '#E5A23B' }, barMaxWidth: 24 },
               { name: 'invocations', type: 'line', yAxisIndex: 1, data: tl.map(x => x.invocations), showSymbol: false, lineStyle: { color: '#4A9EFF' }, itemStyle: { color: '#4A9EFF' } }],
    }),
  ]);
}
