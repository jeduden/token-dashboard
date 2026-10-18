"""Per-tool cost drill-down (cost_drilldown.tool_params + /api/tool-costs/params).

Pricing from test_cost_insights: $1 per cache-read token, $10 per cache-create
token. A result of N tokens costs N*10 (create, next turn) + N*1 per later turn.
"""
import http.server
import json
import socket
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request

from token_dashboard import cost_drilldown as cd
from token_dashboard.server import build_handler
from tests.test_cost_insights import Base, Builder, PRICING


def _get(db, tool, **kw):
    cd._cache.clear()
    return cd.tool_params(db, PRICING, tool, **kw)


def _by_key(out):
    return {r["key"]: r for r in out["rows"]}


class GroupingTests(Base):
    def _bash(self):
        b = Builder()
        b.assistant([("t1", "Bash", {"command": "git status"})]); b.result("t1", 400)       # 100 tok
        b.assistant([("t2", "Bash", {"command": "git diff -x"})]); b.result("t2", 4000)     # 1000 tok
        b.assistant([("t3", "Bash", {"command": "FOO=1 npm test"})]); b.result("t3", 400)   # 100 tok
        b.assistant(); b.assistant()
        b.write(self.root); self.scan()

    def test_bash_first_word_and_cost_math(self):
        self._bash()
        out = _get(self.db, "Bash")
        self.assertEqual(out["group"], "command")        # auto resolves
        r = _by_key(out)
        # t1: 100*10 + 3 carry turns*100; t2: 1000*10 + 2*1000; t3: 100*10 + 100
        self.assertAlmostEqual(r["git"]["est_cost_usd"], 1300 + 12000)
        self.assertAlmostEqual(r["git"]["carry_cost_usd"], 300 + 2000)
        self.assertEqual(r["git"]["calls"], 2)
        self.assertAlmostEqual(r["git"]["cost_per_call"], 13300 / 2)
        self.assertEqual(r["git"]["avg_result_tokens"], 550)
        self.assertEqual(r["git"]["max_result_tokens"], 1000)
        self.assertEqual(r["npm"]["calls"], 1)           # env assignment skipped
        self.assertEqual(out["rows"][0]["key"], "git")   # ranked by cost
        self.assertAlmostEqual(out["totals"]["est_cost_usd"], 14400)
        self.assertAlmostEqual(r["git"]["share_of_tool_cost"], 13300 / 14400, places=3)
        self.assertAlmostEqual(out["rows"][-1]["cumulative_share"], 1.0)
        # matches the existing tool-costs figure
        from token_dashboard import cost_insights as ci
        ci._cache.clear()
        tc = [t for t in ci.tool_costs(self.db, PRICING, catalog={})["tools"] if t["tool_name"] == "Bash"][0]
        self.assertAlmostEqual(tc["est_cost_usd"], out["totals"]["est_cost_usd"])

    def test_bash_two_words_and_target_modes(self):
        self._bash()
        r = _by_key(_get(self.db, "Bash", group="command2"))
        self.assertEqual(set(r), {"git status", "git diff", "npm test"})
        self.assertAlmostEqual(r["git diff"]["est_cost_usd"], 12000)
        t = _by_key(_get(self.db, "Bash", group="target"))
        self.assertIn("git status", t)
        self.assertEqual(len(t), 3)

    def test_available_groups(self):
        self._bash()
        keys = [g["key"] for g in _get(self.db, "Bash")["available_groups"] if g["kind"] == "parameter"]
        self.assertEqual(keys, ["command", "command2", "pipeline", "target"])

    def test_read_extension_and_directory(self):
        b = Builder()
        for i, p in enumerate(["/a/x.py", "/a/y.py", "/b/z.md", "/b/Makefile"]):
            b.assistant([("r%d" % i, "Read", {"file_path": p})]); b.result("r%d" % i, 40)
        b.assistant(); b.assistant()
        b.write(self.root); self.scan()
        ext = _by_key(_get(self.db, "Read", group="extension"))
        self.assertEqual((ext[".py"]["calls"], ext[".md"]["calls"], ext["(none)"]["calls"]), (2, 1, 1))
        d = _by_key(_get(self.db, "Read", group="directory"))
        self.assertEqual((d["/a"]["calls"], d["/b"]["calls"]), (2, 2))
        self.assertEqual(_get(self.db, "Read")["group"], "directory")

    def test_webfetch_host_and_grep_token(self):
        b = Builder()
        for i, u in enumerate(["https://example.com/a", "https://example.com/b?x=1", "https://Other.org:8080/"]):
            b.assistant([("w%d" % i, "WebFetch", {"url": u})]); b.result("w%d" % i, 40)
        for i, p in enumerate(["TODO.*fix", "TODO", "src/**/*.py"]):
            b.assistant([("g%d" % i, "Grep", {"pattern": p})]); b.result("g%d" % i, 40)
        b.assistant(); b.assistant()
        b.write(self.root); self.scan()
        h = _by_key(_get(self.db, "WebFetch"))
        self.assertEqual((h["example.com"]["calls"], h["other.org"]["calls"]), (2, 1))
        g = _by_key(_get(self.db, "Grep"))
        self.assertEqual((g["TODO"]["calls"], g["src"]["calls"]), (2, 1))

    def test_single_group_tools(self):
        b = Builder()
        b.assistant([("s1", "Skill", {"skill": "demo"})]); b.result("s1", 40)
        b.assistant(); b.assistant()
        b.write(self.root); self.scan()
        out = _get(self.db, "Skill", catalog={})
        self.assertEqual([g["key"] for g in out["available_groups"] if g["kind"] == "parameter"], ["target"])
        self.assertEqual(out["rows"][0]["key"], "demo")

    def test_bad_inputs(self):
        self._bash()
        with self.assertRaises(ValueError):
            _get(self.db, "NoSuchTool")
        with self.assertRaises(ValueError):
            _get(self.db, "")
        with self.assertRaises(ValueError):
            _get(self.db, "Bash", group="extension")


