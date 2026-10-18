"""Cost model: recorded compaction events end the carry; the cache-read heuristic is the fallback.

Pricing (expiry_fixture): $1 per cache-read token, $10 per 5m write token. A 4000-char result
is 1000 tokens: create = 10,000, each carried turn = 1,000.
"""
from token_dashboard import call_detail as cdx
from token_dashboard import cost_drilldown as cd
from token_dashboard import cost_insights as ci
from token_dashboard import skill_drilldown as sd
from tests.compaction_fixture import CSess
from tests.expiry_fixture import ExpiryBase, PRICING

READ = lambda f: ("t", "Read", {"file_path": f})


def two_call_session(sid="s1", event=25, trigger="auto", extra_events=()):
    """t1 is produced before the event, t2 after it. Every turn looks warm to the heuristic."""
    s = CSess(sid, cwd="/work/" + sid)
    s.turn(0, [("t1", "Read", {"file_path": "/a"})], c5=50000)
    s.result(1, "t1")
    s.turn(10, read=50000, c5=1000)                 # turn1: consumes t1 (create 10,000)
    s.turn(20, read=51000, c5=100)                  # turn2: carry
    for ev in ([event] if event is not None else []) + list(extra_events):
        s.boundary(ev, trigger=trigger)
    s.turn(30, [("t2", "Read", {"file_path": "/b"})], read=51100, c5=100)     # turn3
    s.result(31, "t2")
    s.turn(40, read=51200, c5=1000)                 # turn4: consumes t2
    s.turn(50, read=52200, c5=100)                  # turn5
    return s


class CostBase(ExpiryBase):
    def calls(self, tool="Read"):
        ci._cache.clear()
        out = ci.top_calls(self.db, PRICING, tool=tool, catalog={}, limit=50)
        return {c["target"]: c for c in out["calls"]}, out["meta"]


class EventCutoffTests(CostBase):
    def test_carry_stops_exactly_at_the_event(self):
        self.scan(two_call_session())
        c, meta = self.calls()
        t1, t2 = c["/a"], c["/b"]
        self.assertAlmostEqual(t1["create_cost_usd"], 10000)
        self.assertAlmostEqual(t1["carry_cost_usd"], 1000)        # only turn2 precedes the event
        self.assertEqual(t1["carry_turns"], 1)
        self.assertAlmostEqual(t1["est_cost_usd"], 11000)
        # produced after the event: create at turn4 (10,000) + carry turn5 (1,000), unaffected
        self.assertAlmostEqual(t2["est_cost_usd"], 11000)
        self.assertEqual(meta["compaction_source"], "events")

    def test_without_events_the_warm_turns_are_all_carried(self):
        self.scan(two_call_session(event=None))
        c, meta = self.calls()
        self.assertAlmostEqual(c["/a"]["carry_cost_usd"], 4000)   # turns 2..5
        self.assertAlmostEqual(c["/b"]["est_cost_usd"], 11000)
        self.assertEqual(meta["compaction_source"], "heuristic")

    def test_event_after_the_last_turn_cuts_nothing(self):
        self.scan(two_call_session(event=500))
        c, _ = self.calls()
        self.assertAlmostEqual(c["/a"]["carry_cost_usd"], 4000)

    def test_two_events_before_one_turn_count_once(self):
        self.scan(two_call_session(event=24, extra_events=(26,), trigger="manual"))
        c, _ = self.calls()
        self.assertAlmostEqual(c["/a"]["carry_cost_usd"], 1000)

    def test_events_replace_the_heuristic_for_that_session(self):
        # turn2 is a prefix change (reads drop from 51000 to 5000): the heuristic would cut there,
        # but this session has recorded events (the real one is later), so the carry goes on.
        s = CSess("s1", cwd="/w")
        s.turn(0, [("t1", "Read", {"file_path": "/a"})], c5=50000)
        s.result(1, "t1")
        s.turn(10, read=50000, c5=1000)
        s.turn(20, read=5000, c5=1000)                 # drop, no event here
        s.turn(30, read=6000, c5=100)
        s.boundary(35)
        s.turn(40, read=6100, c5=100)
        self.scan(s)
        c, meta = self.calls()
        self.assertAlmostEqual(c["/a"]["carry_cost_usd"], 2000)    # turns 2 and 3, cut at turn 4
        self.assertEqual(meta["compaction_source"], "events")

    def test_mixed_when_only_some_sessions_have_events(self):
        self.scan(two_call_session("s1"), two_call_session("s2", event=None))
        _, meta = self.calls()
        self.assertEqual(meta["compaction_source"], "mixed")

    def test_expiry_turn_that_is_also_a_compaction_is_not_double_counted(self):
        def sess(sid, event):
            s = CSess(sid, cwd="/w")
            s.turn(0, [("t1", "Read", {"file_path": "/a"})], c5=50000)
            s.result(1, "t1")
            s.turn(10, read=50000, c5=1000)
            s.turn(20, read=51000, c5=100)
            if event:
                s.boundary(300)
            s.turn(620, c5=51200)                     # 600 s idle: whole prefix re-written
            s.turn(630, read=51200, c5=100)
            return s
        self.scan(sess("s1", True))
        c, _ = self.calls()
        self.assertEqual(c["/a"]["expiry_events"], 0)
        self.assertAlmostEqual(c["/a"]["expiry_cost_usd"], 0)
        self.assertAlmostEqual(c["/a"]["carry_cost_usd"], 1000)    # carry ends at the compaction turn
        self.setUp()
        self.scan(sess("s1", False))                              # same data without the event
        c, _ = self.calls()
        self.assertEqual(c["/a"]["expiry_events"], 1)
        self.assertAlmostEqual(c["/a"]["expiry_cost_usd"], 10000)

    def test_sidechain_threads_are_isolated(self):
        main = two_call_session("s1")                            # event at 25 on the main thread
        agent = CSess("s1-ag1", cwd="/w", agent="ag1", session="s1")
        agent.turn(0, [("a1", "Read", {"file_path": "/agent"})], c5=50000)
        agent.result(1, "a1")
        agent.turn(10, read=50000, c5=1000)
        for sec in (20, 30, 40, 50):
            agent.turn(sec, read=51000 + sec, c5=100)
        self.scan(main, agent)
        c, _ = self.calls()
        self.assertAlmostEqual(c["/agent"]["carry_cost_usd"], 4000)    # main's event does not cut it
        self.setUp()
        agent.boundary(25, agent="ag1")                              # now the agent's own event
        main2 = two_call_session("s1", event=None)
        self.scan(main2, agent)
        c, _ = self.calls()
        self.assertAlmostEqual(c["/agent"]["carry_cost_usd"], 1000)    # cut at the agent's turn after 25
        self.assertAlmostEqual(c["/a"]["carry_cost_usd"], 4000)        # the agent's event leaves main alone


