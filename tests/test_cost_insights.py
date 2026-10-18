import http.server
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request

from token_dashboard.db import init_db, SCHEMA
from token_dashboard.scanner import scan_dir, parse_record
from token_dashboard import cost_insights as ci
from token_dashboard import cost_diagnostics as dx
from token_dashboard.server import build_handler

# $1 per cached-read token, $10 per cache-create token: keeps the math integral.
PRICING = {"models": {"m": {"input": 0, "output": 0, "cache_read": 1_000_000,
                            "cache_create_5m": 10_000_000, "cache_create_1h": 10_000_000}},
           "tier_fallback": {}}


def _ts(i):
    return "2026-05-01T00:00:%02dZ" % i


class Builder:
    """Writes a synthetic one-session JSONL transcript."""

    def __init__(self, session="s1"):
        self.session, self.lines, self.n, self.last = session, [], 0, None

    def _rec(self, typ, message, **extra):
        self.n += 1
        rec = {"type": typ, "uuid": "%s-u%d" % (self.session, self.n), "parentUuid": self.last, "sessionId": self.session,
               "timestamp": _ts(self.n), "message": message, **extra}
        self.last = rec["uuid"]
        self.lines.append(json.dumps(rec))

    def assistant(self, tools=(), cr=0, cc=0):
        content = [{"type": "tool_use", "id": i, "name": n, "input": inp} for i, n, inp in tools]
        self._rec("assistant", {"id": "%s-msg%d" % (self.session, self.n + 1), "model": "m", "content": content or [{"type": "text", "text": "x"}],
                                "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": cr,
                                          "cache_creation": {"ephemeral_5m_input_tokens": cc, "ephemeral_1h_input_tokens": 0}}})

    def result(self, tid, chars, err=False, body="SECRET-BODY"):
        self._rec("user", {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": (body * chars)[:chars], "is_error": err}]})

    def write(self, root, name=None):
        d = os.path.join(root, "proj")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, (name or self.session) + ".jsonl"), "w") as f:
            f.write("\n".join(self.lines) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "projects")
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def scan(self):
        ci._cache.clear()
        scan_dir(self.root, self.db)

    def basic(self, cr3=0, cr4=0):
        b = Builder()
        b.assistant([("t1", "Read", {"file_path": "/a/f.py"})])
        b.result("t1", 4000)                       # 1000 tokens
        b.assistant(cc=1000)
        b.assistant(cr=cr3)
        b.assistant(cr=cr4)
        b.write(self.root)
        self.scan()


class LinkingTests(Base):
    def test_parse_links_use_and_result(self):
        _, tools = parse_record({"type": "assistant", "uuid": "a", "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]}}, "p")
        self.assertEqual(tools[0]["tool_use_id"], "toolu_1")
        _, res = parse_record({"type": "user", "uuid": "b", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "x" * 40, "is_error": True}]}}, "p")
        self.assertEqual((res[0]["tool_use_id"], res[0]["result_chars"], res[0]["is_error"]), ("toolu_1", 40, 1))

    def test_no_result_text_stored(self):
        self.basic()
        with sqlite3.connect(self.db) as c:
            dump = "\n".join(str(r) for t in ("tool_calls", "messages") for r in c.execute("SELECT * FROM " + t))
        self.assertNotIn("SECRET-BODY", dump)

    def test_migration_idempotent_and_clears(self):
        p = os.path.join(self.tmp, "old.db")
        c = sqlite3.connect(p)
        c.executescript(SCHEMA)
        c.execute("DROP INDEX idx_tools_use_id")
        c.execute("ALTER TABLE tool_calls DROP COLUMN tool_use_id")
        c.execute("ALTER TABLE tool_calls DROP COLUMN result_chars")
        c.execute("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, timestamp) VALUES ('x','s','p','Read','t')")
        c.commit(); c.close()
        init_db(p); init_db(p)
        with sqlite3.connect(p) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(tool_calls)")}
            self.assertTrue({"tool_use_id", "result_chars"} <= cols)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0], 0)


