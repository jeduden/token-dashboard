"""Per-tool cost drill-down: which parameters / call frequencies drive a tool's cost.

Reuses the cost model of ``cost_insights`` (create cost + carry cost) by feeding
its per-thread call stream into a collector that keeps one tool only. Calls are
grouped by a tool-aware key (see GROUPS) and ranked by estimated USD.

Streaming: one thread is in memory at a time plus one small accumulator per
distinct key (capped at KEY_TRACK_CAP; overflow lands in OTHER_KEY and the
response reports ``truncated``). Threads are processed grouped by session, so
distinct sessions per key and (session, target) repeats need only O(1) state per
key / per session. No prompt or result text is ever read (none is stored).
Calls without a linked result are counted in ``totals.unattributed_calls`` and
excluded from the ranking rather than shown as $0.
"""
from __future__ import annotations

import math
import os
import re
import time
from urllib.parse import urlsplit

from .db import connect, _range_clause
from . import cost_insights as ci
from . import cost_diagnostics as dx
from .bash_keys import bash_key
from . import cost_dimensions as dm

KEY_TRACK_CAP = 20_000        # max distinct keys tracked per request
OTHER_KEY = "(other: key cap reached)"
KEY_MAX_CHARS = ci.TARGET_MAX_CHARS
CACHE_TTL_SECONDS = 20.0
FREQUENT_MIN_CALLS = 3        # a key needs at least this many calls to be "frequent"
# Thresholds are cost-weighted, because call counts are heavy-tailed and a plain median over
# all keys is dragged to ~nothing by the long tail of tiny keys:
#   expensive = cost/call >= the tool's overall average cost/call (total cost / attributed calls)
#   frequent  = calls >= P75 of calls among the keys that make up the top COST_COVERAGE of cost
#               (and at least FREQUENT_MIN_CALLS)
COST_COVERAGE = 0.8
FREQUENT_PERCENTILE = 0.75
THRESHOLD_METHOD = ("expensive: cost/call >= tool average (total cost / calls); frequent: calls >= P75 of "
                    "calls among the keys making up the top 80% of cost (min 3)")
QUADRANT_LABELS = {
    # Human wording is relative to THIS tool's heaviest keys ("high volume" is not "common");
    # the machine keys frequent_*/rare_* are kept for API compatibility.
    "frequent_expensive": "high volume & expensive: biggest lever, fix first",
    "frequent_cheap": "high volume & cheap: batch or cache",
    "rare_expensive": "lower volume & expensive: trim the output",
    "rare_cheap": "lower volume & cheap: ignore",
}

_cache: dict = {}


def g_command(t):
    return bash_key(t, "command")


def g_command2(t):
    return bash_key(t, "command2")


def g_pipeline(t):
    return bash_key(t, "pipeline")


def g_extension(t):
    if not t:
        return "(none)"
    base = re.split(r"[\\/]", t)[-1]
    i = base.rfind(".")
    return base[i:].lower() if 0 < i < len(base) - 1 else "(none)"


def g_directory(t):
    if not t:
        return "(none)"
    parts = re.split(r"[\\/]", t)
    d = t[: len(t) - len(parts[-1]) - 1] if len(parts) > 1 else ""
    return d or ("/" if t.startswith(("/", "\\")) else "(none)")


def g_pattern(t):
    m = re.match(r"[\w-]+", (t or "").lstrip("*/.^(\\"))
    return m.group(0) if m else "(pattern)"


def g_host(t):
    try:
        return (urlsplit(t or "").hostname or "(invalid)")
    except ValueError:
        return "(invalid)"


def g_target(t):
    return (t or "(none)")[:KEY_MAX_CHARS]


_ALL = {
    "command": ("first real command", g_command), "command2": ("first real command + next word", g_command2),
    "pipeline": ("whole pipeline", g_pipeline),
    "extension": ("file extension", g_extension), "directory": ("parent directory", g_directory),
    "pattern": ("pattern's leading token", g_pattern), "host": ("host", g_host),
    "target": ("exact target", g_target),
}
# tool -> (default mode for auto, ordered modes). Unlisted tools: target only.
GROUPS = {
    "Bash": ("command", ["command", "command2", "pipeline", "target"]),
    "Read": ("directory", ["directory", "extension", "target"]),
    "Edit": ("directory", ["directory", "extension", "target"]),
    "Write": ("directory", ["directory", "extension", "target"]),
    "Grep": ("pattern", ["pattern", "target"]),
    "Glob": ("pattern", ["pattern", "target"]),
    "WebFetch": ("host", ["host", "target"]),
}


def _tool_modes(tool):
    return GROUPS.get(tool, ("target", ["target"]))


def available_groups(tool):
    """Tool-specific modes first, then the cross-cutting dimensions valid for every tool."""
    return ([{"key": m, "label": _ALL[m][0], "kind": "parameter"} for m in _tool_modes(tool)[1]]
            + [{"key": m, "label": lab, "kind": "dimension"} for m, lab in dm.DIMS.items()])


