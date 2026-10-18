"""The Compactions route renders under Node with a stubbed DOM: loading state, escaping, errors, links."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class CompactionsRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        r = subprocess.run(["node", os.path.join(ROOT, "tests", "js", "compactions_probe.mjs"),
                            os.path.join(ROOT, "web"), tmp], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError(r.stderr)
        cls.out = json.loads(r.stdout)

    def test_loading_state_comes_first(self):
        self.assertIn("Loading", self.out["ok"][0])

    def test_user_text_is_escaped_and_session_links_are_real_anchors(self):
        html = self.out["ok"][-1]
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn('<a href="#/sessions/sa%22b"', html)
        self.assertNotIn("<b>x</b>", html)

    def test_content_sections(self):
        html = self.out["ok"][-1]
        for needle in ("Compactions per day", "Idle gap before the compaction", "upper bound",
                       "350k → 15k", "By repo", "Sessions with the most compactions", 'href="#/cache"'):
            self.assertIn(needle.lower(), html.lower(), needle)
        self.assertIn('<button type="button" data-range="7d"', html)

    def test_error_text_is_shown(self):
        self.assertIn("bad since &lt;x&gt;", self.out["badRequest"][-1])
        self.assertIn("HTTP 500", self.out["serverError"][-1])


if __name__ == "__main__":
    unittest.main()
