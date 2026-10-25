import json
import os
import tempfile
import unittest
from pathlib import Path

from token_dashboard.rates import BUCKETS, load_with_override, merge_override, normalize

ROOT = Path(__file__).resolve().parent.parent
PRICING = ROOT / "pricing.json"
NO_OVERRIDE = "/nonexistent/override.json"


def entry(**kw):
    e = {"vendor": "v", "class": "opus", "source": "s", "as_of": "2026-01-01",
         "input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_create_5m": 2.5, "cache_create_1h": 4.0}
    e.update(kw)
    return e


def doc(**models):
    return {"schema": 2, "sources": {"s": {"url": "x", "as_of": "2026-01-01"}}, "models": models}


class ShippedFileTests(unittest.TestCase):
    def setUp(self):
        self.p = load_with_override(PRICING, NO_OVERRIDE)

    def test_every_model_has_provenance_and_all_buckets(self):
        for name, e in self.p["models"].items():
            for k in ("vendor", "class", "source", "as_of") + BUCKETS:
                self.assertIn(k, e, f"{name}.{k}")
            self.assertIn(e["source"], self.p["sources"], name)

    def test_class_members_exist(self):
        for cls, m in self.p["classes"].items():
            for vendor, model in m.items():
                if vendor != "label" and model:
                    self.assertIn(model, self.p["models"], f"{cls}.{vendor}")

    def test_fast_rates_are_dearer_than_standard(self):
        fast = {n: e for n, e in self.p["models"].items() if "fast" in e}
        self.assertTrue(fast)
        for n, e in fast.items():
            self.assertGreater(e["fast"]["input"], e["input"], n)
            self.assertGreater(e["fast"]["output"], e["output"], n)


class NormalizeTests(unittest.TestCase):
    def test_null_bucket_is_billed_as_input_and_marked_derived(self):
        p = normalize(doc(m=entry(cache_create_5m=None, cache_create_1h=None)))
        e = p["models"]["m"]
        self.assertEqual(e["cache_create_5m"], 2.0)
        self.assertEqual(e["cache_create_1h"], 2.0)
        self.assertEqual(e["derived"], ["cache_create_5m", "cache_create_1h"])

    def test_missing_output_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize(doc(m=entry(output=None)))

    def test_negative_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize(doc(m=entry(cache_read=-1)))

    def test_schema2_requires_provenance(self):
        e = entry()
        del e["as_of"]
        with self.assertRaises(ValueError):
            normalize(doc(m=e))

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize(doc(m=entry(source="nope")))

    def test_schema1_needs_no_provenance(self):
        p = normalize({"models": {"m": {"input": 1, "output": 2, "cache_read": 0.1,
                                        "cache_create_5m": 1, "cache_create_1h": 2}}})
        self.assertEqual(p["models"]["m"]["output"], 2)

    def test_does_not_mutate_input(self):
        d = doc(m=entry(cache_create_5m=None))
        normalize(d)
        self.assertIsNone(d["models"]["m"]["cache_create_5m"])


class OverrideTests(unittest.TestCase):
    def _write(self, d, name, obj):
        p = os.path.join(d, name)
        Path(p).write_text(obj if isinstance(obj, str) else json.dumps(obj))
        return p

    def test_override_replaces_same_named_entry_and_adds_new_ones(self):
        with tempfile.TemporaryDirectory() as d:
            ov = self._write(d, "o.json", {"models": {
                "claude-opus-5-5": entry(vendor="anthropic", source="anthropic", input=1.0),
                "my-model": entry(vendor="local", source="anthropic")}})
            p = load_with_override(PRICING, ov)
        self.assertEqual(p["models"]["claude-opus-5-5"]["input"], 1.0)
        self.assertIn("my-model", p["models"])
        self.assertIn("claude-sonnet-5-5", p["models"])  # untouched entries stay

    def test_invalid_override_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            ov = self._write(d, "o.json", {"models": {"bad": entry(output=None)}})
            p = load_with_override(PRICING, ov)
        self.assertNotIn("bad", p["models"])

    def test_unparseable_override_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            ov = self._write(d, "o.json", "{not json")
            p = load_with_override(PRICING, ov)
        self.assertIn("claude-opus-5-5", p["models"])

    def test_env_var_names_the_override(self):
        with tempfile.TemporaryDirectory() as d:
            ov = self._write(d, "o.json", {"plans": {"team": {"monthly": 30, "label": "Team"}}})
            os.environ["TOKEN_DASHBOARD_PRICING"] = ov
            try:
                p = load_with_override(PRICING)
            finally:
                del os.environ["TOKEN_DASHBOARD_PRICING"]
        self.assertIn("team", p["plans"])

    def test_merge_override_keeps_scalar_keys(self):
        self.assertEqual(merge_override({"schema": 2}, {"schema": 3})["schema"], 3)


if __name__ == "__main__":
    unittest.main()
