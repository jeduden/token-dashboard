// range.js — the 7d / 30d / 90d / all selector kept in the hash (?range=), shared by the newer tabs.
import { $$ } from '/web/app.js';

export const RANGES = [['7d', 7], ['30d', 30], ['90d', 90], ['all', null]];

export function readRange(fallback = '7d') {
  const q = new URLSearchParams(location.hash.split('?')[1] || '');
  return RANGES.some(r => r[0] === q.get('range')) ? q.get('range') : fallback;
}

export function sinceOf(range) {
  const days = (RANGES.find(r => r[0] === range) || [])[1];
  return days ? new Date(Date.now() - days * 864e5).toISOString() : null;
}

export function withSince(url, range) {
  const s = sinceOf(range);
  return s ? url + (url.includes('?') ? '&' : '?') + 'since=' + encodeURIComponent(s) : url;
}

export const rangeTabs = range =>
  `<div class="range-tabs">${RANGES.map(([k]) => `<button data-range="${k}" class="${k === range ? 'active' : ''}">${k}</button>`).join('')}</div>`;

export function bindRange(root) {
  $$('[data-range]', root).forEach(b => b.addEventListener('click', () => {
    const base = location.hash.replace(/^#/, '').split('?')[0];
    location.hash = '#' + base + '?range=' + b.dataset.range;
  }));
}
