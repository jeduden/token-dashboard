// Exercises web/whatif.js helpers under Node with a stubbed localStorage; prints results as JSON.
// Usage: node whatif_probe.mjs file:///abs/path/to/web/whatif.js
const store = {};
globalThis.localStorage = { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } };
const w = await import(process.argv[2]);
const out = {};
out.off = [w.getTargets(), w.withCompare('/api/x'), w.altLine({ alt_costs: { openai: 1 } })];
w.setTargets(['openai', 'kimi', 'evil<b>']);
out.targets = w.getTargets();
out.url = [w.withCompare('/api/x'), w.withCompare('/api/x?a=1')];
out.line = w.altLine({ alt_costs: { openai: 12.345, kimi: null }, alt_unpriced_tokens: { openai: 0, kimi: 5000 } });
out.partial = w.altLine({ alt_costs: { openai: 3, kimi: 2 }, alt_unpriced_tokens: { openai: 0, kimi: 5000 } });
out.none = w.altLine({});
out.note = w.altNote(); out.caveats = w.caveatsHtml();
w.setTargets([]); out.noteOff = w.altNote();
console.log(JSON.stringify(out));
