"""Per-skill cost drill-down (skill_drilldown.skill_detail + /api/skill-costs/detail).

Pricing: $1 per cache-read token, $10 per cache-create token (model "m"; "m2"
has the same prices). A skill of N tokens costs N*10 (create, next turn) +
N*1 per later turn until compaction/session end.
"""
import http.server
import json
import os
import socket
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request

from token_dashboard import cost_insights as ci
from token_dashboard import skill_drilldown as sd
from token_dashboard.server import build_handler
from tests.test_cost_insights import Base, Builder, PRICING

P2 = {"models": {**PRICING["models"], "m2": PRICING["models"]["m"]}, "tier_fallback": {}}
CAT = {"demo": {"tokens": 500, "path": "/home/someone/.claude/skills/demo/SKILL.md"}}


def emit(b, root, proj="projA", day="2026-05-01", hour="00", model=None):
    """Write builder ``b`` under project dir ``proj``, rewriting day/hour/model."""
    lines = [l.replace("2026-05-01T00:", "%sT%s:" % (day, hour)) for l in b.lines]
    if model:
        lines = [l.replace('"model": "m"', '"model": "%s"' % model) for l in lines]
    d = os.path.join(root, proj)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, b.session + ".jsonl"), "w") as f:
        f.write("\n".join(lines) + "\n")


def skill_session(sid, name="demo", after=4, calls=1, cc=0):
    """`calls` Skill invocations (one per turn) followed by `after` plain turns."""
    b = Builder(sid)
    for i in range(calls):
        b.assistant([("k%d" % i, "Skill", {"skill": name})])
        b.result("k%d" % i, 40)
    b.assistant(cc=cc)
    for _ in range(after):
        b.assistant()
    return b


def _get(db, skill, catalog=CAT, **kw):
    ci._cache.clear()
    sd._cache.clear()
    return sd.skill_detail(db, P2, skill, catalog=catalog, **kw)


def _rows(out):
    return {r["key"]: r for r in out["rows"]}


class TotalsTests(Base):
    def test_totals_match_skill_costs(self):
        emit(skill_session("s1"), self.root, "projA")
        emit(skill_session("s2", calls=2, after=3), self.root, "projB", day="2026-05-02")
        self.scan()
        out = _get(self.db, "demo")
        ci._cache.clear()
        ref = [s for s in ci.skill_costs(self.db, P2, catalog=CAT)["skills"] if s["skill"] == "demo"][0]
        t = out["totals"]
        self.assertEqual((t["invocations"], t["sessions"]), (ref["invocations"], ref["sessions"]))
        self.assertAlmostEqual(t["est_cost_usd"], ref["est_cost_usd"])
        self.assertAlmostEqual(t["carry_cost_usd"], ref["carry_cost_usd"])
        self.assertAlmostEqual(t["cost_per_invocation"], ref["cost_per_invocation"])
        self.assertEqual(t["est_load_tokens"], ref["est_load_tokens"])
        # s1: 5000 create + 4 turns*500 ; s2: 2x5000 create + (4+3)*500
        self.assertAlmostEqual(t["est_cost_usd"], 5000 + 2000 + 10000 + 3500)
        self.assertAlmostEqual(t["load_cost_usd"], 15000)
        self.assertAlmostEqual(t["carry_cost_usd"], 5500)
        self.assertFalse(t["estimated"])
        self.assertEqual(t["first_used"][:10], "2026-05-01")
        self.assertEqual(t["last_used"][:10], "2026-05-02")

    def test_catalog_info_hides_absolute_path(self):
        emit(skill_session("s1"), self.root); self.scan()
        out = _get(self.db, "demo")
        self.assertEqual(out["catalog"], {"installed": True, "root": "skills", "tokens": 500})
        self.assertNotIn("/home/", json.dumps(out))
        self.assertNotIn("someone", json.dumps(out))
        none = _get(self.db, "demo", catalog={})
        self.assertFalse(none["catalog"]["installed"])

    def test_estimated_load_from_cache_jump(self):
        emit(skill_session("s1", cc=3000), self.root); self.scan()
        out = _get(self.db, "demo", catalog={})
        self.assertTrue(out["totals"]["estimated"])
        self.assertEqual(out["totals"]["est_load_tokens"], 2990)
        self.assertTrue(any(d["rule"] == "estimated_load" and d["severity"] == "info"
                            for d in out["diagnostics"]))
        cat = _get(self.db, "demo")
        self.assertFalse(any(d["rule"] == "estimated_load" for d in cat["diagnostics"]))

    def test_rank_and_percentile_vs_average(self):
        emit(skill_session("s1"), self.root)
        emit(skill_session("s2", name="small"), self.root, "projB")
        self.scan()
        cat = {**CAT, "small": {"tokens": 10, "path": "/x/.claude/skills/small/SKILL.md"}}
        out = _get(self.db, "demo", catalog=cat)["totals"]
        self.assertEqual((out["rank"], out["skills_total"]), (1, 2))
        self.assertEqual(out["cost_percentile"], 1.0)
        self.assertAlmostEqual(out["avg_skill_cost_usd"], (7000 + 140) / 2)
        small = _get(self.db, "small", catalog=cat)["totals"]
        self.assertEqual(small["rank"], 2)
        self.assertEqual(small["cost_percentile"], 0.5)

    def test_since_until_filter(self):
        emit(skill_session("s1"), self.root, day="2026-05-01")
        emit(skill_session("s2"), self.root, day="2026-06-01")
        self.scan()
        out = _get(self.db, "demo", since="2026-05-15T00:00:00Z")
        self.assertEqual(out["totals"]["invocations"], 1)


