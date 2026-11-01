import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from tests.test_whatif import PRICING, M
from token_dashboard.activity import activity
from token_dashboard.agent_types import agent_types
from token_dashboard.db import init_db
from token_dashboard.findings import findings
from token_dashboard.prompt_costs import prompt_costs
from token_dashboard.prompt_kinds import classify

IDLE = 'Another Claude session sent a message:\n<teammate-message teammate_id="a">\n{"type":"idle_notification","from":"a"}'


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.db = os.path.join(d, "t.db")
        init_db(self.db)
        self.n = 0

    def add(self, sid, ts, typ="assistant", model="a-opus", agent=None, text=None, **kw):
        self.n += 1
        cols = ["uuid", "session_id", "project_slug", "type", "timestamp", "model", "agent_id", "prompt_text", "is_sidechain"]
        vals = [f"u{self.n}", sid, "p", typ, ts, model if typ == "assistant" else None, agent, text, 1 if agent else 0]
        cols += list(kw)
        vals += list(kw.values())
        with sqlite3.connect(self.db) as c:
            c.execute(f"INSERT INTO messages ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)

    def spawn(self, sid, agent, typ, name=None):
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO subagent_spawns (tool_use_id, session_id, subagent_type, name, agent_id, description) "
                      "VALUES (?,?,?,?,?,?)", (f"t{agent}", sid, typ, name, agent, "task " + agent))


class KindTests(unittest.TestCase):
    def test_classify(self):
        self.assertIsNone(classify("fix the bug"))
        self.assertIsNone(classify(None))
        self.assertEqual(classify(IDLE), "idle_notification")
        self.assertEqual(classify("Another Claude session sent a message: Phase 9 done"), "teammate_message")
        self.assertEqual(classify("<task-notification>\n<task-id>x"), "task_notification")


class PromptCostTests(Base):
    def setUp(self):
        super().setUp()
        # prompt 1 (person): main turn 1M out + subagent turn 1M in; prompt 2 (idle notice): main turn
        self.add("s1", "2026-01-01T10:00:00Z", "user", text="build it")
        self.add("s1", "2026-01-01T10:01:00Z", output_tokens=M)                 # opus out: $25
        self.add("s1", "2026-01-01T10:02:00Z", agent="ag1", input_tokens=M)     # opus in: $5
        self.add("s1", "2026-01-01T10:10:00Z", "user", text=IDLE)
        self.add("s1", "2026-01-01T10:11:00Z", input_tokens=M)                  # $5
        self.add("s1", "2026-01-01T10:12:00Z", "user", agent="ag1", text="subagent task")  # not a prompt

    def test_subagent_turns_belong_to_the_prompt_in_force(self):
        rows = {p["prompt_text"][:8]: p for p in prompt_costs(self.db, PRICING, sort="cost")}
        p = rows["build it"]
        self.assertAlmostEqual(p["cost_main_usd"], 25.0)
        self.assertAlmostEqual(p["cost_sub_usd"], 5.0)
        self.assertEqual((p["api_calls"], p["subagent_calls"], p["tokens_sub"]), (2, 1, M))
        self.assertIsNone(p["kind"])
        self.assertEqual(len(rows), 2)

    def test_kind_filter_and_sort(self):
        self.assertEqual([p["kind"] for p in prompt_costs(self.db, PRICING, kind="automated")], ["idle_notification"])
        self.assertEqual([p["kind"] for p in prompt_costs(self.db, PRICING, kind="human")], [None])
        self.assertEqual(prompt_costs(self.db, PRICING, sort="recent")[0]["prompt_text"][:7], "Another")
        self.assertEqual(prompt_costs(self.db, PRICING, sort="cost")[0]["prompt_text"], "build it")


