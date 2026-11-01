import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

from tests.test_agent_insights import Base
from tests.test_whatif import PRICING, M
from token_dashboard import findings_store as fs
from token_dashboard.findings_facts import facts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOOD = {"window_days": 7, "since": "2026-01-01T00:00:00",
        "findings": [{"kind": "bad", "fig": "41%", "subject": "titan", "text": "used most of the week", "link": "#/agents"}],
        "recommendations": [{"text": "Split long agents.", "link": "#/sessions/abc-123"}]}


class ValidateTests(unittest.TestCase):
    def bad(self, **change):
        doc = json.loads(json.dumps(GOOD))
        doc["findings"][0].update(change)
        with self.assertRaises(fs.Invalid):
            fs.validate(doc)

    def test_good_document_round_trips(self):
        out = fs.validate(GOOD)
        self.assertEqual(out["findings"][0]["fig"], "41%")
        self.assertEqual(out["recommendations"][0]["link"], "#/sessions/abc-123")

    def test_rejects_bad_kind_long_text_and_foreign_links(self):
        self.bad(kind="red")
        self.bad(fig="x" * 15)
        self.bad(text="x" * 401)
        self.bad(text="")
        self.bad(link="https://example.com/")
        self.bad(link="javascript:alert(1)")
        self.bad(link="#/a b")

    def test_rejects_shapes(self):
        for doc in ([], {"findings": []}, {"findings": "x"}, {**GOOD, "window_days": 0},
                    {**GOOD, "findings": GOOD["findings"] * 9}):
            with self.assertRaises(fs.Invalid):
                fs.validate(doc)


class StoreTests(Base):
    def test_save_load_clear(self):
        self.assertIsNone(fs.load(self.db))
        fs.save(self.db, GOOD)
        got = fs.load(self.db)
        self.assertEqual(got["findings"][0]["subject"], "titan")
        self.assertLess(got["age_days"], 0.1)
        fs.save(self.db, {**GOOD, "window_days": 30})          # replaces, one row
        self.assertEqual(fs.load(self.db)["window_days"], 30)
        fs.clear(self.db)
        self.assertIsNone(fs.load(self.db))

    def test_facts_bundle(self):
        self.add("s1", "2026-01-02T10:00:00Z", "user", text="go")
        self.add("s1", "2026-01-02T10:01:00Z", input_tokens=M)
        self.add("s1", "2026-01-02T10:02:00Z", agent="ag1", input_tokens=M)
        f = facts(self.db, PRICING, 7, now=datetime(2026, 1, 3, tzinfo=timezone.utc))
        self.assertAlmostEqual(f["cost_usd"], 10.0)
        self.assertAlmostEqual(f["subagent_share_pct"], 50.0)
        self.assertEqual(f["top_prompts"][0]["kind"], "person")
        for key in ("projects", "agent_types", "top_runs", "automated_messages", "cache", "activity",
                    "attribution_note", "rule_findings", "previous_window_cost_usd"):
            self.assertIn(key, f)
        json.dumps(f)

    def cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, os.path.join(ROOT, "cli.py"), "--db", self.db, *args],
                              input=stdin, capture_output=True, text=True, cwd=ROOT, timeout=60)

    def test_cli_save_reports_errors_and_stores(self):
        r = self.cli("findings-save", "-", stdin=json.dumps({"findings": [{"kind": "red"}]}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not saved", r.stderr)
        self.assertIsNone(fs.load(self.db))
        r = self.cli("findings-save", "-", stdin=json.dumps(GOOD))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("saved 1 findings", r.stdout)
        self.assertEqual(fs.load(self.db)["findings"][0]["fig"], "41%")
        self.assertIn("window_days", json.loads(self.cli("findings-data", "--days", "3").stdout))
        self.assertEqual(self.cli("findings-clear").returncode, 0)
        self.assertIsNone(fs.load(self.db))


if __name__ == "__main__":
    unittest.main()