class GroupTests(Base):
    def setUp(self):
        super().setUp()
        emit(skill_session("s1"), self.root, "projA", day="2026-05-01", hour="09", model="m")
        emit(skill_session("s2", calls=2, after=3), self.root, "projB", day="2026-05-02", hour="14", model="m2")
        self.scan()

    def test_project_default_with_names_and_repo(self):
        out = _get(self.db, "demo")
        self.assertEqual(out["group"], "project")
        r = _rows(out)
        self.assertEqual(set(r), {"projA", "projB"})
        self.assertEqual(r["projB"]["project_name"], "projB")
        self.assertIn("repo", r["projB"]); self.assertIn("repo_name", r["projB"])
        self.assertEqual(r["projB"]["invocations"], 2)
        self.assertAlmostEqual(r["projB"]["est_cost_usd"], 13500)
        self.assertAlmostEqual(r["projB"]["load_cost_usd"], 10000)
        self.assertAlmostEqual(r["projB"]["carry_cost_usd"], 3500)
        self.assertAlmostEqual(r["projB"]["cost_per_invocation"], 6750)
        self.assertAlmostEqual(r["projB"]["avg_carry_turns"], 3.5)
        self.assertAlmostEqual(r["projA"]["avg_carry_turns"], 4)
        self.assertEqual(out["rows"][0]["key"], "projB")       # ranked by cost
        self.assertAlmostEqual(r["projB"]["share"], 13500 / 20500, places=3)
        self.assertAlmostEqual(out["rows"][-1]["cumulative_share"], 1.0)
        self.assertEqual(r["projA"]["first_seen"][:10], "2026-05-01")
        self.assertEqual({g["key"] for g in out["available_groups"]},
                         {"project", "repo", "day", "session", "hour_of_day", "model"})

    def test_repo_merges_projects(self):
        out = _get(self.db, "demo", group="repo")
        self.assertEqual(sum(r["invocations"] for r in out["rows"]), 3)
        self.assertAlmostEqual(sum(r["est_cost_usd"] for r in out["rows"]), 20500)
        for r in out["rows"]:
            self.assertIn("repo_name", r)

    def test_day_session_hour_model(self):
        self.assertEqual(set(_rows(_get(self.db, "demo", group="day"))), {"2026-05-01", "2026-05-02"})
        s = _rows(_get(self.db, "demo", group="session"))
        self.assertEqual(set(s), {"s1", "s2"})
        self.assertEqual(s["s2"]["project_name"], "projB")
        h = _rows(_get(self.db, "demo", group="hour_of_day"))
        self.assertEqual(set(h), {"09:00", "14:00"})
        m = _rows(_get(self.db, "demo", group="model"))
        self.assertEqual(set(m), {"m", "m2"})
        self.assertAlmostEqual(m["m2"]["carry_cost_usd"], 3500)

    def test_limit_and_bad_inputs(self):
        self.assertEqual(len(_get(self.db, "demo", group="session", limit=1)["rows"]), 1)
        for args in ({"skill": ""}, {"skill": "nope"}, {"skill": "demo", "group": "bogus"}):
            with self.assertRaises(ValueError):
                _get(self.db, **args)
        with self.assertRaises(ValueError):
            _get(self.db, "_tool_result")


class CarryTests(Base):
    def test_compaction_cutoff_counts_turns(self):
        b = Builder("c1")
        b.assistant([("k0", "Skill", {"skill": "demo"})]); b.result("k0", 40)
        b.assistant(cc=0)
        b.assistant(cr=50000)
        b.assistant(cr=10000)       # compaction
        b.assistant(cr=10000)
        emit(b, self.root); self.scan()
        out = _get(self.db, "demo")
        self.assertEqual(out["rows"][0]["avg_carry_turns"], 1)
        self.assertAlmostEqual(out["totals"]["carry_cost_usd"], 500)

    def test_short_vs_long_split(self):
        emit(skill_session("short", after=1), self.root)      # k=0, n=3: 0 carry turns
        emit(skill_session("long", after=sd.LONG_CARRY_TURNS + 5), self.root, "projB")
        self.scan()
        sp = _get(self.db, "demo")["repeat"]["carry_split"]
        self.assertEqual(sp["short"]["invocations"], 1)
        self.assertEqual(sp["long"]["invocations"], 1)
        self.assertEqual(sp["medium"]["invocations"], 0)
        self.assertGreater(sp["long"]["est_cost_usd"], sp["short"]["est_cost_usd"])