def resolve_group(tool, group):
    default, modes = _tool_modes(tool)
    if not group or group == "auto":
        return default
    if group not in modes and group not in dm.DIMS:
        raise ValueError("unknown group %r for tool %r" % (group, tool))
    return group


class _Collector:
    """Duck-types the aggregate that ``cost_insights._process_thread`` fills."""

    def __init__(self, tool, fn, ctx=None):
        self.tool, self.fn, self.ctx = tool, fn, ctx
        self.time = dm.TimeSeries()
        self.rows: dict = {}
        self.truncated = False
        self.sources: set = set()
        self.total_calls = 0
        self.unattributed = 0
        self.usd = self.carry = self.expiry = 0.0
        self.expiry_events = 0
        self.big: dict = {}
        self.rereads: list = []
        self.rep_calls = 0
        self.rep_usd = 0.0
        self._sid = None
        self._seen: set = set()
        self.sessions = 0

    def add(self, c):
        if self.ctx:
            self.ctx.observe(c)
        if c["tool_name"] != self.tool:
            return
        self.time.add(c)
        self.total_calls += 1
        if not c["attributed"]:
            self.unattributed += 1
            return
        if c["session_id"] != self._sid:
            self._sid, self._seen = c["session_id"], set()
            self.sessions += 1
        self.usd += c["usd"]
        self.carry += c["carry_usd"]
        self.expiry += c["expiry_usd"]
        self.expiry_events += c["expiry_events"]
        dx.note_big_output(self.big, c)
        tgt = g_target(c["target"])
        repeat = tgt in self._seen
        self._seen.add(tgt)
        if repeat:
            self.rep_calls += 1
            self.rep_usd += c["usd"]
        key = self.fn(c)[:KEY_MAX_CHARS]
        a = self.rows.get(key)
        if a is None:
            if len(self.rows) >= KEY_TRACK_CAP:
                self.truncated = True
                key = OTHER_KEY
                a = self.rows.get(key)
            if a is None:
                a = self.rows[key] = {"calls": 0, "usd": 0.0, "carry": 0.0, "expiry": 0.0, "tok": 0, "tok_max": 0,
                                      "errors": 0, "sessions": 0, "sid": None, "first": None,
                                      "last": None, "rep": 0, "rep_usd": 0.0}
        a["calls"] += 1
        a["usd"] += c["usd"]
        a["carry"] += c["carry_usd"]
        a["expiry"] += c["expiry_usd"]
        rt = c["result_tokens"] or 0
        a["tok"] += rt
        a["tok_max"] = max(a["tok_max"], rt)
        a["errors"] += c["is_error"]
        if a["sid"] != c["session_id"]:
            a["sid"] = c["session_id"]
            a["sessions"] += 1
        ts = c["timestamp"]
        if ts:
            a["first"] = ts if a["first"] is None else min(a["first"], ts)
            a["last"] = ts if a["last"] is None else max(a["last"], ts)
        if repeat:
            a["rep"] += 1
            a["rep_usd"] += c["usd"]

    def add_rereads(self, found):
        self.rereads.extend(found)
        if len(self.rereads) > 500:
            self.rereads.sort(key=lambda r: -r["count"])
            del self.rereads[100:]


def _tool_exists(conn, tool):
    return conn.execute("SELECT 1 FROM tool_calls WHERE tool_name = ? AND tool_name != '_tool_result' LIMIT 1",
                        (tool,)).fetchone() is not None


