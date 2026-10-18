"""Cache-expiry component of the cost model (create + carry + expiry)."""
from token_dashboard import cost_insights as ci
from tests.expiry_fixture import ExpiryBase, PRICING, Sess, one_result_session


class ExpiryCostTests(ExpiryBase):
    def one(self, sess, tool="Read"):
        self.scan(sess)
        calls = ci.top_calls(self.db, PRICING, tool=tool, catalog={})["calls"]
        return calls[0]

    def test_no_expiry_unchanged(self):
        c = self.one(one_result_session([20, 20]))
        self.assertAlmostEqual(c["create_cost_usd"], 10000)
        self.assertAlmostEqual(c["carry_cost_usd"], 2000)
        self.assertEqual(c["expiry_cost_usd"], 0)
        self.assertEqual(c["expiry_events"], 0)
        self.assertAlmostEqual(c["est_cost_usd"], 12000)

    def test_one_5m_expiry(self):
        # turns after the consuming one: +20s (read), +600s idle (expiry), +20s (read)
        c = self.one(one_result_session([20, 600, 20]))
        self.assertAlmostEqual(c["create_cost_usd"], 10000)       # 1000 tok x $10
        self.assertAlmostEqual(c["carry_cost_usd"], 2000)         # two read turns x 1000 x $1
        self.assertAlmostEqual(c["expiry_cost_usd"], 10000)       # re-written once at the 5m rate
        self.assertEqual(c["expiry_events"], 1)
        self.assertAlmostEqual(c["expiry_gap_total_s"], 600)
        self.assertAlmostEqual(c["est_cost_usd"], 22000)
        self.assertEqual(c["carry_turns"], 3)                     # still in context on all 3 later turns

    def test_1h_tier_prices_create_and_rewrite_at_1h(self):
        c = self.one(one_result_session([20, 4000, 20], tier="1h"))
        self.assertAlmostEqual(c["create_cost_usd"], 20000)       # created by a 1h write
        self.assertAlmostEqual(c["expiry_cost_usd"], 20000)       # re-written at the 1h rate
        self.assertAlmostEqual(c["carry_cost_usd"], 2000)
        self.assertEqual(c["expiry_events"], 1)

    def test_1h_cache_survives_gap_that_kills_5m(self):
        c = self.one(one_result_session([20, 1200, 20], tier="1h"))
        self.assertEqual(c["expiry_events"], 0)
        self.assertAlmostEqual(c["carry_cost_usd"], 3000)

    def test_mixed_tier_rewrite_is_proportional(self):
        mix = lambda cur: dict(c5=(cur + 100) // 2, c1=(cur + 100) // 2)
        c = self.one(one_result_session([20, 600], mix=mix))
        # expiry turn wrote half 5m ($10) half 1h ($20) => $15 per token
        self.assertAlmostEqual(c["expiry_cost_usd"], 15000, places=0)

    def test_multiple_expiries(self):
        c = self.one(one_result_session([20, 600, 20, 900, 400, 20]))
        self.assertEqual(c["expiry_events"], 3)                    # 600, 900, 400 all > 5 min
        self.assertAlmostEqual(c["expiry_cost_usd"], 30000)
        self.assertAlmostEqual(c["expiry_gap_total_s"], 1900)
        self.assertAlmostEqual(c["carry_cost_usd"], 3000)          # three read turns
        self.assertAlmostEqual(c["est_cost_usd"], 10000 + 3000 + 30000)

    def test_expiry_on_the_consuming_turn_is_just_the_create(self):
        s = Sess()
        s.turn(0, [("t1", "Read", {"file_path": "/a"})], c5=50000)
        s.result(1, "t1")
        s.turn(900, c5=51000)                    # idle gap before the consuming turn: whole prefix written
        s.turn(910, read=51000, c5=100)
        c = self.one(s)
        self.assertEqual(c["expiry_events"], 0)
        self.assertAlmostEqual(c["create_cost_usd"], 10000)
        self.assertAlmostEqual(c["carry_cost_usd"], 1000)

    def test_expiry_is_not_a_compaction(self):
        # cache_read falls to 0 at the expiry turn, but the prompt does not shrink => carry continues
        c = self.one(one_result_session([20, 600, 20]))
        self.assertEqual(c["carry_turns"], 3)

    def test_expiry_with_shrinking_prompt_is_a_compaction(self):
        s = Sess()
        s.turn(0, [("t1", "Read", {"file_path": "/a"})], c5=50000)
        s.result(1, "t1")
        s.turn(10, read=50000, c5=1000)
        s.turn(20, read=51000, c5=100)                         # read turn
        s.turn(620, c5=8000)                                   # idle AND context collapsed to 8k => compaction
        s.turn(630, read=8000, c5=100)
        c = self.one(s)
        self.assertEqual(c["expiry_events"], 0)
        self.assertEqual(c["carry_turns"], 1)                  # only the turn before the cutoff
        self.assertAlmostEqual(c["carry_cost_usd"], 1000)
        self.assertAlmostEqual(c["est_cost_usd"], 11000)

    def test_model_switch_is_not_expiry(self):
        s = Sess()
        s.turn(0, [("t1", "Read", {"file_path": "/a"})], c5=50000)
        s.result(1, "t1")
        s.turn(10, read=50000, c5=1000)
        s.turn(20, read=51000, c5=100)
        s.turn(900, c5=51100, model="m2")
        s.turn(910, read=51100, c5=100, model="m2")
        c = self.one(s)
        self.assertEqual(c["expiry_events"], 0)

    def test_warm_usage_after_long_gap_is_not_expiry(self):
        s = Sess()
        s.turn(0, [("t1", "Read", {"file_path": "/a"})], c5=50000)
        s.result(1, "t1")
        s.turn(10, read=50000, c5=1000)
        s.turn(20, read=51000, c5=100)
        s.turn(2000, read=51100, c5=100)                       # long gap but the cache was evidently warm
        c = self.one(s)
        self.assertEqual(c["expiry_events"], 0)
        self.assertAlmostEqual(c["carry_cost_usd"], 2000)

    def test_totals_are_sum_of_components_everywhere(self):
        s = one_result_session([20, 600, 20])
        self.scan(s)
        t = [x for x in ci.tool_costs(self.db, PRICING, catalog={})["tools"] if x["tool_name"] == "Read"][0]
        self.assertAlmostEqual(t["est_cost_usd"], t["create_cost_usd"] + t["carry_cost_usd"] + t["expiry_cost_usd"])
        self.assertAlmostEqual(t["expiry_cost_usd"], 10000)
        self.assertEqual(t["expiry_events"], 1)
        top = ci.top_calls(self.db, PRICING, catalog={})["calls"][0]
        self.assertAlmostEqual(top["est_cost_usd"], t["est_cost_usd"])

    def test_unattributed_calls_unaffected(self):
        s = Sess()
        s.turn(0, [("t1", "Read", {"file_path": "/a"})], c5=50000)   # no result ever recorded
        s.turn(900, c5=51000)
        s.turn(1800, c5=51000)
        c = self.one(s)
        self.assertFalse(c["attributed"])
        self.assertEqual((c["est_cost_usd"], c["expiry_cost_usd"], c["expiry_events"]), (0, 0, 0))

    def test_skill_load_estimate_ignores_rewritten_prefix_on_expiry(self):
        s = Sess()
        s.turn(0, [("t1", "Skill", {"skill": "zz"})], c5=50000)
        s.result(1, "t1", chars=40)                         # 10 tokens of result text
        s.turn(900, c5=50000 + 3010)                        # expiry: prefix 50000 + 3000 new + 10 result
        s.turn(910, read=53010, c5=100)
        self.scan(s)
        c = ci.top_calls(self.db, PRICING, tool="Skill", catalog={})["calls"][0]
        self.assertEqual(c["context_tokens"], 3000)
