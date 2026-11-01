import { api, fmt } from '/web/app.js';

export default async function (root) {
  const [tips, fd] = await Promise.all([api('/api/tips'), api('/api/findings?days=7')]);
  const esc = fmt.htmlSafe;
  const finding = f => `<div class="finding ${esc(f.kind)}"><span class="fig">${esc(f.fig)}</span>
    <span><b>${esc(f.subject)}</b> ${esc(f.text)} ${f.link ? `<a href="${esc(f.link)}">details →</a>` : ''}</span></div>`;
  const callouts = rs => rs.map(r => `<div class="callout">${esc(r.text)} ${r.link ? `<a href="${esc(r.link)}">→</a>` : ''}</div>`).join('');
  const written = fd.source === 'claude';
  const when = written ? new Date(fd.generated_at * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC' : '';
  const claudeCard = written ? `
    <div class="card" style="margin-bottom:16px">
      <h2>Findings <span class="muted" style="font-size:12px;font-weight:400">· written by Claude · last ${fd.window_days} days · ${when}${fd.age_days > 7 ? ' · <b>old, rerun /dashboard-findings</b>' : ''}</span></h2>
      ${fd.findings.map(finding).join('')}${callouts(fd.recommendations)}
      <p class="muted" style="font-size:11px;margin:8px 0 0">Written by Claude Code from your local numbers when you ran <code>/dashboard-findings</code>; the dashboard never calls a model. Figures are estimates and a model can misread them: check them against the tabs they link to.</p>
    </div>` : `
    <div class="card" style="margin-bottom:16px">
      <h2>Findings from Claude</h2>
      <p class="muted" style="margin:0">None yet. In Claude Code, in this project, run <code>/dashboard-findings</code>: Claude reads your numbers (locally), writes findings and recommendations like the ones below but in its own words, and saves them here.</p>
    </div>`;
  const r = fd.rules;
  root.innerHTML = claudeCard + `
    <div class="card" style="margin-bottom:16px">
      <h2>Rule-based checks <span class="muted" style="font-size:12px;font-weight:400">· last ${r.window_days} days · shares are of estimated cost</span></h2>
      ${r.findings.length ? r.findings.map(finding).join('') : '<p class="muted">Not enough activity in this window.</p>'}
      ${callouts(r.recommendations)}
      <p class="muted" style="font-size:11px;margin:8px 0 0">Fixed rules with fixed thresholds over your local data. Subagent and automated-message costs follow the attribution described on the Prompts tab.</p>
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
