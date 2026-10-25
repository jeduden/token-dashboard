import os
import unittest

from tests.pricing_fixture import PRICING as FIXTURE
from token_dashboard.pricing import load_pricing, cost_for, format_for_user

PRICING = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "pricing.json"))


class CostTests(unittest.TestCase):
    def setUp(self):
        self.p = FIXTURE

    def _u(self, **kw):
        base = {
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
        }
        base.update(kw)
        return base

    def test_known_opus_input_cost(self):
        c = cost_for("claude-opus-4-7", self._u(input_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 15.00, places=4)
        self.assertFalse(c["estimated"])

    def test_known_sonnet_output_cost(self):
        c = cost_for("claude-sonnet-4-6", self._u(output_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 15.00, places=4)

    def test_unknown_opus_falls_back(self):
        c = cost_for("claude-opus-9-9-experimental", self._u(input_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 15.00, places=4)
        self.assertTrue(c["estimated"])

    def test_unknown_unparseable_returns_none(self):
        c = cost_for("custom-local-model", self._u(input_tokens=9999), self.p)
        self.assertIsNone(c["usd"])

    def test_cache_read_cheaper_than_input(self):
        c_in = cost_for("claude-opus-4-7", self._u(input_tokens=1_000_000), self.p)
        c_cr = cost_for("claude-opus-4-7", self._u(cache_read_tokens=1_000_000), self.p)
        self.assertLess(c_cr["usd"], c_in["usd"])


class RealPricingTests(unittest.TestCase):
    """Checks against the shipped pricing.json (values from Anthropic's pricing page)."""
    def setUp(self):
        self.p = load_pricing(PRICING)

    def _in(self, model, **kw):
        u = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0}
        u.update(kw)
        return cost_for(model, u, self.p)

    def test_opus_4_7_is_5_dollars(self):
        self.assertAlmostEqual(self._in("claude-opus-4-7", input_tokens=1_000_000)["usd"], 5.0)

    def test_fable_is_priced(self):
        c = self._in("claude-fable-5-1", output_tokens=1_000_000)
        self.assertAlmostEqual(c["usd"], 50.0)
        self.assertFalse(c["estimated"])

    def test_mythos_falls_back_to_fable(self):
        c = self._in("claude-mythos-9", input_tokens=1_000_000)
        self.assertAlmostEqual(c["usd"], 10.0)
        self.assertTrue(c["estimated"])

    def test_dated_and_1m_ids_match_exactly(self):
        for mid in ("claude-opus-4-1-20250805", "claude-sonnet-4-6[1m]"):
            self.assertFalse(self._in(mid, input_tokens=1)["estimated"], mid)
        self.assertAlmostEqual(self._in("claude-opus-4-1-20250805", input_tokens=1_000_000)["usd"], 15.0)

    def test_unknown_newer_tiers_use_current_fallback(self):
        self.assertAlmostEqual(self._in("claude-opus-9", input_tokens=1_000_000)["usd"], 5.0)
        self.assertAlmostEqual(self._in("claude-haiku-9", input_tokens=1_000_000)["usd"], 0.1)

    def test_cache_read_rate_for_5_5_models(self):
        self.assertAlmostEqual(self._in("claude-sonnet-5-5", cache_read_tokens=1_000_000)["usd"], 0.1)
        self.assertAlmostEqual(self._in("claude-fable-5-1", cache_read_tokens=1_000_000)["usd"], 0.25)


class PlanFormatTests(unittest.TestCase):
    def setUp(self):
        self.p = load_pricing(PRICING)

    def test_api_plan_returns_raw(self):
        out = format_for_user(12.34, "api", self.p)
        self.assertEqual(out["display_usd"], 12.34)
        self.assertIsNone(out["subscription_usd"])

    def test_pro_plan_returns_subscription_subtitle(self):
        out = format_for_user(12.34, "pro", self.p)
        self.assertEqual(out["subscription_usd"], 20)
        self.assertIn("Pro", out["subtitle"])


if __name__ == "__main__":
    unittest.main()
