"""Cross-cutting cost dimensions of the per-tool drill-down (project/repo/day/session/hour/model/result_size)."""
import http.server
import json
import socket
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request

from token_dashboard import cost_drilldown as cd
from token_dashboard import cost_dimensions as dm
from token_dashboard.server import build_handler
from tests.dim_fixture import DimBase, PRICING

DIMS = ["project", "repo", "day", "session", "hour_of_day", "model", "result_size"]


class BucketTests(unittest.TestCase):
    def test_boundaries(self):
        k = dm.result_bucket
        for tok, want in [(None, "lt_500"), (0, "lt_500"), (499, "lt_500"), (500, "500_2k"), (1999, "500_2k"),
                          (2000, "2k_8k"), (7999, "2k_8k"), (8000, "8k_20k"), (19999, "8k_20k"),
                          (20000, "gt_20k"), (10 ** 7, "gt_20k")]:
            self.assertEqual(k(tok), want, tok)

    def test_labels(self):
        self.assertEqual([b[1] for b in dm.BUCKETS], ["<500 tok", "500-2k tok", "2-8k tok", "8-20k tok", ">20k tok"])


class ModeTests(DimBase):
    def test_available_groups_order_and_labels(self):
        gs = self.params("Bash")["available_groups"]
        keys = [g["key"] for g in gs]
        self.assertEqual(keys, ["command", "command2", "pipeline", "target"] + DIMS)
        self.assertTrue(all(g["label"] for g in gs))
        self.assertEqual([g["key"] for g in self.params("Read")["available_groups"]],
                         ["directory", "extension", "target"] + DIMS)
        self.assertEqual(self.params("Bash")["group"], "command")   # auto unchanged

    def test_every_mode_sums_to_tool_total(self):
        for tool in ("Bash", "Read"):
            total = self.tool_total(tool)
            for g in [x["key"] for x in self.params(tool)["available_groups"]]:
                out = self.params(tool, group=g)
                self.assertEqual(out["group"], g)
                self.assertAlmostEqual(sum(r["est_cost_usd"] for r in out["rows"]), total["est_cost_usd"], places=4, msg=(tool, g))
                self.assertAlmostEqual(sum(r["carry_cost_usd"] for r in out["rows"]), total["carry_cost_usd"], places=4)
                self.assertEqual(sum(r["calls"] for r in out["rows"]), total["attributed_calls"], (tool, g))
                self.assertAlmostEqual(out["totals"]["est_cost_usd"], total["est_cost_usd"], places=4)

    def test_expected_bash_total(self):
        self.assertAlmostEqual(self.params("Bash")["totals"]["est_cost_usd"], 1100 + 110000 + 44000 + 550000 + 22)

    def test_project_names_and_repo_fields(self):
        r = {x["key"]: x for x in self.params("Bash", group="project")["rows"]}
        self.assertEqual(len(r), 3)
        names = sorted(x["label"] for x in r.values())
        self.assertEqual(names, ["alpha", "alpha-wt", "beta"])
        wt = [x for x in r.values() if x["label"] == "alpha-wt"][0]
        al = [x for x in r.values() if x["label"] == "alpha"][0]
        self.assertEqual(wt["repo_name"], "alpha")
        self.assertEqual(wt["repo"], al["repo"])
        self.assertTrue(wt["key"].startswith("-"))             # stable machine key = slug
        self.assertAlmostEqual(al["est_cost_usd"], 111100)
        self.assertAlmostEqual(wt["est_cost_usd"], 44000)

    def test_repo_rollup_merges_worktrees(self):
        out = self.params("Bash", group="repo")
        r = {x["label"]: x for x in out["rows"]}
        self.assertEqual(set(r), {"alpha", "beta"})
        self.assertAlmostEqual(r["alpha"]["est_cost_usd"], 111100 + 44000)
        self.assertEqual(r["alpha"]["calls"], 3)
        self.assertEqual(r["alpha"]["sessions"], 2)
        self.assertEqual(r["alpha"]["repo_name"], "alpha")
        self.assertNotEqual(r["alpha"]["key"], r["alpha"]["label"])

    def test_day_and_hour(self):
        d = {x["key"]: x for x in self.params("Bash", group="day")["rows"]}
        self.assertEqual(set(d), {"2026-05-01", "2026-05-02"})
        self.assertAlmostEqual(d["2026-05-01"]["est_cost_usd"], 111100)
        h = {x["key"]: x for x in self.params("Bash", group="hour_of_day")["rows"]}
        self.assertEqual(set(h), {"09", "14"})
        self.assertEqual(h["14"]["calls"], 3)

    def test_model_is_consuming_model(self):
        m = {x["key"]: x for x in self.params("Bash", group="model")["rows"]}
        # s2 produced by m1 but consumed (priced) by m2
        self.assertAlmostEqual(m["m1"]["est_cost_usd"], 111100)
        self.assertAlmostEqual(m["m2"]["est_cost_usd"], 44000 + 550022)
        self.assertEqual(m["m2"]["calls"], 3)

    def test_session_rows(self):
        out = self.params("Bash", group="session")
        r = {x["key"]: x for x in out["rows"]}
        self.assertEqual(set(r), {"s1", "s2", "s3"})
        self.assertEqual(r["s1"]["session_id"], "s1")
        self.assertEqual(r["s2"]["project"], "alpha-wt")
        self.assertIn("alpha-wt", r["s2"]["label"])
        self.assertEqual(r["s3"]["calls"], 2)
        self.assertEqual(self.params("Bash", group="session", limit=1)["rows"][0]["key"], "s3")

    def test_result_size_buckets(self):
        r = {x["key"]: x for x in self.params("Bash", group="result_size")["rows"]}
        self.assertEqual({k: v["calls"] for k, v in r.items()},
                         {"lt_500": 2, "2k_8k": 1, "gt_20k": 1, "8k_20k": 1})
        self.assertEqual(r["gt_20k"]["label"], ">20k tok")
        self.assertAlmostEqual(r["gt_20k"]["share_of_tool_cost"], 550000 / 705122, places=3)
        self.assertEqual(r["8k_20k"]["max_result_tokens"], 10000)

    def test_unattributed_counted_separately(self):
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE tool_calls SET tool_use_id=NULL WHERE target='ls -la'")
        for g in DIMS:
            out = self.params("Bash", group=g)
            self.assertEqual(out["totals"]["unattributed_calls"], 1, g)
            self.assertEqual(sum(r["calls"] for r in out["rows"]), 4, g)
            self.assertAlmostEqual(sum(r["est_cost_usd"] for r in out["rows"]), 1100 + 44000 + 550022, places=4)

    def test_limit_truncates_rows_not_totals(self):
        out = self.params("Bash", group="session", limit=2)
        self.assertEqual(len(out["rows"]), 2)
        self.assertEqual(out["distinct_keys"], 3)
        self.assertEqual(out["totals"]["calls"], 5)

    def test_key_cap_applies_to_dimensions(self):
        old = cd.KEY_TRACK_CAP
        cd.KEY_TRACK_CAP = 1
        try:
            out = self.params("Bash", group="session")
        finally:
            cd.KEY_TRACK_CAP = old
        self.assertTrue(out["truncated"])
        self.assertAlmostEqual(sum(r["est_cost_usd"] for r in out["rows"]), out["totals"]["est_cost_usd"], places=4)

    def test_invalid_mode_for_tool(self):
        with self.assertRaises(ValueError):
            self.params("Read", group="command")
        with self.assertRaises(ValueError):
            self.params("Bash", group="bogus")

    def test_range_filter(self):
        out = self.params("Bash", group="day", since="2026-05-02T00:00:00Z")
        self.assertEqual([r["key"] for r in out["rows"]], ["2026-05-02"])


