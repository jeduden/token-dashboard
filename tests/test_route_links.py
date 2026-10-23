"""Every in-app link must point at a registered route.

Routes are the keys of ROUTES in web/app.js; links are `#/<route>...` literals in
the frontend sources (hrefs and `location.hash = ...` assignments).
"""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def _routes():
    src = (WEB / "app.js").read_text(encoding="utf-8")
    block = src[src.index("const ROUTES"):src.index("};", src.index("const ROUTES"))]
    return set(re.findall(r"'/([a-z-]+)':", block))


class RouteLinkTests(unittest.TestCase):
    def test_all_hash_links_hit_a_registered_route(self):
        routes = _routes()
        self.assertIn("sessions", routes)
        bad = []
        for f in [WEB / "app.js", *sorted((WEB / "routes").glob("*.js"))]:
            for m in re.finditer(r"""#/([A-Za-z0-9_-]+)""", f.read_text(encoding="utf-8")):
                if m.group(1) not in routes:
                    bad.append((f.name, m.group(0)))
        self.assertEqual(bad, [])

    def test_session_links_encode_the_id(self):
        # #/sessions/<encoded id> is what sessions.js parses; a raw id could break the route
        for f in sorted((WEB / "routes").glob("*.js")):
            for m in re.finditer(r"#/sessions/(.{0,22})", f.read_text(encoding="utf-8")):
                self.assertTrue(m.group(1).startswith("${encodeURIComponent("), (f.name, m.group(0)))


if __name__ == "__main__":
    unittest.main()
