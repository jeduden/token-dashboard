import { api, fmt } from '/web/app.js';

export default async function (root) {
  const [tips, fd] = await Promise.all([api('/api/tips'), api('/api/findings?days=7')]);
  const finding = f => `<div class="finding ${fmt.htmlSafe(f.kind)}"><span class="fig">${fmt.htmlSafe(f.fig)}</span>
    <span><b>${fmt.htmlSafe(f.subject)}</b> ${fmt.htmlSafe(f.text)} ${f.link ? `<a href="${fmt.htmlSafe(f.link)}">details →</a>` : ''}</span></div>`;
  root.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <h2>Findings <span class="muted" style="font-size:12px;font-weight:400">· last ${fd.window_days} days · shares are of estimated cost</span></h2>
      ${fd.findings.length ? fd.findings.map(finding).join('') : '<p class="muted">Not enough activity in this window.</p>'}
      ${fd.recommendations.map(r => `<div class="callout">${fmt.htmlSafe(r.text)} ${r.link ? `<a href="${fmt.htmlSafe(r.link)}">→</a>` : ''}</div>`).join('')}
      <p class="muted" style="font-size:11px;margin:8px 0 0">Written by fixed rules over your local data, not by a model. Subagent and automated-message costs follow the attribution described on the Prompts tab.</p>
    </div>
    <div class="card">
      <h2>Suggestions</h2>
      ${tips.length === 0
        ? '<p class="muted">No suggestions right now. Token Dashboard surfaces patterns weekly — check back after more activity.</p>'
        : `<p class="muted" style="margin:-8px 0 14px">Rule-based pattern detection over the last 7 days. Dismissed tips re-appear after 14 days.</p>`}
      ${tips.map(t => `
        <div class="tip">
          <div class="tip-head">
            <span class="badge">${fmt.htmlSafe(t.category)}</span>
            <strong>${fmt.htmlSafe(t.title)}</strong>
            <span class="spacer"></span>
            <button class="ghost" data-key="${fmt.htmlSafe(t.key)}">dismiss</button>
          </div>
          <p class="tip-body">${fmt.htmlSafe(t.body)}</p>
        </div>`).join('')}
    </div>`;
  root.querySelectorAll('button[data-key]').forEach(b => {
    b.addEventListener('click', async () => {
      await fetch('/api/tips/dismiss', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: b.dataset.key }),
      });
      location.reload();
    });
  });
}
