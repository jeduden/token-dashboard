"""Prompt-cache miss analytics, computed from the `messages` table only.

One streaming pass over assistant turns ordered by thread
(session_id, is_sidechain, agent_id, timestamp). Memory is O(sessions), never
O(rows). Everything here is a HEURISTIC: transcripts do not record why a cache
write happened, so causes are inferred from time gaps and token shapes.

Why no SQL LAG(): the previous-turn state we need ("which TTL tier did this
thread last write?") is not a simple lag, and a Python pass over an ordered
cursor works on every SQLite build (window functions need >= 3.25).

No index is added: benchmarked on 1M synthetic rows, a (session_id, is_sidechain,
agent_id, timestamp) index was not picked up by the planner and changed nothing
(~3.4s either way; the temp-B-tree sort is ~1s of that, the rest is the Python pass).
"""
from __future__ import annotations

import heapq
from typing import Iterator, Optional

from .db import connect, best_project_name
from .pricing import cost_for
from .repos import slug_repo_map
from .cache_gaps import Gaps
from .cache_events import (  # noqa: F401  (re-exported: the expiry decision lives in cache_events)
    TTL_5M_S, TTL_1H_S, MISS_SHARE, MIN_MISS_TOKENS, PREFIX_DROP_RATIO, ThreadTracker,
    classify, is_miss, epoch as _epoch)

TOP_SESSIONS = 20

CAUSES = ("model_switch", "ttl_expired_1h", "ttl_expired_5m",
          "prefix_changed", "new_content", "unknown")

CAUSE_HELP = {
    "ttl_expired_5m": "Gap since the previous turn exceeded the 5-minute cache lifetime, so the prefix was written again.",
    "ttl_expired_1h": "Gap exceeded the 1-hour cache lifetime of the previous 1h write.",
    "model_switch": "The model changed; caches are per model, so the whole prefix was re-created.",
    "prefix_changed": "Within the TTL but cache reads dropped sharply: something early in the prompt changed (edit, compaction, tool/skill/system-prompt or MCP change).",
    "new_content": "Cache reads were retained but a lot of new content was added at once (e.g. a huge tool result). Little is truly wasted.",
    "unknown": "Could not be attributed; not enough history in the thread.",
}

def _stream(conn, until) -> Iterator[tuple]:
    sql = ("SELECT session_id, project_slug, is_sidechain, agent_id, timestamp, model, "
           "input_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens "
           "FROM messages WHERE type='assistant' "
           "AND (input_tokens + cache_read_tokens + cache_create_5m_tokens + cache_create_1h_tokens) > 0")
    args: tuple = ()
    if until:
        sql += " AND timestamp < ?"
        args = (until,)
    sql += " ORDER BY session_id, is_sidechain, agent_id, timestamp, uuid"
    cur = conn.execute(sql, args)
    while True:
        batch = cur.fetchmany(5000)
        if not batch:
            return
        yield from batch


class _Rates:
    """Per-model $/Mtok, derived through pricing.cost_for so fallbacks match."""

    def __init__(self, pricing):
        self.pricing, self.memo = pricing, {}
        self.estimated = False

    def get(self, model):
        if model not in self.memo:
            u = {k: 1_000_000 for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                                        "cache_create_5m_tokens", "cache_create_1h_tokens")}
            c = cost_for(model or "", u, self.pricing)
            self.memo[model] = None if c["usd"] is None else c["breakdown"]
            if c["usd"] is not None and c["estimated"]:
                self.estimated = True
        return self.memo[model]


def wasted_usd(rates, inp, c5, c1, read, prev_ctx) -> Optional[float]:
    """Extra $ vs. had the re-created tokens been cache reads (None if unpriced)."""
    if rates is None:
        return None
    fresh = inp + c5 + c1
    recreated = min(fresh, max(0, prev_ctx - read))
    if fresh <= 0 or recreated <= 0:
        return 0.0
    f = recreated / fresh
    extra = (inp * (rates["input"] - rates["cache_read"])
             + c5 * (rates["cache_create_5m"] - rates["cache_read"])
             + c1 * (rates["cache_create_1h"] - rates["cache_read"]))
    return f * extra / 1_000_000


def _bucket():
    return {"turns": 0, "miss_turns": 0, "cold_starts": 0, "tokens_read": 0,
            "tokens_created": 0, "tokens_input": 0, "wasted_usd": 0.0}


def _add(b, read, created, inp, miss, cold, waste):
    b["turns"] += 1
    b["tokens_read"] += read
    b["tokens_created"] += created
    b["tokens_input"] += inp
    if miss:
        b["miss_turns"] += 1
        b["wasted_usd"] += waste
    if cold:
        b["cold_starts"] += 1


