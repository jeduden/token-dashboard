"""Every consumer of the cost model agrees on create / carry / expiry totals."""
from token_dashboard import call_detail as cdx
from token_dashboard import cost_drilldown as cd
from token_dashboard import cost_insights as ci
from token_dashboard import skill_drilldown as sd
from tests.expiry_fixture import ExpiryBase, PRICING, Sess

CATALOG = {"myskill": {"tokens": 2000, "path": "/home/u/.claude/skills/myskill/SKILL.md"}}
MODES = ["command", "command2", "pipeline", "target", "project", "repo", "day", "session",
         "hour_of_day", "model", "result_size"]


def session(sid, gap=900, skill=True):
    s = Sess(sid, cwd="/work/" + sid)
    tools = [("b1", "Bash", {"command": "ls -la"}), ("b2", "Bash", {"command": "git status"})]
    if skill:
        tools.append(("k1", "Skill", {"skill": "myskill"}))
    s.turn(0, tools, c5=50000)
    s.result(1, "b1", 4000)
    s.result(2, "b2", 8000)
    if skill:
        s.result(3, "k1", 40)
    s.turn(10, read=50000, c5=4000)
    s.turn(20, read=54000, c5=100)
    s.turn(20 + gap, c5=54100)                    # idle: expiry
    s.turn(30 + gap, read=54100, c5=100)
    s.turn(40 + gap, read=54200, c5=100)
    return s


class Base(ExpiryBase):
    def setUp(self):
        super().setUp()
        self.scan(session("s1"), session("s2", gap=4000))
        for m in (ci, cd, cdx, sd):
            m._cache.clear()

    def tools(self):
        ci._cache.clear()
        return {t["tool_name"]: t for t in ci.tool_costs(self.db, PRICING, catalog=CATALOG)["tools"]}


class ToolTests(Base):
    def test_components_add_up_and_expiry_present(self):
        for t in self.tools().values():
            self.assertAlmostEqual(t["est_cost_usd"], t["create_cost_usd"] + t["carry_cost_usd"] + t["expiry_cost_usd"], places=4)
        b = self.tools()["Bash"]
        # 2 sessions x (1000 + 2000 tokens): two expiries each at $10
        self.assertAlmostEqual(b["expiry_cost_usd"], 2 * 3000 * 10)
        self.assertEqual(b["expiry_events"], 4)

    def test_calls_rows_sum_to_tool(self):
        calls = ci.top_calls(self.db, PRICING, tool="Bash", limit=50, catalog=CATALOG)["calls"]
        t = self.tools()["Bash"]
        self.assertAlmostEqual(sum(c["est_cost_usd"] for c in calls), t["est_cost_usd"], places=4)
        self.assertAlmostEqual(sum(c["expiry_cost_usd"] for c in calls), t["expiry_cost_usd"], places=4)
        self.assertTrue(all(c["est_cost_usd"] == round(c["create_cost_usd"] + c["carry_cost_usd"] + c["expiry_cost_usd"], 6)
                            or abs(c["est_cost_usd"] - (c["create_cost_usd"] + c["carry_cost_usd"] + c["expiry_cost_usd"])) < 1e-5
                            for c in calls))


