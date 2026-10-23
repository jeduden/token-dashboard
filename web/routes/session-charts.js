import { fmt } from '/web/app.js';

const esc = fmt.htmlSafe;
const TYPES = [
  ['input', 'input', '#4A9EFF'], ['output', 'output', '#7C5CFF'],
  ['cache_read', 'cache read', '#3FB68B'], ['cache_create', 'cache write', '#E8A23B'],
];
const KINDS = { text: 'text reply', tools: 'tool-calling', subagent: 'subagent' };
const AXIS = '#8B98A6', GRID = '#1F2630', PROMPT = '#8B98A6', COMPACT = '#E5484D';
const TIP = { trigger: 'axis', backgroundColor: '#0F1419', borderColor: '#283040', borderWidth: 1,
  textStyle: { color: '#E6EDF3', fontSize: 12 }, padding: [8, 12] };

function chart(el, option) {
  const c = echarts.init(el, null, { renderer: 'svg' });
  c.setOption({ textStyle: { color: '#E6EDF3' }, grid: { left: 40, right: 48, top: 30, bottom: 24, containLabel: true }, ...option });
  window.addEventListener('resize', () => c.resize());
  return c;
}

const timeAxis = { type: 'time', axisLine: { lineStyle: { color: GRID } }, axisLabel: { color: AXIS }, axisTick: { show: false } };
const valAxis = (name, f) => ({ type: 'value', name, nameTextStyle: { color: AXIS }, axisLabel: { color: AXIS, formatter: f },
  splitLine: { lineStyle: { color: GRID } }, axisLine: { show: false }, axisTick: { show: false } });

function marks(tl) {
  const lines = [
    ...tl.prompts.slice(0, 300).map(p => ({ xAxis: p.t, lineStyle: { color: PROMPT, type: 'dotted', width: 1, opacity: 0.6 }, label: { show: false }, name: 'prompt' })),
    ...tl.compactions.map(c => ({ xAxis: c.timestamp, lineStyle: { color: COMPACT, type: 'solid', width: 1.5 }, label: { show: false }, name: 'compaction' })),
  ];
  return { silent: true, symbol: 'none', data: lines };
}

function shareList(title, entries, total, fmtV) {
  const rows = entries.filter(e => e.value > 0).sort((a, b) => b.value - a.value);
  return `<div><h4 style="margin:0 0 8px;font-size:13px">${esc(title)}</h4>${rows.map(e => `
    <div style="margin:4px 0;font-size:12px">
      <div style="display:flex;justify-content:space-between"><span>${esc(e.label)}</span>
        <span class="mono">${fmtV(e.value)} · ${total ? Math.round(100 * e.value / total) : 0}%${e.note ? ' · ' + esc(e.note) : ''}</span></div>
      <div style="background:${GRID};height:5px;border-radius:3px"><div style="width:${total ? 100 * e.value / total : 0}%;height:5px;border-radius:3px;background:${e.color || '#4A9EFF'}"></div></div>
    </div>`).join('') || '<span class="muted">none</span>'}</div>`;
}

const signed = n => (n > 0 ? '+' : n < 0 ? '−' : '') + fmt.compact(Math.abs(n));
const localTime = t => (t || '').replace('T', ' ').replace(/\.\d+Z?$|Z$/, '');

// The biggest context jumps are where the interesting messages are; offer them as one-click entry points.
function jumps(tl) {
  const top = tl.turns.map((t, i) => ({ i, d: t.context_delta, t })).filter(x => x.d > 0 && x.t.kind !== 'subagent')
    .sort((a, b) => b.d - a.d).slice(0, 6);
  if (!top.length) return '';
  return `<div style="margin-top:10px;font-size:12px"><span class="muted">Biggest context jumps:</span>
    ${top.map(x => `<button type="button" class="jump" data-turn="${x.i}" style="margin:2px 4px">${signed(x.d)} · ${esc(localTime(x.t.t).slice(5, 16))}</button>`).join('')}</div>`;
}

