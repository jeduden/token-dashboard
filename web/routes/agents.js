import { api, fmt } from '/web/app.js';
import { readRange, withSince, rangeTabs, bindRange } from '/web/range.js';

const esc = fmt.htmlSafe;
const pct = n => n >= 10 ? n.toFixed(0) + '%' : n.toFixed(1) + '%';

export default async function (root) {
  const range = readRange('30d');
  const d = await api(withSince('/api/agent-types?limit=25', range));
  const t = d.totals;
  const kpi = (label, value, sub = '') => `<div class="card kpi"><div class="label">${label}</div><div class="value">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;
  const maxType = Math.max(...d.by_type.map(x => x.cost_usd), 1e-12);
  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px">Agents</h2>
      <span class="muted" style="font-size:12px">what subagents cost, by type and as single runs · estimates</span>
      <div class="spacer"></div>${rangeTabs(range)}
    </div>
    <div class="row cols-4">
      ${kpi('Subagent cost', fmt.usd(t.sub_cost_usd), `${pct(t.sub_share_pct)} of ${fmt.usd(t.cost_usd)}`)}
      ${kpi('Main thread cost', fmt.usd(t.main_cost_usd))}
      ${kpi('Subagent runs', fmt.int(t.runs))}
      ${kpi('Average run', t.runs ? fmt.usd(t.sub_cost_usd / t.runs) : '—')}
    </div>
    <div class="card" style="margin-top:16px">
      <h3 style="margin-top:0">By type</h3>
      <p class="muted" style="font-size:12px;margin:0 0 8px">Type is what the spawning Agent call asked for, else the name in the agent id; runs with neither are <code>unknown</code>.
        Share is of all cost in the range, main thread included.</p>
      <table>
        <thead><tr><th>type</th><th class="num">runs</th><th class="num">turns</th><th class="num">tokens</th><th class="num">cost</th><th class="num">avg / run</th><th class="num">biggest run</th><th class="num">share</th><th style="min-width:120px"></th></tr></thead>
        <tbody>${d.by_type.slice(0, 40).map(x => `<tr>
          <td>${esc(x.type)}</td><td class="num">${fmt.int(x.runs)}</td><td class="num">${fmt.int(x.turns)}</td>
          <td class="num" title="${fmt.int(x.total_tokens)} incl. cache reads">${fmt.compact(x.billable_tokens)}</td>
          <td class="num">${fmt.usd(x.cost_usd)}</td><td class="num">${fmt.usd(x.avg_run_usd)}</td><td class="num">${fmt.usd(x.max_run_usd)}</td>
          <td class="num">${pct(x.share_pct)}</td>
          <td><div style="height:8px;border-radius:2px;background:#7C5CFF;width:${Math.max(2, 100 * x.cost_usd / maxType).toFixed(1)}%"></div></td></tr>`).join('')
          || '<tr><td colspan="9" class="muted">no subagent runs in this range</td></tr>'}</tbody>
      </table>
    </div>
    <div class="card" style="margin-top:16px">
      <h3 style="margin-top:0">Most expensive runs</h3>
      <p class="muted" style="font-size:12px;margin:0 0 8px">One subagent, start to finish. A long run re-reads its growing context on every turn, so cost climbs faster than turns.</p>
      <table>
        <thead><tr><th>run</th><th>type</th><th>project</th><th class="num">turns</th><th class="num">peak context</th><th class="num">cost</th><th class="num">share</th><th>session</th></tr></thead>
        <tbody>${d.runs.map(r => `<tr>
          <td class="blur-sensitive" title="${esc(r.description || '')}">${esc(fmt.short(r.label, 60))}</td><td>${esc(r.type)}</td>
          <td>${esc(r.project_name)}</td><td class="num">${fmt.int(r.turns)}</td><td class="num">${fmt.compact(r.context_max)}</td>
          <td class="num">${fmt.usd(r.cost_usd)}</td><td class="num">${pct(r.share_pct)}</td>
          <td><a class="mono" href="#/sessions/${encodeURIComponent(r.session_id)}">${esc(r.session_id.slice(0, 8))}…</a></td></tr>`).join('')
          || '<tr><td colspan="8" class="muted">no subagent runs in this range</td></tr>'}</tbody>
      </table>
    </div>`;
  bindRange(root);
}