class RepeatAndFrequencyTests(Base):
    def test_repeat_detection_and_frequency(self):
        b = Builder("s1")
        for i in range(3):
            b.assistant([("r%d" % i, "Read", {"file_path": "/same.py"})]); b.result("r%d" % i, 40)
        b.assistant(); b.assistant()
        b.write(self.root)
        c = Builder("s2")
        c.assistant([("q0", "Read", {"file_path": "/same.py"})]); c.result("q0", 40)
        c.assistant(); c.assistant()
        c.write(self.root); self.scan()
        out = _get(self.db, "Read", group="target")
        row = out["rows"][0]
        self.assertEqual((row["calls"], row["sessions"]), (4, 2))
        self.assertAlmostEqual(row["frequency_per_session"], 2.0)
        self.assertEqual(out["repeat"]["repeat_calls"], 2)       # calls 2 and 3 of s1
        # call1: 100 + 20 carry, call2: 100 + 10 carry
        self.assertAlmostEqual(out["repeat"]["repeat_cost_usd"], 230)
        self.assertAlmostEqual(out["repeat"]["share_of_calls"], 0.5)
        self.assertTrue(any(d["rule"] == "reread" for d in out["diagnostics"]))
        self.assertIsNotNone(row["first_seen"]); self.assertLessEqual(row["first_seen"], row["last_seen"])

    def test_quadrants(self):
        b = Builder()
        seq = ["ls", "find", "ls", "find", "cat", "ls", "find", "echo", "ls", "find"]
        for i, cmd in enumerate(seq):
            big = cmd in ("cat", "find")
            b.assistant([("c%d" % i, "Bash", {"command": cmd})]); b.result("c%d" % i, 8000 if big else 40)
        b.assistant(); b.assistant()
        b.write(self.root); self.scan()
        out = _get(self.db, "Bash")
        r = _by_key(out)
        self.assertEqual(r["find"]["quadrant"], "frequent_expensive")
        self.assertEqual(r["ls"]["quadrant"], "frequent_cheap")
        self.assertEqual(r["cat"]["quadrant"], "rare_expensive")
        self.assertEqual(r["echo"]["quadrant"], "rare_cheap")
        self.assertIn("batch or cache", r["ls"]["quadrant_label"])
        self.assertIn("trim the output", r["cat"]["quadrant_label"])
        th = out["thresholds"]
        self.assertIn("calls_threshold", th); self.assertIn("cost_per_call_threshold", th)
        self.assertIn("average", th["method"])
        self.assertNotIn("median_calls", th)


