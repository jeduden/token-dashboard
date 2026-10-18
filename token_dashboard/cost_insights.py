"""Per-tool / per-skill cost attribution (estimates).

Model
-----
A tool call at assistant turn ``k`` produces a result that enters the
context. Its cost (details in ``cost_model``) is:

* create: ``result_tokens * cache-write rate`` once, at turn k+1, priced with
  the cache tier(s) (5m / 1h) that turn wrote;
* carry: ``result_tokens * cache_read rate`` for every later assistant turn
  k+2 ... in the same thread (session + agent_id) that finds the cache warm,
  until the thread ends or a compaction is detected (see COMPACTION_*);
* expiry: ``result_tokens * cache-write rate`` instead of the read rate on a
  later turn that is a cache TTL expiry (idle gap longer than the lifetime of
  the last-written tier, decided by ``cache_events`` exactly as on the Cache tab).

Rates come from the model of the *consuming* turn. Skills use the same math
with ``tokens`` = the skill's definition size (catalog ``tokens_per_call``
when known, else estimated from the cache_create jump on the next turn).

All numbers are estimates: tokens are chars // 4, expiry detection is a
heuristic that needs the turn's observed usage to look like a miss, and
partial-prefix cache misses are ignored. Rows with NULL ``tool_use_id``
(scanned before the column existed) have no linked result and are counted as
"unattributed".

Processing is streaming: one thread's turns and calls are in memory at a
time; ``prompt_text`` is never selected.
"""
from __future__ import annotations

import heapq
import os
import time
from collections import Counter
from typing import Dict, Optional

from .db import connect, _range_clause
from . import cost_diagnostics as dx
from .cost_model import (COMPACTION_DROP_RATIO, COMPACTION_MIN_PREV_READ,  # noqa: F401 (re-exported)
                         Tables, cost_of, load_thread)

# --- assumptions (named so they can be tuned/tested) -----------------------
SKILL_ESTIMATE_CAP = 60_000       # max tokens an estimated skill load can claim
TARGET_MAX_CHARS = 200            # targets are truncated in API output
TARGET_TRACK_CAP = 20_000         # max distinct targets tracked per tool
TOP_TARGETS = 5
CACHE_TTL_SECONDS = 20.0

_cache: dict = {}


def _tool_rows(conn, session_id, agent_id):
    sql = """
      SELECT t.id AS call_id, t.tool_name, t.target, t.tool_use_id, t.project_slug, t.message_uuid, t.timestamp,
             t.is_error AS use_err, r.result_tokens, r.is_error AS res_err,
             (r.id IS NOT NULL) AS has_result
        FROM tool_calls t
        JOIN messages m ON m.uuid = t.message_uuid
        LEFT JOIN tool_calls r ON r.session_id = t.session_id
             AND r.tool_use_id = t.tool_use_id AND r.tool_name = '_tool_result'
             AND t.tool_use_id IS NOT NULL
       WHERE t.tool_name != '_tool_result'
         AND m.session_id = ? AND COALESCE(m.agent_id,'') = ?
       ORDER BY t.timestamp, t.id
    """
    return conn.execute(sql, (session_id, agent_id)).fetchall()


class _Agg:
    """Streaming accumulator for tool, skill, call and diagnostic inputs."""

    def __init__(self, calls_limit, tool_filter):
        self.tools: Dict[str, dict] = {}
        self.skills: Dict[str, dict] = {}
        self.calls_limit = calls_limit
        self.tool_filter = tool_filter
        self.heap: list = []
        self._seq = 0
        self.unattributed = 0
        self.total_calls = 0
        self.rereads: list = []
        self.big: Dict[str, dict] = {}

    def add(self, c):
        self.total_calls += 1
        if not c["attributed"]:
            self.unattributed += 1
        t = self.tools.setdefault(c["tool_name"], {
            "calls": 0, "attributed": 0, "tokens": [], "usd": 0.0, "carry": 0.0,
            "create": 0.0, "expiry": 0.0, "expiry_events": 0, "errors": 0, "targets": {}})
        t["calls"] += 1
        if c["attributed"]:
            t["attributed"] += 1
            t["tokens"].append(c["result_tokens"])
            t["errors"] += c["is_error"]
        t["usd"] += c["usd"]
        t["carry"] += c["carry_usd"]
        t["create"] += c["create_usd"]
        t["expiry"] += c["expiry_usd"]
        t["expiry_events"] += c["expiry_events"]
        tgt = (c["target"] or "(none)")[:TARGET_MAX_CHARS]
        if tgt in t["targets"] or len(t["targets"]) < TARGET_TRACK_CAP:
            t["targets"][tgt] = t["targets"].get(tgt, 0.0) + c["usd"]
        if c["tool_name"] == "Skill" and c["target"]:
            s = self.skills.setdefault(c["target"], {
                "invocations": 0, "sessions": set(), "load": 0, "usd": 0.0,
                "carry": 0.0, "create": 0.0, "expiry": 0.0, "expiry_events": 0,
                "last": "", "estimated": False})
            s["invocations"] += 1
            s["sessions"].add(c["session_id"])
            s["load"] += c["tokens"]
            s["usd"] += c["usd"]
            s["carry"] += c["carry_usd"]
            s["create"] += c["create_usd"]
            s["expiry"] += c["expiry_usd"]
            s["expiry_events"] += c["expiry_events"]
            s["last"] = max(s["last"], c["timestamp"] or "")
            s["estimated"] = s["estimated"] or c["estimated"]
        dx.note_big_output(self.big, c)
        if self.calls_limit and (not self.tool_filter or c["tool_name"] == self.tool_filter):
            self._seq += 1
            item = (c["usd"], self._seq, c)
            if len(self.heap) < self.calls_limit:
                heapq.heappush(self.heap, item)
            elif item > self.heap[0]:
                heapq.heapreplace(self.heap, item)

    def add_rereads(self, found):
        self.rereads.extend(found)
        if len(self.rereads) > 500:
            self.rereads.sort(key=lambda r: -r["count"])
            del self.rereads[100:]


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return 0
    i = max(0, min(len(sorted_vals) - 1, int(round(p * len(sorted_vals) + 0.5)) - 1))
    return sorted_vals[i]


