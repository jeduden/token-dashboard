"""One-session synthetic transcripts with explicit timestamps and cache tiers.

Pricing: $1 per cache-read token, $10 per 5m cache-write token, $20 per 1h cache-write token.
A 4000-char result is 1000 tokens, so create = 10000 (5m) / 20000 (1h), carry = 1000 per read turn.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from token_dashboard.db import init_db
from token_dashboard.scanner import scan_dir
from token_dashboard import cost_insights as ci

PRICING = {"models": {
    "m": {"input": 0, "output": 0, "cache_read": 1_000_000, "cache_create_5m": 10_000_000, "cache_create_1h": 20_000_000},
    "m2": {"input": 0, "output": 0, "cache_read": 1_000_000, "cache_create_5m": 10_000_000, "cache_create_1h": 20_000_000},
}, "tier_fallback": {}}
T0 = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)


def iso(sec):
    return (T0 + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Sess:
    def __init__(self, sid="s1", cwd=None):
        self.sid, self.lines, self.n, self.last, self.cwd = sid, [], 0, None, cwd

    def _rec(self, sec, typ, message):
        self.n += 1
        r = {"type": typ, "uuid": "%s-u%d" % (self.sid, self.n), "parentUuid": self.last,
             "sessionId": self.sid, "timestamp": iso(sec), "message": message}
        if self.cwd:
            r["cwd"] = self.cwd
        self.last = r["uuid"]
        self.lines.append(json.dumps(r))

    def turn(self, sec, tools=(), read=0, c5=0, c1=0, model="m"):
        """tools: [(tool_use_id, name, input)]"""
        content = [{"type": "tool_use", "id": i, "name": n, "input": inp} for i, n, inp in tools]
        self._rec(sec, "assistant", {
            "id": "%s-m%d" % (self.sid, self.n + 1), "model": model,
            "content": content or [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": read,
                      "cache_creation": {"ephemeral_5m_input_tokens": c5, "ephemeral_1h_input_tokens": c1}}})

    def result(self, sec, tid, chars=4000):
        self._rec(sec, "user", {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "z" * chars, "is_error": False}]})

    def write(self, root):
        d = os.path.join(root, "proj")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, self.sid + ".jsonl"), "w") as f:
            f.write("\n".join(self.lines) + "\n")


def one_result_session(gaps, tier="5m", sid="s1", ctx=50000, tools=(("t1", "Read", {"file_path": "/a/f.py"}),),
                       mix=None):
    """Turn0 issues the call (cold write), turn1 consumes the result, then one turn per entry of
    ``gaps`` (idle seconds before that turn). Turns after a gap > TTL re-create the whole prefix."""
    s = Sess(sid)
    w = (lambda n: dict(c1=n)) if tier == "1h" else (lambda n: dict(c5=n))
    s.turn(0, list(tools), **w(ctx))
    s.result(1, "t1")
    t = 10
    s.turn(t, read=ctx, **w(1000))                    # consumes the 1000-token result
    cur = ctx + 1000
    for g in gaps:
        t += g
        if g > (3600 if tier == "1h" else 300):       # expired: whole prefix rewritten
            kw = mix(cur) if mix else w(cur + 100)
            s.turn(t, read=0, **kw)
        else:
            s.turn(t, read=cur, **w(100))
        cur += 100
    return s


class ExpiryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.root = os.path.join(self.tmp, "projects")
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def scan(self, *sessions):
        for s in sessions:
            s.write(self.root)
        ci._cache.clear()
        scan_dir(self.root, self.db)

    def call_ids(self):
        import sqlite3
        with sqlite3.connect(self.db) as c:
            return [r[0] for r in c.execute(
                "SELECT id FROM tool_calls WHERE tool_name != '_tool_result' ORDER BY id")]
