"""Compactions report (``/api/compactions``): when context was compacted, how big it was, and
how many compactions followed an idle gap long enough for the prompt cache to expire.

Everything comes from the ``compactions`` table (real ``compact_boundary`` events) plus
``messages`` for per-thread turn counts and the cache tier / model in force before an event.
No prompt, summary or tool text is read. ``pre_tokens`` / ``post_tokens`` are Claude Code's own
counts. Results are cached briefly, keyed by database file state.
"""
from __future__ import annotations

import math
import os
import statistics
import time
from bisect import bisect_right

from .cache_events import TTL_1H_S, TTL_5M_S
from .cost_model import rates
from .db import connect, best_project_name, _range_clause
from .repos import slug_repo_map

CACHE_TTL_SECONDS = 30
TOP_SESSIONS = 10
# (key, label, inclusive upper edge); the last bucket is open-ended.
GAP_BUCKETS = (("lt5m", "<5 min", 300), ("5_10m", "5-10 min", 600), ("10_30m", "10-30 min", 1800),
               ("30_60m", "30-60 min", 3600), ("1_2h", "1-2 h", 7200), ("gt2h", ">2 h", None))
PRE_BUCKETS = (("lt150k", "<150k", 150_000), ("150_300k", "150-300k", 300_000),
               ("300_500k", "300-500k", 500_000), ("gt500k", ">500k", None))
WHAT_IF_NOTE = (
    "Upper bound, not a measurement. A compaction that starts after the cache expired has to "
    "process about pre_tokens at the cache-write rate; had the context been compacted before the "
    "pause it would have been read at the cache-read rate. avoidable = sum(pre_tokens x (write "
    "rate - read rate)) of the tier and model in force before the pause. It assumes the "
    "compaction call really paid the write rate, which transcripts do not show, and that you "
    "would have compacted right before leaving.")

_cache: dict = {}


def _bucket_key(buckets, v):
    for key, _label, edge in buckets:
        if edge is None or v <= edge:
            return key


def _median(vals):
    return statistics.median(vals) if vals else None


def _p90(vals):
    s = sorted(vals)
    return s[max(0, math.ceil(0.9 * len(s)) - 1)] if s else None


def _split():
    return {"auto": 0, "manual": 0, "other": 0}


def _bump(d, trigger):
    d[trigger if trigger in ("auto", "manual") else "other"] += 1


def _turns_between(conn, rows):
    """Assistant turns between consecutive compactions of the same thread (whole-session counts)."""
    threads: dict = {}
    for r in rows:
        threads.setdefault((r["session_id"], r["agent_id"] or ""), []).append(r["timestamp"])
    out = []
    for (sid, aid), stamps in threads.items():
        if len(stamps) < 2:
            continue
        turns = [t[0] for t in conn.execute(
            "SELECT timestamp FROM messages WHERE session_id=? AND COALESCE(agent_id,'')=? "
            "AND type='assistant' ORDER BY timestamp", (sid, aid))]
        out += [bisect_right(turns, b) - bisect_right(turns, a) for a, b in zip(stamps, stamps[1:])]
    return out


def _after_expiry(conn, pricing, rows):
    """Compactions that followed an idle gap longer than the cache lifetime in force, plus the what-if."""
    res = {"count": 0, "auto": 0, "manual": 0, "other": 0, "pre_tokens_total": 0,
           "avoidable_usd_upper_bound": 0.0, "unpriced": 0, "note": WHAT_IF_NOTE}
    memo: dict = {}
    for r in rows:
        if r["gap_before_s"] is None:
            continue
        w = conn.execute(
            "SELECT model, cache_create_5m_tokens c5, cache_create_1h_tokens c1 FROM messages "
            "WHERE session_id=? AND COALESCE(agent_id,'')=? AND type='assistant' AND timestamp < ? "
            "AND (cache_create_5m_tokens > 0 OR cache_create_1h_tokens > 0) "
            "ORDER BY timestamp DESC LIMIT 1", (r["session_id"], r["agent_id"] or "", r["timestamp"])).fetchone()
        one_hour = bool(w and w["c1"] > 0)
        if r["gap_before_s"] <= (TTL_1H_S if one_hour else TTL_5M_S):
            continue
        res["count"] += 1
        _bump(res, r["trigger"])
        pre = r["pre_tokens"]
        if pre is None:
            res["unpriced"] += 1
            continue
        res["pre_tokens_total"] += pre
        read, w5, w1 = rates(w["model"] if w else None, pricing, memo)[:3]
        write = w1 if one_hour else w5
        if write <= 0:
            res["unpriced"] += 1
        else:
            res["avoidable_usd_upper_bound"] += pre * (write - read)
    res["avoidable_usd_upper_bound"] = round(res["avoidable_usd_upper_bound"], 4)
    return res


