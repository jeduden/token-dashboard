import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(self.proj, "demo"))
        with open(os.path.join(self.proj, "demo", "s.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-19T00:00:00Z","isSidechain":false,"message":{"role":"user","content":"hi"}}\n')
            f.write('{"type":"assistant","uuid":"a1","parentUuid":"u1","sessionId":"s1","timestamp":"2026-04-19T00:00:01Z","isSidechain":false,"message":{"model":"claude-haiku-4-5","usage":{"input_tokens":1,"output_tokens":1}}}\n')
        self.db = os.path.join(self.tmp, "t.db")

    def _run(self, *args):
        env = {**os.environ, "TOKEN_DASHBOARD_DB": self.db}
        return subprocess.run(
            [sys.executable, "cli.py", *args],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )

    def test_scan_then_today(self):
        r1 = self._run("scan", "--projects-dir", self.proj)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertIn("scanned", r1.stdout)
        r2 = self._run("today")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("Token Dashboard", r2.stdout)

    def test_stats(self):
        self._run("scan", "--projects-dir", self.proj)
        r = self._run("stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all time", r.stdout)

    def _run_isolated(self, *args):
        """Run the CLI with HOME in a temp dir and no path env vars.

        If a --db/--projects-dir flag is ignored, the default path resolves
        inside the temp HOME, never the real ~/.claude.
        """
        home = os.path.join(self.tmp, "home")
        os.makedirs(home, exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if k not in ("TOKEN_DASHBOARD_DB", "CLAUDE_PROJECTS_DIR")}
        env.update(HOME=home, USERPROFILE=home)
        r = subprocess.run([sys.executable, "cli.py", *args],
                           cwd=ROOT, env=env, capture_output=True, text=True)
        return r, os.path.join(home, ".claude", "token-dashboard.db")

    def _scanned_messages(self, db):
        import sqlite3
        with sqlite3.connect(db) as c:
            return c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def test_flags_before_subcommand_are_honoured(self):
        r, default_db = self._run_isolated("--db", self.db, "--projects-dir", self.proj, "scan")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._scanned_messages(self.db), 2)
        self.assertFalse(os.path.exists(default_db), "must not fall back to the default DB")

    def test_flags_after_subcommand_are_honoured(self):
        r, default_db = self._run_isolated("scan", "--db", self.db, "--projects-dir", self.proj)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._scanned_messages(self.db), 2)
        self.assertFalse(os.path.exists(default_db))

    def test_flags_split_around_subcommand_are_honoured(self):
        r, default_db = self._run_isolated("--db", self.db, "scan", "--projects-dir", self.proj)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._scanned_messages(self.db), 2)
        self.assertFalse(os.path.exists(default_db))

    def test_flag_after_subcommand_wins_over_flag_before(self):
        wrong = os.path.join(self.tmp, "wrong.db")
        r, _ = self._run_isolated("--db", wrong, "scan", "--db", self.db, "--projects-dir", self.proj)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._scanned_messages(self.db), 2)
        self.assertFalse(os.path.exists(wrong))

    def test_flags_before_dashboard_subcommand_are_parsed(self):
        # Parser-level check: starting a server here would need a free port.
        sys.path.insert(0, ROOT)
        try:
            import cli
            args = cli.build_parser().parse_args(
                ["--db", "/x.db", "--projects-dir", "/p", "dashboard", "--no-open"])
        finally:
            sys.path.remove(ROOT)
        self.assertEqual((args.db, args.projects_dir, args.no_open), ("/x.db", "/p", True))

    def test_tips_runs_without_data(self):
        r = self._run("tips")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no suggestions", r.stdout)


if __name__ == "__main__":
    unittest.main()
