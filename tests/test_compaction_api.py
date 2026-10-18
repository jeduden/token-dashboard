"""/api/compactions: summary, buckets, repo/project roll-ups and the idle-gap what-if.

Five compactions (hand-checked):
  A1 auto   600k -> 10k, idle 900 s   (5m tier)   project proj,  session A
  A2 manual 200k -> 12k, idle 5 s                  project proj,  session A (2 turns after A1)
  B  manual 350k -> 20k, idle 4000 s  (5m tier)   project proj,  session B
  C  auto   100k ->  8k, idle 120 s                project proj2, session C
  D  auto   450k ->  9k, idle 2000 s  (1h tier)   project proj2, session D
Pricing: read $1, 5m write $10 per token, so a write costs $9 more than a read.
"""
import http.server
import json
import os
import socket
import threading
import unittest
import urllib.error
import urllib.request

from token_dashboard import compaction_insights as ki
from token_dashboard.server import build_handler
from tests.compaction_fixture import CSess
from tests.expiry_fixture import ExpiryBase, PRICING


def place(s, root, project):
    s.write(root)
    os.makedirs(os.path.join(root, project), exist_ok=True)
    os.replace(os.path.join(root, "proj", s.sid + ".jsonl"), os.path.join(root, project, s.sid + ".jsonl"))


def session(sid, cwd, idle, trigger, pre, post, tier="5m", dur=30000):
    s = CSess(sid, cwd=cwd)
    w = (lambda n: dict(c1=n)) if tier == "1h" else (lambda n: dict(c5=n))
    s.turn(0, **w(1000))
    s.turn(10, read=1000, **w(100))
    s._rec(10 + idle, "user", {"role": "user", "content": "continue"})
    s.boundary(12 + idle, trigger=trigger, pre=pre, post=post, dur=dur)
    s.summary(13 + idle)
    s.turn(30 + idle, **w(post))
    return s


class ApiBase(ExpiryBase):
    def build(self):
        a = CSess("sa", cwd="/work/a")
        a.turn(0, c5=1000)
        a.turn(10, read=1000, c5=100)
        a._rec(910, "user", {"role": "user", "content": "continue"})
        a.boundary(912, trigger="auto", pre=600000, post=10000)
        a.summary(913)
        a.turn(930, c5=10000)
        a.turn(1000, read=10000, c5=100)                     # the two turns between the compactions
        a._rec(1005, "user", {"role": "user", "content": "again"})
        a.boundary(1006, trigger="manual", pre=200000, post=12000)
        a.summary(1007)
        a.turn(1030, c5=12000)
        place(a, self.root, "proj")
        place(session("sb", "/work/b", 4000, "manual", 350000, 20000), self.root, "proj")
        place(session("sc", "/work/c", 120, "auto", 100000, 8000), self.root, "proj2")
        place(session("sd", "/work/d", 2000, "auto", 450000, 9000, tier="1h"), self.root, "proj2")
        from token_dashboard.scanner import scan_dir
        scan_dir(self.root, self.db)
        ki._cache.clear()

    def report(self, **kw):
        ki._cache.clear()
        return ki.compactions_report(self.db, PRICING, **kw)


