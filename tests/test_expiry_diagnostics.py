import unittest

from token_dashboard import cost_diagnostics as dx
from token_dashboard import cost_insights as ci
from token_dashboard import cost_drilldown as cd
from token_dashboard import skill_drilldown as sd
from tests.expiry_fixture import ExpiryBase, PRICING, Sess
from tests.test_expiry_consumers import session, CATALOG


class UnitTests(unittest.TestCase):
    def call(self, usd, expiry, events=1, gap=900.0):
        return {"tool_name": "Bash", "result_tokens": 10, "read_n": 0, "is_error": 0, "usd": usd,
                "thread_usd": 0.0, "flags": [], "expiry_usd": expiry, "expiry_events": events,
                "expiry_gap_s": gap}

    def test_call_flag_above_threshold(self):
        c = self.call(1.0, 0.5)
        dx.flag_call(c)
        f = [x for x in c["flags"] if x["rule"] == "expiry_heavy"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["severity"], "warn")
        self.assertIn("50%", f[0]["text"])
        self.assertEqual(f[0]["evidence"]["expiry_events"], 1)

    def test_call_flag_needs_share_and_min_usd(self):
        for usd, ex in ((1.0, 0.1), (0.04, 0.03)):
            c = self.call(usd, ex)
            dx.flag_call(c)
            self.assertFalse([x for x in c["flags"] if x["rule"] == "expiry_heavy"], (usd, ex))

    def test_tool_and_skill_rows(self):
        row = {"tool_name": "Bash", "error_rate": 0, "attributed_calls": 1, "est_cost_usd": 10.0,
               "expiry_cost_usd": 4.0, "expiry_events": 3}

        class A:
            big, rereads, unattributed, total_calls = {}, [], 0, 1
        out = [d for d in dx.tool_diagnostics(A, [row]) if d["rule"] == "expiry_heavy"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["evidence"]["tool"], "Bash")
        self.assertIn("suggestion", out[0])
        small = dict(row, expiry_cost_usd=0.1)
        self.assertFalse([d for d in dx.tool_diagnostics(A, [small]) if d["rule"] == "expiry_heavy"])
        sk = {"skill": "s", "est_cost_usd": 10.0, "sessions": 1, "carry_cost_usd": 0.0,
              "expiry_cost_usd": 5.0, "expiry_events": 2}
        out = [d for d in dx.skill_diagnostics([sk]) if d["rule"] == "expiry_heavy"]
        self.assertEqual(out[0]["evidence"]["skill"], "s")


class IntegrationTests(ExpiryBase):
    def setUp(self):
        super().setUp()
        self.scan(session("s1"), session("s2", gap=4000))
        cd._cache.clear()
        sd._cache.clear()

    def rules(self, diags):
        return [d for d in diags if d["rule"] == "expiry_heavy"]

    def test_tool_and_skill_diagnostics(self):
        tc = ci.tool_costs(self.db, PRICING, catalog=CATALOG)
        r = self.rules(tc["diagnostics"])
        self.assertTrue(r)
        self.assertTrue(all(d["suggestion"] and d["text"] for d in r))
        self.assertEqual({d["evidence"]["tool"] for d in r}, {"Bash", "Skill"})
        sc = ci.skill_costs(self.db, PRICING, catalog=CATALOG)
        self.assertEqual(self.rules(sc["diagnostics"])[0]["evidence"]["skill"], "myskill")
        d = cd.tool_params(self.db, PRICING, "Bash", catalog={})
        self.assertTrue(self.rules(d["diagnostics"]))
        sdt = sd.skill_detail(self.db, PRICING, "myskill", catalog=CATALOG)
        self.assertTrue(self.rules(sdt["diagnostics"]))

    def test_call_rows_flagged(self):
        calls = ci.top_calls(self.db, PRICING, tool="Bash", catalog=CATALOG)["calls"]
        self.assertTrue(all(self.rules(c["diagnostics"]) for c in calls))

    def test_no_expiry_no_flags(self):
        s = Sess("quiet")
        s.turn(0, [("t1", "Read", {"file_path": "/a"})], c5=50000)
        s.result(1, "t1")
        s.turn(10, read=50000, c5=1000)
        s.turn(20, read=51000, c5=100)
        self.scan(s)
        ci._cache.clear()
        calls = [c for c in ci.top_calls(self.db, PRICING, tool="Read", catalog={})["calls"]]
        self.assertFalse(self.rules(calls[0]["diagnostics"]))


if __name__ == "__main__":
    unittest.main()
