import { api, fmt } from '/web/app.js';
import { VENDORS, caveatsHtml } from '/web/whatif.js';

const esc = fmt.htmlSafe;
const GROUPS = [['total', 'total'], ['day', 'day'], ['project', 'project'], ['session', 'session'], ['model', 'model']];
const RANGES = [['7d', 7], ['30d', 30], ['90d', 90], ['all', null]];
const BUCKETS = [['input', 'input'], ['output', 'output'], ['cache_read', 'cache read'], ['cache_create_5m', 'cache write 5m'], ['cache_create_1h', 'cache write 1h']];
const rate = x => '$' + (+x.toFixed(3)).toFixed(Math.max(2, (String(+x.toFixed(3)).split('.')[1] || '').length));

function query() {
  const q = new URLSearchParams(location.hash.split('?')[1] || '');
  const group = GROUPS.some(g => g[0] === q.get('group')) ? q.get('group') : 'total';
  const range = RANGES.some(r => r[0] === q.get('range')) ? q.get('range') : 'all';
  return { group, range };
}

const delta = (alt, real) => {
  if (alt == null || !real) return '';
  const p = Math.round(100 * (alt - real) / real);
  return p === 0 ? '<span class="muted">0%</span>' : `<span class="${p < 0 ? 'good' : 'bad'}">${p < 0 ? '−' : '+'}${Math.abs(p)}%</span>`;
};

export default async function (root) {
  const { group, range } = query();
  const days = RANGES.find(r => r[0] === range)[1];
  const targets = VENDORS.map(v => v.id).join(',');
  let url = `/api/whatif?group=${group}&compare=${targets}&limit=50`;
  if (days) url += '&since=' + encodeURIComponent(new Date(Date.now() - days * 864e5).toISOString());
  const [wi, tg] = await Promise.all([api(url), api('/api/pricing/targets')]);
  const label = id => (VENDORS.find(v => v.id === id) || {}).label || id;
  const total = wi.total;
  const hdr = wi.targets.map(t => `<th class="num">${esc(label(t))}</th><th class="num">Δ</th>`).join('');
  const tab = (name, items, cur) => `<div class="range-tabs">${items.map(([k, l]) =>
    `<button data-${name}="${k}" class="${k === cur ? 'active' : ''}">${l}</button>`).join('')}</div>`;

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px">Compare</h2>
      <span class="muted" style="font-size:12px">the same tokens at other models' list prices · estimates</span>
      <div class="spacer"></div>${tab('group', GROUPS, group)}${tab('range', RANGES.map(r => [r[0], r[0]]), range)}
    </div>
    ${caveatsHtml()}
    <div class="card" style="margin-top:16px">
      <p class="muted" style="margin:0 0 10px;font-size:12px">Every figure re-prices the recorded token counts; it does not predict what another model would have used.
        Tokenizers, caching behaviour and answer length differ, so treat differences under ~20% as noise. Cache writes use each vendor's own cache-write price.
        <b>*</b> = some tokens have no equivalent model on that vendor and are left out of its figure.</p>
      <table>
        <thead><tr><th>${group}</th><th class="num">tokens</th><th class="num">actual</th>${hdr}</tr></thead>
        <tbody>${wi.rows.map(r => `<tr>
          <td class="${group === 'session' || group === 'project' ? 'blur-sensitive' : ''}">${esc(fmt.short(r.key, 60))}</td>
          <td class="num">${fmt.compact(r.tokens)}</td><td class="num">${fmt.usd(r.cost_usd)}</td>
          ${wi.targets.map(t => `<td class="num">${r.alt_costs[t] == null ? 'n/a' : fmt.usd(r.alt_costs[t])}${r.alt_unpriced_tokens[t] ? '*' : ''}</td><td class="num">${delta(r.alt_costs[t], r.cost_usd)}</td>`).join('')}
        </tr>`).join('') || `<tr><td colspan="9" class="muted">no usage in this range</td></tr>`}</tbody>
        ${group === 'total' ? '' : `<tfoot><tr><th>all</th><th class="num">${fmt.compact(total.tokens)}</th><th class="num">${fmt.usd(total.cost_usd)}</th>
          ${wi.targets.map(t => `<th class="num">${total.alt_costs[t] == null ? 'n/a' : fmt.usd(total.alt_costs[t])}${total.alt_unpriced_tokens[t] ? '*' : ''}</th><th class="num">${delta(total.alt_costs[t], total.cost_usd)}</th>`).join('')}</tr></tfoot>`}
      </table>
      ${wi.targets.filter(t => total.alt_unpriced_tokens[t]).map(t => `<p class="muted" style="font-size:12px">* ${esc(label(t))}: ${fmt.compact(total.alt_unpriced_tokens[t])} tokens (small-model usage) have no equivalent and are not priced, so its total is lower than a full re-pricing.</p>`).join('')}
      <p class="muted" style="font-size:12px">Actual cost by token type: ${BUCKETS.map(([k, l]) => `${l} ${fmt.usd(total.by_bucket[k])}`).join(' · ')}</p>
      <p class="muted" style="font-size:12px">Cost views for tools, skills, calls, cache and compactions use the Anthropic cache model and are not compared.</p>
    </div>
    <div class="card" style="margin-top:16px">
      <h3>Equivalent models</h3>
      <p class="muted" style="margin:0 0 10px;font-size:12px">$ per million tokens: input / output (cached input). Each Claude model is paired with its class peer; ≈ marks prices not confirmed on the vendor's own page.</p>
      <table><thead><tr><th>class</th>${tg.vendors.map(v => `<th>${esc(v.label)}</th>`).join('')}</tr></thead><tbody>
        ${tg.classes.map(c => `<tr><td><span class="badge ${esc(c.id)}">${esc(c.label)}</span></td>${tg.vendors.map(v => {
          const m = c.members[v.id];
          return m ? `<td title="${esc(m.note || '')}"><div class="mono">${esc(m.model)}${m.verified === false ? ' ≈' : ''}</div>
            <div class="muted">${rate(m.input)} / ${rate(m.output)} (${rate(m.cache_read)})</div></td>` : '<td class="muted">none</td>'; }).join('')}</tr>`).join('')}
      </tbody></table>
      <p class="muted" style="margin:8px 0 0;font-size:11px">${Object.entries(tg.sources).map(([k, s]) => `${esc(k)}: ${esc(s.url)} (checked ${esc(s.as_of)}${s.stale ? ', <b>stale</b>' : ''})`).join(' · ')}</p>
    </div>`;

  const go = (k, v) => {
    const q = new URLSearchParams({ group, range, [k]: v });
    location.hash = '#/compare?' + q.toString();
  };
  root.querySelectorAll('[data-group]').forEach(b => b.addEventListener('click', () => go('group', b.dataset.group)));
  root.querySelectorAll('[data-range]').forEach(b => b.addEventListener('click', () => go('range', b.dataset.range)));
}
