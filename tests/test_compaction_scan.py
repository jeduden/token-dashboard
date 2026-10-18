"""Scanner support for compact_boundary records (table rows, gap, idempotency, privacy)."""
import json
import os
import sqlite3
from contextlib import closing
import tempfile
import unittest

from token_dashboard.db import init_db, expensive_prompts
from token_dashboard.scanner import scan_dir
from tests.compaction_fixture import CSess, SECRET_CONTENT, SECRET_SUMMARY, SECRET_TOOL


def idle_compaction_session(sid="s1", gap=900, trigger="auto", agent=None):
    s = CSess(sid, cwd="/work/p", agent=agent)
    s.turn(0, [("t1", "Read", {"file_path": "/a/f.py"})], c5=50000)
    s.result(1, "t1")
    s.turn(10, read=50000, c5=1000)
    s._rec(10 + gap, "user", {"role": "user", "content": "please continue"})
    s.boundary(12 + gap, trigger=trigger, pre=300000, post=15000, dur=30000)
    s.summary(13 + gap)
    s.turn(40 + gap, c5=16000)
    return s


class ScanBase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.root = os.path.join(self.tmp, "projects")
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def q(self, sql, *args):
        c = sqlite3.connect(self.db)
        try:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, args)]
        finally:
            c.close()

    def counts(self):
        return {t: self.q("SELECT COUNT(*) n FROM %s" % t)[0]["n"]
                for t in ("messages", "tool_calls", "compactions")}


class ScanTests(ScanBase):
    def test_row_is_stored_with_idle_gap(self):
        idle_compaction_session().write(self.root)
        scan_dir(self.root, self.db)
        (row,) = self.q("SELECT * FROM compactions")
        self.assertEqual(row["session_id"], "s1")
        self.assertEqual(row["project_slug"], "proj")
        self.assertEqual(row["trigger"], "auto")
        self.assertEqual(row["pre_tokens"], 300000)
        self.assertEqual(row["post_tokens"], 15000)
        self.assertEqual(row["dropped_tokens"], 285000)
        self.assertEqual(row["duration_ms"], 30000)
        # last assistant turn at 10s, the user returned at 910s: idle 900s
        self.assertEqual(row["gap_before_s"], 900.0)

    def test_mid_turn_compaction_has_no_idle_gap(self):
        s = CSess("s1")
        s.turn(0, [("t1", "Read", {"file_path": "/a"})], c5=50000)
        s.result(2, "t1")
        s.boundary(3)
        s.write(self.root)
        scan_dir(self.root, self.db)
        self.assertEqual(self.q("SELECT gap_before_s g FROM compactions")[0]["g"], 2.0)

    def test_no_previous_assistant_gives_null_gap(self):
        s = CSess("s1")
        s._rec(0, "user", {"role": "user", "content": "hi"})
        s.boundary(5)
        s.write(self.root)
        scan_dir(self.root, self.db)
        self.assertIsNone(self.q("SELECT gap_before_s g FROM compactions")[0]["g"])

    def test_gap_ignores_other_threads_of_the_session(self):
        s = CSess("s1")
        s.turn(0, c5=1000)
        s.turn(500, c5=1000)                     # main thread, last activity at 500
        s.boundary(560)
        a = CSess("s1-ag1", agent="ag1", session="s1")
        a.turn(540, c5=1000)                     # sub-agent turn must not shrink the main gap
        s.write(self.root)
        a.write(self.root)
        scan_dir(self.root, self.db)
        self.assertEqual(self.q("SELECT gap_before_s g FROM compactions")[0]["g"], 60.0)

    def test_sidechain_event_keeps_agent_id(self):
        idle_compaction_session(agent="agent-9").write(self.root)
        scan_dir(self.root, self.db)
        (row,) = self.q("SELECT is_sidechain, agent_id FROM compactions")
        self.assertEqual((row["is_sidechain"], row["agent_id"]), (1, "agent-9"))

    def test_privacy_nothing_private_in_the_database(self):
        idle_compaction_session().write(self.root)
        scan_dir(self.root, self.db)
        with closing(sqlite3.connect(self.db)) as c:
            dump = "\n".join(c.iterdump())
        for secret in (SECRET_CONTENT, SECRET_SUMMARY, SECRET_TOOL):
            self.assertNotIn(secret, dump)

    def test_summary_record_is_not_a_prompt(self):
        idle_compaction_session().write(self.root)
        scan_dir(self.root, self.db)
        rows = self.q("SELECT prompt_text, prompt_chars, type FROM messages WHERE type='user'")
        self.assertEqual(len(rows), 3)           # tool result, real prompt, summary record kept as a row
        texts = sorted(r["prompt_text"] or "" for r in rows)
        self.assertEqual(texts, ["", "", "please continue"])
        shown = [p["prompt_text"] for p in expensive_prompts(self.db, limit=50)]
        self.assertNotIn(SECRET_SUMMARY, " ".join(shown))

    def test_boundary_does_not_create_usage_rows(self):
        idle_compaction_session().write(self.root)
        scan_dir(self.root, self.db)
        (row,) = self.q("SELECT type, input_tokens, cache_read_tokens, message_id FROM messages WHERE type='system'")
        self.assertEqual((row["input_tokens"], row["cache_read_tokens"], row["message_id"]), (0, 0, None))

    def test_full_rescan_is_idempotent(self):
        idle_compaction_session().write(self.root)
        scan_dir(self.root, self.db)
        before = self.counts()
        path = os.path.join(self.root, "proj", "s1.jsonl")
        os.utime(path, (1, 1))                   # changed mtime, same size => rescan from byte 0
        scan_dir(self.root, self.db)
        self.assertEqual(self.counts(), before)
        self.assertEqual(before["compactions"], 1)

    def test_incremental_append(self):
        s = idle_compaction_session()
        head = list(s.lines[:4])
        os.makedirs(os.path.join(self.root, "proj"))
        path = os.path.join(self.root, "proj", "s1.jsonl")
        with open(path, "w") as f:
            f.write("\n".join(head) + "\n")
        scan_dir(self.root, self.db)
        self.assertEqual(self.counts()["compactions"], 0)
        with open(path, "a") as f:
            f.write("\n".join(s.lines[4:]) + "\n")
        scan_dir(self.root, self.db)
        self.assertEqual(self.counts()["compactions"], 1)
        self.assertEqual(self.q("SELECT gap_before_s g FROM compactions")[0]["g"], 900.0)

    def test_partial_boundary_line_is_retried(self):
        s = idle_compaction_session()
        os.makedirs(os.path.join(self.root, "proj"))
        path = os.path.join(self.root, "proj", "s1.jsonl")
        bi = next(i for i, l in enumerate(s.lines) if "compact_boundary" in l)
        with open(path, "w") as f:
            f.write("\n".join(s.lines[:bi]) + "\n" + s.lines[bi][:60])   # mid-flush
        scan_dir(self.root, self.db)
        self.assertEqual(self.counts()["compactions"], 0)
        with open(path, "w") as f:
            f.write("\n".join(s.lines) + "\n")
        scan_dir(self.root, self.db)
        scan_dir(self.root, self.db)
        self.assertEqual(self.counts()["compactions"], 1)


if __name__ == "__main__":
    unittest.main()