def _roll(rows, key_fn):
    groups: dict = {}
    for r in rows:
        groups.setdefault(key_fn(r), []).append(r["pre_tokens"])
    return groups


def compactions_report(db_path, pricing, since=None, until=None) -> dict:
    try:
        st = os.stat(db_path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    key = (str(db_path), since, until, sig, id(pricing))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    rng, args = _range_clause(since, until)
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM compactions WHERE 1=1 %s ORDER BY timestamp, uuid" % rng, args)]
        slugs = sorted({r["project_slug"] for r in rows})
        names = {s: best_project_name([c[0] for c in conn.execute(
            "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL", (s,))], s) for s in slugs}
        repos = slug_repo_map(conn, slugs)
        between = _turns_between(conn, rows)
        after = _after_expiry(conn, pricing, rows)
    out = _assemble(rows, names, repos, between, after)
    _cache.clear()
    _cache[key] = (time.time(), out)
    return out


def _assemble(rows, names, repos, between, after):
    pre = [r["pre_tokens"] for r in rows if r["pre_tokens"] is not None]
    post = [r["post_tokens"] for r in rows if r["post_tokens"] is not None]
    dur = [r["duration_ms"] / 1000 for r in rows if r["duration_ms"] is not None]
    trig = _split()
    by_day, gaps, pres = {}, {k: _split() for k, _l, _e in GAP_BUCKETS}, {k: _split() for k, _l, _e in PRE_BUCKETS}
    sessions: dict = {}
    for r in rows:
        _bump(trig, r["trigger"])
        _bump(by_day.setdefault(r["timestamp"][:10], _split()), r["trigger"])
        if r["gap_before_s"] is not None:
            _bump(gaps[_bucket_key(GAP_BUCKETS, r["gap_before_s"])], r["trigger"])
        if r["pre_tokens"] is not None:
            _bump(pres[_bucket_key(PRE_BUCKETS, r["pre_tokens"])], r["trigger"])
        s = sessions.setdefault(r["session_id"], {"n": 0, "max": None, "slug": r["project_slug"]})
        s["n"] += 1
        if r["pre_tokens"] is not None and (s["max"] is None or r["pre_tokens"] > s["max"]):
            s["max"] = r["pre_tokens"]

    def with_count(d):
        return dict(d, count=d["auto"] + d["manual"] + d["other"])

    by_project = [{"project_slug": s, "project_name": names.get(s), "repo": repos[s]["key"],
                   "repo_name": repos[s]["name"], "count": len(v), "median_pre_tokens": _median([p for p in v if p is not None])}
                  for s, v in _roll(rows, lambda r: r["project_slug"]).items()]
    by_repo = [{"repo": k, "repo_name": next(r["repo_name"] for r in by_project if r["repo"] == k),
                "count": len(v), "median_pre_tokens": _median([p for p in v if p is not None])}
               for k, v in _roll(rows, lambda r: repos[r["project_slug"]]["key"]).items()]
    worst = sorted(sessions.items(), key=lambda kv: (-kv[1]["n"], -(kv[1]["max"] or 0), kv[0]))[:TOP_SESSIONS]
    return {
        "summary": {
            "count": len(rows), "auto": trig["auto"], "manual": trig["manual"], "other": trig["other"],
            "sidechain": sum(1 for r in rows if r["is_sidechain"]),
            "median_pre_tokens": _median(pre), "median_post_tokens": _median(post),
            "p90_pre_tokens": _p90(pre), "median_duration_s": _median([round(d, 1) for d in dur]),
            "total_dropped_tokens": sum(r["dropped_tokens"] or 0 for r in rows),
            "median_turns_between": _median(between),
        },
        "by_day": [dict(with_count(by_day[d]), day=d) for d in sorted(by_day)],
        "gap_buckets": [dict(with_count(gaps[k]), key=k, label=label) for k, label, _e in GAP_BUCKETS],
        "gap_unknown": sum(1 for r in rows if r["gap_before_s"] is None),
        "pre_token_buckets": [dict(with_count(pres[k]), key=k, label=label) for k, label, _e in PRE_BUCKETS],
        "pre_tokens_unknown": len(rows) - len(pre),
        "by_project": sorted(by_project, key=lambda x: (-x["count"], x["project_slug"])),
        "by_repo": sorted(by_repo, key=lambda x: (-x["count"], x["repo"])),
        "worst_sessions": [{"session_id": sid, "project_slug": v["slug"], "project_name": names.get(v["slug"]),
                            "compactions": v["n"], "max_pre_tokens": v["max"]} for sid, v in worst],
        "after_expiry": after,
        "thresholds": {"ttl_5m_s": TTL_5M_S, "ttl_1h_s": TTL_1H_S},
        "meta": {"source": "compact_boundary events; run a scan to backfill older transcripts",
                 "tokens_note": "preTokens/postTokens are Claude Code's own counts"},
    }