class RepeatTests(Base):
    def test_reload_in_session(self):
        emit(skill_session("s1", calls=3, after=3), self.root)
        emit(skill_session("s2"), self.root, "projB")
        self.scan()
        out = _get(self.db, "demo")
        rp = out["repeat"]
        self.assertEqual(rp["repeat_invocations"], 2)
        self.assertEqual(rp["sessions_with_repeats"], 1)
        # repeats: 2nd call (5000 + 3 carry turns*500... k=1: start=3, n=7 -> 4 turns) ...
        # turns: 0,1,2 skill, 3 plain, 4,5,6 plain => n=7. k=1: create 5000 + 4*500; k=2: 5000 + 3*500
        self.assertAlmostEqual(rp["repeat_cost_usd"], (5000 + 2000) + (5000 + 1500))
        self.assertGreater(rp["share_of_cost"], 0)
        rule = [d for d in out["diagnostics"] if d["rule"] == "reloaded_in_session"][0]
        self.assertEqual(rule["evidence"]["session_id"], "s1")
        self.assertTrue(rule["suggestion"])

    def test_no_repeat(self):
        emit(skill_session("s1"), self.root); self.scan()
        out = _get(self.db, "demo")
        self.assertEqual(out["repeat"]["repeat_invocations"], 0)
        self.assertFalse(any(d["rule"] == "reloaded_in_session" for d in out["diagnostics"]))


class UnattributedTests(Base):
    def test_unattributed_counted_separately(self):
        emit(skill_session("s1", cc=0), self.root)   # no catalog, no cache jump -> no tokens
        emit(skill_session("s2", cc=3000), self.root, "projB")
        self.scan()
        with sqlite3.connect(self.db) as c:           # s1 predates tool_use_id
            c.execute("UPDATE tool_calls SET tool_use_id=NULL WHERE session_id='s1'"); c.commit()
        out = _get(self.db, "demo", catalog={})
        t = out["totals"]
        self.assertEqual((t["invocations"], t["unattributed_invocations"], t["attributed_invocations"]), (2, 1, 1))
        self.assertEqual(sum(r["invocations"] for r in out["rows"]), 1)
        self.assertEqual([r["key"] for r in out["rows"]], ["projB"])
        self.assertTrue(any(d["rule"] == "unattributed" for d in out["diagnostics"]))

    def test_null_tool_use_id_with_catalog_still_counts(self):
        emit(skill_session("s1"), self.root); self.scan()
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE tool_calls SET tool_use_id=NULL"); c.commit()
        out = _get(self.db, "demo")
        self.assertEqual(out["totals"]["unattributed_invocations"], 0)   # catalog size known


class TimelineAndSessionsTests(Base):
    def test_timeline_and_top_sessions(self):
        for i in range(12):
            emit(skill_session("s%02d" % i, after=1 + i), self.root, "projA",
                 day="2026-05-%02d" % (1 + i % 3))
        self.scan()
        out = _get(self.db, "demo")
        tl = out["invocation_timeline"]
        self.assertEqual([x["day"] for x in tl], ["2026-05-01", "2026-05-02", "2026-05-03"])
        self.assertEqual(sum(x["invocations"] for x in tl), 12)
        self.assertAlmostEqual(sum(x["est_cost_usd"] for x in tl), out["totals"]["est_cost_usd"], places=4)
        top = out["top_sessions"]
        self.assertEqual(len(top), 10)
        self.assertEqual(top[0]["session_id"], "s11")
        for k in ("session_id", "project_slug", "project_name", "invocations", "carry_turns", "est_cost_usd"):
            self.assertIn(k, top[0])
        self.assertEqual([t["est_cost_usd"] for t in top], sorted((t["est_cost_usd"] for t in top), reverse=True))


