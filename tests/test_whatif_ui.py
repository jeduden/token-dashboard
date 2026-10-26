"""web/whatif.js: the compare setting and the 'about $X' line, run under Node."""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class WhatIfHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run(["node", os.path.join(ROOT, "tests", "js", "whatif_probe.mjs"),
                            "file://" + os.path.join(ROOT, "web", "whatif.js")],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(r.stderr)
        cls.out = json.loads(r.stdout)

    def test_off_adds_nothing(self):
        self.assertEqual(self.out["off"], [[], "/api/x", ""])
        self.assertEqual(self.out["none"], "")

    def test_only_known_vendors_are_kept_and_sent(self):
        self.assertEqual(self.out["targets"], ["openai", "kimi"])
        self.assertEqual(self.out["url"], ["/api/x?compare=openai%2Ckimi", "/api/x?a=1&compare=openai%2Ckimi"])

    def test_line_shows_estimates_and_gaps(self):
        line = self.out["line"]
        self.assertIn("≈$12 OpenAI", line)
        self.assertIn("Kimi n/a", line)
        self.assertIn("Estimate", line)
        self.assertIn("5,000 tokens", line)
        self.assertIn("≈$2.00* Kimi", self.out["partial"])


if __name__ == "__main__":
    unittest.main()
