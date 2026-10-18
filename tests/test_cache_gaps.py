import os
import sqlite3
import tempfile
import unittest

from token_dashboard.cache_insights import cache_report
from token_dashboard.db import init_db
from tests.test_cache_insights import PRICING, ts


class GapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        self.n = 0

    def add(self, sec, sid="s1", model="m", read=0, c5=0, c1=0):
        self.n += 1
        with sqlite3.connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model,"
                " input_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"u{self.n}", sid, "p", "assistant", ts(sec), model, 0, read, c5, c1))

    def gaps(self, **kw):
        return cache_report(self.db, PRICING, **kw)["gaps"]

    def by_key(self, g):
        return {b["key"]: b for b in g["buckets"]}

    def test_empty(self):
        g = self.gaps()
        self.assertEqual([b["key"] for b in g["buckets"]],
                         ["5_10m", "10_30m", "30_60m", "1_2h", "2_6h", "gt_6h"])
        self.assertTrue(all(b["turns"] == 0 and b["wasted_usd"] == 0 for b in g["buckets"]))
        self.assertEqual(g["by_tier"]["5m"]["turns"], 0)
        self.assertEqual(g["by_tier"]["1h"]["turns"], 0)
        self.assertEqual(g["what_if_1h"]["expiries"], 0)
        self.assertEqual(g["what_if_1h"]["net_usd"], 0)

    def test_buckets_and_tiers(self):
        # session a: 5m write, then expiries after 7 min, 20 min, 45 min, 90 min, 3h, 8h
        t = 0
        self.add(t, "a", c5=50000)
        for gap in (420, 1200, 2700, 5400, 3 * 3600, 8 * 3600):
            t += gap
            self.add(t, "a", c5=50000)
        # session b: 1h write, expires after 2h
        self.add(0, "b", c1=50000)
        self.add(7200, "b", c1=50000)
        g = self.gaps()
        b = self.by_key(g)
        for k in ("5_10m", "10_30m", "30_60m", "1_2h", "2_6h", "gt_6h"):
            exp = 2 if k == "1_2h" else 1
            self.assertEqual(b[k]["turns"], exp, k)
        # 5m expiries: each wastes 50000 * (12.5 - 1.0) / 1e6; ... but the last 5m ones keep tier 5m
        w5 = 50000 * 11.5 / 1e6
        w1 = 50000 * (20.0 - 1.0) / 1e6
        self.assertEqual(g["by_tier"]["5m"]["turns"], 6)
        self.assertEqual(g["by_tier"]["1h"]["turns"], 1)
        self.assertAlmostEqual(g["by_tier"]["5m"]["wasted_usd"], 6 * w5, places=4)
        self.assertAlmostEqual(g["by_tier"]["1h"]["wasted_usd"], w1, places=4)
        self.assertAlmostEqual(b["1_2h"]["wasted_usd"], w5 + w1, places=4)
        self.assertEqual(sum(x["turns"] for x in g["buckets"]), 7)

    def test_buckets_sum_to_ttl_causes_in_by_cause(self):
        self.add(0, c5=50000)
        self.add(600, c5=50000)
        self.add(600 + 4000, c5=50000)
        self.add(600 + 4000 + 100, c5=50000)       # prefix_changed, not an expiry
        r = cache_report(self.db, PRICING)
        ttl = sum(c["turns"] for c in r["by_cause"] if c["cause"].startswith("ttl_"))
        ttl_usd = sum(c["wasted_usd"] for c in r["by_cause"] if c["cause"].startswith("ttl_"))
        self.assertEqual(sum(b["turns"] for b in r["gaps"]["buckets"]), ttl)
        self.assertAlmostEqual(sum(b["wasted_usd"] for b in r["gaps"]["buckets"]), ttl_usd, places=3)

    def test_what_if_1h(self):
        self.add(0, c5=50000)
        self.add(600, c5=50000)                    # 5m expiry, 10 min gap: a 1h cache would have held
        self.add(600 + 7200, c5=50000)             # 5m expiry, 2h gap: even 1h would have expired
        w = self.gaps()["what_if_1h"]
        self.assertEqual(w["expiries"], 1)
        self.assertAlmostEqual(w["recoverable_usd"], 50000 * 11.5 / 1e6, places=4)
        # premium on every 5m token written (3 x 50000) at (20.0 - 12.5) $/Mtok
        self.assertAlmostEqual(w["extra_write_cost_usd"], 150000 * 7.5 / 1e6, places=4)
        self.assertAlmostEqual(w["net_usd"], w["recoverable_usd"] - w["extra_write_cost_usd"], places=4)

    def test_since_filters_gaps(self):
        self.add(0, c5=50000)
        self.add(600, c5=50000)
        self.add(5000, c5=50000)
        g = self.gaps(since=ts(2000))
        self.assertEqual(sum(b["turns"] for b in g["buckets"]), 1)


if __name__ == "__main__":
    unittest.main()
