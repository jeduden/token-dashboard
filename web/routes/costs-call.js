// costs-call.js — single tool-call drill-down: #/costs/call/<tool_calls.id>?range=30d
import { fmt } from '/web/app.js';

const SEV_COLOR = { high: '#E5484D', warn: '#F5A623', info: '#4A9EFF' };
const END_TEXT = {
  session_end: 'Carry ended: session end (no more turns in this thread).',
  compaction_detected: 'Carry ended: compaction detected (cache read dropped sharply).',
  ongoing: 'Carry still running: the thread was active recently, so cost may still grow.',
  not_attributed: 'No linked result for this call, so no cost is estimated.',
};
const END_SHORT = { session_end: 'session end', compaction_detected: 'compaction', ongoing: 'ongoing' };
const esc = fmt.htmlSafe;

function param(name) {
  const m = new RegExp('(?:^|&)' + name + '=([^&]+)').exec(location.hash.split('?')[1] || '');
  return m ? decodeURIComponent(m[1]) : '';
}
function idFromHash() {
  const m = /^#?\/costs\/call\/([^/?]+)/.exec(location.hash);
  return m ? decodeURIComponent(m[1]) : '';
}

const badge = (text, color) => `<span class="badge"${color ? ` style="color:${color};border-color:${color}"` : ''}>${esc(text)}</span>`;
const kpi = (label, value, sub) => `<div class="card kpi"><div class="label">${esc(label)}</div><div class="value">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;

function callLink(id, range, inner) {
  return `<a href="#/costs/call/${encodeURIComponent(id)}?range=${encodeURIComponent(range)}">${inner}</a>`;
}

function callsTable(title, rows, range, empty) {
  return `<div class="card" style="margin-top:16px"><h3>${esc(title)}</h3><div style="overflow-x:auto"><table>
    <thead><tr><th>tool</th><th>target</th><th class="num">result tok</th><th class="num">est. cost</th><th>when</th></tr></thead><tbody>
    ${rows.map(c => `<tr><td><span class="badge">${esc(c.tool_name)}</span></td>
      <td class="mono" style="font-size:11px;word-break:break-all">${callLink(c.call_id, range, esc(fmt.short(c.target || '(none)', 90)))}</td>
      <td class="num">${c.result_tokens == null ? '—' : fmt.int(c.result_tokens)}</td><td class="num">${fmt.usd4(c.est_cost_usd)}</td>
      <td class="mono">${fmt.ts(c.timestamp)}</td></tr>`).join('') || `<tr><td colspan="5" class="muted">${esc(empty)}</td></tr>`}
    </tbody></table></div></div>`;
}

function sameTarget(st, range) {
  if (!st.applicable) return '<div class="card" style="margin-top:16px"><h3>Same target</h3><p class="muted">This call has no target, so repeats cannot be matched.</p></div>';
  return `<div class="card" style="margin-top:16px"><h3>Same tool and target</h3>
    <p>${fmt.int(st.other_calls)} other call(s) with the identical target, ${fmt.usd4(st.other_cost_usd)} in total,
      ${fmt.int(st.in_this_session)} of them in this session.
      <span class="muted">First seen ${fmt.ts(st.first_seen) || '—'}, last seen ${fmt.ts(st.last_seen) || '—'}.</span></p>
    ${st.top.length ? `<table><thead><tr><th>most expensive repeats</th><th>session</th><th>when</th><th class="num">est. cost</th></tr></thead><tbody>
      ${st.top.map(t => `<tr><td>${callLink(t.call_id, range, 'call #' + esc(String(t.call_id)))}</td>
        <td class="mono">${esc((t.session_id || '').slice(0, 8))}</td><td class="mono">${fmt.ts(t.timestamp)}</td>
        <td class="num">${fmt.usd4(t.est_cost_usd)}</td></tr>`).join('')}</tbody></table>` : ''}</div>`;
}

function diags(list) {
  return `<div class="card" style="margin-top:16px"><h3>Diagnostics</h3>${list.length
    ? `<table><tbody>${list.map(x => `<tr><td style="width:70px">${badge(x.severity, SEV_COLOR[x.severity] || '#888')}</td>
      <td>${esc(x.text)}${x.suggestion ? `<div class="muted" style="font-size:12px">Suggestion: ${esc(x.suggestion)}</div>` : ''}</td></tr>`).join('')}</tbody></table>`
    : '<p class="muted">Nothing suspicious found for this call.</p>'}</div>`;
}

function accrual(d) {
  const pts = [];
  if (d.create_turn) pts.push({ x: '+1 create', v: d.create_turn.cost_usd, ts: d.create_turn.timestamp, model: d.create_turn.model });
  d.carry_timeline.forEach(t => pts.push({ x: '+' + t.turn_offset, v: t.cumulative_usd, ts: t.timestamp, model: t.model }));
  return pts;
}

function drawChart(el, d, cleanup) {
  const pts = accrual(d);
  if (!pts.length || typeof echarts === 'undefined') return;
  const c = echarts.init(el, null, { renderer: 'svg' });
  const onResize = () => c.resize();
  window.addEventListener('resize', onResize);
  cleanup.push(() => { window.removeEventListener('resize', onResize); c.dispose(); });
  const reason = d.carry_end.reason;
  const last = pts[pts.length - 1].x;
  c.setOption({
    textStyle: { color: '#E6EDF3', fontFamily: 'Inter' },
    grid: { left: 40, right: 24, top: 28, bottom: 28, containLabel: true },
    tooltip: {
      trigger: 'axis', backgroundColor: '#0F1419', borderColor: '#283040', textStyle: { color: '#E6EDF3', fontSize: 12 },
      formatter: p => { const q = pts[p[0].dataIndex]; return `${esc(q.x)} turn, ${esc(fmt.ts(q.ts))}<br>${esc(fmt.modelShort(q.model))}<br>cumulative ${fmt.usd4(q.v)}`; },
    },
    xAxis: { type: 'category', data: pts.map(p => p.x), boundaryGap: false, axisLabel: { color: '#8B98A6' }, axisLine: { lineStyle: { color: '#1F2630' } }, axisTick: { show: false } },
    yAxis: { type: 'value', axisLabel: { color: '#8B98A6', formatter: v => '$' + v }, splitLine: { lineStyle: { color: '#1F2630' } } },
    series: [{
      type: 'line', step: 'end', data: pts.map(p => p.v), showSymbol: pts.length < 40, lineStyle: { width: 2, color: '#E5A23B' },
      itemStyle: { color: '#E5A23B' }, areaStyle: { opacity: 0.12, color: '#E5A23B' },
      markLine: END_SHORT[reason] ? {
        silent: true, symbol: 'none', lineStyle: { color: reason === 'compaction_detected' ? '#E5484D' : '#8B98A6', type: 'dashed' },
        label: { color: '#8B98A6', formatter: END_SHORT[reason], position: 'insideEndTop' }, data: [{ xAxis: last }],
      } : undefined,
    }],
  });
}

function view(d, range) {
  const id = d.identity, c = d.cost, st = d.same_target;
  const where = [id.project ? esc(id.project) : '', id.repo_name && id.repo_name !== id.project ? 'repo ' + esc(id.repo_name) : '']
    .filter(Boolean).join(' · ');
  const thread = id.thread.is_sidechain ? 'subagent ' + esc(id.thread.agent_id || '') : 'main thread';
  const rankSub = c.rank == null ? 'not ranked' : `of ${fmt.int(c.rank_of)} ${esc(id.tool_name)} calls, top ${(100 * c.rank / c.rank_of).toFixed(1)}%`;
  return `
    <div class="flex" style="margin-bottom:14px">
      <a href="#/costs?range=${esc(range)}">&larr; Costs</a>
      <h2 style="margin:0;font-size:16px">${badge(id.tool_name)} call #${esc(String(id.call_id))}</h2>
      <div class="spacer"></div>
      <span class="muted mono" style="font-size:12px">${esc(fmt.ts(id.timestamp))}</span>
    </div>
    <div class="card"><h3>Target</h3>
      <pre class="mono" style="white-space:pre-wrap;word-break:break-all;margin:0;font-size:12px">${esc(id.target || '(none)')}</pre>
      <p class="muted" style="font-size:12px;margin:10px 0 0">${where ? where + ' · ' : ''}${thread}${id.is_error ? ' · ' + badge('error', SEV_COLOR.high) : ''}
        · <a href="#/sessions/${encodeURIComponent(d.session_link)}">session ${esc(d.session_link.slice(0, 8))}</a>
        · tool_use_id <span class="mono">${esc(id.tool_use_id || '—')}</span></p></div>
    <div class="grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:16px 0">
      ${kpi('est. cost', fmt.usd4(c.est_cost_usd), `${fmt.pct(c.share_of_session_cost)} of session tool cost, ${fmt.pct(c.share_of_thread_spend)} of thread spend`)}
      ${kpi('create vs carry', `${fmt.usd4(c.create_cost_usd)} / ${fmt.usd4(c.carry_cost_usd)}`, `carried over ${fmt.int(c.carry_turns)} turn(s)`)}
      ${kpi('rank', c.rank == null ? '—' : '#' + fmt.int(c.rank), rankSub)}
      ${kpi('result tokens', id.result_tokens == null ? '—' : fmt.int(id.result_tokens), id.result_chars == null ? '' : fmt.int(id.result_chars) + ' chars')}
    </div>
    <div class="card"><h3>Cost accrual over later turns</h3>
      <p class="muted" style="font-size:12px">${esc(END_TEXT[d.carry_end.reason] || '')}</p>
      <div id="ch-accrual" style="height:260px"></div></div>
    ${callsTable('Same assistant message (parallel batch)', d.siblings, range, 'no sibling calls in this message')}
    ${callsTable('Previous calls in this thread', d.previous, range, 'none before it')}
    ${callsTable('Next calls in this thread', d.next, range, 'none after it')}
    ${sameTarget(st, range)}
    ${diags(d.diagnostics)}
    <p style="margin-top:16px"><a href="#/sessions/${encodeURIComponent(d.session_link)}">Open the session &rarr;</a></p>`;
}

export default async function (root, idArg) {
  const id = idArg || idFromHash();
  const range = param('range') || '30d';
  const cleanup = [];
  let gone = false;
  const off = () => { gone = true; window.removeEventListener('hashchange', off); cleanup.splice(0).forEach(f => f()); };
  window.addEventListener('hashchange', off);
  const back = `<a href="#/costs?range=${esc(range)}">&larr; Back to Costs</a>`;
  root.innerHTML = `<div class="card">${back}<p class="muted" role="status">Loading call ${esc(id)}…</p></div>`;
  let d;
  try {
    const r = await fetch('/api/tool-costs/call?id=' + encodeURIComponent(id));
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || ('request failed (' + r.status + ')'));
    d = body;
  } catch (e) {
    root.innerHTML = `<div class="card">${back}<p role="alert">${esc(String(e.message || e))}</p></div>`;
    return;
  }
  if (gone) return;
  root.innerHTML = view(d, range);
  drawChart(root.querySelector('#ch-accrual'), d, cleanup);
}