class TimeSectionTests(DimBase):
    def test_time_sums_equal_totals(self):
        for tool in ("Bash", "Read"):
            for g in ("auto", "model"):
                out = self.params(tool, group=g)
                t = out["time"]
                self.assertEqual(sum(x["calls"] for x in t["days"]), out["totals"]["attributed_calls"])
                self.assertAlmostEqual(sum(x["est_cost_usd"] for x in t["days"]), out["totals"]["est_cost_usd"], places=4)
                self.assertEqual([x["day"] for x in t["days"]], sorted(x["day"] for x in t["days"]))

    def test_time_values(self):
        d = {x["day"]: x for x in self.params("Bash")["time"]["days"]}
        self.assertEqual(d["2026-05-01"]["calls"], 2)
        self.assertAlmostEqual(d["2026-05-02"]["est_cost_usd"], 44000 + 550022)

    def test_unattributed_in_time(self):
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE tool_calls SET tool_use_id=NULL WHERE target='ls -la'")
        t = self.params("Bash")["time"]["days"]
        self.assertEqual(sum(x["unattributed_calls"] for x in t), 1)
        self.assertEqual(sum(x["calls"] for x in t), 4)


class EndpointTests(DimBase):
    def test_http(self):
        h = build_handler(self.db, "/nonexistent")
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), h)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d/api/tool-costs/params?tool=Bash&limit=500&group=" % port
            for g in DIMS:
                d = json.load(urllib.request.urlopen(base + g))
                self.assertEqual(d["group"], g)
                self.assertIn("time", d)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "nonsense")
            self.assertEqual(cm.exception.code, 400)
            self.assertIn("unknown group", json.load(cm.exception)["error"])
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
