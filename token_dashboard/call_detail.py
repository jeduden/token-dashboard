"""Single tool-call drill-down: what one call cost, why, and what surrounded it.

Uses the exact cost model of ``cost_insights`` (create cost at the next turn,
carry cost per later turn at the consuming turn's cache-read rate, compaction
cutoff): the call's own thread is run through ``cost_insights._process_thread``
with a collector, so totals agree with ``/api/tool-costs/calls`` by construction.
Only that thread's assistant turns are loaded for the carry timeline.

Rank / percentile / same-target statistics need the cost of every call of the
same tool, so one pass over the threads that contain that tool is made and the
compact result cached for CACHE_TTL_SECONDS (keyed by DB signature + tool).

No prompt text is selected and no result text exists in the DB.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from .db import connect, best_project_name
from . import cost_insights as ci
from . import cost_model as cm
from . import cost_diagnostics as dx
from .repos import slug_repo_map

ONGOING_SECONDS = 30 * 60      # last turn this recent => thread may still be running
NEIGHBORS = 3                  # previous/next calls shown around the call
SHORT_TARGET = 120
TOP_SAME_TARGET = 5
CACHE_TTL_SECONDS = 120.0

SUGGESTIONS = {
    "big_output": "Narrow the command (head/limit, tighter pattern) so less output enters the context.",
    "reread": "Keep the file's contents in mind (or in a note) instead of re-reading it; each read re-pays the cost.",
    "error": "A failed call still costs context; fix the cause (path, flags) before retrying.",
    "dominant_call": "Split the work or request less data; one call dominates this session's spend.",
    "expiry_heavy": dx.EXPIRY_SUGGESTION,
    "idle_gap": ("Compact or clear before stepping away, or finish in one sitting; with the 1h cache an "
                 "idle gap under an hour would not have expired the cache."),
    "unattributed": "Rescan so the result is linked; until then this call is not priced.",
}
_cache: dict = {}


class NotFound(LookupError):
    """No tool call with that id."""


class _Collect:
    """Stands in for cost_insights._Agg: keeps every call of the processed threads."""

    def __init__(self):
        self.calls = []

    def add(self, c):
        self.calls.append(c)

    def add_rereads(self, found):
        pass


class _ToolPass:
    """Compact per-call costs for one tool: (usd, call_id, session_id, target, timestamp)."""

    def __init__(self, tool):
        self.tool = tool
        self.rows = []

    def add(self, c):
        if c["tool_name"] == self.tool:
            self.rows.append((c["usd"], c["call_id"], c["session_id"], c["target"] or "",
                              c["timestamp"] or "", c["attributed"]))

    def add_rereads(self, found):
        pass


def _sig(db_path):
    try:
        st = os.stat(db_path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _tool_pass(conn, db_path, pricing, catalog, tool, memo):
    key = (str(db_path), tool, _sig(db_path), id(pricing), id(catalog))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    tp = _ToolPass(tool)
    threads = [(r[0], r[1]) for r in conn.execute("""
        SELECT DISTINCT m.session_id, COALESCE(m.agent_id,'')
          FROM tool_calls t JOIN messages m ON m.uuid = t.message_uuid
         WHERE t.tool_name = ?""", (tool,))]
    for sid, aid in threads:
        ci._process_thread(conn, pricing, memo, catalog, sid, aid, None, None, tp)
    _cache.clear()
    _cache[key] = (time.time(), tp)
    return tp


def _parse_ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _now_ts(now):
    if now is None:
        return time.time()
    return _parse_ts(now) if isinstance(now, str) else float(now)


def _timeline(call, turns, uuid, now):
    """(create_turn, carry_timeline, carry_end) for an attributed call."""
    k = next((i for i, t in enumerate(turns) if t["uuid"] == uuid), None)
    empty = {"reason": "not_attributed", "turn_offset": None, "timestamp": None}
    if k is None or not call["attributed"]:
        return None, [], empty
    n, tokens = len(turns), call["tokens"]
    if k + 1 >= n:
        return None, [], _end_reason(turns, n, n, k, now, first_carry=False)
    ct = turns[k + 1]
    create = tokens * ct["create"]
    create_turn = {"turn_offset": 1, "timestamp": ct["ts"], "model": ct["model"], "cost_usd": create}
    end = cm.Tables(turns).nxt[k + 2] if k + 2 < n else n
    timeline, cum = [], create
    for i in range(k + 2, end):
        rewrite = cm.is_expiry(turns, i)
        add = tokens * (turns[i]["create"] if rewrite else turns[i]["read"])
        cum += add
        timeline.append({"turn_offset": i - k, "timestamp": turns[i]["ts"], "model": turns[i]["model"],
                         "kind": "rewrite" if rewrite else "read", "gap_s": round(turns[i]["gap_s"], 1),
                         "cost_usd": add, "cumulative_usd": cum})
    return create_turn, timeline, _end_reason(turns, end, n, k, now)


def _end_reason(turns, end, n, k, now, first_carry=True):
    if end < n:
        return {"reason": "compaction_detected", "turn_offset": end - k, "timestamp": turns[end]["ts"]}
    last = _parse_ts(turns[-1]["ts"]) if turns else None
    if last is not None and _now_ts(now) - last < ONGOING_SECONDS:
        return {"reason": "ongoing", "turn_offset": None, "timestamp": turns[-1]["ts"]}
    return {"reason": "session_end", "turn_offset": None, "timestamp": turns[-1]["ts"] if turns else None}


def _brief(c):
    return {"call_id": c["call_id"], "tool_name": c["tool_name"],
            "target": (c["target"] or "")[:SHORT_TARGET], "result_tokens": c["result_tokens"],
            "est_cost_usd": round(c["usd"], 6), "timestamp": c["timestamp"]}


def _rank(tp, call):
    ranked = [r for r in tp.rows if r[5]]
    if not call["attributed"] or not ranked:
        return None, len(ranked), None
    rank = 1 + sum(1 for r in ranked if r[0] > call["usd"])
    n = len(ranked)
    return rank, n, round(100.0 * (n - rank + 1) / n, 1)


def _same_target(tp, call):
    tgt = call["target"] or ""
    out = {"applicable": bool(tgt), "other_calls": 0, "other_cost_usd": 0.0, "in_this_session": 0,
           "first_seen": None, "last_seen": None, "top": []}
    if not tgt:
        return out
    same = [r for r in tp.rows if r[3] == tgt]
    others = [r for r in same if r[1] != call["call_id"]]
    out["other_calls"] = len(others)
    out["other_cost_usd"] = round(sum(r[0] for r in others), 6)
    out["in_this_session"] = sum(1 for r in others if r[2] == call["session_id"])
    stamps = [r[4] for r in same if r[4]]
    out["first_seen"], out["last_seen"] = (min(stamps), max(stamps)) if stamps else (None, None)
    top = sorted(others, key=lambda r: (-r[0], r[1]))[:TOP_SAME_TARGET]
    out["top"] = [{"call_id": r[1], "session_id": r[2], "timestamp": r[4], "est_cost_usd": round(r[0], 6)}
                  for r in top]
    return out


def _diagnostics(call):
    flags = list(call["flags"])
    if not call["attributed"]:
        flags.append({"rule": "unattributed", "severity": "info",
                      "text": "This call has no linked result, so its cost cannot be estimated.",
                      "evidence": {}})
    if call["expiry_events"]:
        flags.append({"rule": "idle_gap", "severity": "info",
                      "text": (f"The thread sat idle past the cache lifetime {call['expiry_events']} time(s) while this "
                               f"result was in context (total idle {_fmt_gap(call['expiry_gap_s'])}); "
                               "each time it was written to the cache again."),
                      "evidence": {"expiry_events": call["expiry_events"],
                                   "expiry_gap_total_s": round(call["expiry_gap_s"], 1),
                                   "expiry_cost_usd": round(call["expiry_usd"], 6)}})
    return [dict(f, suggestion=f.get("suggestion") or SUGGESTIONS.get(f["rule"], "")) for f in flags]


def _fmt_gap(s):
    return "%d min" % round(s / 60) if s < 7200 else "%.1f h" % (s / 3600)


def _project(conn, slug, sid):
    cwds = [r[0] for r in conn.execute(
        "SELECT DISTINCT cwd FROM messages WHERE session_id=? AND cwd IS NOT NULL AND cwd != ''", (sid,))]
    info = slug_repo_map(conn, [slug]).get(slug, {})
    return best_project_name(cwds, slug), info.get("key"), info.get("name")


def call_detail(db_path, pricing, call_id, catalog=None, now=None) -> dict:
    if catalog is None:
        from .skills import cached_catalog
        catalog = cached_catalog()
    memo: dict = {}
    with connect(db_path) as conn:
        row = conn.execute("""
            SELECT t.id, t.message_uuid, t.session_id, t.project_slug, t.tool_name, t.target,
                   t.tool_use_id, t.is_error, t.timestamp, m.agent_id, m.is_sidechain
              FROM tool_calls t LEFT JOIN messages m ON m.uuid = t.message_uuid
             WHERE t.id = ? AND t.tool_name != '_tool_result'""", (call_id,)).fetchone()
        if row is None:
            raise NotFound("no tool call with id %d" % call_id)
        sid, aid = row["session_id"], row["agent_id"] or ""
        res = conn.execute("""
            SELECT result_tokens, result_chars, is_error FROM tool_calls
             WHERE session_id=? AND tool_use_id=? AND tool_name='_tool_result' AND ? IS NOT NULL
             LIMIT 1""", (sid, row["tool_use_id"], row["tool_use_id"])).fetchone()
        by_thread = {}
        for a in {(r[0] or "") for r in conn.execute(
                "SELECT DISTINCT agent_id FROM messages WHERE session_id=?", (sid,))} | {aid}:
            col = by_thread[a] = _Collect()
            ci._process_thread(conn, pricing, memo, catalog, sid, a, None, None, col)
        mine = [c for c in by_thread[aid].calls if c["call_id"] == call_id]
        if not mine:                       # call whose message row is missing: nothing to price
            raise NotFound("tool call %d has no assistant message to price" % call_id)
        call = mine[0]
        turns, _ = cm.load_thread(conn, pricing, memo, sid, aid)
        create_turn, timeline, end = _timeline(call, turns, row["message_uuid"], now)
        tp = _tool_pass(conn, db_path, pricing, catalog, row["tool_name"], memo)
        project, repo, repo_name = _project(conn, row["project_slug"], sid)
    session_calls = [c for col in by_thread.values() for c in col.calls]
    return _assemble(row, res, call, by_thread[aid].calls, session_calls,
                     (create_turn, timeline, end), tp, (project, repo, repo_name))


def _assemble(row, res, call, thread, session_calls, tl, tp, proj):
    create_turn, timeline, end = tl
    sid = row["session_id"]
    mu = row["message_uuid"]
    batch = [c for c in thread if c["message_uuid"] == mu and c["call_id"] != call["call_id"]]
    pos = next(i for i, c in enumerate(thread) if c["call_id"] == call["call_id"])
    before = [c for c in thread[:pos] if c["message_uuid"] != mu]
    after = [c for c in thread[pos + 1:] if c["message_uuid"] != mu]
    rank, rank_of, pct = _rank(tp, call)
    session_total = sum(c["usd"] for c in session_calls)
    thread_usd = call["thread_usd"]
    usd = call["usd"]
    return {
        "identity": {
            "call_id": call["call_id"], "tool_name": row["tool_name"], "target": row["target"],
            "timestamp": row["timestamp"], "session_id": sid, "project_slug": row["project_slug"],
            "project": proj[0], "repo": proj[1], "repo_name": proj[2],
            "thread": {"agent_id": row["agent_id"] or None, "is_sidechain": bool(row["is_sidechain"])},
            "message_uuid": mu, "tool_use_id": row["tool_use_id"],
            "is_error": call["is_error"],
            "result_tokens": call["result_tokens"], "result_chars": res["result_chars"] if res else None,
            "context_tokens": call["tokens"], "estimated": call["estimated"],
        },
        "attributed": call["attributed"],
        "cost": {
            "est_cost_usd": round(usd, 6), "create_cost_usd": round(call["create_usd"], 6),
            "carry_cost_usd": round(call["carry_usd"], 6), "carry_turns": call["carry_turns"],
            "expiry_cost_usd": round(call["expiry_usd"], 6), "expiry_events": call["expiry_events"],
            "expiry_gap_total_s": round(call["expiry_gap_s"], 1),
            "rank": rank, "rank_of": rank_of, "percentile": pct,
            "share_of_session_cost": round(usd / session_total, 4) if session_total else None,
            "share_of_thread_spend": round(usd / thread_usd, 4) if thread_usd else None,
            "thread_spend_usd": round(thread_usd, 6), "session_tool_cost_usd": round(session_total, 6),
        },
        "create_turn": create_turn, "carry_timeline": timeline, "carry_end": end,
        "siblings": [_brief(c) for c in batch],
        "previous": [_brief(c) for c in before[-NEIGHBORS:]],
        "next": [_brief(c) for c in after[:NEIGHBORS]],
        "same_target": _same_target(tp, call),
        "diagnostics": _diagnostics(call),
        "session_link": sid,
        "meta": {"estimated": True, "note": ci._meta(_MetaStub(session_calls))["note"]},
    }


class _MetaStub:
    def __init__(self, calls):
        self.total_calls = len(calls)
        self.unattributed = sum(1 for c in calls if not c["attributed"])
