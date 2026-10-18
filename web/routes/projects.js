import { api, fmt } from '/web/app.js';
import { repoTable, wireRepoTable } from '/web/routes/repos.js';

const RANGES = [
  { key: '7d',  label: '7d',  days: 7 },
  { key: '30d', label: '30d', days: 30 },
  { key: '90d', label: '90d', days: 90 },
  { key: 'all', label: 'All', days: null },
];
const VIEWS = [{ key: 'repo', label: 'By repo' }, { key: 'worktree', label: 'By worktree' }];

function readRange() {
  const m = /(?:^|&)range=([^&]+)/.exec(location.hash.split('?')[1] || '');
  const k = m && decodeURIComponent(m[1]);
  return RANGES.find(r => r.key === k) || RANGES[RANGES.length - 1];
}

function readView() {
  try { const v = localStorage.getItem('td.projects-view'); if (v === 'repo' || v === 'worktree') return v; }
  catch (e) { /* storage unavailable */ }
  return 'repo';
}

function saveView(v) {
  try { localStorage.setItem('td.projects-view', v); } catch (e) { /* ignore */ }
}

function worktreeTable(rows) {
  return `
    <table>
      <thead><tr><th>project</th><th class="num">sessions</th><th class="num">turns</th><th class="num">billable tokens</th><th class="num">cache reads</th></tr></thead>
      <tbody>
        ${rows.map(r => `
          <tr>
            <td title="${fmt.htmlSafe(r.project_slug)}">${fmt.htmlSafe(r.project_name || r.project_slug)}
              ${r.repo_name && r.repo_name !== r.project_name ? `<span class="muted" style="font-size:11px">in ${fmt.htmlSafe(r.repo_name)}</span>` : ''}</td>
            <td class="num">${fmt.int(r.sessions)}</td>
            <td class="num">${fmt.int(r.turns)}</td>
            <td class="num">${fmt.int(r.billable_tokens)}</td>
            <td class="num">${fmt.int(r.cache_read_tokens)}</td>
          </tr>`).join('')}
      </tbody>
    </table>`;
}

export default async function (root) {
  const range = readRange();
  const view = readView();
  const since = range.days ? '?since=' + encodeURIComponent(new Date(Date.now() - range.days * 86400000).toISOString()) : '';
  const rows = await api((view === 'repo' ? '/api/repos' : '/api/projects') + since);
  const seg = (items, cur, attr) => items.map(i =>
    `<button ${attr}="${i.key}" class="${i.key === cur ? 'active' : ''}" aria-pressed="${i.key === cur}">${i.label}</button>`).join('');
  root.innerHTML = `
    <div class="card">
      <div class="flex" style="margin-bottom:6px">
        <h2 style="margin:0">Projects</h2>
        <div class="spacer"></div>
        <div class="range-tabs" role="group" aria-label="View">${seg(VIEWS, view, 'data-view')}</div>
        <div class="range-tabs" role="group" aria-label="Date range">${seg(RANGES, range.key, 'data-range')}</div>
      </div>
      <p class="muted" style="margin:0 0 14px">${view === 'repo'
        ? 'Git worktrees of one repository are rolled into one row; click a row to see them. “inferred” means a deleted worktree was matched by its folder name.'
        : 'One row per working directory Claude Code was started in (each git worktree separately).'}
        Sorted by billable token spend. Cache reads are billed cheaper, so high cache-read columns are good.</p>
      ${view === 'repo' ? repoTable(rows) : worktreeTable(rows)}
    </div>`;
  if (view === 'repo') wireRepoTable(root);
  root.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => {
    saveView(b.dataset.view);
    window.dispatchEvent(new HashChangeEvent('hashchange'));  // re-render in place
  }));
  root.querySelectorAll('[data-range]').forEach(b => b.addEventListener('click', () => {
    location.hash = '#' + (location.hash.replace(/^#/, '').split('?')[0] || '/projects') + '?range=' + encodeURIComponent(b.dataset.range);
  }));
}