class ParamsTests(Base):
    def test_every_mode_sums_to_tool_total(self):
        tool = self.tools()["Bash"]
        for mode in MODES:
            cd._cache.clear()
            d = cd.tool_params(self.db, PRICING, "Bash", group=mode, limit=500, catalog={})
            self.assertAlmostEqual(d["totals"]["est_cost_usd"], tool["est_cost_usd"], places=4, msg=mode)
            self.assertAlmostEqual(d["totals"]["expiry_cost_usd"], tool["expiry_cost_usd"], places=4, msg=mode)
            self.assertAlmostEqual(d["totals"]["create_cost_usd"], tool["create_cost_usd"], places=4, msg=mode)
            self.assertAlmostEqual(d["totals"]["carry_cost_usd"], tool["carry_cost_usd"], places=4, msg=mode)
            self.assertAlmostEqual(sum(r["est_cost_usd"] for r in d["rows"]), tool["est_cost_usd"], places=4, msg=mode)
            self.assertAlmostEqual(sum(r["expiry_cost_usd"] for r in d["rows"]), tool["expiry_cost_usd"], places=4, msg=mode)
            for r in d["rows"]:
                self.assertIn("expiry_share", r)
                self.assertAlmostEqual(r["expiry_share"], r["expiry_cost_usd"] / r["est_cost_usd"], places=3)
            self.assertAlmostEqual(sum(x["expiry_cost_usd"] for x in d["time"]["days"]), tool["expiry_cost_usd"], places=4)

    def test_totals_share(self):
        d = cd.tool_params(self.db, PRICING, "Bash", limit=500, catalog={})
        self.assertAlmostEqual(d["totals"]["expiry_share"], d["totals"]["expiry_cost_usd"] / d["totals"]["est_cost_usd"], places=3)


class CallTests(Base):
    def call(self, sid, target):
        import sqlite3
        with sqlite3.connect(self.db) as c:
            return c.execute("SELECT id FROM tool_calls WHERE session_id=? AND target=? AND tool_name!='_tool_result'",
                             (sid, target)).fetchone()[0]

    def detail(self, cid):
        cdx._cache.clear()
        ci._cache.clear()
        return cdx.call_detail(self.db, PRICING, cid, catalog=CATALOG, now="2030-01-01T00:00:00Z")

    def test_cost_block_matches_top_calls_and_timeline(self):
        cid = self.call("s1", "ls -la")
        d = self.detail(cid)
        top = [c for c in ci.top_calls(self.db, PRICING, tool="Bash", limit=50, catalog=CATALOG)["calls"]
               if c["call_id"] == cid][0]
        c = d["cost"]
        self.assertAlmostEqual(c["est_cost_usd"], top["est_cost_usd"], places=5)
        self.assertAlmostEqual(c["expiry_cost_usd"], 10000, places=3)
        self.assertEqual(c["expiry_events"], 1)
        self.assertAlmostEqual(c["expiry_gap_total_s"], 900)
        self.assertAlmostEqual(c["create_cost_usd"] + c["carry_cost_usd"] + c["expiry_cost_usd"], c["est_cost_usd"], places=5)
        tl = d["carry_timeline"]
        self.assertEqual([t["kind"] for t in tl], ["read", "rewrite", "read", "read"])
        self.assertAlmostEqual(tl[1]["gap_s"], 900)
        self.assertEqual(tl[0]["gap_s"], 10)
        self.assertAlmostEqual(tl[1]["cost_usd"], 10000)
        self.assertAlmostEqual(sum(t["cost_usd"] for t in tl if t["kind"] == "rewrite"), c["expiry_cost_usd"], places=4)
        self.assertAlmostEqual(sum(t["cost_usd"] for t in tl if t["kind"] == "read"), c["carry_cost_usd"], places=4)
        self.assertAlmostEqual(tl[-1]["cumulative_usd"], c["est_cost_usd"], places=4)
        self.assertEqual(d["carry_end"]["reason"], "session_end")

    def test_idle_gap_info_diagnostic(self):
        d = self.detail(self.call("s1", "ls -la"))
        rules = {x["rule"]: x for x in d["diagnostics"]}
        self.assertEqual(rules["idle_gap"]["severity"], "info")
        self.assertTrue(rules["idle_gap"]["suggestion"])
        self.assertEqual(rules["idle_gap"]["evidence"]["expiry_events"], 1)

    def test_no_idle_gap_diag_without_expiry(self):
        s = Sess("s9")
        s.turn(0, [("t1", "Read", {"file_path": "/q"})], c5=50000)
        s.result(1, "t1")
        s.turn(10, read=50000, c5=1000)
        s.turn(20, read=51000, c5=100)
        self.scan(s)
        d = self.detail(self.call("s9", "/q"))
        self.assertNotIn("idle_gap", [x["rule"] for x in d["diagnostics"]])
        self.assertEqual(d["cost"]["expiry_events"], 0)
        self.assertTrue(all(t["kind"] == "read" for t in d["carry_timeline"]))