class AgentTypeTests(Base):
    def setUp(self):
        super().setUp()
        self.add("s1", "2026-01-01T10:00:00Z", input_tokens=M)                           # main $5
        for i in range(2):
            self.add("s1", f"2026-01-01T10:0{i + 1}:00Z", agent="ag1", input_tokens=M)   # $5 each
        self.add("s1", "2026-01-01T10:05:00Z", agent="ag2", input_tokens=M)              # $5
        self.spawn("s1", "ag1", "builder")
        self.spawn("s1", "ag2", "tester")

    def test_totals_types_and_runs(self):
        r = agent_types(self.db, PRICING)
        self.assertAlmostEqual(r["totals"]["cost_usd"], 20.0)
        self.assertAlmostEqual(r["totals"]["sub_share_pct"], 75.0)
        types = {t["type"]: t for t in r["by_type"]}
        self.assertEqual((types["builder"]["runs"], types["builder"]["turns"]), (1, 2))
        self.assertAlmostEqual(types["builder"]["cost_usd"], 10.0)
        self.assertAlmostEqual(types["builder"]["share_pct"], 50.0)
        self.assertEqual(r["runs"][0]["agent_id"], "ag1")
        self.assertEqual(r["runs"][0]["label"], "task ag1")


class ActivityTests(Base):
    def test_idle_gap_splits_segments_and_peak_counts_overlap(self):
        for t in ("10:00", "10:05", "11:00", "11:04"):       # s1: two stretches (55 min gap)
            self.add("s1", f"2026-01-01T{t}:00Z", input_tokens=1)
        for t in ("11:01", "11:03"):                         # s2 overlaps s1's second stretch
            self.add("s2", f"2026-01-01T{t}:00Z", input_tokens=1)
        r = activity(self.db)
        self.assertEqual(r["overall"]["sessions"], 2)
        self.assertAlmostEqual(r["overall"]["active_hours"], (5 + 4 + 2) / 60, places=1)
        self.assertAlmostEqual(r["overall"]["wall_hours"], (64 + 2) / 60, places=1)
        self.assertEqual(r["overall"]["peak_concurrent"], 2)
        day = r["by_day"][0]
        self.assertEqual(day["date"], "2026-01-01")
        self.assertEqual(day["peak_concurrent"], 2)
        s1 = next(s for s in day["sessions"] if s["session_id"] == "s1")
        self.assertEqual(s1["segments"], [(600.0, 605.0), (660.0, 664.0)])

    def test_segment_crossing_midnight_is_cut(self):
        self.add("s1", "2026-01-01T23:58:00Z", input_tokens=1)
        self.add("s1", "2026-01-02T00:03:00Z", input_tokens=1)
        days = {d["date"]: d for d in activity(self.db)["by_day"]}
        self.assertEqual(days["2026-01-01"]["sessions"][0]["segments"], [(1438.0, 1440.0)])
        self.assertEqual(days["2026-01-02"]["sessions"][0]["segments"], [(0.0, 3.0)])


class FindingsTests(Base):
    def test_findings_and_recommendations(self):
        now = datetime(2026, 1, 3, tzinfo=timezone.utc)
        self.add("s1", "2026-01-02T10:00:00Z", "user", text="go")
        self.add("s1", "2026-01-02T10:01:00Z", input_tokens=1000, cache_read_tokens=9000)
        for i in range(4):
            self.add("s1", f"2026-01-02T10:0{i + 2}:00Z", agent="ag1", input_tokens=M)
        self.add("s1", "2026-01-02T11:00:00Z", "user", text=IDLE)
        self.add("s1", "2026-01-02T11:01:00Z", agent="ag1", input_tokens=M)
        self.spawn("s1", "ag1", "builder")
        r = findings(self.db, PRICING, days=7, now=now)
        by = {f["key"]: f for f in r["findings"]}
        self.assertEqual(by["subagent-share"]["kind"], "bad")
        self.assertEqual(by["biggest-run"]["subject"], "task ag1")
        self.assertEqual(by["automated-prompts"]["kind"], "bad")
        self.assertIn("1 of them idle notifications", by["automated-prompts"]["text"])
        self.assertIn("activity", by)
        self.assertTrue(any("Idle notifications" in x["text"] for x in r["recommendations"]))

    def test_empty_window_has_no_findings(self):
        r = findings(self.db, PRICING, days=7, now=datetime(2026, 1, 3, tzinfo=timezone.utc))
        self.assertEqual((r["findings"], r["recommendations"]), ([], []))


if __name__ == "__main__":
    unittest.main()