def _collect(db_path, pricing, tool, mode, since, until, catalog):
    try:
        st = os.stat(db_path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    key = (str(db_path), tool, mode, since, until, sig, id(pricing), id(catalog))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    memo: dict = {}
    rng, args = _range_clause(since, until, "t.timestamp")
    with connect(db_path) as conn:
        if not _tool_exists(conn, tool):
            raise ValueError("unknown tool %r" % tool)
        ctx = dm.DimContext(conn, mode) if mode in dm.DIMS else None
        if ctx:
            col = _Collector(tool, ctx.key, ctx)
        else:
            tfn = _ALL[mode][1]
            col = _Collector(tool, lambda c: tfn(c["target"]))
        threads = [(r[0], r[1]) for r in conn.execute(f"""
            SELECT DISTINCT m.session_id, COALESCE(m.agent_id,'')
              FROM tool_calls t JOIN messages m ON m.uuid = t.message_uuid
             WHERE t.tool_name = ? {rng}
             ORDER BY m.session_id""", [tool] + args)]
        for sid, aid in threads:
            if ctx:
                ctx.begin_thread(conn, sid, aid, since, until)
            ci._process_thread(conn, pricing, memo, catalog, sid, aid, since, until, col)
    _cache.clear()
    _cache[key] = (time.time(), col)
    return col


def compute_thresholds(items, total_usd, total_calls):
    """items: [(calls, usd)] per key -> (calls_threshold, cost_per_call_threshold)."""
    cpc_thr = total_usd / total_calls if total_calls else 0.0
    ranked = sorted(items, key=lambda x: -x[1])
    covered, cum = [], 0.0
    for calls, usd in ranked:
        covered.append(calls)
        cum += usd
        if cum >= COST_COVERAGE * total_usd:
            break
    covered.sort()
    calls_thr = covered[max(math.ceil(FREQUENT_PERCENTILE * len(covered)) - 1, 0)] if covered else 0
    return calls_thr, cpc_thr


def quadrant(calls, cpc, calls_thr, cpc_thr):
    freq = calls >= calls_thr and calls >= FREQUENT_MIN_CALLS
    exp = cpc >= cpc_thr
    return ("frequent" if freq else "rare") + "_" + ("expensive" if exp else "cheap")


def tool_params(db_path, pricing, tool, since=None, until=None, limit=50, group="auto",
                catalog=None) -> dict:
    if not tool or tool == "_tool_result":
        raise ValueError("tool is required")
    mode = resolve_group(tool, group)
    if catalog is None:
        if tool == "Skill":
            from .skills import cached_catalog
            catalog = cached_catalog()
        else:
            catalog = {}
    col = _collect(db_path, pricing, tool, mode, since, until, catalog)
    attributed = col.total_calls - col.unattributed
    items = [(k, a, a["usd"] / a["calls"]) for k, a in col.rows.items()]
    calls_thr, cpc_thr = compute_thresholds([(a["calls"], a["usd"]) for _, a, _ in items],
                                            col.usd, sum(a["calls"] for _, a, _ in items))
    items.sort(key=lambda x: (-x[1]["usd"], -x[1]["calls"], x[0]))
    cum, rows = 0.0, []
    for k, a, cpc in items:
        cum += a["usd"]
        q = quadrant(a["calls"], cpc, calls_thr, cpc_thr)
        rows.append({
            "key": k, **(col.ctx.describe(k) if col.ctx else {}), "calls": a["calls"],
            "share_of_tool_cost": round(a["usd"] / col.usd, 4) if col.usd else 0.0,
            "est_cost_usd": round(a["usd"], 6), "carry_cost_usd": round(a["carry"], 6),
            "expiry_cost_usd": round(a["expiry"], 6),
            "expiry_share": round(a["expiry"] / a["usd"], 4) if a["usd"] else 0.0,
            "cost_per_call": round(cpc, 6),
            "avg_result_tokens": round(a["tok"] / a["calls"], 1), "max_result_tokens": a["tok_max"],
            "error_rate": round(a["errors"] / a["calls"], 4),
            "sessions": a["sessions"], "first_seen": a["first"], "last_seen": a["last"],
            "frequency_per_session": round(a["calls"] / a["sessions"], 2) if a["sessions"] else 0.0,
            "cumulative_share": round(cum / col.usd, 4) if col.usd else 0.0,
            "repeat_calls": a["rep"], "repeat_cost_usd": round(a["rep_usd"], 6),
            "quadrant": q, "quadrant_label": QUADRANT_LABELS[q],
        })
    errors = sum(a["errors"] for _, a, _ in items)
    totals = {
        "calls": col.total_calls, "attributed_calls": attributed,
        "unattributed_calls": col.unattributed, "sessions": col.sessions,
        "est_cost_usd": round(col.usd, 6), "carry_cost_usd": round(col.carry, 6),
        "create_cost_usd": round(col.usd - col.carry - col.expiry, 6),
        "expiry_cost_usd": round(col.expiry, 6), "expiry_events": col.expiry_events,
        "expiry_share": round(col.expiry / col.usd, 4) if col.usd else 0.0,
        "error_rate": round(errors / attributed, 4) if attributed else None,
    }
    diag_row = {"tool_name": tool, "error_rate": totals["error_rate"], "attributed_calls": attributed,
                "est_cost_usd": col.usd, "expiry_cost_usd": col.expiry, "expiry_events": col.expiry_events}
    return {
        "tool": tool, "group": mode, "available_groups": available_groups(tool),
        "totals": totals, "distinct_keys": len(col.rows), "truncated": col.truncated,
        "rows": rows[:limit], "time": {"days": col.time.to_list()},
        "repeat": {
            "repeat_calls": col.rep_calls, "repeat_cost_usd": round(col.rep_usd, 6),
            "share_of_calls": round(col.rep_calls / attributed, 4) if attributed else 0.0,
            "share_of_cost": round(col.rep_usd / col.usd, 4) if col.usd else 0.0,
        },
        "thresholds": {"calls_threshold": calls_thr, "cost_per_call_threshold": round(cpc_thr, 6),
                       "frequent_min_calls": FREQUENT_MIN_CALLS, "method": THRESHOLD_METHOD},
        "quadrant_labels": QUADRANT_LABELS,
        "diagnostics": dx.tool_diagnostics(col, [diag_row]),
        "meta": {**ci._meta(col), "key_cap": KEY_TRACK_CAP},
    }
