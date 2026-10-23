import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db
from token_dashboard.pricing import load_pricing
from token_dashboard.recent_feed import recent_feed
from token_dashboard.server import PRICING_JSON


def _asst(c, uuid, sid, ts, inp, out, cread, tools=None):
    c.execute(
        "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model,"
        " input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens,"
        " cache_create_1h_tokens, tool_calls_json) VALUES (?,?,?,?,?,?,?,?,?,0,0,?)",
        (uuid, sid, "p", "assistant", ts, "claude-haiku-4-5", inp, out, cread, tools))


class RecentFeedTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "t.db")
        init_db(self.db)
        self.pricing = load_pricing(PRICING_JSON)
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, prompt_text)"
                      " VALUES ('u1','A','p','user','2026-01-01T00:00:00Z','hello')")
            _asst(c, 'a1', 'A', '2026-01-01T00:00:01Z', 10, 5, 90, '[{"name":"Read"}]')
            _asst(c, 'b1', 'B', '2026-03-01T00:00:00Z', 100, 50, 0)
            _asst(c, 'b2', 'B', '2026-03-01T00:00:05Z', 20, 10, 200)
            c.commit()

    def test_combined_newest_first_across_sessions(self):
        r = recent_feed(self.db, self.pricing, limit=10)
        self.assertEqual([m["uuid"] for m in r["messages"]], ["b2", "b1", "a1", "u1"])
        self.assertEqual(r["messages"][2]["tools"], ["Read"])

    def test_limit_applies_to_whole_feed(self):
        r = recent_feed(self.db, self.pricing, limit=2)
        self.assertEqual({s["session_id"] for s in r["sessions"]}, {"B"})

    def test_session_summary(self):
        r = recent_feed(self.db, self.pricing, limit=10)
        b = next(s for s in r["sessions"] if s["session_id"] == "B")
        self.assertEqual(b["assistant_turns"], 2)
        self.assertEqual(b["context_now"], 220)
        self.assertEqual(b["series_context"], [100, 220])
        self.assertAlmostEqual(b["cache_hit"], 200 / 320)

    def test_session_filter_is_bound(self):
        r = recent_feed(self.db, self.pricing, sessions=["A", "x' OR '1'='1"])
        self.assertEqual({m["session_id"] for m in r["messages"]}, {"A"})


if __name__ == "__main__":
    unittest.main()