def _process_thread(conn, pricing, memo, catalog, sid, aid, since, until, agg):
    turns, index = load_thread(conn, pricing, memo, sid, aid)
    tables = Tables(turns)
    thread_usd = sum(t["usd"] for t in turns)
    rows = _tool_rows(conn, sid, aid)
    turn_results: Counter = Counter()
    for r in rows:
        turn_results[r["message_uuid"]] += r["result_tokens"] or 0
    reads: Counter = Counter()
    read_tokens: Counter = Counter()
    for r in rows:
        ts = r["timestamp"] or ""
        if (since and ts < since) or (until and ts >= until):
            continue
        k = index.get(r["message_uuid"])
        attributed = bool(r["tool_use_id"]) and bool(r["has_result"])
        rt = r["result_tokens"] if attributed else None
        tokens, estimated = rt or 0, False
        if r["tool_name"] == "Skill" and r["target"] and k is not None:
            known = catalog.get(r["target"])
            if known:
                tokens = known["tokens"]
            elif k + 1 < len(turns):
                nxt_t = turns[k + 1]
                # On an expiry turn the cache write also re-creates the old prefix: not skill load.
                rewritten = min(nxt_t["cc"], nxt_t["prev_ctx"]) if nxt_t["expired"] else 0
                jump = nxt_t["cc"] - rewritten - turn_results[r["message_uuid"]]
                tokens, estimated = max(0, min(SKILL_ESTIMATE_CAP, jump)), True
            attributed = attributed or tokens > 0
        create = carry = expiry = gap_s = 0.0
        carry_turns = events = 0
        if k is not None and attributed:
            create, carry, expiry, carry_turns, events, gap_s = cost_of(tokens, k, turns, tables)
        call = {
            "call_id": r["call_id"], "tool_use_id": r["tool_use_id"], "message_uuid": r["message_uuid"],
            "tool_name": r["tool_name"], "target": r["target"], "session_id": sid,
            "timestamp": r["timestamp"], "result_tokens": rt, "tokens": tokens,
            "is_error": 1 if (r["res_err"] or r["use_err"]) else 0,
            "attributed": attributed, "estimated": estimated,
            "usd": create + carry + expiry, "create_usd": create, "carry_usd": carry,
            "expiry_usd": expiry, "expiry_events": events, "expiry_gap_s": gap_s,
            "carry_turns": carry_turns, "flags": [], "thread_usd": thread_usd,
            # extra context for drill-downs (additive): project, model that first re-read it
            "project_slug": r["project_slug"],
            "model": turns[k + 1]["model"] if k is not None and k + 1 < len(turns) else None,
        }
        if r["tool_name"] == "Read" and r["target"]:
            reads[r["target"]] += 1
            read_tokens[r["target"]] += tokens
            call["read_n"] = reads[r["target"]]
        dx.flag_call(call)
        agg.add(call)
    agg.add_rereads(dx.find_rereads(sid, reads, read_tokens))


