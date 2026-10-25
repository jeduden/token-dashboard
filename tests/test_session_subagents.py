import json
import os
import sqlite3
import tempfile
import unittest

from tests.pricing_fixture import PRICING
from token_dashboard.db import init_db
from token_dashboard.scanner import scan_dir
from token_dashboard.session_subagents import session_subagents


def rec(**kw):
    base = {"sessionId": "S", "cwd": "/w", "version": "1"}
    base.update(kw)
    return json.dumps(base)


def usage(i=10, o=5):
    return {"input_tokens": i, "output_tokens": o}


class SubagentTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        proj = os.path.join(root, "-w")
        os.makedirs(proj)
        self.db = os.path.join(root, "t.db")
        init_db(self.db)
        self.pricing = PRICING
        lines = [
            rec(uuid="u0", type="user", timestamp="2026-01-01T00:00:00Z", message={"role": "user", "content": "go"}),
            rec(uuid="a0", type="assistant", timestamp="2026-01-01T00:00:01Z", message={
                "id": "m0", "model": "claude-haiku-4-5", "usage": usage(), "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "Agent",
                     "input": {"description": "Review the diff", "subagent_type": "code-reviewer", "prompt": "Look at X"}}]}),
            rec(uuid="r0", type="user", timestamp="2026-01-01T00:00:02Z",
                toolUseResult={"status": "async_launched", "agentId": "abc123"},
                message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}),
        ]
        side = [
            rec(uuid="s0", type="user", isSidechain=True, agentId="abc123", timestamp="2026-01-01T00:00:03Z",
                message={"role": "user", "content": "Look at X"}),
            rec(uuid="s1", type="assistant", isSidechain=True, agentId="abc123", timestamp="2026-01-01T00:00:04Z", message={
                "id": "m1", "model": "claude-haiku-4-5", "usage": usage(100, 50), "content": [
                    {"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {"file_path": "x.py"}}]}),
            rec(uuid="s2", type="assistant", isSidechain=True, agentId="abuilder-0123456789abcdef",
                timestamp="2026-01-01T00:00:05Z", message={"id": "m2", "model": "claude-haiku-4-5", "usage": usage(), "content": []}),
        ]
        with open(os.path.join(proj, "S.jsonl"), "w") as f:
            f.write("\n".join(lines) + "\n")
        os.makedirs(os.path.join(proj, "S", "subagents"))
        with open(os.path.join(proj, "S", "subagents", "agent-abc123.jsonl"), "w") as f:
            f.write("\n".join(side) + "\n")
        scan_dir(root, self.db)

    def test_spawn_is_linked_to_the_agent(self):
        a = {x["agent_id"]: x for x in session_subagents(self.db, self.pricing, "S")}
        x = a["abc123"]
        self.assertEqual((x["label"], x["subagent_type"], x["linked"]), ("Review the diff", "code-reviewer", True))
        self.assertEqual(x["prompt_head"], "Look at X")
        self.assertEqual(x["tools"], [("Read", 1)])
        self.assertEqual(x["turns"], 1)
        self.assertGreater(x["cost_usd"], 0)

    def test_unlinked_agent_uses_name_from_id(self):
        a = {x["agent_id"]: x for x in session_subagents(self.db, self.pricing, "S")}
        self.assertEqual((a["abuilder-0123456789abcdef"]["label"], a["abuilder-0123456789abcdef"]["linked"]), ("builder", False))

    def test_rescan_is_idempotent(self):
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE files SET bytes_read = 0, mtime = 0")
        scan_dir(os.path.dirname(self.db), self.db)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM subagent_spawns").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