def zipf_fixture(n=200):
    """Heavy-tailed keys: Zipf call counts; head keys $0.01-0.10/call, tail ~$0.001, a few rare keys at $0.5+."""
    items = []
    for i in range(1, n + 1):
        calls = max(1, int(6000 / i))
        spread = ((i * 7919) % 13) / 12                      # deterministic spread in [0, 1]
        cpc = 0.01 + 0.09 * spread if i <= 30 else 0.0005 + 0.004 * spread   # tail keys are tiny
        if i in (40, 60, 90, 120):
            cpc = 0.6                                        # rare but expensive
        items.append(("k%d" % i, calls, calls * cpc))
    return items


def old_median_label(items):
    from statistics import median
    mc, mp = median(c for _, c, _ in items), median(u / c for _, c, u in items)
    return lambda c, u: ("frequent" if c > mc and c >= 3 else "rare") + "_" + ("expensive" if u / c > mp else "cheap")


class QuadrantWordingTests(unittest.TestCase):
    def test_labels_say_volume_not_rare(self):
        self.assertEqual(set(cd.QUADRANT_LABELS), {"frequent_expensive", "frequent_cheap", "rare_expensive", "rare_cheap"})
        text = " ".join(cd.QUADRANT_LABELS.values())
        self.assertIn("high volume", text)
        self.assertIn("lower volume", text)
        self.assertNotIn("rare", text)
        self.assertNotIn("frequent", text)


class ThresholdTests(unittest.TestCase):
    def _labels(self):
        items = zipf_fixture()
        tot_u, tot_c = sum(u for _, _, u in items), sum(c for _, c, _ in items)
        ct, pt = cd.compute_thresholds([(c, u) for _, c, u in items], tot_u, tot_c)
        top = sorted(items, key=lambda x: -x[2])[:10]
        new = {k: cd.quadrant(c, u / c, ct, pt) for k, c, u in items}
        return items, top, new, (ct, pt, tot_u / tot_c)

    def test_all_quadrants_populated_and_top_keys_spread(self):
        _, top, new, (ct, pt, avg) = self._labels()
        self.assertEqual(set(new.values()), {"frequent_expensive", "frequent_cheap",
                                             "rare_expensive", "rare_cheap"})
        self.assertGreaterEqual(len({new[k] for k, _, _ in top}), 2)
        self.assertAlmostEqual(pt, avg)

    def test_old_median_labels_top_keys_all_in_one_quadrant(self):
        items, top, _, _ = self._labels()
        old = old_median_label(items)
        self.assertEqual({old(c, u) for _, c, u in top}, {"frequent_expensive"})

    def test_expensive_is_at_least_average(self):
        self.assertEqual(cd.quadrant(10, 1.0, 5, 1.0), "frequent_expensive")
        self.assertEqual(cd.quadrant(10, 0.99, 5, 1.0), "frequent_cheap")

    def test_min_calls_floor_and_empty(self):
        self.assertEqual(cd.quadrant(2, 5.0, 1, 1.0), "rare_expensive")
        self.assertEqual(cd.compute_thresholds([], 0.0, 0), (0, 0.0))

    def test_p75_over_top_cost_keys_only(self):
        # tail of 100 one-call keys must not drag the calls threshold down
        items = [(1000, 100.0), (500, 50.0), (200, 20.0), (100, 10.0)] + [(1, 0.01)] * 100
        ct, _ = cd.compute_thresholds(items, sum(u for _, u in items), sum(c for c, _ in items))
        self.assertEqual(ct, 1000)  # covered = [1000, 500]; nearest-rank P75


