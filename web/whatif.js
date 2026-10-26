// whatif.js — the "compare" setting: which vendors' prices to show next to the real cost.
// Self-contained (no import of app.js) so app.js can import it for the topbar.

const KEY = 'td.compare';
export const VENDORS = [
  { id: 'anthropic', label: 'Claude lineup' },
  { id: 'openai',    label: 'OpenAI' },
  { id: 'kimi',      label: 'Kimi' },
];
const label = id => (VENDORS.find(v => v.id === id) || { label: id.replace(/^model:/, '') }).label;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const usd = n => '$' + Number(n).toFixed(n < 10 ? 2 : 0);

export function getTargets() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || '[]');
    return Array.isArray(v) ? v.filter(t => VENDORS.some(x => x.id === t)) : [];
  } catch { return []; }
}

export function setTargets(ids) {
  try { localStorage.setItem(KEY, JSON.stringify(ids)); } catch {}
}

// Adds compare=… to an API url when something is selected.
export function withCompare(url, targets = getTargets()) {
  if (!targets.length) return url;
  return url + (url.includes('?') ? '&' : '?') + 'compare=' + encodeURIComponent(targets.join(','));
}

// "≈$12 OpenAI · ≈$9 Kimi" for a row that carries alt_costs; '' when comparing is off.
export function altLine(row, targets = getTargets()) {
  if (!targets.length || !row || !row.alt_costs) return '';
  const un = row.alt_unpriced_tokens || {};
  const parts = targets.map(t => {
    const v = row.alt_costs[t];
    const gap = un[t] ? '*' : '';
    return v == null ? `<span title="no equivalent model">${esc(label(t))} n/a</span>`
                     : `<span>≈${usd(v)}${gap} ${esc(label(t))}</span>`;
  });
  return `<div class="alt muted" title="${esc(altTitle(row, targets))}">${parts.join(' · ')}</div>`;
}

export function altTitle(row, targets = getTargets()) {
  const un = row && row.alt_unpriced_tokens || {};
  const notes = targets.filter(t => un[t]).map(t => `${label(t)} has no equivalent for ${Number(un[t]).toLocaleString()} tokens, which are left out`);
  return 'Estimate: the same token counts at the other model\'s list price. Tokenizers, caching and answer length differ.'
    + (notes.length ? ' ' + notes.join('; ') + '.' : '');
}

// The topbar pill. Clicking opens a small menu of checkboxes; a change re-renders the page.
export function mountCompareSelector(host) {
  const wrap = document.createElement('span');
  wrap.className = 'compare-sel';
  host.appendChild(wrap);
  const paint = () => {
    const t = getTargets();
    wrap.innerHTML = `<button class="pill ${t.length ? '' : 'muted'}" id="cmp-btn" title="Show what the same usage would cost on other models (estimate)">compare: ${t.length ? esc(t.map(label).join(', ')) : 'off'}</button>
      <div class="cmp-menu" hidden>
        ${VENDORS.map(v => `<label><input type="checkbox" value="${v.id}" ${t.includes(v.id) ? 'checked' : ''}> ${esc(v.label)}</label>`).join('')}
        <a href="#/compare">details →</a>
      </div>`;
    const menu = wrap.querySelector('.cmp-menu');
    wrap.querySelector('#cmp-btn').addEventListener('click', e => { e.stopPropagation(); menu.hidden = !menu.hidden; });
    menu.addEventListener('click', e => e.stopPropagation());
    menu.addEventListener('change', () => {
      setTargets([...menu.querySelectorAll('input:checked')].map(i => i.value));
      paint();
      window.dispatchEvent(new Event('hashchange'));
    });
    menu.querySelector('a').addEventListener('click', () => { menu.hidden = true; });
  };
  document.addEventListener('click', () => { const m = wrap.querySelector('.cmp-menu'); if (m) m.hidden = true; });
  paint();
}
