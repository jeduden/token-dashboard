import { api, fmt } from '/web/app.js';
import { timelineHtml, mountTimeline } from '/web/routes/session-charts.js';

export default async function (root) {
  const id = decodeURIComponent((/^#?\/sessions\/([^/?]+)/.exec(location.hash) || [])[1] || '');
  if (!id) return renderList(root);
  return renderSession(root, id);
}

async function renderList(root) {
  const list = await api('/api/sessions?limit=100');
  const maxCost = Math.max(0, ...list.map(s => s.cost_usd || 0));
  root.innerHTML = `
    <div class="card">
      <h2>Sessions</h2>
      <table>
        <thead><tr><th>started</th><th>project</th><th class="num">turns</th><th class="num">tokens</th><th class="num">cost</th><th style="width:120px"></th><th>session</th></tr></thead>
        <tbody>
          ${list.map(s => `
            <tr>
              <td class="mono">${fmt.ts(s.started)}</td>
              <td title="${fmt.htmlSafe(s.project_slug)}">${fmt.htmlSafe(s.project_name || s.project_slug)}</td>
              <td class="num">${fmt.int(s.turns)}</td>
              <td class="num">${fmt.int(s.tokens)}</td>
              <td class="num">${fmt.usd(s.cost_usd)}</td>
              <td>${costBar(s.cost_usd, maxCost)}</td>
              <td><a href="#/sessions/${encodeURIComponent(s.session_id)}" class="mono">${fmt.htmlSafe(s.session_id.slice(0,8))}…</a></td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}

async function renderSession(root, id) {
  const [turns, tl] = await Promise.all([
    api('/api/sessions/' + encodeURIComponent(id)),
    // The charts are an extra: if this endpoint is missing (e.g. a dashboard started before it existed), still show the turns.
    api('/api/session-timeline?id=' + encodeURIComponent(id)).catch(() => null),
  ]);
  let totalIn = 0, totalOut = 0, totalCacheRd = 0, totalCost = 0;
  let modelCounts = {};
  for (const t of turns) {
    if (t.type !== 'assistant') continue;
    totalIn += t.input_tokens || 0;
    totalOut += t.output_tokens || 0;
    totalCacheRd += t.cache_read_tokens || 0;
    totalCost += t.cost_usd || 0;
    const m = t.model || 'unknown';
    modelCounts[m] = (modelCounts[m] || 0) + 1;
  }
  const slug = (turns[0] && turns[0].project_slug) || '';
  const cwd = (turns.find(t => t.cwd) || {}).cwd || '';
  const base = cwd ? cwd.replace(/\\/g, '/').replace(/\/+$/, '').split('/').pop() : '';
  const project = base || slug;
  const started = (turns[0] && turns[0].timestamp) || '';
  const ended = (turns[turns.length-1] && turns[turns.length-1].timestamp) || '';

  root.innerHTML = `
    <div class="card">
      <h2 style="display:flex;align-items:center">
        <span>Session ${fmt.htmlSafe(id.slice(0,8))}…</span>
        <span class="spacer"></span>
        <a href="#/recent?range=all&bucket=day&sessions=${encodeURIComponent(id)}" class="muted">activity over time</a>
        <span class="muted" style="margin:0 8px">·</span>
        <a href="#/sessions" class="muted">← all sessions</a>
      </h2>
      <div class="flex muted" style="font-family:var(--mono);font-size:12px;flex-wrap:wrap;gap:14px">
        <span>${fmt.htmlSafe(project)}</span>
        <span>${fmt.ts(started)} → ${fmt.ts(ended)}</span>
        <span>${turns.length} records</span>
        <span>${fmt.int(totalIn)} in · ${fmt.int(totalOut)} out · ${fmt.int(totalCacheRd)} cache rd · ${fmt.usd(totalCost)} est.</span>
      </div>
    </div>

    <div style="margin-top:16px">${tl ? timelineHtml(tl) : '<div class="card"><p class="muted">Cost charts unavailable (/api/session-timeline failed). Restart the dashboard to pick up the latest server code.</p></div>'}</div>

    <div class="card" style="margin-top:16px">
      <h3 style="display:flex;align-items:center"><span>Turn-by-turn</span><span class="spacer"></span>
        <label class="muted" style="font-size:12px;font-weight:400"><input type="checkbox" id="show-empty"> show empty rows</label></h3>
      <table>
        <thead><tr><th>time</th><th>type</th><th>model</th><th class="blur-sensitive">prompt / tools</th><th class="num">in</th><th class="num">out</th><th class="num">cache rd</th><th class="num">cache wr</th><th class="num">cost</th><th style="width:140px"></th></tr></thead>
        <tbody>
          ${turnRows(turns, Math.max(0, ...turns.map(t => t.cost_usd || 0)))}
        </tbody>
      </table>
    </div>`;
  if (tl) mountTimeline(root, tl);

  // Empty rows are folded into runs; a fold row opens its own run, the checkbox opens all of them.
  const setRun = (fold, open) => {
    fold.dataset.open = open ? '1' : '';
    fold.querySelector('.chev').textContent = open ? '▾' : '▸';
    root.querySelectorAll(`tr[data-run="${fold.dataset.fold}"]`).forEach(r => { r.hidden = !open; });
  };
  const folds = Array.from(root.querySelectorAll('tr.fold'));
  folds.forEach(f => f.addEventListener('click', () => setRun(f, !f.dataset.open)));
  root.querySelector('#show-empty').addEventListener('change', e => folds.forEach(f => setRun(f, e.target.checked)));
}

const isEmpty = (t, summary) => !summary && !t.model && !(t.input_tokens || t.output_tokens || t.cache_read_tokens
  || t.cache_create_5m_tokens || t.cache_create_1h_tokens);

// Bar length is linear in cost, relative to the most expensive row on the page.
const costBar = (usd, max) => !usd || !max ? '' :
  `<div class="costbar" title="${fmt.usd4(usd)}" style="height:8px;border-radius:2px;background:#4A9EFF;width:${Math.max(2, 100 * usd / max).toFixed(1)}%"></div>`;

function rowHtml(t, summary, run, max) {
  return `<tr${run == null ? '' : ` data-run="${run}" hidden`}>
    <td class="mono">${(t.timestamp || '').slice(11,19)}</td>
    <td>${t.type}${t.is_sidechain ? ' <span class="badge">side</span>' : ''}</td>
    <td>${t.model ? `<span class="badge ${fmt.modelClass(t.model)}">${fmt.htmlSafe(fmt.modelShort(t.model))}</span>` : ''}</td>
    <td class="blur-sensitive">${fmt.htmlSafe(summary)}</td>
    <td class="num">${fmt.int(t.input_tokens)}</td>
    <td class="num">${fmt.int(t.output_tokens)}</td>
    <td class="num">${fmt.int(t.cache_read_tokens)}</td>
    <td class="num">${fmt.int((t.cache_create_5m_tokens || 0) + (t.cache_create_1h_tokens || 0))}</td>
    <td class="num">${t.cost_usd == null ? '' : fmt.usd4(t.cost_usd)}</td>
    <td>${costBar(t.cost_usd, max)}</td>
  </tr>`;
}

// Rows with nothing beyond type + time collapse into one summary row per run of 2 or more.
function turnRows(turns, max) {
  const items = turns.map(t => {
    const tools = t.tool_calls_json ? JSON.parse(t.tool_calls_json) : [];
    const summary = t.prompt_text ? fmt.short(t.prompt_text, 110) : tools.map(x => x.name).join(' · ');
    return { t, summary, empty: isEmpty(t, summary) };
  });
  let out = '', i = 0, run = 0;
  while (i < items.length) {
    let j = i;
    while (j < items.length && items[j].empty === items[i].empty) j++;
    if (items[i].empty && j - i >= 2) {
      const group = items.slice(i, j), counts = {};
      group.forEach(x => { counts[x.t.type] = (counts[x.t.type] || 0) + 1; });
      const types = Object.entries(counts).map(([k, n]) => `${k} ×${n}`).join(', ');
      const span = (group[0].t.timestamp || '').slice(11, 19) + '–' + (group[group.length - 1].t.timestamp || '').slice(11, 19);
      out += `<tr class="fold" data-fold="${run}" style="cursor:pointer"><td class="mono muted">${span}</td>
        <td colspan="9" class="muted"><span class="chev">▸</span> ${group.length} records with no content · ${fmt.htmlSafe(types)}</td></tr>`;
      out += group.map(x => rowHtml(x.t, x.summary, run, max)).join('');
      run++;
    } else {
      out += items.slice(i, j).map(x => rowHtml(x.t, x.summary, null, max)).join('');
    }
    i = j;
  }
  return out;
}
