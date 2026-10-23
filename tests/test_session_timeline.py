import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db
from token_dashboard.pricing import load_pricing
from token_dashboard.session_timeline import session_timeline
from token_dashboard.session_costs import add_turn_costs, add_session_costs
from token_dashboard.db import recent_sessions, session_turns
from token_dashboard.server import PRICING_JSON


def _a(c, uuid, ts, inp, out, cread, ccre, tools=None, side=0):
    c.execute(
        "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model, is_sidechain,"
        " input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,"
        " tool_calls_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)",
        (uuid, "S", "p", "assistant", ts, "claude-haiku-4-5", side, inp, out, cread, ccre, tools))


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "t.db")
        init_db(self.db)
        self.pricing = load_pricing(PRICING_JSON)
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, prompt_text, prompt_chars)"
                      " VALUES ('u','S','p','user','2026-01-01T00:00:00Z','do it',5)")
            _a(c, "a1", "2026-01-01T00:00:01Z", 10, 100, 0, 1000, '[{"name":"Read"}]')
            _a(c, "a2", "2026-01-01T00:00:09Z", 5, 50, 1000, 0)
            _a(c, "a3", "2026-01-01T00:00:10Z", 5, 50, 0, 0, side=1)
            c.execute("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, is_error, timestamp)"
                      " VALUES ('a1','S','p','Read','big.py',900,0,'2026-01-01T00:00:02Z')")
            c.execute("INSERT INTO compactions (uuid, session_id, project_slug, timestamp, trigger, pre_tokens, post_tokens)"
                      " VALUES ('c','S','p','2026-01-01T00:00:11Z','auto',1000,100)")
            c.commit()

    def test_timeline(self):
        r = session_timeline(self.db, self.pricing, "S")
        self.assertEqual([t["kind"] for t in r["turns"]], ["tools", "text", "subagent"])
        self.assertEqual(r["turns"][0]["context_tokens"], 1010)
        self.assertEqual(len(r["prompts"]), 1)
        self.assertEqual(r["compactions"][0]["trigger"], "auto")
        self.assertAlmostEqual(r["turns"][-1]["cum_cost_usd"], r["summary"]["cost_usd"], places=5)
        self.assertAlmostEqual(sum(r["summary"]["by_token_type"].values()), r["summary"]["cost_usd"], places=5)
        self.assertEqual(r["summary"]["by_kind"]["tools"]["turns"], 1)

    def test_caused_by_prompt_and_previous_tool_results(self):
        t = session_timeline(self.db, self.pricing, "S")["turns"]
        self.assertEqual([p["text"] for p in t[0]["caused"]["prompts"]], ["do it"])
        self.assertEqual(t[0]["context_delta"], 1010)
        self.assertEqual(t[0]["caused"]["tool_results"], [])
        # turn 2 follows the Read from turn 1: its result is what grew the context
        self.assertEqual(t[1]["caused"]["tool_results"][0]["target"], "big.py")
        self.assertEqual(t[1]["caused"]["prompts"], [])
        self.assertEqual(t[1]["context_delta"], 1005 - 1010)
        # the sidechain turn is its own thread: no prompt, no main-thread delta
        self.assertEqual(t[2]["caused"]["prompts"], [])
        self.assertEqual(t[2]["context_delta"], 5)

    def test_session_and_turn_costs_agree(self):
        total = session_timeline(self.db, self.pricing, "S")["summary"]["cost_usd"]
        turns = add_turn_costs(session_turns(self.db, "S"), self.pricing)
        self.assertIsNone(turns[0]["cost_usd"])  # the user prompt row
        self.assertAlmostEqual(sum(t["cost_usd"] or 0 for t in turns), total, places=5)
        rows = add_session_costs(self.db, self.pricing, recent_sessions(self.db))
        self.assertAlmostEqual(rows[0]["cost_usd"], total, places=5)

    def test_unknown_session_is_empty(self):
        r = session_timeline(self.db, self.pricing, "nope")
        self.assertEqual(r["turns"], [])


if __name__ == "__main__":
    unittest.main()