class ReportTests(ApiBase):
    def setUp(self):
        super().setUp()
        self.build()
        self.r = self.report()

    def test_summary(self):
        s = self.r["summary"]
        self.assertEqual((s["count"], s["auto"], s["manual"]), (5, 3, 2))
        self.assertEqual(s["median_pre_tokens"], 350000)
        self.assertEqual(s["median_post_tokens"], 10000)
        self.assertEqual(s["p90_pre_tokens"], 600000)
        self.assertEqual(s["median_duration_s"], 30.0)
        self.assertEqual(s["total_dropped_tokens"], 1641000)
        self.assertEqual(s["median_turns_between"], 2)

    def test_by_day(self):
        self.assertEqual(self.r["by_day"], [{"day": "2026-05-01", "auto": 3, "manual": 2, "other": 0, "count": 5}])

    def test_gap_buckets_split_auto_and_manual(self):
        b = {x["key"]: x for x in self.r["gap_buckets"]}
        self.assertEqual(list(b), ["lt5m", "5_10m", "10_30m", "30_60m", "1_2h", "gt2h"])
        got = {k: (v["auto"], v["manual"]) for k, v in b.items()}
        self.assertEqual(got, {"lt5m": (1, 1), "5_10m": (0, 0), "10_30m": (1, 0),
                               "30_60m": (1, 0), "1_2h": (0, 1), "gt2h": (0, 0)})
        self.assertEqual(b["lt5m"]["label"], "<5 min")
        self.assertEqual(self.r["gap_unknown"], 0)

    def test_pre_token_buckets(self):
        b = {x["key"]: (x["count"], x["auto"], x["manual"]) for x in self.r["pre_token_buckets"]}
        self.assertEqual(b, {"lt150k": (1, 1, 0), "150_300k": (1, 0, 1),
                             "300_500k": (2, 1, 1), "gt500k": (1, 1, 0)})

    def test_by_project_and_repo(self):
        p = {x["project_slug"]: x for x in self.r["by_project"]}
        self.assertEqual((p["proj"]["count"], p["proj"]["median_pre_tokens"]), (3, 350000))
        self.assertEqual((p["proj2"]["count"], p["proj2"]["median_pre_tokens"]), (2, 275000))
        self.assertEqual(sum(x["count"] for x in self.r["by_repo"]), 5)
        self.assertTrue(all("repo_name" in x and "repo" in x for x in self.r["by_repo"]))

    def test_worst_sessions(self):
        w = self.r["worst_sessions"]
        self.assertEqual([x["session_id"] for x in w], ["sa", "sd", "sb", "sc"])
        self.assertEqual((w[0]["compactions"], w[0]["max_pre_tokens"]), (2, 600000))
        self.assertIn("project_name", w[0])

    def test_after_expiry(self):
        a = self.r["after_expiry"]
        self.assertEqual((a["count"], a["auto"], a["manual"]), (2, 1, 1))     # A1 and B; D's 1h cache was warm
        self.assertEqual(a["pre_tokens_total"], 950000)
        # upper bound: the cold compaction pays the write rate instead of the read rate on pre tokens
        self.assertAlmostEqual(a["avoidable_usd_upper_bound"], 950000 * 9)
        self.assertEqual(a["unpriced"], 0)
        self.assertIn("upper bound", a["note"].lower())
        self.assertEqual(self.r["thresholds"]["ttl_5m_s"], 300)

    def test_range_filter(self):
        r = self.report(since="2026-05-02T00:00:00Z")
        self.assertEqual(r["summary"]["count"], 0)
        self.assertIsNone(r["summary"]["median_pre_tokens"])
        self.assertEqual(r["after_expiry"]["count"], 0)
        r = self.report(since="2026-05-01T00:16:00Z")           # drops A1 (912 s) and C (132 s)
        self.assertEqual((r["summary"]["count"], r["summary"]["auto"], r["summary"]["manual"]), (3, 1, 2))
        r = self.report(until="2026-05-01T00:16:00Z")
        self.assertEqual(r["summary"]["count"], 2)

    def test_no_prompt_or_summary_text(self):
        blob = json.dumps(self.r)
        for needle in ("SECRET", "continue", "again"):
            self.assertNotIn(needle, blob)

    def test_result_is_cached_briefly(self):
        first = ki.compactions_report(self.db, PRICING)
        self.assertIs(ki.compactions_report(self.db, PRICING), first)


class EmptyTests(ApiBase):
    def test_empty_database(self):
        r = self.report()
        self.assertEqual(r["summary"]["count"], 0)
        self.assertEqual(r["by_day"], [])
        self.assertEqual(len(r["gap_buckets"]), 6)
        self.assertEqual(r["worst_sessions"], [])
        self.assertEqual(r["after_expiry"]["count"], 0)


class UnknownDataTests(ApiBase):
    def test_missing_gap_and_metadata(self):
        s = CSess("sx", cwd="/work/x")
        s.boundary(5, metadata=False)                         # old Claude Code: no metadata, no earlier turn
        place(s, self.root, "proj")
        from token_dashboard.scanner import scan_dir
        scan_dir(self.root, self.db)
        r = self.report()
        self.assertEqual(r["summary"]["count"], 1)
        self.assertEqual((r["summary"]["auto"], r["summary"]["manual"]), (0, 0))
        self.assertEqual(r["gap_unknown"], 1)
        self.assertEqual(r["by_day"][0]["other"], 1)
        self.assertEqual(r["pre_tokens_unknown"], 1)
        self.assertIsNone(r["summary"]["median_pre_tokens"])


class HttpTests(ApiBase):
    def setUp(self):
        super().setUp()
        self.build()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), build_handler(self.db, self.root))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path):
        return json.loads(urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)).read())

    def test_endpoint(self):
        d = self.get("/api/compactions")
        self.assertEqual(d["summary"]["count"], 5)
        d = self.get("/api/compactions?since=2026-05-02T00:00:00Z")
        self.assertEqual(d["summary"]["count"], 0)


if __name__ == "__main__":
    unittest.main()
