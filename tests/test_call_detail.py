"""Single-call drill-down (call_detail.call_detail + /api/tool-costs/call).

Pricing from test_cost_insights: $1 per cache-read token, $10 per cache-create token.
A result of N tokens costs N*10 (create, next turn) + N*1 per later turn.
"""
import http.server
import json
import socket
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request

from token_dashboard import call_detail as cdx
from token_dashboard import cost_insights as ci
from token_dashboard.server import build_handler
from tests.test_cost_insights import Base, Builder, PRICING


class SideBuilder(Builder):
    """Builder whose records are marked as a sidechain (subagent) thread."""

    def __init__(self, session="s1", agent="ag1"):
        super().__init__(session)
        self.agent = agent

    def _rec(self, typ, message, **extra):
        super()._rec(typ, message, isSidechain=True, agentId=self.agent, **extra)


class DetailBase(Base):
    def ids(self, tool=None):
        q = "SELECT id, tool_name, target FROM tool_calls WHERE tool_name != '_tool_result'"
        with sqlite3.connect(self.db) as c:
            rows = c.execute(q + " ORDER BY id").fetchall()
        return [r[0] for r in rows if tool is None or r[1] == tool]

    def id_of(self, session, target):
        with sqlite3.connect(self.db) as c:
            return c.execute("SELECT id FROM tool_calls WHERE session_id=? AND target=? "
                             "AND tool_name != '_tool_result' ORDER BY id", (session, target)).fetchone()[0]

    def detail(self, call_id, **kw):
        cdx._cache.clear()
        ci._cache.clear()
        return cdx.call_detail(self.db, PRICING, call_id, catalog={}, **kw)


class CostTests(DetailBase):
    def test_totals_agree_with_calls_endpoint(self):
        self.basic()
        top = ci.top_calls(self.db, PRICING, tool="Read", catalog={})["calls"][0]
        d = self.detail(top["call_id"])
        self.assertEqual(top["tool_use_id"], "t1")
        self.assertAlmostEqual(d["cost"]["est_cost_usd"], top["est_cost_usd"])
        self.assertAlmostEqual(d["cost"]["carry_cost_usd"], top["carry_cost_usd"])
        self.assertAlmostEqual(d["cost"]["create_cost_usd"], 10000)
        self.assertAlmostEqual(d["cost"]["carry_cost_usd"], 2000)
        self.assertEqual(d["identity"]["tool_use_id"], "t1")
        self.assertEqual(d["identity"]["result_tokens"], 1000)
        self.assertEqual(d["identity"]["result_chars"], 4000)

    def test_carry_timeline_sums_to_carry(self):
        self.basic()
        d = self.detail(self.ids()[0])
        tl = d["carry_timeline"]
        self.assertEqual([t["turn_offset"] for t in tl], [2, 3])
        self.assertAlmostEqual(sum(t["cost_usd"] for t in tl), d["cost"]["carry_cost_usd"])
        self.assertAlmostEqual(tl[-1]["cumulative_usd"], d["cost"]["est_cost_usd"])
        self.assertAlmostEqual(d["create_turn"]["cost_usd"], 10000)
        self.assertEqual(tl[0]["model"], "m")
        self.assertTrue(tl[0]["timestamp"])
        self.assertEqual(d["carry_end"]["reason"], "session_end")

    def test_compaction_cutoff_reason(self):
        self.basic(cr3=50000, cr4=10000)
        d = self.detail(self.ids()[0])
        self.assertEqual(len(d["carry_timeline"]), 1)
        self.assertAlmostEqual(d["cost"]["carry_cost_usd"], 1000)
        self.assertEqual(d["carry_end"]["reason"], "compaction_detected")
        self.assertEqual(d["carry_end"]["turn_offset"], 3)

    def test_ongoing_vs_session_end(self):
        self.basic()
        cid = self.ids()[0]
        self.assertEqual(self.detail(cid, now="2026-05-01T00:05:00Z")["carry_end"]["reason"], "ongoing")
        self.assertEqual(self.detail(cid, now="2026-05-02T00:00:00Z")["carry_end"]["reason"], "session_end")

    def test_unattributed_call_is_graceful(self):
        self.basic()
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE tool_calls SET tool_use_id=NULL")
            c.commit()
        d = self.detail(self.ids()[0])
        self.assertFalse(d["attributed"])
        self.assertEqual(d["cost"]["est_cost_usd"], 0)
        self.assertEqual(d["carry_timeline"], [])
        self.assertIsNone(d["cost"]["rank"])
        self.assertEqual(d["carry_end"]["reason"], "not_attributed")
        self.assertTrue(any(x["rule"] == "unattributed" for x in d["diagnostics"]))


