import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db
from token_dashboard.pricing import load_pricing
from token_dashboard.recent_buckets import message_buckets
from token_dashboard.server import PRICING_JSON


def _ins(c, uuid, sid, ts, typ="assistant", inp=10, out=5):
    c.execute(
        "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model,"
        " input_tokens, output_tokens) VALUES (?,?,?,?,?,?,?,?)",
        (uuid, sid, "p", typ, ts, "claude-haiku-4-5" if typ == "assistant" else None, inp, out))


class BucketTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "t.db")
        init_db(self.db)
        self.pricing = load_pricing(PRICING_JSON)
        with sqlite3.connect(self.db) as c:
            _ins(c, "1", "A", "2026-01-01T10:00:00Z")
            _ins(c, "2", "A", "2026-01-01T10:30:00Z", typ="user", inp=0, out=0)
            _ins(c, "3", "B", "2026-01-01T12:00:00Z")
            _ins(c, "4", "B", "2026-01-04T09:00:00Z")
            c.commit()

    def test_day_buckets_fill_gaps_and_split_by_session(self):
        r = message_buckets(self.db, self.pricing, "day")
        self.assertEqual([b["t"] for b in r["buckets"]],
                         ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"])
        d1 = r["buckets"][0]
        self.assertEqual(d1["messages"], 3)
        self.assertEqual(set(d1["sessions"]), {"A", "B"})
        self.assertEqual(r["buckets"][1]["messages"], 0)

    def test_hour_buckets(self):
        r = message_buckets(self.db, self.pricing, "hour", until="2026-01-02")
        self.assertEqual([b["t"] for b in r["buckets"]],
                         ["2026-01-01T10", "2026-01-01T11", "2026-01-01T12"])
        self.assertEqual(r["buckets"][0]["sessions"]["A"]["messages"], 2)

    def test_session_totals_sorted_by_cost(self):
        r = message_buckets(self.db, self.pricing, "day")
        self.assertEqual({s["session_id"] for s in r["sessions"]}, {"A", "B"})
        b = next(s for s in r["sessions"] if s["session_id"] == "B")
        self.assertEqual(b["messages"], 2)

    def test_bad_bucket_and_too_many_hours(self):
        with self.assertRaises(ValueError):
            message_buckets(self.db, self.pricing, "week")
        with sqlite3.connect(self.db) as c:
            _ins(c, "5", "A", "2027-01-01T00:00:00Z"); c.commit()
        with self.assertRaises(ValueError):
            message_buckets(self.db, self.pricing, "hour")


if __name__ == "__main__":
    unittest.main()
