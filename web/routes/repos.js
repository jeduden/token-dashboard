import { fmt } from '/web/app.js';

const CONF = {
  'git':            { label: 'git',       help: 'Resolved from the .git entry on disk.' },
  'path-heuristic': { label: 'inferred',  help: 'At least one worktree was matched by its folder name (the directory no longer exists).' },
  'slug-only':      { label: 'no repo',   help: 'Not a git checkout we could read; shown as its own project.' },
};

export function confBadge(c) {
  const m = CONF[c] || CONF['slug-only'];
  return `<span class="badge" title="${fmt.htmlSafe(m.help)}">${fmt.htmlSafe(m.label)}</span>`;
}

function worktreeRows(r, idx) {
  return r.worktrees.map(w => `
    <tr class="wt-row" data-parent="${idx}" hidden>
      <td style="padding-left:28px" title="${fmt.htmlSafe(w.cwd || w.project_slug)}">
        <span class="muted">↳</span> ${fmt.htmlSafe(w.project_name || w.project_slug)}
        <span class="muted mono" style="font-size:11px">${fmt.htmlSafe(w.cwd || '')}</span>
      </td>
      <td class="num">${fmt.int(w.sessions)}</td>
      <td class="num"></td>
      <td class="num">${fmt.int(w.billable_tokens)}</td>
      <td class="num">${fmt.pct(w.share)}</td>
    </tr>`).join('');
}

export function repoTable(rows) {
  if (!rows.length) return '<p class="muted">No data in this range.</p>';
  return `
    <table class="repo-table">
      <thead><tr><th>repo</th><th class="num">sessions</th><th class="num">turns</th><th class="num">billable tokens</th><th class="num">cache reads</th></tr></thead>
      <tbody>
        ${rows.map((r, i) => {
          const many = r.worktree_count > 1;
          return `
          <tr class="repo-row" data-idx="${i}" ${many ? 'tabindex="0" role="button" aria-expanded="false" style="cursor:pointer"' : ''}
              title="${fmt.htmlSafe(r.repo_root || r.repo)}">
            <td>${many ? '<span class="caret">▸</span> ' : '&nbsp;&nbsp;&nbsp;'}${fmt.htmlSafe(r.repo_name)}
              ${confBadge(r.repo_confidence)}
              <span class="badge" title="Worktrees / project folders rolled into this repo">${fmt.int(r.worktree_count)} ${r.worktree_count === 1 ? 'worktree' : 'worktrees'}</span>
            </td>
            <td class="num">${fmt.int(r.sessions)}</td>
            <td class="num">${fmt.int(r.turns)}</td>
            <td class="num">${fmt.int(r.billable_tokens)}</td>
            <td class="num">${fmt.int(r.cache_read_tokens)}</td>
          </tr>${many ? worktreeRows(r, i) : ''}`;
        }).join('')}
      </tbody>
    </table>`;
}

export function wireRepoTable(root) {
  const toggle = row => {
    const open = row.getAttribute('aria-expanded') !== 'true';
    row.setAttribute('aria-expanded', String(open));
    const c = row.querySelector('.caret'); if (c) c.textContent = open ? '▾' : '▸';
    root.querySelectorAll(`tr.wt-row[data-parent="${row.dataset.idx}"]`).forEach(t => { t.hidden = !open; });
  };
  root.querySelectorAll('tr.repo-row[tabindex]').forEach(row => {
    row.addEventListener('click', () => toggle(row));
    row.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(row); }
    });
  });
}