class ContextTests(DetailBase):
    def _parallel(self):
        b = Builder()
        b.assistant([("a", "Read", {"file_path": "/a"})]); b.result("a", 400)
        b.assistant([("p1", "Read", {"file_path": "/x"}), ("p2", "Bash", {"command": "ls"}),
                     ("p3", "Grep", {"pattern": "q"})])
        b.result("p1", 4000); b.result("p2", 400); b.result("p3", 40)
        b.assistant([("n1", "Glob", {"pattern": "*.py"})]); b.result("n1", 40)
        b.assistant(); b.assistant()
        b.write(self.root); self.scan()

    def test_siblings_and_neighbors(self):
        self._parallel()
        ids = {t: i for i, t in zip(self.ids(), ["a", "p1", "p2", "p3", "n1"])}
        d = self.detail(ids["p1"])
        sib = {s["call_id"]: s for s in d["siblings"]}
        self.assertEqual(set(sib), {ids["p2"], ids["p3"]})
        self.assertEqual(sib[ids["p2"]]["tool_name"], "Bash")
        self.assertEqual(sib[ids["p2"]]["result_tokens"], 100)
        self.assertIn("est_cost_usd", sib[ids["p2"]])
        self.assertEqual([n["call_id"] for n in d["previous"]], [ids["a"]])
        self.assertEqual([n["call_id"] for n in d["next"]], [ids["n1"]])
        self.assertNotIn(d["identity"]["call_id"], sib)

    def test_same_target_stats_rank_percentile(self):
        b = Builder()
        for i, chars in enumerate([400, 4000, 40, 4000]):
            b.assistant([("r%d" % i, "Read", {"file_path": "/same.py"})]); b.result("r%d" % i, chars)
        b.assistant([("o", "Read", {"file_path": "/other.py"})]); b.result("o", 40000)
        b.assistant(); b.assistant()
        b.write(self.root)
        b2 = Builder("s2")
        b2.assistant([("q", "Read", {"file_path": "/same.py"})]); b2.result("q", 8000)
        b2.assistant(); b2.assistant()
        b2.write(self.root); self.scan()
        with sqlite3.connect(self.db) as c:
            ids = [r[0] for r in c.execute(
                "SELECT id FROM tool_calls WHERE tool_name='Read' ORDER BY session_id, id")]
        d = self.detail(ids[1])                     # 1000-token read of /same.py in s1
        st = d["same_target"]
        self.assertEqual(st["other_calls"], 4)
        self.assertEqual(st["in_this_session"], 3)
        self.assertEqual(len(st["top"]), 4)
        self.assertTrue(all(t["call_id"] != ids[1] for t in st["top"]))
        self.assertEqual(st["top"][0]["call_id"], ids[5])   # s2's 2000-token read costs most
        self.assertAlmostEqual(st["other_cost_usd"], sum(t["est_cost_usd"] for t in st["top"]))
        self.assertTrue(st["first_seen"] <= st["last_seen"])
        c = d["cost"]
        self.assertEqual(c["rank_of"], 6)
        self.assertEqual(c["rank"], 3)              # /other.py (10000 tok), s2 (2000) cost more
        self.assertAlmostEqual(c["percentile"], 100 * (6 - 3 + 1) / 6, places=1)
        self.assertEqual(self.detail(ids[4])["cost"]["rank"], 1)
        self.assertGreater(c["share_of_session_cost"], 0)
        self.assertLess(c["share_of_session_cost"], 1)

    def test_sidechain_thread_isolation(self):
        main = Builder()
        main.assistant([("m1", "Read", {"file_path": "/m"})]); main.result("m1", 400)
        main.assistant(); main.assistant()
        main.write(self.root)
        side = SideBuilder()
        side.n = 20
        side.assistant([("s1", "Read", {"file_path": "/s"})]); side.result("s1", 400)
        side.assistant(cc=100); side.assistant()
        side.write(self.root, "s1x")
        self.scan()
        dm = self.detail(self.id_of("s1", "/m"))
        self.assertEqual(dm["identity"]["thread"], {"agent_id": None, "is_sidechain": False})
        self.assertEqual(len(dm["carry_timeline"]), 1)
        ds = self.detail(self.id_of("s1", "/s"))
        self.assertEqual(ds["identity"]["thread"]["agent_id"], "ag1")
        self.assertTrue(ds["identity"]["thread"]["is_sidechain"])
        self.assertEqual(ds["siblings"], [])
        self.assertEqual(ds["previous"], [])
        self.assertEqual(ds["next"], [])
        self.assertEqual(ds["session_link"], "s1")

    def test_identity_project_and_diagnostics(self):
        b = Builder()
        for i in range(3):
            b.assistant([("r%d" % i, "Read", {"file_path": "/same.py"})]); b.result("r%d" % i, 40)
        b.assistant(); b.write(self.root); self.scan()
        d = self.detail(self.ids()[2])
        idn = d["identity"]
        self.assertEqual(idn["tool_name"], "Read")
        self.assertEqual(idn["target"], "/same.py")
        self.assertEqual(idn["session_id"], "s1")
        self.assertEqual(idn["project_slug"], "proj")
        for k in ("project", "repo", "repo_name", "message_uuid", "timestamp", "is_error"):
            self.assertIn(k, idn)
        rr = [x for x in d["diagnostics"] if x["rule"] == "reread"][0]
        self.assertEqual(rr["severity"], "warn")
        self.assertTrue(rr["suggestion"])
        self.assertEqual(d["session_link"], "s1")


