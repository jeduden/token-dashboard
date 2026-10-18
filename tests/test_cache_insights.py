import http.server
import json
import os
import socket
import threading
import urllib.request
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from token_dashboard.cache_insights import cache_report, classify, is_miss
from token_dashboard.db import init_db
from token_dashboard.scanner import scan_dir
from token_dashboard.server import build_handler

PRICING = {
    "models": {"m": {"input": 10.0, "output": 0.0, "cache_read": 1.0,
                     "cache_create_5m": 12.5, "cache_create_1h": 20.0},
               "m2": {"input": 10.0, "output": 0.0, "cache_read": 1.0,
                      "cache_create_5m": 12.5, "cache_create_1h": 20.0}},
    "tier_fallback": {},
}
T0 = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


def ts(sec):
    return (T0 + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class CacheInsightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        self.n = 0

    def add(self, sec, sid="s1", model="m", inp=0, read=0, c5=0, c1=0,
            side=0, agent=None, slug="p", typ="assistant"):
        self.n += 1
        with sqlite3.connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, is_sidechain, agent_id,"
                " timestamp, model, input_tokens, cache_read_tokens, cache_create_5m_tokens,"
                " cache_create_1h_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"u{self.n}", sid, slug, typ, side, agent, ts(sec), model, inp, read, c5, c1))

    def report(self, **kw):
        return cache_report(self.db, PRICING, **kw)

    def causes(self, r):
        return {c["cause"]: c["turns"] for c in r["by_cause"] if c["turns"]}

    def test_cold_start_not_a_miss(self):
        self.add(0, c5=50000)
        r = self.report()
        self.assertEqual(r["summary"]["cold_starts"], 1)
        self.assertEqual(r["summary"]["miss_turns"], 0)
        self.assertEqual(r["summary"]["wasted_usd"], 0)

    def test_ttl_5m_expiry_and_wasted_math(self):
        self.add(0, c5=50000)
        self.add(60, read=50000, c5=500)          # healthy
        self.add(60 + 600, c5=50500)              # 10 min gap -> expired
        r = self.report()
        self.assertEqual(r["summary"]["miss_turns"], 1)
        self.assertEqual(self.causes(r), {"ttl_expired_5m": 1})
        # 50500 tokens * (12.5 - 1.0) $/Mtok
        self.assertAlmostEqual(r["summary"]["wasted_usd"], 50500 * 11.5 / 1e6, places=4)
        self.assertEqual(r["worst_sessions"][0]["dominant_cause"], "ttl_expired_5m")

    def test_ttl_1h(self):
        self.add(0, c1=50000)
        self.add(2000, c1=50000)                  # >5m but <1h with 1h tier: not a TTL expiry
        self.add(2000 + 4000, c1=50000)           # >1h
        r = self.report()
        self.assertEqual(r["summary"]["miss_turns"], 2)
        self.assertEqual(self.causes(r), {"prefix_changed": 1, "ttl_expired_1h": 1})

    def test_model_switch(self):
        self.add(0, c5=50000)
        self.add(30, model="m2", c5=50000)
        self.assertEqual(self.causes(self.report()), {"model_switch": 1})

    def test_prefix_changed_within_ttl(self):
        self.add(0, c5=60000)
        self.add(30, read=5000, c5=55000)
        r = self.report()
        self.assertEqual(self.causes(r), {"prefix_changed": 1})
        # recreated capped at prev_ctx - read = 55000
        self.assertAlmostEqual(r["summary"]["wasted_usd"], 55000 * 11.5 / 1e6, places=4)

    def test_new_content_is_not_waste(self):
        self.add(0, c5=50000)
        self.add(30, read=50000, c5=60000)        # prefix retained, big append
        r = self.report()
        self.assertEqual(self.causes(r), {"new_content": 1})
        self.assertEqual(r["summary"]["wasted_usd"], 0)

    def test_small_writes_are_not_misses(self):
        self.add(0, c5=50000)
        self.add(30, read=100, c5=900)            # create dominates but tiny
        self.assertEqual(self.report()["summary"]["miss_turns"], 0)

    def test_hit_ratio_math(self):
        self.add(0, read=60, c5=20, c1=10, inp=10)
        r = self.report()
        self.assertAlmostEqual(r["summary"]["hit_ratio"], 0.6)
        self.assertEqual(r["summary"]["tokens_read"], 60)
        self.assertEqual(r["summary"]["tokens_created"], 30)
        self.assertEqual(r["by_day"][0]["day"], "2026-04-10")

    def test_empty_db(self):
        r = self.report()
        self.assertIsNone(r["summary"]["hit_ratio"])
        self.assertEqual(r["worst_sessions"], [])

    def test_sidechain_threads_are_separate(self):
        self.add(0, c5=50000)                                  # main cold
        self.add(1, c5=40000, side=1, agent="a1")              # sidechain cold, not a miss
        self.add(2, read=50000, c5=500)                        # main hit
        self.add(3, read=40000, c5=300, side=1, agent="a1")    # sidechain hit
        r = self.report()
        self.assertEqual(r["summary"]["cold_starts"], 2)
        self.assertEqual(r["summary"]["miss_turns"], 0)

    def test_user_rows_and_zero_rows_ignored(self):
        self.add(0, typ="user")
        self.add(1, c5=50000)
        self.add(2)  # zero-token assistant row
        r = self.report()
        self.assertEqual(r["summary"]["turns"], 1)

    def test_since_does_not_create_false_cold_start(self):
        self.add(0, c5=50000)
        self.add(60, read=50000, c5=500)
        self.add(120, c5=50500)                   # miss (model unchanged, read dropped)
        r = self.report(since=ts(100))
        self.assertEqual(r["summary"]["turns"], 1)
        self.assertEqual(r["summary"]["cold_starts"], 0)
        self.assertEqual(r["summary"]["miss_turns"], 1)

    def test_unpriced_model(self):
        self.add(0, model="mystery", c5=50000)
        self.add(30, model="mystery", c5=50000)
        r = self.report()
        self.assertEqual(r["summary"]["miss_turns"], 1)
        self.assertEqual(r["summary"]["unpriced_miss_turns"], 1)
        self.assertEqual(r["summary"]["wasted_usd"], 0)

    def test_worst_sessions_ranked_and_grouped(self):
        for sid, n in (("a", 1), ("b", 3)):
            t = 0
            self.add(t, sid=sid, c5=50000)
            for _ in range(n):
                t += 1000
                self.add(t, sid=sid, c5=50000)
        r = self.report()
        self.assertEqual([w["session_id"] for w in r["worst_sessions"]], ["b", "a"])
        self.assertEqual(r["by_project"][0]["project_slug"], "p")
        self.assertIn("repo", r["by_project"][0])
        self.assertIn("repo_name", r["by_project"][0])
        self.assertIn("repo", r["worst_sessions"][0])
        self.assertEqual(r["by_model"][0]["model"], "m")
        self.assertEqual(r["summary"]["miss_turns"], 4)

    def test_classify_and_is_miss_units(self):
        self.assertEqual(classify(10, "a", "b", "5m", 0, 100), "model_switch")
        self.assertEqual(classify(None, None, "a", None, 0, 0), "unknown")
        self.assertTrue(is_miss(0, 3000, 0))
        self.assertFalse(is_miss(10000, 3000, 0))


class CacheDedupTests(unittest.TestCase):
    def test_streaming_snapshots_counted_once(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "t.db")
        root = os.path.join(tmp, "projects")
        os.makedirs(os.path.join(root, "slug"))
        init_db(db)

        def a(uuid, sec, out):
            return {"type": "assistant", "uuid": uuid, "parentUuid": "u1", "sessionId": "s1",
                    "timestamp": ts(sec), "isSidechain": False,
                    "message": {"id": "msg_X", "model": "m", "content": [],
                                "usage": {"input_tokens": 1, "output_tokens": out,
                                          "cache_read_input_tokens": 0,
                                          "cache_creation": {"ephemeral_5m_input_tokens": 50000,
                                                             "ephemeral_1h_input_tokens": 0}}}}
        with open(os.path.join(root, "slug", "s1.jsonl"), "w") as f:
            for rec in (a("r1", 1, 5), a("r2", 2, 50), a("r3", 3, 300)):
                f.write(json.dumps(rec) + "\n")
        scan_dir(root, db)
        r = cache_report(db, PRICING)
        self.assertEqual(r["summary"]["turns"], 1)
        self.assertEqual(r["summary"]["tokens_created"], 50000)
        self.assertEqual(r["summary"]["miss_turns"], 0)


class CacheEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with sqlite3.connect(self.db) as c:
            for i, (sec, read, c5) in enumerate([(0, 0, 50000), (30, 0, 50000)]):
                c.execute(
                    "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model,"
                    " input_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f"a{i}", "s", "p", "assistant", ts(sec), "claude-haiku-4-5", 0, read, c5, 0))
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        H = build_handler(self.db, projects_dir="/nonexistent")
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _get(self, path):
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read())

    def test_shape(self):
        body = self._get("/api/cache")
        for k in ("summary", "by_cause", "by_day", "by_project", "by_model", "worst_sessions"):
            self.assertIn(k, body)
        for k in ("hit_ratio", "turns", "miss_turns", "cold_starts",
                  "tokens_read", "tokens_created", "wasted_usd"):
            self.assertIn(k, body["summary"])
        self.assertEqual(body["summary"]["miss_turns"], 1)
        self.assertEqual(body["summary"]["cold_starts"], 1)
        self.assertGreater(body["summary"]["wasted_usd"], 0)
        self.assertEqual(body["worst_sessions"][0]["session_id"], "s")

    def test_since_until(self):
        self.assertEqual(self._get("/api/cache?since=2099-01-01T00:00:00Z")["summary"]["turns"], 0)
        self.assertEqual(self._get("/api/cache?until=2000-01-01T00:00:00Z")["summary"]["turns"], 0)


if __name__ == "__main__":
    unittest.main()