function detailHtml(tl, i) {
  const t = tl.turns[i], c = t.caused;
  const grew = t.context_delta;
  const rows = [
    ...c.prompts.map(p => `<tr><td>your prompt</td><td class="blur-sensitive">${esc(fmt.short(p.text || '', 120))}</td><td class="num">${fmt.int(p.chars)} chars</td><td></td></tr>`),
    ...c.tool_results.map(r => `<tr><td><span class="badge">${esc(r.tool)}</span>${r.is_error ? ' <span class="badge">error</span>' : ''}</td>
      <td class="mono blur-sensitive" style="font-size:11px">${esc(fmt.short(r.target || '', 100))}</td>
      <td class="num">${r.result_tokens == null ? '—' : fmt.int(r.result_tokens) + ' tok'}</td>
      <td class="num muted">${grew > 0 && r.result_tokens ? Math.round(100 * r.result_tokens / grew) + '%' : ''}</td></tr>`),
    c.other_result_tokens ? `<tr><td colspan="2" class="muted">other tool results</td><td class="num">${fmt.int(c.other_result_tokens)} tok</td><td></td></tr>` : '',
  ].join('');
  const sp = t.cost_split;
  return `<div class="card" style="margin-top:12px;background:var(--panel-2)">
    <div class="flex" style="flex-wrap:wrap;gap:12px;align-items:center">
      <strong>Turn ${i + 1} of ${tl.turns.length}</strong>
      <span class="mono muted">${esc(localTime(t.t))}</span>
      ${t.model ? `<span class="badge ${fmt.modelClass(t.model)}">${esc(fmt.modelShort(t.model))}</span>` : ''}
      <span class="badge">${esc(KINDS[t.kind])}</span>
      <span class="spacer"></span>
      <button type="button" id="tl-prev" ${i ? '' : 'disabled'}>← prev</button>
      <button type="button" id="tl-next" ${i < tl.turns.length - 1 ? '' : 'disabled'}>next →</button>
    </div>
    <div class="muted" style="font-size:12px;margin:8px 0">
      context ${fmt.compact(t.context_tokens)} (<strong style="color:${grew > 0 ? '#E8A23B' : 'inherit'}">${signed(grew)}</strong> vs the previous turn${t.kind === 'subagent' ? ' of that subagent' : ''})
      · cost ${fmt.usd4(t.cost_usd)} (input ${fmt.usd4(sp.input)}, output ${fmt.usd4(sp.output)}, cache read ${fmt.usd4(sp.cache_read)}, cache write ${fmt.usd4(sp.cache_create)})
      · ${fmt.int(t.output_tokens)} output tokens${t.tools.length ? ' · called ' + esc(t.tools.join(', ')) : ''}</div>
    ${rows ? `<table><thead><tr><th>what was added</th><th>detail</th><th class="num">size</th><th class="num">of increase</th></tr></thead><tbody>${rows}</tbody></table>`
           : '<p class="muted" style="margin:0">Nothing was added since the previous turn (no new prompt or tool results).</p>'}
  </div>`;
}