class PrivacyTests(DetailBase):
    def test_no_result_or_prompt_text(self):
        self.basic()
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE messages SET prompt_text='PROMPT-SECRET'")
            c.commit()
        d = self.detail(self.ids()[0])
        self.assertNotIn("SECRET", json.dumps(d))
        self.assertNotIn("prompt_text", json.dumps(d))


class EndpointTests(DetailBase):
    def test_endpoint_and_errors(self):
        self.basic()
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        httpd = http.server.HTTPServer(("127.0.0.1", port), build_handler(self.db, "/nonexistent"))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d/api/tool-costs/call" % port
        try:
            with urllib.request.urlopen("%s?id=%d" % (base, self.ids()[0])) as r:
                out = json.loads(r.read())
            for k in ("identity", "cost", "carry_timeline", "carry_end", "siblings", "previous",
                      "next", "same_target", "diagnostics", "session_link", "meta"):
                self.assertIn(k, out)
            for q, code in (("", 400), ("?id=abc", 400), ("?id=", 400), ("?id=1.5", 400),
                            ("?id=999999", 404), ("?id=-3", 404)):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(base + q)
                self.assertEqual(cm.exception.code, code, q)
                self.assertIn("error", json.loads(cm.exception.read()))
        finally:
            httpd.shutdown()

    def test_tool_result_rows_are_not_calls(self):
        self.basic()
        with sqlite3.connect(self.db) as c:
            rid = c.execute("SELECT id FROM tool_calls WHERE tool_name='_tool_result'").fetchone()[0]
        with self.assertRaises(cdx.NotFound):
            self.detail(rid)


if __name__ == "__main__":
    unittest.main()