class EdgeTests(Base):
    def test_unattributed_counted_separately(self):
        b = Builder()
        for i, p in enumerate(["/a/f.py", "/a/g.py"]):
            b.assistant([("r%d" % i, "Read", {"file_path": p})]); b.result("r%d" % i, 40)
        b.assistant(); b.assistant()
        b.write(self.root); self.scan()
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE tool_calls SET tool_use_id=NULL WHERE target='/a/g.py'")
            c.commit()
        out = _get(self.db, "Read", group="target")
        self.assertEqual(out["totals"]["calls"], 2)
        self.assertEqual(out["totals"]["unattributed_calls"], 1)
        self.assertEqual([r["key"] for r in out["rows"]], ["/a/f.py"])
        self.assertTrue(any(d["rule"] == "unattributed" for d in out["diagnostics"]))

    def test_key_cap_truncates(self):
        b = Builder()
        for i in range(5):
            b.assistant([("r%d" % i, "Read", {"file_path": "/f%d.py" % i})]); b.result("r%d" % i, 40)
        b.assistant(); b.assistant()
        b.write(self.root); self.scan()
        old, cd.KEY_TRACK_CAP = cd.KEY_TRACK_CAP, 2
        try:
            out = _get(self.db, "Read", group="target")
        finally:
            cd.KEY_TRACK_CAP = old
        self.assertTrue(out["truncated"])
        self.assertEqual(sum(r["calls"] for r in out["rows"]), 5)
        self.assertTrue(any(r["key"] == cd.OTHER_KEY for r in out["rows"]))
        self.assertFalse(_get(self.db, "Read", group="target")["truncated"])

    def test_target_truncated_to_200(self):
        b = Builder()
        b.assistant([("r0", "Bash", {"command": "x" * 400})]); b.result("r0", 40)
        b.assistant(); b.assistant()
        b.write(self.root); self.scan()
        self.assertEqual(len(_get(self.db, "Bash", group="target")["rows"][0]["key"]), 200)

    def test_parallel_calls_all_appear(self):
        b = Builder()
        for i, p in enumerate(["/p/a.py", "/p/b.py", "/p/c.py"]):
            b._rec("assistant", {"id": "pm1", "model": "m", "content": [
                {"type": "tool_use", "id": "p%d" % i, "name": "Read", "input": {"file_path": p}}],
                "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                          "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}})
        for i in range(3):
            b.result("p%d" % i, 40)
        b.assistant(); b.assistant()
        b.write(self.root); self.scan()
        out = _get(self.db, "Read", group="target")
        self.assertEqual(sorted(r["key"] for r in out["rows"]), ["/p/a.py", "/p/b.py", "/p/c.py"])

    def test_privacy(self):
        b = Builder()
        b.assistant([("r0", "Read", {"file_path": "/a.py"})]); b.result("r0", 400)
        b.assistant(); b.assistant()
        b.write(self.root); self.scan()
        self.assertNotIn("SECRET-BODY", json.dumps(_get(self.db, "Read")))


class EndpointTests(Base):
    def test_endpoint(self):
        b = Builder()
        b.assistant([("t1", "Bash", {"command": "git status"})]); b.result("t1", 400)
        b.assistant(); b.assistant()
        b.write(self.root); self.scan()
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        httpd = http.server.HTTPServer(("127.0.0.1", port), build_handler(self.db, "/nonexistent"))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d/api/tool-costs/params" % port
        try:
            with urllib.request.urlopen(base + "?tool=Bash&limit=5&group=command2") as r:
                out = json.loads(r.read())
            for k in ("tool", "group", "available_groups", "totals", "rows", "repeat",
                      "thresholds", "diagnostics", "truncated", "meta"):
                self.assertIn(k, out)
            for k in ("key", "calls", "share_of_tool_cost", "est_cost_usd", "carry_cost_usd",
                      "cost_per_call", "avg_result_tokens", "max_result_tokens", "error_rate",
                      "sessions", "first_seen", "last_seen", "frequency_per_session",
                      "cumulative_share", "quadrant", "quadrant_label"):
                self.assertIn(k, out["rows"][0])
            self.assertEqual(out["rows"][0]["key"], "git status")
            for q in ("", "?tool=Nope", "?tool=Bash&group=bogus"):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(base + q)
                self.assertEqual(cm.exception.code, 400)
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
