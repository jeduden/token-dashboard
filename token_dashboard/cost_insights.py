"""Per-tool / per-skill cost attribution (estimates).

Model
-----
A tool call at assistant turn ``k`` produces a result that enters the
context. Its cost is then:

* create cost: ``result_tokens * cache_create_5m rate`` once, at turn k+1
  (the next assistant turn writes the result into the prompt cache);
* carry cost: ``result_tokens * cache_read rate`` for every later assistant
  turn k+2 ... in the same thread (session + agent_id), until the thread ends
  or a compaction is detected (see COMPACTION_*), because that content is
  re-read from cache on every turn.

Rates come from the model of the *consuming* turn. Skills use the same math
with ``tokens`` = the skill's definition size (catalog ``tokens_per_call``
when known, else estimated from the cache_create jump on the next turn).

All numbers are estimates: tokens are chars // 4, cache TTL expiry and
partial-prefix cache misses are ignored, and 1h-cache writes are priced at
the 5m rate. Rows with NULL ``tool_use_id`` (scanned before the column
existed) have no linked result and are counted as "unattributed".

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
from .pricing import cost_for
from . import cost_diagnostics as dx

# --- assumptions (named so they can be tuned/tested) -----------------------
COMPACTION_DROP_RATIO = 0.5       # cache_read falling below 50% of previous turn...
COMPACTION_MIN_PREV_READ = 20_000  # ...from at least this many tokens = compaction
SKILL_ESTIMATE_CAP = 60_000       # max tokens an estimated skill load can claim
TARGET_MAX_CHARS = 200            # targets are truncated in API output
TARGET_TRACK_CAP = 20_000         # max distinct targets tracked per tool
TOP_TARGETS = 5
CACHE_TTL_SECONDS = 20.0

_cache: dict = {}


def _rates(model, pricing, memo) -> tuple:
    """(cache_read, cache_create_5m) USD per token for ``model``."""
    r = memo.get(model)
    if r is None:
        z = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0}
        rd = cost_for(model or "", {**z, "cache_read_tokens": 1_000_000}, pricing)["usd"]
        cr = cost_for(model or "", {**z, "cache_create_5m_tokens": 1_000_000}, pricing)["usd"]
        r = ((rd or 0.0) / 1e6, (cr or 0.0) / 1e6)
        memo[model] = r
    return r


def _load_thread(conn, pricing, memo, session_id, agent_id):
    """Ordered assistant turns of one thread with per-turn rates + cost."""
    turns, index = [], {}
    sql = """
      SELECT uuid, model, input_tokens, output_tokens, cache_read_tokens,
             cache_create_5m_tokens, cache_create_1h_tokens
        FROM messages
       WHERE session_id=? AND COALESCE(agent_id,'')=? AND type='assistant'
       ORDER BY timestamp, rowid
    """
    for r in conn.execute(sql, (session_id, agent_id)):
        rd, cr = _rates(r["model"], pricing, memo)
        usage = {k: r[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                                   "cache_create_5m_tokens", "cache_create_1h_tokens")}
        usd = cost_for(r["model"] or "", usage, pricing)["usd"] or 0.0
        index[r["uuid"]] = len(turns)
        turns.append({"read": rd, "create": cr, "usd": usd, "cr": r["cache_read_tokens"],
                      "model": r["model"],
                      "cc": r["cache_create_5m_tokens"] + r["cache_create_1h_tokens"]})
    return turns, index


def _carry_tables(turns):
    """Prefix sums of read rates + next-compaction index for each turn."""
    n = len(turns)
    prefix = [0.0] * (n + 1)
    for i, t in enumerate(turns):
        prefix[i + 1] = prefix[i] + t["read"]
    nxt = [n] * (n + 1)
    for i in range(n - 1, -1, -1):
        comp = (i > 0 and turns[i - 1]["cr"] >= COMPACTION_MIN_PREV_READ
                and turns[i]["cr"] < turns[i - 1]["cr"] * COMPACTION_DROP_RATIO)
        nxt[i] = i if comp else nxt[i + 1]
    return prefix, nxt


def _cost_of(tokens, k, turns, prefix, nxt):
    """(create_usd, carry_usd, carry_turns) for ``tokens`` entering after turn k."""
    n = len(turns)
    if not tokens or k + 1 >= n:
        return 0.0, 0.0, 0
    create = tokens * turns[k + 1]["create"]
    start = k + 2
    if start >= n:
        return create, 0.0, 0
    end = nxt[start]
    return create, tokens * (prefix[end] - prefix[start]), max(0, end - start)


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
            "errors": 0, "targets": {}})
        t["calls"] += 1
        if c["attributed"]:
            t["attributed"] += 1
            t["tokens"].append(c["result_tokens"])
            t["errors"] += c["is_error"]
        t["usd"] += c["usd"]
        t["carry"] += c["carry_usd"]
        tgt = (c["target"] or "(none)")[:TARGET_MAX_CHARS]
        if tgt in t["targets"] or len(t["targets"]) < TARGET_TRACK_CAP:
            t["targets"][tgt] = t["targets"].get(tgt, 0.0) + c["usd"]
        if c["tool_name"] == "Skill" and c["target"]:
            s = self.skills.setdefault(c["target"], {
                "invocations": 0, "sessions": set(), "load": 0, "usd": 0.0,
                "carry": 0.0, "last": "", "estimated": False})
            s["invocations"] += 1
            s["sessions"].add(c["session_id"])
            s["load"] += c["tokens"]
            s["usd"] += c["usd"]
            s["carry"] += c["carry_usd"]
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
    turns, index = _load_thread(conn, pricing, memo, sid, aid)
    prefix, nxt = _carry_tables(turns)
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
                jump = turns[k + 1]["cc"] - turn_results[r["message_uuid"]]
                tokens, estimated = max(0, min(SKILL_ESTIMATE_CAP, jump)), True
            attributed = attributed or tokens > 0
        create = carry = 0.0
        carry_turns = 0
        if k is not None and attributed:
            create, carry, carry_turns = _cost_of(tokens, k, turns, prefix, nxt)
        call = {
            "call_id": r["call_id"], "tool_use_id": r["tool_use_id"], "message_uuid": r["message_uuid"],
            "tool_name": r["tool_name"], "target": r["target"], "session_id": sid,
            "timestamp": r["timestamp"], "result_tokens": rt, "tokens": tokens,
            "is_error": 1 if (r["res_err"] or r["use_err"]) else 0,
            "attributed": attributed, "estimated": estimated,
            "usd": create + carry, "create_usd": create, "carry_usd": carry,
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
                     "at cache-read rate until session end or detected compaction. "
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
            "carry_turns": c["carry_turns"], "is_error": c["is_error"],
            "attributed": c["attributed"], "estimated": c["estimated"],
            "diagnostics": c["flags"],
        })
    return {"calls": calls, "meta": _meta(agg)}