class CostMathTests(Base):
    def test_create_plus_carry(self):
        self.basic()
        r = ci.tool_costs(self.db, PRICING, catalog={})["tools"]
        read = [t for t in r if t["tool_name"] == "Read"][0]
        # create: 1000*10 at turn 2; carry: 1000*1 at each of turns 3 and 4
        self.assertAlmostEqual(read["est_cost_usd"], 12000)
        self.assertAlmostEqual(read["carry_cost_usd"], 2000)
        self.assertEqual(read["result_tokens_total"], 1000)
        self.assertEqual(read["top_targets"][0]["target"], "/a/f.py")

    def test_compaction_stops_carry(self):
        self.basic(cr3=50000, cr4=10000)   # sharp drop at last turn
        read = ci.tool_costs(self.db, PRICING, catalog={})["tools"][0]
        self.assertAlmostEqual(read["carry_cost_usd"], 1000)   # only turn 3 carries

    def test_unattributed_fallback(self):
        self.basic()
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE tool_calls SET tool_use_id=NULL")
            c.commit()
        out = ci.tool_costs(self.db, PRICING, catalog={})
        self.assertEqual(out["meta"]["unattributed_calls"], 1)
        self.assertEqual(out["tools"][0]["est_cost_usd"], 0)
        self.assertTrue(any(d["rule"] == "unattributed" for d in out["diagnostics"]))

    def test_error_rate_diagnostic(self):
        b = Builder()
        for i in range(6):
            b.assistant([("e%d" % i, "Bash", {"command": "false"})])
            b.result("e%d" % i, 40, err=i < 3)
        b.assistant()
        b.write(self.root); self.scan()
        out = ci.tool_costs(self.db, PRICING, catalog={})
        self.assertEqual(out["tools"][0]["error_rate"], 0.5)
        self.assertTrue(any(d["rule"] == "error_rate" for d in out["diagnostics"]))

    def test_big_output_and_dominant_call(self):
        b = Builder()
        b.assistant([("t1", "Bash", {"command": "cat big"})])
        b.result("t1", 4 * (dx.BIG_OUTPUT_TOKENS + 100))
        b.assistant(cc=dx.BIG_OUTPUT_TOKENS + 100)
        b.assistant()
        b.write(self.root); self.scan()
        out = ci.tool_costs(self.db, PRICING, catalog={})
        self.assertTrue(any(d["rule"] == "big_output" for d in out["diagnostics"]))
        calls = ci.top_calls(self.db, PRICING, tool="Bash", catalog={})["calls"]
        rules = {d["rule"] for d in calls[0]["diagnostics"]}
        self.assertTrue({"big_output", "dominant_call"} <= rules)

    def test_reread_rule(self):
        b = Builder()
        for i in range(3):
            b.assistant([("r%d" % i, "Read", {"file_path": "/same.py"})])
            b.result("r%d" % i, 40)
        b.assistant()
        b.write(self.root); self.scan()
        out = ci.tool_costs(self.db, PRICING, catalog={})
        rr = [d for d in out["diagnostics"] if d["rule"] == "reread"][0]
        self.assertEqual(rr["evidence"]["examples"][0]["count"], 3)
        calls = ci.top_calls(self.db, PRICING, tool="Read", catalog={})["calls"]
        self.assertTrue(any(f["rule"] == "reread" for c in calls for f in c["diagnostics"]))


class SkillTests(Base):
    def _skill_sessions(self, n):
        for i in range(n):
            b = Builder("sk%d" % i)
            b.assistant([("k1", "Skill", {"skill": "demo"})])
            b.result("k1", 40)
            b.assistant(cc=3000)
            for _ in range(60):
                b.assistant()
            b.write(self.root)
        self.scan()

    def test_estimated_from_cache_create_jump(self):
        self._skill_sessions(1)
        s = ci.skill_costs(self.db, PRICING, catalog={})["skills"][0]
        self.assertTrue(s["estimated"])
        self.assertEqual(s["est_load_tokens"], 3000 - 10)  # jump minus ack tokens
        self.assertGreater(s["carry_cost_usd"], 0)

    def test_catalog_tokens_and_carry_diagnostic(self):
        self._skill_sessions(3)
        out = ci.skill_costs(self.db, PRICING, catalog={"demo": {"tokens": 500}})
        s = out["skills"][0]
        self.assertFalse(s["estimated"])
        self.assertEqual((s["invocations"], s["sessions"], s["est_load_tokens"]), (3, 3, 500))
        self.assertAlmostEqual(s["est_cost_usd"], 3 * (500 * 10 + 500 * 60))
        self.assertAlmostEqual(s["cost_per_invocation"], 35000)
        self.assertTrue(any(d["rule"] == "skill_carry" for d in out["diagnostics"]))


class EndpointTests(Base):
    def test_endpoints_shape(self):
        self.basic()
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        httpd = http.server.HTTPServer(("127.0.0.1", port), build_handler(self.db, "/nonexistent"))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def get(p):
                with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, p)) as r:
                    return json.loads(r.read())
            tc = get("/api/tool-costs")
            self.assertIn("tools", tc); self.assertIn("diagnostics", tc); self.assertIn("meta", tc)
            self.assertIn("skills", get("/api/skill-costs"))
            calls = get("/api/tool-costs/calls?tool=Read&limit=5")["calls"]
            self.assertEqual(calls[0]["tool_name"], "Read")
            self.assertIn("diagnostics", calls[0])
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
