import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db
from token_dashboard.pricer import Pricer
from token_dashboard.whatif import WhatIf, parse_targets
from token_dashboard.whatif_report import whatif_report

M = 1_000_000


def rs(i, o, cr, w5, w1):
    return {"input": i, "output": o, "cache_read": cr, "cache_create_5m": w5, "cache_create_1h": w1}


def model(vendor, cls, rates, **kw):
    return {"vendor": vendor, "class": cls, **rates, **kw}


PRICING = {
    "models": {
        "a-opus": model("anthropic", "opus", rs(5, 25, 0.5, 6.25, 10)),
        "a-haiku": model("anthropic", "haiku", rs(1, 5, 0.1, 1.25, 2),
                         long_context={"above_prompt_tokens": 100_000, **rs(2, 10, 0.2, 2.5, 4)}),
        "o-mid": model("openai", "opus", rs(2, 10, 0.2, 2.5, 5)),
        "o-small": model("openai", "haiku", rs(0.1, 0.5, 0.01, 0.125, 0.25)),
        "k-mid": model("kimi", "opus", rs(2, 10, 0.2, 2, 4)),
    },
    "tier_fallback": {},
    "classes": {"opus": {"anthropic": "a-opus", "openai": "o-mid", "kimi": "k-mid"},
                "haiku": {"anthropic": "a-haiku", "openai": "o-small", "kimi": None}},
}


def use(**kw):
    base = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0}
    base.update(kw)
    return base


class ParseTargetsTests(unittest.TestCase):
    def test_accepts_vendors_and_models_and_drops_blanks_and_repeats(self):
        self.assertEqual(parse_targets(PRICING, "openai, kimi,,openai,model:o-small"),
                         ["openai", "kimi", "model:o-small"])

    def test_rejects_unknown_target(self):
        for bad in ("nope", "model:nope"):
            with self.assertRaises(ValueError):
                parse_targets(PRICING, bad)

    def test_rejects_more_than_six(self):
        with self.assertRaises(ValueError):
            parse_targets(PRICING, "openai,kimi,anthropic,model:o-mid,model:o-small,openai:fast,kimi:fast")


class PricerHelperTests(unittest.TestCase):
    def setUp(self):
        self.p = Pricer(PRICING)

    def test_resolve_uses_the_class(self):
        self.assertEqual(self.p.resolve("a-opus", "openai"), "o-mid")
        self.assertEqual(self.p.resolve("a-haiku", "openai"), "o-small")
        self.assertIsNone(self.p.resolve("a-haiku", "kimi"))

    def test_resolve_a_fixed_model(self):
        self.assertEqual(self.p.resolve("a-haiku", "model:o-mid"), "o-mid")

    def test_resolve_unlisted_model_by_name(self):
        self.assertEqual(self.p.resolve("claude-opus-9", "openai"), "o-mid")

    def test_band_sql_binds_thresholds(self):
        sql, args = self.p.band_sql("x")
        self.assertEqual(args, [100_000])
        self.assertNotIn("100000", sql)


class WhatIfTests(unittest.TestCase):
    def test_alternatives_are_priced_once_per_group(self):
        w = WhatIf(Pricer(PRICING), ["openai", "kimi"])
        w.add("a-opus", use(input_tokens=M, output_tokens=M), key="s1")
        w.add("a-opus", use(input_tokens=M), key="s2")
        r1, r2, tot = w.result("s1"), w.result("s2"), w.result()
        self.assertAlmostEqual(r1["cost_usd"], 30.0)
        self.assertAlmostEqual(r1["alt_costs"]["openai"], 12.0)
        self.assertAlmostEqual(r2["alt_costs"]["kimi"], 2.0)
        self.assertAlmostEqual(tot["cost_usd"], 35.0)
        self.assertAlmostEqual(tot["alt_costs"]["openai"], 14.0)

    def test_cache_buckets_use_each_vendors_rates(self):
        w = WhatIf(Pricer(PRICING), ["openai"])
        w.add("a-opus", use(cache_read_tokens=M, cache_create_1h_tokens=M))
        r = w.result()
        self.assertAlmostEqual(r["cost_usd"], 10.5)
        self.assertAlmostEqual(r["alt_costs"]["openai"], 5.2)
        self.assertAlmostEqual(r["by_bucket"]["cache_read"], 0.5)

    def test_missing_peer_is_reported_not_priced(self):
        w = WhatIf(Pricer(PRICING), ["kimi"])
        w.add("a-haiku", use(input_tokens=M))
        r = w.result()
        self.assertIsNone(r["alt_costs"]["kimi"])
        self.assertEqual(r["alt_unpriced_tokens"]["kimi"], M)

    def test_partial_peer_coverage_prices_what_it_can(self):
        w = WhatIf(Pricer(PRICING), ["kimi"])
        w.add("a-haiku", use(input_tokens=M))
        w.add("a-opus", use(input_tokens=M))
        r = w.result()
        self.assertAlmostEqual(r["alt_costs"]["kimi"], 2.0)
        self.assertEqual(r["alt_unpriced_tokens"]["kimi"], M)


class ReportTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.db = os.path.join(d, "t.db")
        init_db(self.db)
        self.n = 0
        self.msg("s1", "p", "2026-01-01T10:00:00Z", "a-opus", input_tokens=M, output_tokens=M)
        self.msg("s1", "p", "2026-01-02T10:00:00Z", "a-opus", input_tokens=M)
        self.msg("s2", "q", "2026-01-02T11:00:00Z", "a-haiku", input_tokens=150_000)   # over 100K: long-context
        self.msg("s2", "q", "2026-01-02T12:00:00Z", "a-haiku", input_tokens=60_000)
        self.msg("s2", "q", "2026-01-02T12:30:00Z", "a-haiku", input_tokens=60_000)    # two small: standard

    def msg(self, sid, slug, ts, mdl, **kw):
        self.n += 1
        cols = ["uuid", "session_id", "project_slug", "type", "timestamp", "model"] + list(kw)
        with sqlite3.connect(self.db) as c:
            c.execute(f"INSERT INTO messages ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                      (f"u{self.n}", sid, slug, "assistant", ts, mdl, *kw.values()))

    def test_total(self):
        r = whatif_report(self.db, PRICING, "total", "openai,kimi")
        # opus: 30 + 5 ; haiku: 150K at the long-context $2/M + 120K at $1/M
        self.assertAlmostEqual(r["total"]["cost_usd"], 35 + 0.3 + 0.12)
        self.assertAlmostEqual(r["total"]["alt_costs"]["openai"], 12 + 2 + (270_000 * 0.1 / M))
        self.assertAlmostEqual(r["total"]["alt_costs"]["kimi"], 14.0)  # haiku has no Kimi peer: left out
        self.assertEqual(r["total"]["alt_unpriced_tokens"]["kimi"], 270_000)

    def test_without_targets_there_are_no_alternatives(self):
        r = whatif_report(self.db, PRICING, "total", "")
        self.assertEqual(r["total"]["alt_costs"], {})

    def test_group_by_day_is_chronological(self):
        r = whatif_report(self.db, PRICING, "day", "openai")
        self.assertEqual([x["key"] for x in r["rows"]], ["2026-01-01", "2026-01-02"])
        self.assertAlmostEqual(r["rows"][0]["cost_usd"], 30.0)

    def test_group_by_session_sorted_by_cost(self):
        r = whatif_report(self.db, PRICING, "session", "openai")
        self.assertEqual([x["key"] for x in r["rows"]], ["s1", "s2"])

    def test_group_by_project_has_labels(self):
        r = whatif_report(self.db, PRICING, "project", "")
        self.assertEqual({x["key"] for x in r["rows"]}, {"p", "q"})
        self.assertTrue(all("label" in x for x in r["rows"]))

    def test_group_by_model(self):
        r = whatif_report(self.db, PRICING, "model", "")
        self.assertEqual({x["key"] for x in r["rows"]}, {"a-opus", "a-haiku"})

    def test_range_filter(self):
        r = whatif_report(self.db, PRICING, "total", "", since="2026-01-02", until="2026-01-03")
        self.assertAlmostEqual(r["total"]["cost_usd"], 5 + 0.3 + 0.12)

    def test_limit(self):
        self.assertEqual(len(whatif_report(self.db, PRICING, "session", "", limit=1)["rows"]), 1)

    def test_bad_inputs_raise(self):
        for kw in ({"group": "nope"}, {"compare": "nope"}):
            with self.assertRaises(ValueError):
                whatif_report(self.db, PRICING, **{"group": "total", **kw})


if __name__ == "__main__":
    unittest.main()


class FastTargetTests(unittest.TestCase):
    def setUp(self):
        import json
        from pathlib import Path
        from token_dashboard.rates import normalize
        root = Path(__file__).resolve().parent.parent
        self.p = normalize(json.loads((root / "pricing.json").read_text(encoding="utf-8")))
        self.pricer = Pricer(self.p)

    def test_fast_suffix_is_accepted_and_checked(self):
        self.assertEqual(parse_targets(self.p, "anthropic:fast,kimi:fast"), ["anthropic:fast", "kimi:fast"])
        with self.assertRaises(ValueError):
            parse_targets(self.p, "nobody:fast")

    def test_claude_fast_uses_fast_rates_only_where_listed(self):
        self.assertEqual(self.pricer.resolve_for("claude-opus-5-5", "anthropic:fast"), ("claude-opus-5-5", "fast"))
        self.assertEqual(self.pricer.resolve_for("claude-sonnet-5-5", "anthropic:fast"), ("claude-sonnet-5-5", None))
        self.assertEqual(self.pricer.resolve_for("claude-opus-5-5", "anthropic"), ("claude-opus-5-5", None))

    def test_kimi_fast_swaps_to_the_highspeed_model(self):
        self.assertEqual(self.pricer.resolve_for("claude-opus-5-5", "kimi:fast"), ("kimi-k3.1-pro-highspeed", None))
        self.assertEqual(self.pricer.resolve_for("claude-haiku-5-5", "kimi:fast"), (None, None))
