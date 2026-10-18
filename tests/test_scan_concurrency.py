import http.server
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from token_dashboard import scan_state
from token_dashboard.db import init_db, connect
from token_dashboard.pricing import set_plan, get_plan
from token_dashboard.scanner import scan_dir, scan_file
from token_dashboard.server import build_handler


def _line(i, sid="s1", mid=None):
    rec = {"type": "assistant", "uuid": f"u{sid}{i}", "sessionId": sid,
           "timestamp": "2026-04-19T00:00:00Z",
           "message": {"id": mid or f"m{sid}{i}", "model": "claude-haiku-4-5",
                       "usage": {"input_tokens": 1, "output_tokens": 1}}}
    return json.dumps(rec) + "\n"


def _write(path, n, sid):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for i in range(n):
            f.write(_line(i, sid))


class WalAndLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def test_wal_and_pragmas(self):
        with connect(self.db) as c:
            self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertGreaterEqual(c.execute("PRAGMA busy_timeout").fetchone()[0], 10000)

    def test_set_plan_waits_for_writer(self):
        w = sqlite3.connect(self.db, isolation_level=None, check_same_thread=False)
        w.execute("BEGIN IMMEDIATE")
        w.execute("INSERT OR REPLACE INTO plan (k, v) VALUES ('x', '1')")
        threading.Timer(0.5, lambda: w.execute("COMMIT")).start()
        set_plan(self.db, "max")  # blocks on busy_timeout, must not raise
        self.assertEqual(get_plan(self.db), "max")
        w.close()

    def test_set_plan_passes_between_file_commits(self):
        proj = os.path.join(self.tmp, "p")
        for k in range(3):
            _write(os.path.join(proj, "a", f"f{k}.jsonl"), 50, f"s{k}")
        seen = []

        def progress(done, total, bd, bt):
            if done:
                # a file just committed: the writer lock must be free
                c = sqlite3.connect(self.db, timeout=0.2)
                try:
                    c.execute("INSERT OR REPLACE INTO plan (k, v) VALUES ('plan', ?)", (f"p{done}",))
                    c.commit()
                    seen.append(done)
                finally:
                    c.close()
        scan_dir(proj, self.db, progress=progress)
        self.assertEqual(seen, [1, 2, 3])

    def test_checkpoint_commits_inside_big_file(self):
        p = os.path.join(self.tmp, "p", "a", "big.jsonl")
        _write(p, 200, "s")
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        offsets = []
        scan_file(__import__("pathlib").Path(p), "a", conn,
                  checkpoint=lambda o: offsets.append(o), checkpoint_bytes=2000)
        self.assertGreater(len(offsets), 3)
        conn.close()

    def test_newest_first_and_progress_totals(self):
        proj = os.path.join(self.tmp, "p")
        old, new = os.path.join(proj, "a", "old.jsonl"), os.path.join(proj, "a", "new.jsonl")
        _write(old, 2, "old"); _write(new, 2, "new")
        os.utime(old, (1000, 1000))
        order, calls = [], []
        import token_dashboard.scanner as sc
        orig = sc.scan_file
        sc.scan_file = lambda path, *a, **k: (order.append(path.name), orig(path, *a, **k))[1]
        try:
            scan_dir(proj, self.db, progress=lambda *a: calls.append(a))
        finally:
            sc.scan_file = orig
        self.assertEqual(order, ["new.jsonl", "old.jsonl"])
        self.assertEqual(calls[0][:2], (0, 2))
        self.assertEqual(calls[-1][0], 2)
        self.assertEqual(calls[-1][2], calls[-1][3])

    def test_run_scan_single_flight(self):
        gate, entered = threading.Event(), threading.Event()

        def slow(root, db, progress=None):
            entered.set(); gate.wait(5)
            return {"messages": 0, "tools": 0, "files": 0}
        t = threading.Thread(target=scan_state.run_scan, args=("x", self.db, slow))
        t.start(); entered.wait(5)
        self.assertTrue(scan_state.get_status()["running"])
        started, res = scan_state.run_scan("x", self.db, slow)
        self.assertFalse(started)
        gate.set(); t.join()
        self.assertFalse(scan_state.get_status()["running"])
        self.assertIsNotNone(scan_state.get_status()["last_finished_at"])


class ServerErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self.port), build_handler(self.db, "/nonexistent"))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def test_status_endpoint(self):
        body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/scan/status").read())
        for k in ("running", "files_done", "files_total", "bytes_done", "bytes_total",
                  "started_at", "last_finished_at", "last_error"):
            self.assertIn(k, body)

    def test_post_returns_503_when_locked(self):
        import token_dashboard.db as dbm
        old = dbm.DB_TIMEOUT_S
        dbm.DB_TIMEOUT_S = 0.2
        w = sqlite3.connect(self.db, isolation_level=None)
        w.execute("BEGIN IMMEDIATE")
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/plan", data=b'{"plan":"max"}', method="POST")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 503)
            self.assertIn("error", json.loads(cm.exception.read()))
        finally:
            w.execute("ROLLBACK"); w.close(); dbm.DB_TIMEOUT_S = old


if __name__ == "__main__":
    unittest.main()
