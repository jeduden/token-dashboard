import { api, fmt } from '/web/app.js';

export default async function (root) {
  const [tips, fd] = await Promise.all([api('/api/tips'), api('/api/findings?days=7')]);
  const esc = fmt.htmlSafe;
  const finding = f => `<div class="finding ${esc(f.kind)}"><span class="fig">${esc(f.fig)}</span>
    <span><b>${esc(f.subject)}</b> ${esc(f.text)} ${f.link ? `<a href="${esc(f.link)}">details →</a>` : ''}</span></div>`;
  const callouts = rs => rs.map(r => `<div class="callout">${esc(r.text)} ${r.link ? `<a href="${esc(r.link)}">→</a>` : ''}</div>`).join('');
  const written = fd.source === 'claude';
  const when = written ? new Date(fd.generated_at * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC' : '';
  const meta = written && fd.meta ? ` · ${esc((fd.meta.model || '').replace('claude-', ''))}${fd.meta.cost_usd != null ? ' · $' + Number(fd.meta.cost_usd).toFixed(3) : ''}` : '';
  const claudeCard = `
    <div class="card" style="margin-bottom:16px" id="claude-card">
      <div class="flex">
        <h2 style="margin:0">Findings from Claude${written ? ` <span class="muted" style="font-size:12px;font-weight:400">· last ${fd.window_days} days · ${when}${meta}${fd.age_days > 7 ? ' · <b>old</b>' : ''}</span>` : ''}</h2>
        <div class="spacer"></div>
        <button class="primary" id="gen-open">${written ? 'Regenerate' : 'Generate with Claude'}</button>
      </div>
      <div id="gen-panel"></div>
      ${written ? `${fd.findings.map(finding).join('')}${callouts(fd.recommendations)}
      <p class="muted" style="font-size:11px;margin:8px 0 0">Written by Claude from your local numbers when you pressed Generate. Figures are estimates and a model can misread them: check them against the tabs they link to.</p>`
      : '<p class="muted" style="margin:8px 0 0">Not generated yet. Claude reads your numbers and writes findings and recommendations like the rule-based ones below, in its own words. Nothing is sent until you confirm.</p>'}
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
  bindGenerate(root, esc);
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

// The consent step: show what would be sent, send only after the explicit click.
function bindGenerate(root, esc) {
  const panel = root.querySelector('#gen-panel'), open = root.querySelector('#gen-open');
  const post = body => fetch('/api/findings/generate', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Token-Dashboard': '1' }, body: JSON.stringify(body) });

  async function poll() {
    panel.innerHTML = '<p class="muted">Asking Claude… this takes up to a minute or two.</p>';
    open.disabled = true;
    for (;;) {
      await new Promise(r => setTimeout(r, 2000));
      const st = await api('/api/findings/status');
      if (st.running) continue;
      if (st.error) { open.disabled = false; panel.innerHTML = `<p class="bad">Could not generate findings: ${esc(st.error)}</p>`; return; }
      return location.reload();
    }
  }

  async function consent(prompts) {
    const pv = await api('/api/findings/preview?days=7&prompts=' + (prompts ? 1 : 0));
    if (!pv.available) {
      panel.innerHTML = '<p class="bad">The <code>claude</code> command was not found, so findings cannot be generated here. Install Claude Code, or set <code>TOKEN_DASHBOARD_CLAUDE</code> to its path.</p>';
      return;
    }
    panel.innerHTML = `
      <div class="callout">
        <p style="margin:0 0 6px"><b>This sends about ${(pv.bytes / 1024).toFixed(0)} KB of your usage numbers to Claude.</b>
          Project names, agent labels, costs, counts and cache figures for the last 7 days go to Claude through the <code>claude</code> command on this machine, using your Claude login.
          It counts toward your usage (a few cents) and Claude Code keeps no session of it. The dashboard itself sends nothing.</p>
        <label style="display:block;margin:6px 0"><input type="checkbox" id="gen-prompts" ${prompts ? 'checked' : ''}>
          Also send the first 200 characters of my 10 most expensive prompts (can contain private text)</label>
        <details><summary>Show exactly what will be sent</summary><pre class="blur-sensitive" style="max-height:260px;overflow:auto">${esc(JSON.stringify(pv.facts, null, 1))}</pre></details>
        <div class="flex" style="margin-top:8px"><button class="primary" id="gen-go">Send and generate</button><button class="ghost" id="gen-cancel">Cancel</button></div>
      </div>`;
    panel.querySelector('#gen-prompts').addEventListener('change', e => consent(e.target.checked));
    panel.querySelector('#gen-cancel').addEventListener('click', () => { panel.innerHTML = ''; });
    panel.querySelector('#gen-go').addEventListener('click', async () => {
      const r = await post({ days: 7, include_prompts: panel.querySelector('#gen-prompts').checked, confirm: true });
      if (!r.ok) { panel.insertAdjacentHTML('beforeend', `<p class="bad">${esc((await r.json()).error || r.status)}</p>`); return; }
      poll();
    });
  }

  open.addEventListener('click', () => consent(false));
  api('/api/findings/status').then(st => { if (st.running) poll(); }).catch(() => {});
}
