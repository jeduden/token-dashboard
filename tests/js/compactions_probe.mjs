// Renders web/routes/compactions.js under a stubbed DOM with a mocked fetch; prints the HTML as JSON.
// Usage: node compactions_probe.mjs /abs/path/to/web /abs/path/to/scratch-dir
import fs from 'node:fs';
import path from 'node:path';
const [webDir, tmp] = process.argv.slice(2);
// The routes import absolute '/web/...' URLs; point them at a temp copy of the sources.
const out = path.join(tmp, 'web');
fs.mkdirSync(path.join(out, 'routes'), { recursive: true });
for (const f of ['app.js', 'routes/compactions.js', 'routes/costs-drill-ui.js']) {
  const src = fs.readFileSync(path.join(webDir, f), 'utf8').replaceAll("'/web/", "'file://" + out + '/');
  fs.writeFileSync(path.join(out, f.replace(/\.js$/, '.mjs')), src.replaceAll('.js\'', '.mjs\''));
}
const stub = () => new Proxy(function () {}, {
  get: (_, k) => (k === Symbol.toPrimitive ? () => '' : k === 'then' ? undefined : stub()),
  apply: () => stub(), construct: () => stub(), set: () => true,
});
globalThis.document = stub();
globalThis.window = stub();
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.echarts = { init: () => ({ setOption() {}, dispose() {}, resize() {}, isDisposed: () => false }) };
process.on('unhandledRejection', () => {});

const bucket = (key, label, auto, manual) => ({ key, label, auto, manual, other: 0, count: auto + manual });
const data = {
  summary: { count: 3, auto: 2, manual: 1, other: 0, sidechain: 0, median_pre_tokens: 350000, median_post_tokens: 15000,
    p90_pre_tokens: 600000, median_duration_s: 31.8, total_dropped_tokens: 900000, median_turns_between: 4 },
  by_day: [{ day: '2026-05-01', auto: 2, manual: 1, other: 0, count: 3 }],
  gap_buckets: [bucket('lt5m', '<5 min', 1, 0), bucket('5_10m', '5-10 min', 0, 0), bucket('10_30m', '10-30 min', 0, 0),
    bucket('30_60m', '30-60 min', 0, 0), bucket('1_2h', '1-2 h', 0, 1), bucket('gt2h', '>2 h', 1, 0)],
  gap_unknown: 0,
  pre_token_buckets: [bucket('lt150k', '<150k', 0, 0), bucket('gt500k', '>500k', 2, 1)],
  by_project: [{ project_slug: 'p<1>', project_name: '<script>alert(1)</script>', count: 3, median_pre_tokens: 1 }],
  by_repo: [{ repo: 'r', repo_name: '<img src=x onerror=1>', count: 3, median_pre_tokens: 1 }],
  worst_sessions: [{ session_id: 'sa"b', project_slug: 'p', project_name: 'P', compactions: 3, max_pre_tokens: 5 }],
  after_expiry: { count: 2, auto: 1, manual: 1, other: 0, pre_tokens_total: 950000, avoidable_usd_upper_bound: 12.5,
    unpriced: 0, note: 'Upper bound, not a measurement <b>x</b>' },
  thresholds: { ttl_5m_s: 300, ttl_1h_s: 3600 },
};
const respond = (status, body) => () => Promise.resolve({ ok: status < 400, status, json: () => body === undefined ? Promise.reject(new Error('no body')) : Promise.resolve(body) });

async function run(fetchImpl) {
  globalThis.location = { hash: '#/compactions?range=all' };
  globalThis.fetch = fetchImpl;
  const root = { html: [], set innerHTML(v) { this.html.push(v); }, querySelectorAll: () => [], querySelector: () => null };
  const mod = await import('file://' + path.join(out, 'routes/compactions.mjs'));
  await mod.default(root);
  return root.html;
}
const result = {
  ok: await run(respond(200, data)),
  badRequest: await run(respond(400, { error: 'bad since <x>' })),
  serverError: await run(respond(500, undefined)),
};
console.log(JSON.stringify(result));
process.exit(0);
