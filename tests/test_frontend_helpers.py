"""Frontend helpers that every route shares, run under Node with a stubbed DOM."""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class HtmlSafeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = "file://" + os.path.join(ROOT, "web", "app.js")
        r = subprocess.run(
            ["node", os.path.join(ROOT, "tests", "js", "fmt_probe.mjs"), app],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise AssertionError(r.stderr)
        cls.out = json.loads(r.stdout)

    def test_numbers_are_coerced_not_thrown_on(self):
        # Routes pass ids and counts straight into templates (a call id broke the page).
        self.assertEqual(self.out[0], {"ok": True, "value": "327243"})
        self.assertEqual(self.out[1], {"ok": True, "value": "0"})
        self.assertEqual(self.out[2], {"ok": True, "value": "12.5"})

    def test_null_and_undefined_are_empty(self):
        self.assertEqual(self.out[3], {"ok": True, "value": ""})
        self.assertEqual(self.out[4], {"ok": True, "value": ""})

    def test_markup_is_escaped(self):
        self.assertEqual(
            self.out[5]["value"], "&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;")
        self.assertEqual(self.out[6]["value"], "plain")


if __name__ == "__main__":
    unittest.main()
