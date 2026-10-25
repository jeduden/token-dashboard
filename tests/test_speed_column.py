import json
import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db
from token_dashboard.scanner import scan_dir


def asst(uuid, mid, speed):
    u = {"input_tokens": 1, "output_tokens": 1}
    if speed:
        u["speed"] = speed
    return json.dumps({"uuid": uuid, "type": "assistant", "sessionId": "S", "cwd": "/w", "version": "1",
                       "timestamp": "2026-01-01T00:00:0%dZ" % int(uuid[1:]),
                       "message": {"id": mid, "model": "claude-opus-5-5", "usage": u, "content": []}})


class SpeedTests(unittest.TestCase):
    def test_speed_is_stored_per_message(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "-w"))
        with open(os.path.join(root, "-w", "S.jsonl"), "w") as f:
            f.write("\n".join([asst("a1", "m1", "fast"), asst("a2", "m2", "standard"), asst("a3", "m3", None)]) + "\n")
        db = os.path.join(root, "t.db")
        init_db(db)
        scan_dir(root, db)
        c = sqlite3.connect(db)
        got = dict(c.execute("SELECT uuid, speed FROM messages"))
        self.assertEqual(got, {"a1": "fast", "a2": "standard", "a3": None})

    def test_old_database_gets_the_column_and_one_replay(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "old.db")
        init_db(db)
        c = sqlite3.connect(db)
        c.execute("ALTER TABLE messages DROP COLUMN speed")
        c.execute("DELETE FROM meta WHERE k='speed_backfill_v1'")
        c.execute("INSERT INTO files (path, mtime, bytes_read, scanned_at) VALUES ('p', 5, 99, 0)")
        c.commit(); c.close()
        init_db(db)
        c = sqlite3.connect(db)
        self.assertIn("speed", [r[1] for r in c.execute("PRAGMA table_info(messages)")])
        self.assertEqual(c.execute("SELECT mtime, bytes_read FROM files").fetchone(), (0, 0))
        c.execute("UPDATE files SET mtime=7, bytes_read=7"); c.commit(); c.close()
        init_db(db)  # marker is set: no second replay
        c = sqlite3.connect(db)
        self.assertEqual(c.execute("SELECT mtime FROM files").fetchone()[0], 7)


if __name__ == "__main__":
    unittest.main()
