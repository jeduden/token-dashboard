import http.server
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.request
from unittest import mock

from tests.test_agent_insights import Base
from tests.test_server import _free_port
from tests.test_whatif import PRICING, M
from token_dashboard import findings_generate as fg, findings_store
from token_dashboard.server import build_handler

GOOD = {"window_days": 7, "since": "x", "findings": [
    {"kind": "info", "fig": "7", "subject": "Projects", "text": "there are seven of them", "link": "#/projects"}],
    "recommendations": [{"text": "Keep going."}]}

FAKE = """#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DIR"]
open(os.path.join(d, "argv.json"), "w").write(json.dumps(sys.argv[1:]))
open(os.path.join(d, "stdin.txt"), "w").write(sys.stdin.read())
open(os.path.join(d, "cwd.txt"), "w").write(os.getcwd())
mode = open(os.path.join(d, "mode")).read().strip()
if mode == "fail":
    print("boom", file=sys.stderr); sys.exit(2)
if mode == "notjson":
    print("hello"); sys.exit(0)
result = {"ok": json.dumps({"window_days": 7, "since": "x", "findings": [{"kind": "info", "fig": "7", "subject": "Projects", "text": "there are seven of them", "link": "#/projects"}], "recommendations": [{"text": "Keep going."}]}),
          "fenced": "Here you go:\\n```json\\n" + json.dumps({"window_days": 7, "findings": [{"kind": "bad", "fig": "1", "subject": "s", "text": "t"}]}) + "\\n```",
          "prose": "I could not do that.",
          "invalid": json.dumps({"findings": [{"kind": "red", "fig": "1", "subject": "s", "text": "t"}]}),
          "link": json.dumps({"findings": [{"kind": "bad", "fig": "1", "subject": "s", "text": "t", "link": "https://x.example"}]})}[mode]
print(json.dumps({"is_error": mode == "iserror", "result": result, "total_cost_usd": 0.0123, "modelUsage": {"claude-sonnet-5-5": {}}}))
"""


class FakeClaude(Base):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.exe = os.path.join(self.dir, "claude")
        with open(self.exe, "w") as f:
            f.write(FAKE)
        os.chmod(self.exe, os.stat(self.exe).st_mode | stat.S_IEXEC)
        patcher = mock.patch.dict(os.environ, {"TOKEN_DASHBOARD_CLAUDE": self.exe, "FAKE_DIR": self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mode("ok")
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.add("s1", recent, "user", text="a private prompt")
        self.add("s1", recent, input_tokens=M)

    def mode(self, m):
        with open(os.path.join(self.dir, "mode"), "w") as f:
            f.write(m)

    def seen(self, name):
        with open(os.path.join(self.dir, name)) as f:
            return f.read()


class GenerateTests(FakeClaude):
    def test_success_saves_findings_with_meta(self):
        doc = fg.generate(self.db, PRICING, 7)
        self.assertEqual(doc["findings"][0]["subject"], "Projects")
        self.assertEqual(doc["meta"], {"model": "claude-sonnet-5-5", "cost_usd": 0.0123, "prompts_included": False})
        self.assertEqual(findings_store.load(self.db)["findings"][0]["fig"], "7")

    def test_cli_is_locked_down(self):
        fg.generate(self.db, PRICING, 7)
        argv = json.loads(self.seen("argv.json"))
        for flag in ("-p", "--no-session-persistence", "--disable-slash-commands", "--max-budget-usd"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotEqual(os.path.realpath(self.seen("cwd.txt")), os.path.realpath(os.getcwd()))
        self.assertIn("Facts (JSON", self.seen("stdin.txt"))

    def test_prompt_text_is_sent_only_when_asked(self):
        fg.generate(self.db, PRICING, 7)
        self.assertNotIn("a private prompt", self.seen("stdin.txt"))
        fg.generate(self.db, PRICING, 7, include_prompts=True)
        self.assertIn("a private prompt", self.seen("stdin.txt"))
        self.assertTrue(findings_store.load(self.db)["meta"]["prompts_included"])

    def test_json_inside_a_code_fence_is_found(self):
        self.mode("fenced")
        self.assertEqual(fg.generate(self.db, PRICING, 7)["findings"][0]["kind"], "bad")

    def test_failures_are_reported_and_save_nothing(self):
        for mode, text in (("fail", "boom"), ("notjson", "other than JSON"), ("iserror", "error"),
                           ("prose", "no JSON"), ("invalid", "rejected"), ("link", "rejected")):
            self.mode(mode)
            with self.assertRaises(fg.Failed, msg=mode) as cm:
                fg.generate(self.db, PRICING, 7)
            self.assertIn(text, str(cm.exception), mode)
            self.assertIsNone(findings_store.load(self.db), mode)

    def test_missing_executable(self):
        with mock.patch.dict(os.environ, {"TOKEN_DASHBOARD_CLAUDE": "/nonexistent/claude"}):
            with self.assertRaises(fg.Unavailable):
                fg.generate(self.db, PRICING, 7)


class ServerGenerateTests(FakeClaude):
    def setUp(self):
        super().setUp()
        self.port = _free_port()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), build_handler(self.db, "/nonexistent"))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)

    def call(self, path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method="POST" if body is not None else "GET",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_generate_needs_header_and_confirmation(self):
        self.assertEqual(self.call("/api/findings/generate", {"confirm": True})[0], 403)
        self.assertEqual(self.call("/api/findings/generate", {}, {"X-Token-Dashboard": "1"})[0], 400)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "argv.json")))

    def test_generate_runs_in_the_background_and_publishes_findings(self):
        self.assertEqual(self.call("/api/findings/preview")[1]["available"], True)
        code, _ = self.call("/api/findings/generate", {"confirm": True, "days": 7}, {"X-Token-Dashboard": "1"})
        self.assertEqual(code, 202)
        for _ in range(100):
            st = self.call("/api/findings/status")[1]
            if not st["running"]:
                break
            time.sleep(0.1)
        self.assertIsNone(st["error"])
        f = self.call("/api/findings")[1]
        self.assertEqual((f["source"], f["findings"][0]["subject"]), ("claude", "Projects"))

    def test_failure_shows_up_in_status(self):
        self.mode("fail")
        self.call("/api/findings/generate", {"confirm": True}, {"X-Token-Dashboard": "1"})
        for _ in range(100):
            st = self.call("/api/findings/status")[1]
            if not st["running"]:
                break
            time.sleep(0.1)
        self.assertIn("boom", st["error"])
        self.assertEqual(self.call("/api/findings")[1]["source"], "rules")

    def test_no_executable_is_a_503(self):
        with mock.patch.dict(os.environ, {"TOKEN_DASHBOARD_CLAUDE": "/nonexistent/claude"}):
            self.assertEqual(self.call("/api/findings/generate", {"confirm": True}, {"X-Token-Dashboard": "1"})[0], 503)
            self.assertFalse(self.call("/api/findings/status")[1]["available"])


if __name__ == "__main__":
    unittest.main()
