import unittest
from pathlib import Path

from token_dashboard.pricer import Pricer
from token_dashboard.pricing import cost_for
from token_dashboard.rates import load_with_override
from tests.pricing_fixture import PRICING as OLD

REAL = load_with_override(Path(__file__).resolve().parent.parent / "pricing.json", "/nonexistent")
M = 1_000_000


def u(**kw):
    base = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0}
    base.update(kw)
    return base


class ParityTests(unittest.TestCase):
    """Standard-rate results equal a straight rate-times-tokens sum for every model."""

    def test_every_model_matches_manual_sum(self):
        usage = u(input_tokens=1234, output_tokens=567, cache_read_tokens=89000,
                  cache_create_5m_tokens=4321, cache_create_1h_tokens=765)
        for mid, e in REAL["models"].items():
            want = (1234 * e["input"] + 567 * e["output"] + 89000 * e["cache_read"]
                    + 4321 * e["cache_create_5m"] + 765 * e["cache_create_1h"]) / M
            self.assertAlmostEqual(cost_for(mid, usage, REAL)["usd"], want, places=5, msg=mid)

    def test_summed_usage_never_gets_a_band(self):
        c = cost_for("claude-haiku-5-5", u(input_tokens=500_000), REAL)
        self.assertAlmostEqual(c["usd"], 0.05)  # standard $0.10/M even though 500K tokens


class BandTests(unittest.TestCase):
    def setUp(self):
        self.p = Pricer(REAL)

    def test_thresholds_are_the_union(self):
        self.assertEqual(self.p.thresholds, [100_000, 272_000])

    def test_band_boundaries(self):
        b = self.p.band_of
        self.assertEqual((b(100_000), b(100_001), b(272_000), b(272_001)), (0, 1, 1, 2))

    def test_haiku_5_5_switches_above_100k(self):
        at = cost_for("claude-haiku-5-5", u(input_tokens=100_000, output_tokens=M), REAL, per_request=True)
        over = cost_for("claude-haiku-5-5", u(input_tokens=100_001, output_tokens=M), REAL, per_request=True)
        self.assertAlmostEqual(at["usd"], 0.1 * 0.1 + 0.5, places=5)
        self.assertAlmostEqual(over["usd"], 100_001 * 0.5 / M + 2.5, places=5)

    def test_prompt_size_counts_cache_tokens(self):
        c = cost_for("claude-haiku-5-5", u(input_tokens=10, cache_read_tokens=150_000), REAL, per_request=True)
        self.assertAlmostEqual(c["usd"], 10 * 0.5 / M + 150_000 * 0.05 / M, places=6)

    def test_openai_threshold_is_272k_not_100k(self):
        r = self.p.rates("gpt-6.1-sol", band=1)
        self.assertEqual(r["input"], 2)      # band 1 = over 100K only: still standard
        r = self.p.rates("gpt-6.1-sol", band=2)
        self.assertEqual(r["input"], 4)

    def test_model_without_long_context_ignores_band(self):
        self.assertEqual(self.p.rates("claude-sonnet-5-5", band=2)["input"], 2)


class FastTests(unittest.TestCase):
    def test_fast_rates_apply_per_request(self):
        usage = {**u(input_tokens=M, output_tokens=M), "speed": "fast"}
        self.assertAlmostEqual(cost_for("claude-opus-5-5", usage, REAL, per_request=True)["usd"], 48.0)
        self.assertAlmostEqual(cost_for("claude-opus-5-5", usage, REAL)["usd"], 24.0)  # summed: standard

    def test_fast_cache_rates_stack(self):
        usage = {**u(cache_read_tokens=M, cache_create_1h_tokens=M), "speed": "fast"}
        self.assertAlmostEqual(cost_for("claude-opus-5", usage, REAL, per_request=True)["usd"], 21.0)

    def test_fast_on_a_model_without_fast_mode_is_standard(self):
        usage = {**u(input_tokens=M), "speed": "fast"}
        self.assertAlmostEqual(cost_for("claude-sonnet-5-5", usage, REAL, per_request=True)["usd"], 2.0)

    def test_standard_speed_and_missing_speed_agree(self):
        a = cost_for("claude-opus-5-5", {**u(input_tokens=M), "speed": "standard"}, REAL, per_request=True)
        b = cost_for("claude-opus-5-5", u(input_tokens=M), REAL, per_request=True)
        self.assertEqual(a["usd"], b["usd"])


class FallbackTests(unittest.TestCase):
    def test_unknown_id_is_estimated_from_its_tier(self):
        c = cost_for("claude-opus-9-9", u(input_tokens=M), REAL)
        self.assertTrue(c["estimated"])
        self.assertAlmostEqual(c["usd"], 5.0)

    def test_unknown_non_tier_id_is_unpriced(self):
        self.assertIsNone(cost_for("mystery-model", u(input_tokens=M), REAL)["usd"])

    def test_old_schema1_table_still_works(self):
        self.assertAlmostEqual(cost_for("claude-opus-4-7", u(input_tokens=M), OLD)["usd"], 15.0)

    def test_rates_are_memoised_per_variant(self):
        p = Pricer(REAL)
        self.assertIs(p.rates("claude-opus-5-5"), p.rates("claude-opus-5-5"))
        self.assertIsNot(p.rates("claude-opus-5-5", speed="fast"), p.rates("claude-opus-5-5"))


if __name__ == "__main__":
    unittest.main()
