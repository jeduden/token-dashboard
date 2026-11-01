import { api, fmt } from '/web/app.js';

const SORTS = [
  { key: 'cost', label: 'Most cost' },
  { key: 'tokens', label: 'Most tokens' },
  { key: 'recent', label: 'Most recent' },
];
const KINDS = [
  { key: 'all', label: 'All' },
  { key: 'human', label: 'You' },
  { key: 'automated', label: 'Agents & tasks' },
];
const KIND_LABEL = { idle_notification: 'idle notice', teammate_message: 'teammate', task_notification: 'task' };

function params() {
  const q = new URLSearchParams(location.hash.split('?')[1] || '');
  return {
    sort: SORTS.find(s => s.key === q.get('sort')) || SORTS[0],
    kind: KINDS.find(k => k.key === q.get('kind')) || KINDS[0],
  };
}

function go(p) {
  const base = (location.hash.replace(/^#/, '').split('?')[0]) || '/prompts';
  location.hash = '#' + base + '?sort=' + encodeURIComponent(p.sort.key) + '&kind=' + encodeURIComponent(p.kind.key);
}

export default async function (root) {
  const p = params();
  const rows = await api('/api/prompts?limit=100&sort=' + encodeURIComponent(p.sort.key)
    + (p.kind.key === 'all' ? '' : '&kind=' + p.kind.key));
  const tabs = (name, items, cur) => `<div class="range-tabs" role="tablist">${items.map(i =>
    `<button data-${name}="${i.key}" class="${i.key === cur.key ? 'active' : ''}">${i.label}</button>`).join('')}</div>`;

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Prompts</h2>
      <div class="spacer"></div>
      ${tabs('kind', KINDS, p.kind)}${tabs('sort', SORTS, p.sort)}
    </div>

    <div class="card">
      <p class="muted" style="margin:0 0 14px">Each prompt with everything that ran because of it: its own turns and the subagents it started.
        Agents' and tasks' messages into the session (idle notices, teammate messages, task results) also start turns, so they are listed too and marked.
        Subagents that keep running after you send the next prompt are counted under the later prompt. Click a row to see the full prompt.</p>
      <table id="prompts">
        <thead><tr>
          <th>when</th><th>prompt</th><th>model</th>
          <th class="num">API calls</th><th class="num">subagents</th><th class="num">tokens</th>
          <th class="num">cost main</th><th class="num">cost sub</th><th class="num">cost</th><th>session</th>
        </tr></thead>
        <tbody>
          ${rows.map((r,i) => `
            <tr data-i="${i}" style="cursor:pointer">
              <td class="mono">${fmt.ts(r.timestamp)}</td>
              <td class="blur-sensitive">${r.kind ? `<span class="badge auto">${KIND_LABEL[r.kind]}</span> ` : ''}${fmt.htmlSafe(fmt.short(r.prompt_text.replace(/\s+/g, ' '), 110))}</td>
              <td><span class="badge ${fmt.modelClass(r.model)}">${fmt.htmlSafe(fmt.modelShort(r.model))}</span></td>
              <td class="num">${fmt.int(r.api_calls)}</td>
              <td class="num">${r.subagent_calls ? fmt.int(r.subagent_calls) : '—'}</td>
              <td class="num">${fmt.compact(r.billable_tokens)}</td>
              <td class="num">${fmt.usd(r.cost_main_usd)}</td>
              <td class="num">${r.cost_sub_usd ? fmt.usd(r.cost_sub_usd) : '—'}</td>
              <td class="num">${fmt.usd(r.cost_usd)}</td>
              <td><a href="#/sessions/${encodeURIComponent(r.session_id)}" class="mono" onclick="event.stopPropagation()">${fmt.htmlSafe(r.session_id.slice(0,8))}…</a></td>
            </tr>`).join('') || '<tr><td colspan="10" class="muted">no prompts yet</td></tr>'}
        </tbody>
      </table>
    </div>
    <div id="drawer"></div>
  `;

  root.querySelectorAll('[data-sort]').forEach(b => b.addEventListener('click', () =>
    go({ ...p, sort: SORTS.find(s => s.key === b.dataset.sort) })));
  root.querySelectorAll('[data-kind]').forEach(b => b.addEventListener('click', () =>
    go({ ...p, kind: KINDS.find(k => k.key === b.dataset.kind) })));

  root.querySelectorAll('#prompts tbody tr').forEach(tr => {
    tr.addEventListener('click', () => {
      const r = rows[Number(tr.dataset.i)];
      const drawer = document.getElementById('drawer');
      drawer.innerHTML = `
        <div class="card">
          <h3 style="display:flex;align-items:center">
            <span>Prompt detail</span>
            <span class="spacer"></span>
            <span class="badge ${fmt.modelClass(r.model)}">${fmt.htmlSafe(fmt.modelShort(r.model))}</span>
          </h3>
          <pre class="blur-sensitive">${fmt.htmlSafe(r.prompt_text || '')}</pre>
          <div class="flex" style="margin-top:12px;flex-wrap:wrap;gap:14px">
            <span class="muted">${fmt.ts(r.timestamp)}</span>
            <span class="muted">${fmt.int(r.billable_tokens)} billable (${fmt.int(r.tokens_main)} main, ${fmt.int(r.tokens_sub)} sub) · ${fmt.int(r.cache_read_tokens)} cache rd · ${fmt.usd4(r.cost_usd)} est.</span>
            <span class="spacer"></span>
            <a href="#/sessions/${encodeURIComponent(r.session_id)}">Open session →</a>
          </div>
        </div>`;
      drawer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
  });
}