export function timelineHtml(tl) {
  const s = tl.summary, total = s.cost_usd;
  const types = TYPES.map(([k, label, color]) => ({ label, value: s.by_token_type[k], color }));
  const kinds = Object.entries(s.by_kind).map(([k, v]) => ({ label: KINDS[k], value: v.cost_usd, note: `${fmt.int(v.turns)} turns` }));
  const models = Object.entries(s.by_model).map(([k, v]) => ({ label: fmt.modelShort(k), value: v.cost_usd, note: `${fmt.int(v.turns)} turns` }));
  if (!tl.turns.length) return '<div class="card"><h3>Cost over time</h3><p class="muted">No assistant turns in this session.</p></div>';
  return `
    <div class="card">
      <h3 style="margin-top:0">Cost over time <span class="muted" style="font-size:12px;font-weight:400">· ${fmt.usd(total)} total (estimated) · ${tl.turns.length} turns</span></h3>
      <p class="muted" style="font-size:12px;margin:0 0 6px">Bars: cost of each turn by token type. Line: cumulative cost.
        Dotted lines: your prompts. <span style="color:${COMPACT}">Red lines</span>: compactions.</p>
      <div id="tl-cost" style="height:260px"></div>
      <h4 style="margin:14px 0 0;font-size:13px">Context size per turn <span class="muted" style="font-weight:400">(input + cache read + cache write)</span></h4>
      <div id="tl-ctx" style="height:170px"></div>
      ${jumps(tl)}
      <div id="tl-detail"><p class="muted" style="font-size:12px;margin:10px 0 0">Click a bar or point to see which messages and tool results grew the context at that turn.</p></div>
      <div class="row cols-3" style="margin-top:14px;gap:24px">
        ${shareList('Cost by token type', types, total, fmt.usd)}
        ${shareList('Cost by turn kind', kinds, total, fmt.usd)}
        ${shareList('Cost by model', models, total, fmt.usd)}
      </div>
    </div>`;
}

export function mountTimeline(root, tl) {
  const cost = root.querySelector('#tl-cost'), ctx = root.querySelector('#tl-ctx');
  if (!cost || !ctx) return;
  const mk = marks(tl);
  const bars = TYPES.map(([k, label, color], i) => ({
    name: label, type: 'bar', stack: 'cost', yAxisIndex: 0, barMinWidth: 2, barMaxWidth: 14,
    itemStyle: { color }, data: tl.turns.map(t => [t.t, t.cost_split[k]]),
    ...(i === 0 ? { markLine: mk } : {}),
  }));
  const c1 = chart(cost, {
    tooltip: { ...TIP, valueFormatter: v => fmt.usd4(v) },
    legend: { textStyle: { color: AXIS }, top: 0, right: 0, icon: 'roundRect', itemWidth: 8, itemHeight: 8 },
    xAxis: timeAxis,
    yAxis: [valAxis('per turn', v => '$' + v), { ...valAxis('cumulative', v => '$' + v), splitLine: { show: false } }],
    series: [...bars, { name: 'cumulative', type: 'line', yAxisIndex: 1, showSymbol: false, step: 'end',
      lineStyle: { width: 2, color: '#E6EDF3' }, itemStyle: { color: '#E6EDF3' },
      data: tl.turns.map(t => [t.t, t.cum_cost_usd]) }],
  });
  const c2 = chart(ctx, {
    tooltip: { ...TIP, valueFormatter: v => fmt.int(v) + ' tokens' },
    xAxis: timeAxis, yAxis: valAxis('', v => fmt.compact(v)),
    series: [{ name: 'context', type: 'line', step: 'end', showSymbol: false, lineStyle: { width: 1.5, color: '#4A9EFF' },
      areaStyle: { opacity: 0.12, color: '#4A9EFF' }, data: tl.turns.map(t => [t.t, t.context_tokens]), markLine: mk },
      { id: 'sel', name: 'selected', type: 'scatter', symbolSize: 11, silent: true, itemStyle: { color: '#E8A23B', borderColor: '#fff', borderWidth: 1.5 }, data: [] }],
  });
  echarts.connect([c1, c2]);

  const box = root.querySelector('#tl-detail');
  const show = i => {
    if (i == null || !tl.turns[i]) return;
    box.innerHTML = detailHtml(tl, i);
    c2.setOption({ series: [{ id: 'sel', data: [[tl.turns[i].t, tl.turns[i].context_tokens]] }] });
    const go = d => () => show(i + d);
    const prev = box.querySelector('#tl-prev'), next = box.querySelector('#tl-next');
    if (prev) prev.addEventListener('click', go(-1));
    if (next) next.addEventListener('click', go(1));
  };
  c1.on('click', ev => show(ev.dataIndex));
  c2.on('click', ev => show(ev.dataIndex));
  root.querySelectorAll('button.jump').forEach(b => b.addEventListener('click', () => {
    show(Number(b.dataset.turn));
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }));
}