def analyze(db_path, pricing, since=None, until=None, catalog=None,
            calls_limit=0, tool_filter=None) -> dict:
    """Single streaming pass; returns raw aggregates (see tool_costs/skill_costs)."""
    if catalog is None:
        from .skills import cached_catalog
        catalog = cached_catalog()
    try:
        st = os.stat(db_path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    key = (str(db_path), since, until, calls_limit, tool_filter, sig, id(pricing), id(catalog))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    agg = _Agg(calls_limit, tool_filter)
    memo: dict = {}
    rng, args = _range_clause(since, until, "t.timestamp")
    with connect(db_path) as conn:
        threads = [(r[0], r[1]) for r in conn.execute(f"""
            SELECT DISTINCT m.session_id, COALESCE(m.agent_id,'')
              FROM tool_calls t JOIN messages m ON m.uuid = t.message_uuid
             WHERE t.tool_name != '_tool_result' {rng}
             ORDER BY m.session_id""", args)]
        for sid, aid in threads:
            _process_thread(conn, pricing, memo, catalog, sid, aid, since, until, agg)
    _cache.clear()
    _cache[key] = (time.time(), agg)
    return agg


def _meta(agg) -> dict:
    return {"total_calls": agg.total_calls, "unattributed_calls": agg.unattributed,
            "estimated": True,
            "note": ("Estimates: tokens=chars/4; create cost once at next turn, carry cost "
                     "at cache-read rate until session end or detected compaction, expiry cost = "
                     "re-write at the cache-write rate after an idle gap longer than the cache lifetime. "
                     "Unattributed calls were scanned before tool_use_id was stored.")}


def tool_costs(db_path, pricing, since=None, until=None, limit=50, catalog=None) -> dict:
    agg = analyze(db_path, pricing, since, until, catalog)
    out = []
    for name, t in agg.tools.items():
        toks = sorted(t["tokens"])
        top = sorted(t["targets"].items(), key=lambda kv: -kv[1])[:TOP_TARGETS]
        out.append({
            "tool_name": name, "calls": t["calls"], "attributed_calls": t["attributed"],
            "result_tokens_total": sum(toks),
            "avg_result_tokens": round(sum(toks) / len(toks), 1) if toks else 0,
            "p95_result_tokens": _percentile(toks, 0.95),
            "max_result_tokens": toks[-1] if toks else 0,
            "est_cost_usd": round(t["usd"], 6), "carry_cost_usd": round(t["carry"], 6),
            "create_cost_usd": round(t["create"], 6), "expiry_cost_usd": round(t["expiry"], 6),
            "expiry_events": t["expiry_events"],
            "error_rate": round(t["errors"] / t["attributed"], 4) if t["attributed"] else None,
            "top_targets": [{"target": k, "est_cost_usd": round(v, 6)} for k, v in top],
        })
    out.sort(key=lambda r: -r["est_cost_usd"])
    return {"tools": out[:limit], "diagnostics": dx.tool_diagnostics(agg, out), "meta": _meta(agg)}


def skill_costs(db_path, pricing, since=None, until=None, limit=100, catalog=None) -> dict:
    agg = analyze(db_path, pricing, since, until, catalog)
    out = []
    for name, s in agg.skills.items():
        n = s["invocations"]
        out.append({
            "skill": name, "invocations": n, "sessions": len(s["sessions"]),
            "est_load_tokens": round(s["load"] / n) if n else 0,
            "est_cost_usd": round(s["usd"], 6), "carry_cost_usd": round(s["carry"], 6),
            "create_cost_usd": round(s["create"], 6), "expiry_cost_usd": round(s["expiry"], 6),
            "expiry_events": s["expiry_events"],
            "cost_per_invocation": round(s["usd"] / n, 6) if n else 0.0,
            "last_used": s["last"] or None, "estimated": s["estimated"],
        })
    out.sort(key=lambda r: -r["est_cost_usd"])
    return {"skills": out[:limit], "diagnostics": dx.skill_diagnostics(out), "meta": _meta(agg)}


def top_calls(db_path, pricing, since=None, until=None, tool=None, limit=50, catalog=None) -> dict:
    agg = analyze(db_path, pricing, since, until, catalog, calls_limit=limit, tool_filter=tool)
    calls = []
    for _usd, _seq, c in sorted(agg.heap, reverse=True):
        calls.append({
            "call_id": c["call_id"], "tool_use_id": c["tool_use_id"],
            "tool_name": c["tool_name"], "session_id": c["session_id"],
            "timestamp": c["timestamp"], "target": (c["target"] or "")[:TARGET_MAX_CHARS],
            "result_tokens": c["result_tokens"], "context_tokens": c["tokens"],
            "est_cost_usd": round(c["usd"], 6), "carry_cost_usd": round(c["carry_usd"], 6),
            "create_cost_usd": round(c["create_usd"], 6), "expiry_cost_usd": round(c["expiry_usd"], 6),
            "expiry_events": c["expiry_events"], "expiry_gap_total_s": round(c["expiry_gap_s"], 1),
            "carry_turns": c["carry_turns"], "is_error": c["is_error"],
            "attributed": c["attributed"], "estimated": c["estimated"],
            "diagnostics": c["flags"],
        })
    return {"calls": calls, "meta": _meta(agg)}
