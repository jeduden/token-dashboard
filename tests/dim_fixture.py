"""Synthetic multi-project / multi-day / multi-model transcripts for cost-dimension tests.

Pricing: model m1 reads cache at $1/token and writes at $10/token; m2 doubles both.
A tool result is consumed (cache-created) by the assistant turn after its call, so the
*consuming* model is the model of the next turn.
"""
import json
import os
import tempfile
import unittest

from token_dashboard.db import init_db
from token_dashboard.scanner import scan_dir
from token_dashboard import cost_insights as ci, cost_drilldown as cd
from token_dashboard.repo_resolve import encode_slug

PRICING = {"models": {
    "m1": {"input": 0, "output": 0, "cache_read": 1_000_000, "cache_create_5m": 10_000_000, "cache_create_1h": 10_000_000},
    "m2": {"input": 0, "output": 0, "cache_read": 2_000_000, "cache_create_5m": 20_000_000, "cache_create_1h": 20_000_000},
}, "tier_fallback": {}}


def make_repo(root):
    os.makedirs(os.path.join(root, ".git", "objects"))
    with open(os.path.join(root, ".git", "HEAD"), "w") as f:
        f.write("ref: refs/heads/main\n")
    return root


def make_worktree(repo, wt, name):
    gd = os.path.join(repo, ".git", "worktrees", name)
    os.makedirs(gd)
    with open(os.path.join(gd, "commondir"), "w") as f:
        f.write("../..\n")
    os.makedirs(wt)
    with open(os.path.join(wt, ".git"), "w") as f:
        f.write("gitdir: " + gd + "\n")
    return wt


def write_session(root, cwd, sid, day, hour, calls, models=("m1", "m1", "m1")):
    """calls: [(tool, input, result_chars)] issued in ONE assistant message (parallel).
    Turns: [calls turn, consuming turn, carry turn] with the given models."""
    d = os.path.join(root, encode_slug(cwd))
    os.makedirs(d, exist_ok=True)
    lines, n, last = [], [0], [None]

    def rec(typ, message):
        n[0] += 1
        r = {"type": typ, "uuid": "%s-u%d" % (sid, n[0]), "parentUuid": last[0], "sessionId": sid, "cwd": cwd,
             "timestamp": "%sT%02d:%02d:%02dZ" % (day, hour, n[0] // 60, n[0] % 60), "message": message}
        last[0] = r["uuid"]
        lines.append(json.dumps(r))

    def asst(model, content):
        rec("assistant", {"id": "%s-m%d" % (sid, n[0] + 1), "model": model,
                          "content": content or [{"type": "text", "text": "x"}],
                          "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                                    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}})

    asst(models[0], [{"type": "tool_use", "id": "%s-t%d" % (sid, i), "name": t, "input": inp}
                     for i, (t, inp, _) in enumerate(calls)])
    rec("user", {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "%s-t%d" % (sid, i), "content": "z" * ch, "is_error": False}
        for i, (_, _, ch) in enumerate(calls)]})
    asst(models[1], [])
    asst(models[2], [])
    with open(os.path.join(d, sid + ".jsonl"), "w") as f:
        f.write("\n".join(lines) + "\n")


class DimBase(unittest.TestCase):
    """alpha repo (main checkout + worktree), beta repo, 3 sessions on 2 days."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.root = os.path.join(self.tmp, "projects")
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        git = os.path.join(self.tmp, "git")
        self.alpha = make_repo(os.path.join(git, "alpha"))
        self.alpha_wt = make_worktree(self.alpha, os.path.join(git, "alpha-wt"), "wt")
        self.beta = make_repo(os.path.join(git, "beta"))
        # s1: two parallel Bash (100 tok, 10000 tok) + a Read (1000 tok), all m1
        write_session(self.root, self.alpha, "s1", "2026-05-01", 9, [
            ("Bash", {"command": "git status"}, 400), ("Bash", {"command": "ls -la"}, 40000),
            ("Read", {"file_path": "/a/f.py"}, 4000)])
        # s2: worktree of alpha, producing m1, consuming m2
        write_session(self.root, self.alpha_wt, "s2", "2026-05-02", 14, [
            ("Bash", {"command": "git diff"}, 8000)], models=("m1", "m2", "m2"))
        # s3: beta, all m2, a >20k-token result and a tiny one
        write_session(self.root, self.beta, "s3", "2026-05-02", 14, [
            ("Bash", {"command": "cat big"}, 100000), ("Bash", {"command": "echo hi"}, 4),
            ("Read", {"file_path": "/b/g.md"}, 2000)], models=("m2", "m2", "m2"))
        ci._cache.clear(); cd._cache.clear()
        scan_dir(self.root, self.db)

    def params(self, tool, **kw):
        cd._cache.clear()
        kw.setdefault("limit", 500)
        return cd.tool_params(self.db, PRICING, tool, **kw)

    def tool_total(self, tool):
        ci._cache.clear()
        t = [x for x in ci.tool_costs(self.db, PRICING, catalog={})["tools"] if x["tool_name"] == tool][0]
        return t