class DiagnosticTests(Base):
    def rules(self, out):
        return {d["rule"]: d for d in out["diagnostics"]}

    def test_long_carry(self):
        emit(skill_session("s1", after=sd.LONG_CARRY_TURNS + 20), self.root); self.scan()
        d = self.rules(_get(self.db, "demo"))["long_carry"]
        self.assertEqual(d["severity"], "warn")
        self.assertTrue(d["suggestion"] and d["text"])

    def test_no_long_carry_when_short(self):
        emit(skill_session("s1"), self.root); self.scan()
        self.assertNotIn("long_carry", self.rules(_get(self.db, "demo")))

    def test_big_definition(self):
        emit(skill_session("s1"), self.root); self.scan()
        big = {"demo": {"tokens": sd.BIG_DEFINITION_TOKENS + 1, "path": "/a/.claude/plugins/x/SKILL.md"}}
        out = _get(self.db, "demo", catalog=big)
        self.assertIn("big_definition", self.rules(out))
        self.assertEqual(out["catalog"]["root"], "plugins")
        self.assertNotIn("big_definition", self.rules(_get(self.db, "demo")))

    def test_rarely_used_but_heavy(self):
        emit(skill_session("h1", name="heavy"), self.root)
        for i in range(4):
            emit(skill_session("l%d" % i, name="light"), self.root, "projB")
        self.scan()
        cat = {"heavy": {"tokens": 6000, "path": "/a/.claude/skills/heavy/SKILL.md"},
               "light": {"tokens": 10, "path": "/a/.claude/skills/light/SKILL.md"}}
        self.assertIn("rarely_used_but_heavy", self.rules(_get(self.db, "heavy", catalog=cat)))
        self.assertNotIn("rarely_used_but_heavy", self.rules(_get(self.db, "light", catalog=cat)))

    def test_reuses_skill_carry_rule(self):
        for i in range(3):
            emit(skill_session("s%d" % i, after=30), self.root, "projA")
        self.scan()
        self.assertIn("skill_carry", self.rules(_get(self.db, "demo")))

    def test_severity_sorted_and_shaped(self):
        emit(skill_session("s1", calls=2, cc=3000, after=sd.LONG_CARRY_TURNS + 20), self.root); self.scan()
        diags = _get(self.db, "demo", catalog={})["diagnostics"]
        order = {"high": 0, "warn": 1, "info": 2}
        self.assertEqual([order[d["severity"]] for d in diags], sorted(order[d["severity"]] for d in diags))
        for d in diags:
            for k in ("rule", "severity", "text", "evidence", "suggestion"):
                self.assertIn(k, d)


class RobustnessTests(Base):
    def test_parallel_skill_calls_in_one_message(self):
        b = Builder()
        for i in range(3):
            b._rec("assistant", {"id": "pm1", "model": "m", "content": [
                {"type": "tool_use", "id": "p%d" % i, "name": "Skill", "input": {"skill": "demo"}}],
                "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                          "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}})
        for i in range(3):
            b.result("p%d" % i, 40)
        b.assistant(); b.assistant()
        emit(b, self.root); self.scan()
        out = _get(self.db, "demo")
        self.assertEqual(out["totals"]["invocations"], 3)
        self.assertEqual(out["repeat"]["repeat_invocations"], 2)

    def test_privacy(self):
        b = Builder()
        b.assistant([("k0", "Skill", {"skill": "demo"})]); b.result("k0", 400)
        b.assistant(); b.assistant()
        emit(b, self.root); self.scan()
        self.assertNotIn("SECRET-BODY", json.dumps(_get(self.db, "demo")))

    def test_result_cache_and_key_cap(self):
        for i in range(3):
            emit(skill_session("s%d" % i), self.root); self.scan()
        old, sd.KEY_TRACK_CAP = sd.KEY_TRACK_CAP, 2
        try:
            out = _get(self.db, "demo", group="session")
        finally:
            sd.KEY_TRACK_CAP = old
        self.assertTrue(out["truncated"])
        self.assertEqual(sum(r["invocations"] for r in out["rows"]), 3)

    def test_no_prompt_text_selected(self):
        import inspect
        self.assertNotIn("prompt_text", inspect.getsource(sd))


class EndpointTests(Base):
    def test_endpoint(self):
        emit(skill_session("s1"), self.root); self.scan()
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        httpd = http.server.HTTPServer(("127.0.0.1", port), build_handler(self.db, "/nonexistent"))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d/api/skill-costs/detail" % port
        try:
            with urllib.request.urlopen(base + "?skill=demo&group=day&limit=5") as r:
                out = json.loads(r.read())
            for k in ("skill", "group", "available_groups", "totals", "catalog", "rows", "repeat",
                      "invocation_timeline", "top_sessions", "diagnostics", "truncated", "meta"):
                self.assertIn(k, out)
            for k in ("key", "invocations", "sessions", "est_cost_usd", "load_cost_usd", "carry_cost_usd",
                      "cost_per_invocation", "avg_carry_turns", "share", "cumulative_share",
                      "first_seen", "last_seen"):
                self.assertIn(k, out["rows"][0])
            for q in ("", "?skill=nope", "?skill=demo&group=bogus"):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(base + q)
                self.assertEqual(cm.exception.code, 400)
                self.assertIn("error", json.loads(cm.exception.read()))
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
