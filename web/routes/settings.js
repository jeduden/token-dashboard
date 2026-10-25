import { api, state, $, fmt } from '/web/app.js';

const esc = fmt.htmlSafe;

// $ per million tokens: at least 2 decimals, up to 3 (0.125 stays 0.125)
const rate = x => '$' + (+x.toFixed(3)).toFixed(Math.max(2, (String(+x.toFixed(3)).split('.')[1] || '').length));

export default async function (root) {
  const [cur, tg] = await Promise.all([api('/api/plan'), api('/api/pricing/targets')]);
  const plans = Object.entries(cur.pricing.plans);
  root.innerHTML = `
    <div class="card">
      <h2>Settings</h2>
      <h3 style="margin-top:16px">Plan</h3>
      <p class="muted" style="margin:0 0 12px">Sets how cost is displayed. API mode shows pay-per-token rates. Subscription modes show what you actually pay each month.</p>
      <div class="flex">
        <select id="plan">
          ${plans.map(([k,v]) => `<option value="${k}" ${k===cur.plan?'selected':''}>${v.label}${v.monthly?` — $${v.monthly}/mo`:''}</option>`).join('')}
        </select>
        <button class="primary" id="save">Save</button>
        <span id="msg" class="muted"></span>
      </div>

      <hr class="divider">

      <h3>Equivalent models</h3>
      <p class="muted" style="margin:0 0 12px">Each Claude class next to the model other vendors sell at a comparable level; these are what the what-if costs will use. Prices are $ per million tokens: input / output (cached input in brackets). <span class="muted">≈</span> marks figures I could not confirm on the vendor's own page.</p>
      <table>
        <thead><tr><th>class</th>${tg.vendors.map(v => `<th>${esc(v.label)}</th>`).join('')}</tr></thead>
        <tbody>
          ${tg.classes.map(c => `<tr><td><span class="badge ${esc(c.id)}">${esc(c.label)}</span></td>
            ${tg.vendors.map(v => { const m = c.members[v.id]; return m ? `<td title="${esc(m.note || '')}">
              <div class="mono">${esc(m.model)}${m.verified === false ? ' ≈' : ''}</div>
              <div class="muted num" style="text-align:left">${rate(m.input)} / ${rate(m.output)} <span>(${rate(m.cache_read)})</span></div></td>` : '<td class="muted">none</td>'; }).join('')}
          </tr>`).join('')}
        </tbody>
      </table>
      <p class="muted" style="margin:8px 0 16px;font-size:11px">${Object.entries(tg.sources).map(([k, s]) => `${esc(k)}: ${esc(s.url)} (checked ${esc(s.as_of)}${s.stale ? ', <b>stale</b>' : ''}${s.verified === false ? ', partly unverified' : ''})`).join(' · ')}</p>

      <h3>Pricing table</h3>
      <p class="muted" style="margin:0 0 12px">Rates come from <code>pricing.json</code>. Put your own in <code>~/.claude/token-dashboard.pricing.json</code> (or the file named by <code>TOKEN_DASHBOARD_PRICING</code>); its entries replace the same-named ones. Restart the dashboard after editing.</p>
      <table>
        <thead><tr><th>model</th><th>vendor</th><th class="num">input</th><th class="num">output</th><th class="num">cache read</th><th class="num">cache 5m</th><th class="num">cache 1h</th><th class="num">fast in / out</th></tr></thead>
        <tbody>
          ${Object.entries(cur.pricing.models).map(([k,v]) => `
            <tr><td><span class="badge ${v.class || v.tier || ''}">${esc(k)}</span></td>
              <td class="muted">${esc(v.vendor || '')}</td>
              <td class="num">${rate(v.input)}</td>
              <td class="num">${rate(v.output)}</td>
              <td class="num">${rate(v.cache_read)}</td>
              <td class="num">${rate(v.cache_create_5m)}</td>
              <td class="num">${rate(v.cache_create_1h)}</td>
              <td class="num">${v.fast ? `${rate(v.fast.input)} / ${rate(v.fast.output)}` : ''}</td>
            </tr>`).join('')}
        </tbody>
      </table>
      <p class="muted" style="margin-top:8px;font-size:11px">Rates per 1M tokens, USD.</p>

      <hr class="divider">

      <h3>Privacy</h3>
      <p class="muted">Press <code>Cmd/Ctrl + B</code> anywhere to blur prompt text and other sensitive content for screenshots.</p>
    </div>`;

  $('#save').addEventListener('click', async () => {
    const plan = $('#plan').value;
    await fetch('/api/plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plan }) });
    state.plan = plan;
    document.getElementById('plan-pill').textContent = plan;
    $('#msg').textContent = 'Saved.';
    $('#msg').style.color = 'var(--good)';
  });
}
