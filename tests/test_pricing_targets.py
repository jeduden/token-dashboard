import unittest
from datetime import date
from pathlib import Path

from token_dashboard.pricing import cost_for
from token_dashboard.pricing_targets import pricing_targets
from token_dashboard.rates import load_with_override

P = load_with_override(Path(__file__).resolve().parent.parent / "pricing.json", "/nonexistent")
USAGE = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_tokens": 0,
         "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0}


class TargetsTests(unittest.TestCase):
    def setUp(self):
        self.t = pricing_targets(P, today=date(2026, 10, 30))
        self.by_class = {c["id"]: c for c in self.t["classes"]}

    def test_three_vendors(self):
        self.assertEqual([v["id"] for v in self.t["vendors"]], ["anthropic", "openai", "kimi"])

    def test_every_class_has_every_vendor_key(self):
        for c in self.t["classes"]:
            self.assertEqual(set(c["members"]), {"anthropic", "openai", "kimi"})

    def test_sonnet_peers(self):
        m = self.by_class["sonnet"]["members"]
        self.assertEqual((m["openai"]["model"], m["kimi"]["model"]), ("gpt-6.1-sol", "kimi-k3.1-pro"))
        self.assertEqual((m["anthropic"]["input"], m["anthropic"]["output"]), (m["openai"]["input"], m["openai"]["output"]))

    def test_haiku_has_no_kimi_peer(self):
        self.assertIsNone(self.by_class["haiku"]["members"]["kimi"])

    def test_derived_cache_writes_are_reported(self):
        gpt55 = P["models"]["gpt-5.5"]
        self.assertEqual(gpt55["cache_create_5m"], gpt55["input"])
        self.assertEqual(gpt55["derived"], ["cache_create_5m", "cache_create_1h"])

    def test_unverified_models_are_flagged(self):
        self.assertFalse(P["models"]["kimi-k2.6"]["verified"])

    def test_staleness(self):
        self.assertFalse(self.t["sources"]["anthropic"]["stale"])
        old = pricing_targets(P, today=date(2027, 3, 1))
        self.assertTrue(old["sources"]["anthropic"]["stale"])

    def test_other_vendor_models_price_through_cost_for(self):
        self.assertAlmostEqual(cost_for("gpt-6.1-sol", USAGE, P)["usd"], 12.0)
        self.assertAlmostEqual(cost_for("kimi-k3.1-pro", USAGE, P)["usd"], 12.0)
        self.assertFalse(cost_for("gpt-6.1-sol", USAGE, P)["estimated"])


if __name__ == "__main__":
    unittest.main()
