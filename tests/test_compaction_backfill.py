"""One-time replay that fills `compactions` for databases scanned before the table existed."""
import json
import os
import sqlite3
from contextlib import closing

from token_dashboard.db import init_db
from token_dashboard.scanner import scan_dir
from tests.compaction_fixture import CSess
from tests.test_compaction_scan import ScanBase, idle_compaction_session


def parallel_snapshot_session(sid="p1"):
    """One response streamed as two snapshots holding different parallel tool_use blocks."""
    s = CSess(sid, cwd="/work/p")
    def snap(sec, uid, tid, out):
        s.lines.append(json.dumps({
            "type": "assistant", "uuid": uid, "parentUuid": None, "sessionId": sid,
            "timestamp": "2026-05-01T00:00:%02d.000Z" % sec,
            "message": {"id": "msg-1", "model": "m", "content": [
                {"type": "tool_use", "id": tid, "name": "Read", "input": {"file_path": "/f/" + tid}}],
                "usage": {"input_tokens": 1, "output_tokens": out}}}))
    snap(1, "pa", "ta", 5)        # partial snapshot with tool a
    snap(2, "pb", "tb", 9)        # final snapshot with tool b
    s.n = 10
    s.result(5, "ta")
    s.result(6, "tb")
    s.turn(10, read=1000, c5=100)
    return s


def fingerprint(db):
    with closing(sqlite3.connect(db)) as c:
        msgs = c.execute("SELECT uuid, type, input_tokens, output_tokens, cache_read_tokens, "
                         "cache_create_5m_tokens, message_id, prompt_chars FROM messages ORDER BY uuid").fetchall()
        tools = c.execute("SELECT message_uuid, tool_name, tool_use_id, result_tokens, target "
                          "FROM tool_calls ORDER BY message_uuid, tool_name, tool_use_id").fetchall()
    return msgs, tools


class BackfillTests(ScanBase):
    def build_legacy(self):
        for s in (idle_compaction_session("s1"), idle_compaction_session("s2", gap=100, trigger="manual"),
                  parallel_snapshot_session()):
            s.write(self.root)
        scan_dir(self.root, self.db)
        with closing(sqlite3.connect(self.db)) as c:        # what a pre-feature database looks like
            c.execute("DROP TABLE compactions")
            c.execute("DROP TABLE meta")
            c.commit()

    def bytes_read(self):
        return {r["path"]: r["bytes_read"] for r in self.q("SELECT path, bytes_read FROM files")}

    def test_replay_is_idempotent_and_fills_the_table(self):
        self.build_legacy()
        base = fingerprint(self.db)
        n_msgs, n_tools = len(base[0]), len(base[1])
        self.assertGreater(n_tools, 4)
        init_db(self.db)                            # migration resets the high-water marks only
        self.assertEqual(fingerprint(self.db), base)
        self.assertTrue(all(v == 0 for v in self.bytes_read().values()))
        scan_dir(self.root, self.db)
        after = fingerprint(self.db)
        self.assertEqual((len(after[0]), len(after[1])), (n_msgs, n_tools))
        self.assertEqual(after, base)
        self.assertEqual(self.counts()["compactions"], 2)
        self.assertEqual({r["trigger"] for r in self.q("SELECT trigger FROM compactions")}, {"auto", "manual"})

    def test_parallel_tool_calls_survive_replay(self):
        self.build_legacy()
        ids = lambda: sorted(r["tool_use_id"] for r in self.q(
            "SELECT tool_use_id FROM tool_calls WHERE session_id='p1' AND tool_use_id IS NOT NULL"))
        before = ids()
        self.assertEqual(before, ["ta", "ta", "tb", "tb"])      # two calls + two results
        init_db(self.db)
        scan_dir(self.root, self.db)
        self.assertEqual(ids(), before)
        self.assertEqual(self.q("SELECT output_tokens o FROM messages WHERE message_id='msg-1'")[0]["o"], 9)

    def test_runs_exactly_once(self):
        self.build_legacy()
        init_db(self.db)
        scan_dir(self.root, self.db)
        done = self.bytes_read()
        self.assertTrue(all(v > 0 for v in done.values()))
        init_db(self.db)
        init_db(self.db)
        self.assertEqual(self.bytes_read(), done)               # no second replay
        before = self.counts()
        self.assertEqual(scan_dir(self.root, self.db)["files"], 0)
        self.assertEqual(self.counts(), before)

    def test_fresh_database_does_not_touch_anything(self):
        idle_compaction_session().write(self.root)
        scan_dir(self.root, self.db)
        done = self.bytes_read()
        init_db(self.db)
        self.assertEqual(self.bytes_read(), done)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM meta")[0]["n"], 1)

    def test_data_stays_visible_while_the_replay_runs(self):
        self.build_legacy()
        init_db(self.db)
        before = self.counts()
        seen = []
        scan_dir(self.root, self.db,
                 progress=lambda *a: seen.append(self.counts()))
        self.assertGreaterEqual(len(seen), 3)
        for c in seen:                                  # never emptied, never double counted
            self.assertEqual(c["messages"], before["messages"])
            self.assertEqual(c["tool_calls"], before["tool_calls"])
        self.assertEqual(seen[0]["compactions"], 0)
        self.assertEqual(seen[-1]["compactions"], 2)