def _finish(b, **extra):
    denom = b["tokens_read"] + b["tokens_created"] + b["tokens_input"]
    out = dict(extra, **b)
    out["hit_ratio"] = (b["tokens_read"] / denom) if denom else None
    out["wasted_usd"] = round(b["wasted_usd"], 4)
    return out


def cache_report(db_path, pricing, since=None, until=None) -> dict:
    rates = _Rates(pricing)
    total, causes = _bucket(), {c: {"turns": 0, "wasted_usd": 0.0, "tokens_created": 0} for c in CAUSES}
    by_day, by_project, by_model, sessions = {}, {}, {}, {}
    unpriced = 0
    cur_key = None
    tracker = ThreadTracker()
    gaps = Gaps()

    with connect(db_path) as conn:
        for (sid, slug, side, agent, ts, model, inp, read, c5, c1) in _stream(conn, until):
            key = (sid, side, agent)
            if key != cur_key:
                cur_key, tracker = key, ThreadTracker()
            st = tracker.step(_epoch(ts), model, inp, read, c5, c1)
            first, miss, cause = st.first, st.miss, st.cause
            created = c5 + c1
            in_range = not (since and ts < since)
            waste = 0.0
            if miss and in_range:
                w = wasted_usd(rates.get(model), inp, c5, c1, read, st.prev_ctx)
                if w is None:
                    unpriced += 1
                else:
                    waste = w
            if in_range:
                day = ts[:10]
                args = (read, created, inp, miss, first, waste)
                _add(total, *args)
                _add(by_day.setdefault(day, _bucket()), *args)
                _add(by_project.setdefault(slug, _bucket()), *args)
                _add(by_model.setdefault(model or "unknown", _bucket()), *args)
                s = sessions.setdefault(sid, {"slug": slug, "miss_count": 0, "wasted_usd": 0.0, "causes": {}})
                if miss:
                    s["miss_count"] += 1
                    s["wasted_usd"] += waste
                    cc = s["causes"].setdefault(cause, [0.0, 0])
                    cc[0] += waste
                    cc[1] += 1
                    cb = causes[cause]
                    cb["turns"] += 1
                    cb["wasted_usd"] += waste
                    cb["tokens_created"] += created
                    if st.expired:
                        gaps.expired(st.tier, st.gap_s, waste)
                gaps.wrote_5m(c5, rates.get(model))

        worst = heapq.nlargest(
            TOP_SESSIONS,
            ((v["wasted_usd"], v["miss_count"], sid) for sid, v in sessions.items() if v["miss_count"]),
        )
        slugs = set(by_project) | {sessions[sid]["slug"] for _, _, sid in worst}
        names = {}
        for slug in slugs:
            cwds = [r["cwd"] for r in conn.execute(
                "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL", (slug,))]
            names[slug] = best_project_name(cwds, slug)
        repos = slug_repo_map(conn, list(slugs))

    worst_sessions = []
    for w, n, sid in worst:
        v = sessions[sid]
        dom = max(v["causes"].items(), key=lambda kv: (kv[1][0], kv[1][1]))[0]
        worst_sessions.append({
            "session_id": sid, "project_slug": v["slug"], "project_name": names.get(v["slug"]),
            "repo": repos[v["slug"]]["key"], "repo_name": repos[v["slug"]]["name"],
            "miss_count": n, "wasted_usd": round(w, 4), "dominant_cause": dom,
        })

    summary = _finish(total, unpriced_miss_turns=unpriced, estimated_pricing=rates.estimated)
    summary["miss_rate"] = (total["miss_turns"] / total["turns"]) if total["turns"] else None
    return {
        "summary": summary,
        "by_cause": [dict(cause=c, help=CAUSE_HELP[c], turns=v["turns"],
                          wasted_usd=round(v["wasted_usd"], 4), tokens_created=v["tokens_created"])
                     for c, v in causes.items()],
        "by_day": [_finish(by_day[d], day=d) for d in sorted(by_day)],
        "by_project": sorted((_finish(b, project_slug=s, project_name=names.get(s),
                                      repo=repos[s]["key"], repo_name=repos[s]["name"]) for s, b in by_project.items()),
                             key=lambda r: -r["wasted_usd"]),
        "by_model": sorted((_finish(b, model=m) for m, b in by_model.items()),
                           key=lambda r: -r["wasted_usd"]),
        "worst_sessions": worst_sessions,
        "gaps": gaps.to_dict(),
        "thresholds": {
            "ttl_5m_s": TTL_5M_S, "ttl_1h_s": TTL_1H_S, "miss_share": MISS_SHARE,
            "min_miss_tokens": MIN_MISS_TOKENS, "prefix_drop_ratio": PREFIX_DROP_RATIO,
        },
    }
