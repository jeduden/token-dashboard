"""parse_compaction: a pure function from one JSONL record to a compactions row."""
import json
import unittest

from token_dashboard.compaction_parse import parse_compaction
from tests.compaction_fixture import CSess, SECRET_CONTENT, SECRET_TOOL


def rec(**kw):
    s = CSess("sess", cwd="/work/p")
    return json.loads(json.dumps(s.boundary(100, **kw)))


class ParseTests(unittest.TestCase):
    def test_all_fields(self):
        r = rec(trigger="manual", pre=541039, post=15756, dur=31835, cum=999)
        row = parse_compaction(r, "slug")
        self.assertEqual(row["uuid"], r["uuid"])
        self.assertEqual(row["session_id"], "sess")
        self.assertEqual(row["project_slug"], "slug")
        self.assertEqual(row["cwd"], "/work/p")
        self.assertEqual(row["git_branch"], "main")
        self.assertEqual(row["timestamp"], r["timestamp"])
        self.assertEqual(row["trigger"], "manual")
        self.assertEqual(row["is_sidechain"], 0)
        self.assertEqual(row["pre_tokens"], 541039)
        self.assertEqual(row["post_tokens"], 15756)
        self.assertEqual(row["duration_ms"], 31835)
        # per-event drop is pre - post; cumulativeDroppedTokens runs over the whole session
        self.assertEqual(row["dropped_tokens"], 541039 - 15756)
        self.assertEqual(row["cumulative_dropped_tokens"], 999)
        self.assertEqual(row["cc_version"], "2.1.0")
        self.assertIsNone(row["agent_id"])

    def test_sidechain_agent(self):
        row = parse_compaction(rec(agent="ag7"), "slug")
        self.assertEqual((row["is_sidechain"], row["agent_id"]), (1, "ag7"))

    def test_missing_metadata_is_defensive(self):
        row = parse_compaction(rec(metadata=False), "slug")
        self.assertIsNotNone(row)
        for k in ("trigger", "pre_tokens", "post_tokens", "duration_ms", "dropped_tokens",
                  "cumulative_dropped_tokens"):
            self.assertIsNone(row[k], k)

    def test_garbage_metadata_values(self):
        r = rec()
        r["compactMetadata"] = {"trigger": 5, "preTokens": "many", "postTokens": True, "durationMs": -3}
        row = parse_compaction(r, "slug")
        self.assertIsNone(row["trigger"])
        self.assertIsNone(row["pre_tokens"])
        self.assertIsNone(row["post_tokens"])
        self.assertIsNone(row["duration_ms"])
        r["compactMetadata"] = "oops"
        self.assertIsNone(parse_compaction(r, "slug")["pre_tokens"])

    def test_unknown_trigger_is_kept_as_none(self):
        r = rec()
        r["compactMetadata"]["trigger"] = "scheduled"
        self.assertIsNone(parse_compaction(r, "slug")["trigger"])

    def test_missing_post_leaves_drop_to_cumulative_free_none(self):
        row = parse_compaction(rec(post=None), "slug")
        self.assertIsNone(row["dropped_tokens"])

    def test_other_records_are_ignored(self):
        self.assertIsNone(parse_compaction({"type": "system", "subtype": "turn_duration", "uuid": "u",
                                            "sessionId": "s", "timestamp": "t"}, "p"))
        self.assertIsNone(parse_compaction({"type": "user", "uuid": "u"}, "p"))
        r = rec()
        del r["sessionId"]
        self.assertIsNone(parse_compaction(r, "p"))
        r = rec()
        del r["timestamp"]
        self.assertIsNone(parse_compaction(r, "p"))

    def test_privacy_no_content_or_tool_lists(self):
        row = parse_compaction(rec(), "slug")
        blob = json.dumps(row)
        self.assertNotIn(SECRET_CONTENT, blob)
        self.assertNotIn(SECRET_TOOL, blob)
        self.assertNotIn("content", row)


if __name__ == "__main__":
    unittest.main()
