import os
import sqlite3
import tempfile
import unittest

from tests.test_whatif import PRICING, M
from token_dashboard.db import init_db, recent_sessions
from token_dashboard.recent_buckets import message_buckets
from token_dashboard.session_costs import add_session_costs


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.db = os.path.join(d, "t.db")
        init_db(self.db)
        self.n = 0
        # s1: opus main + haiku subagent ; s2: opus only (a different day)
        self.msg("s1", "2026-01-01T10:00:00Z", "a-opus", 0, input_tokens=M, output_tokens=M)
        self.msg("s1", "2026-01-01T10:05:00Z", "a-haiku", 1, input_tokens=M)
        self.msg("s2", "2026-01-02T10:00:00Z", "a-opus", 0, input_tokens=M)

    def msg(self, sid, ts, mdl, side, **kw):
        self.n += 1
        cols = ["uuid", "session_id", "project_slug", "type", "timestamp", "model", "is_sidechain"] + list(kw)
        with sqlite3.connect(self.db) as c:
            c.execute(f"INSERT INTO messages ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                      (f"u{self.n}", sid, "p", "assistant", ts, mdl, side, *kw.values()))


class SessionAltTests(Base):
    def rows(self, targets):
        return {s["session_id"]: s for s in add_session_costs(self.db, PRICING, recent_sessions(self.db), targets)}

    def test_without_targets_nothing_is_added(self):
        s = self.rows(None)["s1"]
        self.assertNotIn("alt_costs", s)
        self.assertAlmostEqual(s["cost_usd"], 30 + 2)  # the 1M-token haiku prompt is in the long-context band

    def test_main_sub_and_total_alternatives(self):
        s = self.rows(["openai"])["s1"]
        self.assertAlmostEqual(s["alt_costs_main"]["openai"], 12.0)   # opus -> o-mid
        self.assertAlmostEqual(s["alt_costs_sub"]["openai"], 0.1)     # haiku -> o-small
        self.assertAlmostEqual(s["alt_costs"]["openai"], 12.1)

    def test_missing_peer_only_on_the_subagent_keeps_the_main_cost(self):
        s = self.rows(["kimi"])["s1"]
        self.assertIsNone(s["alt_costs_sub"]["kimi"])
        self.assertAlmostEqual(s["alt_costs"]["kimi"], 12.0)
        self.assertEqual(s["alt_unpriced_tokens"]["kimi"], M)

    def test_session_with_only_unpriceable_usage_is_none(self):
        self.msg("s3", "2026-01-03T10:00:00Z", "a-haiku", 0, input_tokens=M)
        s = self.rows(["kimi"])["s3"]
        self.assertIsNone(s["alt_costs"]["kimi"])


class BucketAltTests(Base):
    def test_costs_unchanged_without_targets(self):
        r = message_buckets(self.db, PRICING, "day")
        self.assertNotIn("alt_costs", r["buckets"][0])
        self.assertAlmostEqual(r["buckets"][0]["cost_usd"], 32.0)

    def test_buckets_cells_and_sessions_carry_alternatives(self):
        r = message_buckets(self.db, PRICING, "day", targets="openai")
        day1, day2 = r["buckets"]
        self.assertAlmostEqual(day1["alt_costs"]["openai"], 12.1)
        self.assertAlmostEqual(day1["sessions"]["s1"]["alt_costs"]["openai"], 12.1)
        self.assertAlmostEqual(day2["alt_costs"]["openai"], 2.0)
        by_sid = {s["session_id"]: s for s in r["sessions"]}
        self.assertAlmostEqual(by_sid["s2"]["alt_costs"]["openai"], 2.0)

    def test_bad_target_raises(self):
        with self.assertRaises(ValueError):
            message_buckets(self.db, PRICING, "day", targets="nope")


if __name__ == "__main__":
    unittest.main()
