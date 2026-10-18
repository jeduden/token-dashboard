import { api, fmt } from '/web/app.js';
import { barChart } from '/web/charts.js';

const RANGES = [
  { key: '7d',  label: '7d',  days: 7 },
  { key: '30d', label: '30d', days: 30 },
  { key: '90d', label: '90d', days: 90 },
  { key: 'all', label: 'All', days: null },
];
const SEV_COLOR = { high: '#E5484D', warn: '#F5A623', info: '#4A9EFF' };

function readRange() {
  const q = (location.hash.split('?')[1] || '');
  const m = /(?:^|&)range=([^&]+)/.exec(q);
  const k = m && decodeURIComponent(m[1]);
  return RANGES.find(r => r.key === k) || RANGES[1];
}
// `calls=<tool>` filters the "most expensive calls" table; `tool=<tool>` is
// the drill-down (see export default), so old/hand-typed links still land there.
function readTool() {
  const m = /(?:^|&)calls=([^&]+)/.exec(location.hash.split('?')[1] || '');
  return m ? decodeURIComponent(m[1]) : '';
}
function navigate(range, tool) {
  location.hash = '#/costs?range=' + encodeURIComponent(range) + (tool ? '&calls=' + encodeURIComponent(tool) : '');
}

const sevBadge = sev => `<span class="badge" style="color:${SEV_COLOR[sev] || '#888'};border-color:${SEV_COLOR[sev] || '#888'}">${fmt.htmlSafe(sev)}</span>`;

function evidenceText(e) {
  const ex = (e && e.examples && e.examples[0]) || null;
  if (ex) return `${ex.session_id ? 'session ' + ex.session_id.slice(0, 8) : ''} ${ex.target || ''}`.trim();
  if (e && e.tool) return 'tool ' + e.tool;
  if (e && e.skill) return 'skill ' + e.skill;
  return '';
}

function diagPanel(title, diags) {
  return `<div class="card" style="margin-top:16px"><h3>${title}</h3>
    ${diags.length ? `<table><tbody>${diags.map(d => `
      <tr><td style="width:70px">${sevBadge(d.severity)}</td>
          <td>${fmt.htmlSafe(d.text)}<div class="muted mono" style="font-size:11px">${fmt.htmlSafe(fmt.short(evidenceText(d.evidence), 140))}</div></td></tr>`).join('')}
    </tbody></table>` : '<p class="muted">Nothing suspicious found.</p>'}</div>`;
}