class ConsumerTests(CostBase):
    def setUp(self):
        super().setUp()
        self.scan(two_call_session("s1"), two_call_session("s2", trigger="manual"))
        for m in (ci, cd, cdx, sd):
            m._cache.clear()

    def test_every_consumer_agrees(self):
        tool = {t["tool_name"]: t for t in ci.tool_costs(self.db, PRICING, catalog={})["tools"]}["Read"]
        self.assertAlmostEqual(tool["est_cost_usd"], 4 * 11000)
        for mode in ("project", "session", "day", "target", "result_size"):
            cd._cache.clear()
            d = cd.tool_params(self.db, PRICING, "Read", group=mode, limit=500, catalog={})
            self.assertAlmostEqual(d["totals"]["est_cost_usd"], tool["est_cost_usd"], places=4, msg=mode)
            self.assertAlmostEqual(sum(r["est_cost_usd"] for r in d["rows"]), tool["est_cost_usd"], places=4, msg=mode)
        import sqlite3
        with sqlite3.connect(self.db) as c:
            ids = [r[0] for r in c.execute("SELECT id FROM tool_calls WHERE tool_name='Read' "
                                           "AND target IS NOT NULL AND tool_name != '_tool_result' ORDER BY id")]
        total = 0.0
        for i in ids:
            cdx._cache.clear()
            total += cdx.call_detail(self.db, PRICING, i, catalog={})["cost"]["est_cost_usd"]
        self.assertAlmostEqual(total, tool["est_cost_usd"], places=3)

    def test_call_detail_names_the_trigger(self):
        import sqlite3
        with sqlite3.connect(self.db) as c:
            rows = dict(c.execute("SELECT session_id || target, id FROM tool_calls "
                                  "WHERE target IN ('/a') AND tool_name='Read'").fetchall())
        for sid, why in (("s1", "compaction_auto"), ("s2", "compaction_manual")):
            cdx._cache.clear()
            d = cdx.call_detail(self.db, PRICING, rows[sid + "/a"], catalog={})
            end = d["carry_end"]
            self.assertEqual(end["reason"], why)
            self.assertEqual(d["meta"]["compaction_source"], "events")
            self.assertEqual(end["turn_offset"], 3)             # cut at turn3, the call was at turn0
            self.assertEqual(len(d["carry_timeline"]), 1)


class HeuristicDetailTests(CostBase):
    def test_call_detail_keeps_compaction_detected_for_the_heuristic(self):
        s = CSess("h1", cwd="/w")
        s.turn(0, [("t1", "Read", {"file_path": "/a"})], c5=50000)
        s.result(1, "t1")
        s.turn(10, read=50000, c5=1000)
        s.turn(20, read=51000, c5=100)
        s.turn(30, read=5000, c5=45000)                          # reads collapse: heuristic compaction
        s.turn(40, read=50100, c5=100)
        self.scan(s)
        import sqlite3
        with sqlite3.connect(self.db) as c:
            cid = c.execute("SELECT id FROM tool_calls WHERE target='/a' AND tool_name='Read'").fetchone()[0]
        cdx._cache.clear()
        self.assertEqual(cdx.call_detail(self.db, PRICING, cid, catalog={})["carry_end"]["reason"],
                         "compaction_detected")
