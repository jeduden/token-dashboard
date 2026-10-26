import unittest

from tests.test_alt_costs import Base
from tests.test_whatif import PRICING, M
from token_dashboard.db import session_turns
from token_dashboard.recent_feed import recent_feed
from token_dashboard.session_costs import add_turn_costs
from token_dashboard.session_subagents import session_subagents
from token_dashboard.session_timeline import session_timeline


class RowAltTests(Base):
    def test_turn_costs(self):
        rows = add_turn_costs(session_turns(self.db, "s1"), PRICING, ["openai", "kimi"])
        opus, haiku = rows[0], rows[1]
        self.assertAlmostEqual(opus["alt_costs"]["openai"], 12.0)
        self.assertAlmostEqual(opus["alt_costs"]["kimi"], 12.0)
        self.assertAlmostEqual(haiku["alt_costs"]["openai"], 0.1)
        self.assertIsNone(haiku["alt_costs"]["kimi"])

    def test_turn_costs_without_targets_add_nothing(self):
        self.assertNotIn("alt_costs", add_turn_costs(session_turns(self.db, "s1"), PRICING)[0])

    def test_recent_feed_messages_and_sessions(self):
        r = recent_feed(self.db, PRICING, targets="openai")
        by_uuid = {m["uuid"]: m for m in r["messages"]}
        self.assertAlmostEqual(by_uuid["u1"]["alt_costs"]["openai"], 12.0)
        by_sid = {s["session_id"]: s for s in r["sessions"]}
        self.assertAlmostEqual(by_sid["s1"]["alt_costs"]["openai"], 12.1)
        self.assertAlmostEqual(by_sid["s2"]["alt_costs"]["openai"], 2.0)

    def test_recent_feed_default_is_unchanged(self):
        r = recent_feed(self.db, PRICING)
        self.assertNotIn("alt_costs", r["messages"][0])
        self.assertEqual(r["targets"], [])

    def test_timeline_turn_summary_and_models(self):
        r = session_timeline(self.db, PRICING, "s1", targets="openai,kimi")
        self.assertAlmostEqual(r["turns"][0]["alt_costs"]["openai"], 12.0)
        s = r["summary"]
        self.assertAlmostEqual(s["alt_costs"]["openai"], 12.1)
        self.assertAlmostEqual(s["alt_costs"]["kimi"], 12.0)
        self.assertEqual(s["alt_unpriced_tokens"]["kimi"], M)
        self.assertAlmostEqual(s["by_model"]["a-opus"]["alt_costs"]["openai"], 12.0)
        self.assertIsNone(s["by_model"]["a-haiku"]["alt_costs"]["kimi"])

    def test_timeline_default_is_unchanged(self):
        r = session_timeline(self.db, PRICING, "s1")
        self.assertNotIn("alt_costs", r["turns"][0])
        self.assertNotIn("alt_costs", r["summary"])

    def test_subagents(self):
        self.msg("s1", "2026-01-01T10:10:00Z", "a-haiku", 1, input_tokens=M, agent_id="ag1")
        self.msg("s1", "2026-01-01T10:11:00Z", "a-opus", 1, input_tokens=M, agent_id="ag2")
        agents = {a["agent_id"]: a for a in session_subagents(self.db, PRICING, "s1", ["openai", "kimi"])}
        self.assertAlmostEqual(agents["ag1"]["alt_costs"]["openai"], 0.1)
        self.assertIsNone(agents["ag1"]["alt_costs"]["kimi"])
        self.assertAlmostEqual(agents["ag2"]["alt_costs"]["kimi"], 2.0)
        self.assertNotIn("alt_costs", session_subagents(self.db, PRICING, "s1")[0])


if __name__ == "__main__":
    unittest.main()