export default async function (root) {
  // Skill drill-down first, so 'skill' is never treated as a tool name below.
  const sm = /^#\/costs\/skill\/([^?]+)/.exec(location.hash);
  if (sm) return (await import('/web/routes/costs-skill.js')).default(root, decodeURIComponent(sm[1]));
  // Likewise 'call' must never be read as a tool name.
  const cm = /^#\/costs\/call\/(\d+)/.exec(location.hash);
  if (cm) return (await import('/web/routes/costs-call.js')).default(root, cm[1]);
  const dm = /^#\/costs\/([^?]+)/.exec(location.hash);
  if (dm) return (await import('/web/routes/costs-drill.js')).default(root, decodeURIComponent(dm[1]));
  const legacy = /(?:^|&)tool=([^&]+)/.exec(location.hash.split('?')[1] || '');
  if (legacy) {
    location.replace('#/costs/' + legacy[1] + '?range=' + encodeURIComponent(readRange().key));
    return;
  }
  const range = readRange();
  const tool = readTool();
  const since = range.days ? new Date(Date.now() - range.days * 86400e3).toISOString() : null;
  const q = since ? '&since=' + encodeURIComponent(since) : '';
  const [tc, sc, calls] = await Promise.all([
    api('/api/tool-costs?limit=30' + q),
    api('/api/skill-costs?limit=30' + q),
    api('/api/tool-costs/calls?limit=25' + (tool ? '&tool=' + encodeURIComponent(tool) : '') + q),
  ]);
  const meta = tc.meta || {};
  const unattr = meta.unattributed_calls || 0;

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px">Costs</h2>
      <span class="muted" style="font-size:12px">estimated: result tokens written to cache once, then re-read every later turn</span>
      <div class="spacer"></div>
      <div class="range-tabs">${RANGES.map(r => `<button data-range="${r.key}" class="${r.key === range.key ? 'active' : ''}">${r.label}</button>`).join('')}</div>
    </div>
    ${unattr ? `<div class="card" style="margin-bottom:16px"><span class="muted">${fmt.int(unattr)} of ${fmt.int(meta.total_calls)} calls are not yet attributed (rescan in progress or older data).</span></div>` : ''}
    <div class="card"><h3>Tools by estimated cost</h3><div id="ch-tools" style="height:280px"></div>
      <table><thead><tr><th>tool</th><th class="num">calls</th><th class="num">avg tok</th><th class="num">p95 tok</th><th class="num">max tok</th>
        <th class="num">est. cost</th><th class="num">carry</th><th class="num">errors</th><th>top targets</th></tr></thead><tbody>
      ${tc.tools.map(t => `<tr data-drill="${fmt.htmlSafe(t.tool_name)}" style="cursor:pointer">
        <td><a href="#/costs/${encodeURIComponent(t.tool_name)}?range=${range.key}" title="drill into ${fmt.htmlSafe(t.tool_name)} parameters"><span class="badge">${fmt.htmlSafe(t.tool_name)}</span></a>
          <a href="#" data-tool="${fmt.htmlSafe(t.tool_name)}" class="muted" style="font-size:11px">top calls</a></td>
        <td class="num">${fmt.int(t.calls)}</td><td class="num">${fmt.int(Math.round(t.avg_result_tokens))}</td>
        <td class="num">${fmt.int(t.p95_result_tokens)}</td><td class="num">${fmt.int(t.max_result_tokens)}</td>
        <td class="num">${fmt.usd(t.est_cost_usd)}</td><td class="num">${fmt.usd(t.carry_cost_usd)}</td>
        <td class="num">${fmt.pct(t.error_rate)}</td>
        <td class="mono" style="font-size:11px">${t.top_targets.slice(0, 3).map(x => fmt.htmlSafe(fmt.short(x.target, 60))).join('<br>')}</td></tr>`).join('')
        || '<tr><td colspan="9" class="muted">no data</td></tr>'}
      </tbody></table></div>
    <div class="card" style="margin-top:16px"><h3>Skills by estimated cost</h3><div id="ch-skills" style="height:240px"></div>
      <table><thead><tr><th>skill</th><th class="num">invocations</th><th class="num">sessions</th><th class="num">load tok</th>
        <th class="num">est. cost</th><th class="num">carry</th><th class="num">per call</th><th>last used</th></tr></thead><tbody>
      ${sc.skills.map(s => `<tr data-skill-drill="${fmt.htmlSafe(s.skill)}" style="cursor:pointer">
        <td><a class="skill-link" href="#/costs/skill/${encodeURIComponent(s.skill)}?range=${range.key}" title="drill into ${fmt.htmlSafe(s.skill)}"><span class="badge">${fmt.htmlSafe(s.skill)}</span></a></td>
        <td class="num">${fmt.int(s.invocations)}</td><td class="num">${fmt.int(s.sessions)}</td>
        <td class="num">${fmt.int(s.est_load_tokens)}${s.estimated ? '~' : ''}</td>
        <td class="num">${fmt.usd(s.est_cost_usd)}</td><td class="num">${fmt.usd(s.carry_cost_usd)}</td>
        <td class="num">${fmt.usd(s.cost_per_invocation)}</td><td class="mono">${fmt.ts(s.last_used)}</td></tr>`).join('')
        || '<tr><td colspan="8" class="muted">no skills invoked</td></tr>'}
      </tbody></table></div>
    <div class="card" style="margin-top:16px"><h3>Most expensive calls${tool ? ' — ' + fmt.htmlSafe(tool) + ' <a href="#" id="clear-tool">(clear)</a>' : ''}</h3>
      <table><thead><tr><th>tool</th><th>target</th><th class="num">result tok</th><th class="num">est. cost</th><th>when</th><th>flags</th></tr></thead><tbody>
      ${calls.calls.map(c => `<tr${c.call_id != null ? ` data-call-drill="${Number(c.call_id)}" style="cursor:pointer"` : ''}><td><span class="badge">${fmt.htmlSafe(c.tool_name)}</span></td>
        <td class="mono" style="font-size:11px">${c.call_id != null
          ? `<a href="#/costs/call/${Number(c.call_id)}?range=${range.key}" title="drill into this call">${fmt.htmlSafe(fmt.short(c.target || '(no target)', 90))}</a>`
          : fmt.htmlSafe(fmt.short(c.target, 90))}</td>
        <td class="num">${c.result_tokens == null ? '—' : fmt.int(c.result_tokens)}</td><td class="num">${fmt.usd4(c.est_cost_usd)}</td>
        <td class="mono">${fmt.ts(c.timestamp)}<div class="muted">${fmt.htmlSafe((c.session_id || '').slice(0, 8))}</div></td>
        <td>${c.diagnostics.map(d => sevBadge(d.severity) + ' ' + fmt.htmlSafe(d.rule)).join('<br>')}</td></tr>`).join('')
        || '<tr><td colspan="6" class="muted">no data</td></tr>'}
      </tbody></table></div>
    ${diagPanel('Diagnostics: tools', tc.diagnostics)}
    ${diagPanel('Diagnostics: skills', sc.diagnostics)}
  `;

  root.querySelectorAll('.range-tabs button').forEach(b => b.addEventListener('click', () => navigate(b.dataset.range, tool)));
  root.querySelectorAll('a[data-tool]').forEach(a => a.addEventListener('click', e => { e.preventDefault(); navigate(range.key, a.dataset.tool); }));
  // Whole tool row opens the drill-down; the badge and "top calls" links keep their own targets.
  root.querySelectorAll('tr[data-drill]').forEach(tr => tr.addEventListener('click', e => {
    if (e.target.closest('a')) return;
    location.hash = '#/costs/' + encodeURIComponent(tr.dataset.drill) + '?range=' + range.key;
  }));
  // Whole call row opens the single-call drill-down; the target link is the keyboard-focusable target.
  root.querySelectorAll('tr[data-call-drill]').forEach(tr => tr.addEventListener('click', e => {
    if (e.target.closest('a')) return;
    location.hash = '#/costs/call/' + tr.dataset.callDrill + '?range=' + range.key;
  }));
  // Whole skill row opens the skill drill-down; the link inside is the keyboard-focusable target.
  root.querySelectorAll('tr[data-skill-drill]').forEach(tr => tr.addEventListener('click', e => {
    if (e.target.closest('a')) return;
    location.hash = '#/costs/skill/' + encodeURIComponent(tr.dataset.skillDrill) + '?range=' + range.key;
  }));
  const clr = root.querySelector('#clear-tool');
  if (clr) clr.addEventListener('click', e => { e.preventDefault(); navigate(range.key, ''); });

  const chart = (id, rows, label) => {
    const top = rows.slice(0, 12);
    barChart(document.getElementById(id), {
      categories: top.map(r => fmt.short(label(r), 26)), values: top.map(r => r.est_cost_usd), color: '#E5A23B',
    });
  };
  chart('ch-tools', tc.tools, r => r.tool_name);
  chart('ch-skills', sc.skills, r => r.skill);
}
