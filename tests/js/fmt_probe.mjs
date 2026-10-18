// Loads web/app.js under a stubbed DOM and prints fmt.htmlSafe() results as JSON.
// Usage: node fmt_probe.mjs file:///abs/path/to/web/app.js
const stub = () => new Proxy(function () {}, {
  get: (_, k) => (k === Symbol.toPrimitive ? () => '' : k === 'then' ? undefined : stub()),
  apply: () => stub(), construct: () => stub(), set: () => true,
});
globalThis.document = stub();
globalThis.window = stub();
globalThis.location = { hash: '' };
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.fetch = () => Promise.reject(new Error('no network in tests'));
process.on('unhandledRejection', () => {});

const { fmt } = await import(process.argv[2]);
const inputs = [327243, 0, 12.5, null, undefable(), '<a href="x">&\'</a>', 'plain'];
function undefable() { return undefined; }
const out = inputs.map(v => {
  try { return { ok: true, value: fmt.htmlSafe(v) }; } catch (e) { return { ok: false, error: String(e.message) }; }
});
console.log(JSON.stringify(out));
process.exit(0);