class SkillTests(Base):
    def test_skill_totals_agree(self):
        sc = {s["skill"]: s for s in ci.skill_costs(self.db, PRICING, catalog=CATALOG)["skills"]}["myskill"]
        cd._cache.clear()
        sd._cache.clear()
        d = sd.skill_detail(self.db, PRICING, "myskill", limit=50, catalog=CATALOG)
        t = d["totals"]
        self.assertAlmostEqual(t["est_cost_usd"], sc["est_cost_usd"], places=4)
        self.assertAlmostEqual(t["expiry_cost_usd"], sc["expiry_cost_usd"], places=4)
        self.assertAlmostEqual(t["expiry_cost_usd"], 2 * 2000 * 10, places=4)
        self.assertAlmostEqual(t["load_cost_usd"], sc["create_cost_usd"], places=4)
        self.assertAlmostEqual(t["load_cost_usd"] + t["carry_cost_usd"] + t["expiry_cost_usd"], t["est_cost_usd"], places=4)
        for mode, _ in sd.GROUPS:
            sd._cache.clear()
            dm = sd.skill_detail(self.db, PRICING, "myskill", group=mode, limit=50, catalog=CATALOG)
            self.assertAlmostEqual(sum(r["expiry_cost_usd"] for r in dm["rows"]), t["expiry_cost_usd"], places=4, msg=mode)
            self.assertAlmostEqual(sum(r["est_cost_usd"] for r in dm["rows"]), t["est_cost_usd"], places=4, msg=mode)
            for r in dm["rows"]:
                self.assertIn("expiry_share", r)
        split = d["repeat"]["carry_split"]
        self.assertAlmostEqual(sum(v["expiry_cost_usd"] for v in split.values()), t["expiry_cost_usd"], places=4)
        self.assertAlmostEqual(sum(s["expiry_cost_usd"] for s in d["top_sessions"]), t["expiry_cost_usd"], places=4)


class PrivacyTests(Base):
    def test_no_result_text_in_any_payload(self):
        import json
        cid = CallTests.call(self, "s1", "ls -la")
        payloads = [ci.tool_costs(self.db, PRICING, catalog=CATALOG), ci.skill_costs(self.db, PRICING, catalog=CATALOG),
                    ci.top_calls(self.db, PRICING, catalog=CATALOG),
                    cd.tool_params(self.db, PRICING, "Bash", catalog={}),
                    sd.skill_detail(self.db, PRICING, "myskill", catalog=CATALOG),
                    cdx.call_detail(self.db, PRICING, cid, catalog=CATALOG)]
        blob = json.dumps(payloads)
        self.assertNotIn("zzzz", blob)          # the synthetic result body is "zzzz..."


class HttpTests(Base):
    def test_endpoints_expose_expiry(self):
        import http.server, socket, threading, urllib.request, json
        from token_dashboard.server import build_handler
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        httpd = http.server.HTTPServer(("127.0.0.1", port), build_handler(self.db, projects_dir="/nonexistent"))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        get = lambda p: json.loads(urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, p)).read())
        tools = {t["tool_name"]: t for t in get("/api/tool-costs")["tools"]}
        self.assertIn("expiry_cost_usd", tools["Bash"])
        cid = CallTests.call(self, "s1", "ls -la")
        self.assertEqual(get("/api/tool-costs/call?id=%d" % cid)["cost"]["expiry_events"], 1)
        self.assertIn("gaps", get("/api/cache"))
        self.assertIn("expiry_share", get("/api/tool-costs/params?tool=Bash")["rows"][0])
