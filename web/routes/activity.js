import { api, fmt } from '/web/app.js';
import { readRange, withSince, rangeTabs, bindRange } from '/web/range.js';

const esc = fmt.htmlSafe;
const hm = m => String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(Math.round(m % 60)).padStart(2, '0');
const COLORS = ['#4A9EFF', '#7C5CFF', '#3FB68B', '#E8A23B', '#E5675D', '#2CB5C8'];

export default async function (root) {
  const range = readRange('7d');
  const d = await api(withSince('/api/activity', range));
  const o = d.overall;
  const kpi = (label, value, sub = '') => `<div class="card kpi"><div class="label">${label}</div><div class="value">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;
  const idle = o.wall_hours ? Math.round(100 * (1 - o.active_hours / o.wall_hours)) : 0;
  const colors = {};
  const colorOf = id => colors[id] ??= COLORS[Object.keys(colors).length % COLORS.length];

  const day = x => `
    <div class="card" style="margin-top:12px">
      <h3 style="margin:0 0 8px;font-size:13px">${esc(x.date)} <span class="muted" style="font-weight:400">· ${x.sessions.length} sessions · ${(x.active_min / 60).toFixed(1)} h active · up to ${x.peak_concurrent} at once · UTC</span></h3>
      <div class="gantt">
        <div class="gantt-axis">${[0, 6, 12, 18, 24].map(h => `<span style="left:${100 * h / 24}%">${String(h).padStart(2, '0')}</span>`).join('')}</div>
        ${x.sessions.map(s => `<div class="gantt-row">
          <a class="gantt-label blur-sensitive" href="#/sessions/${encodeURIComponent(s.session_id)}" title="${esc(s.project_name)} · ${esc(s.session_id)}">${esc(fmt.short(s.project_name, 28))}</a>
          <div class="gantt-track">${s.segments.map(([a, b]) =>
            `<div class="gantt-seg" title="${hm(a)}–${hm(b)}" style="left:${100 * a / 1440}%;width:${Math.max(0.3, 100 * (b - a) / 1440)}%;background:${colorOf(s.session_id)}"></div>`).join('')}</div>
        </div>`).join('')}
      </div>
    </div>`;

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px">Activity</h2>
      <span class="muted" style="font-size:12px">when you and your agents were working · a pause over ${d.idle_gap_min} min ends a stretch</span>
      <div class="spacer"></div>${rangeTabs(range)}
    </div>
    <div class="row cols-4">
      ${kpi('Active time', o.active_hours + ' h', 'sum of working stretches')}
      ${kpi('Wall-clock', o.wall_hours + ' h', `first to last message, ${idle}% idle`)}
      ${kpi('Sessions', fmt.int(o.sessions))}
      ${kpi('Peak parallel', fmt.int(o.peak_concurrent), 'sessions working at once')}
    </div>
    ${d.by_day.slice().reverse().map(day).join('') || '<div class="card" style="margin-top:12px"><p class="muted">No activity in this range.</p></div>'}`;
  bindRange(root);
}
