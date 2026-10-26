import { fmt } from '/web/app.js';
import { altLine } from '/web/whatif.js';

const esc = fmt.htmlSafe;
const hms = t => (t || '').slice(11, 19);

function duration(a, b) {
  const s = Math.max(0, (Date.parse(b) - Date.parse(a)) / 1000);
  return isNaN(s) ? '' : s >= 3600 ? (s / 3600).toFixed(1) + ' h' : s >= 60 ? Math.round(s / 60) + ' min' : Math.round(s) + ' s';
}

export function agentsHtml(agents) {
  if (!agents.length) return '';
  const total = agents.reduce((n, a) => n + a.cost_usd, 0), max = Math.max(...agents.map(a => a.cost_usd), 1e-12);
  return `<div class="card" style="margin-top:16px">
    <h3 style="margin-top:0">Subagents <span class="muted" style="font-size:12px;font-weight:400">· ${agents.length} · ${fmt.usd(total)} of this session's cost</span></h3>
    <p class="muted" style="font-size:12px;margin:0 0 6px">Click a row for the task it was given. "guessed" means the spawning call wasn't recorded, so the name comes from the agent id or its first prompt (restart the dashboard and let it rescan to fill these in).</p>
    <table id="agents">
      <thead><tr><th>agent</th><th>asked to</th><th>started</th><th class="num">ran</th><th class="num">turns</th><th>top tools</th><th class="num">cost</th><th style="min-width:120px"></th></tr></thead>
      <tbody>${agents.map((a, i) => `
        <tr data-ai="${i}" style="cursor:pointer" title="click to expand">
          <td><span class="chev">▸</span> <strong>${esc(a.label)}</strong>${a.subagent_type && a.subagent_type !== a.label ? ` <span class="badge">${esc(a.subagent_type)}</span>` : ''}${a.linked ? '' : ' <span class="muted" style="font-size:11px">guessed</span>'}</td>
          <td class="blur-sensitive" style="max-width:340px;overflow-wrap:anywhere">${esc(fmt.short(a.description || a.prompt_head || '', 90))}</td>
          <td class="mono">${hms(a.first)}</td>
          <td class="num">${duration(a.first, a.last)}</td>
          <td class="num">${fmt.int(a.turns)}</td>
          <td class="muted" style="font-size:12px">${a.tools.map(([n, c]) => esc(n) + ' ×' + c).join(', ')}</td>
          <td class="num">${fmt.usd(a.cost_usd)}${altLine(a)}</td>
          <td style="min-width:120px">${a.cost_usd ? `<div title="${fmt.usd4(a.cost_usd)}" style="height:8px;border-radius:2px;background:#7C5CFF;width:${Math.max(2, 100 * a.cost_usd / max).toFixed(1)}%"></div>` : ''}</td>
        </tr>`).join('')}
      </tbody>
    </table></div>`;
}

export function mountAgents(root, agents) {
  const tbl = root.querySelector('#agents');
  if (!tbl) return;
  tbl.addEventListener('click', e => {
    const tr = e.target.closest('tr[data-ai]');
    if (!tr || String(window.getSelection()).length) return;
    const open = tr.nextElementSibling && tr.nextElementSibling.classList.contains('detail');
    if (open) { tr.nextElementSibling.remove(); tr.querySelector('.chev').textContent = '▸'; return; }
    const a = agents[Number(tr.dataset.ai)];
    const d = document.createElement('tr');
    d.className = 'detail';
    d.innerHTML = `<td colspan="8">
      <div class="muted" style="font-size:11px">agent id ${esc(a.agent_id)} · ${a.models.map(m => esc(fmt.modelShort(m))).join(', ')} · peak context ${fmt.compact(a.context_max)} · ${fmt.ts(a.first)} → ${hms(a.last)}</div>
      ${a.description ? `<div style="margin:6px 0"><strong>${esc(a.description)}</strong></div>` : ''}
      <pre class="blur-sensitive" style="margin:4px 0;white-space:pre-wrap;word-break:break-word">${esc(a.prompt_head || '(no prompt recorded)')}${(a.prompt_head || '').length >= 300 ? '…' : ''}</pre>
      <div class="muted" style="font-size:11px">Only the first 300 characters of the task are kept.</div></td>`;
    tr.after(d);
    tr.querySelector('.chev').textContent = '▾';
  });
}

// agent_id -> label, for the "side" badges in the turn table
export const agentLabels = agents => Object.fromEntries(agents.map(a => [a.agent_id, a.label]));
