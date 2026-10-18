"""Per-thread cache cost model shared by every Costs view (estimates).

A result of ``T`` tokens produced at assistant turn ``k`` costs, over the rest
of its thread (session + agent_id):

* create: ``T * write rate`` once, at turn k+1, priced with the tier(s) that
  turn actually wrote (its 5m / 1h cache-write token mix);
* carry: ``T * cache-read rate`` for every later turn k+2.. that finds the
  prompt cache warm;
* expiry: ``T * write rate`` instead of the read rate for every later turn that
  is a cache TTL expiry (see ``cache_events``: idle gap longer than the lifetime
  of the tier last written, and the turn's usage shows the prefix was written
  again). After a re-write the result continues to be carried at the read rate.

The carry/expiry run ends at the first detected compaction: cache reads dropping
below COMPACTION_DROP_RATIO of the previous turn from at least COMPACTION_MIN_PREV_READ
tokens. An expiry also makes reads drop to ~0, so a TTL-expiry turn only counts as a
compaction when the prompt itself shrank below that ratio of the previous prompt.

All O(1) per result through prefix sums built once per thread.
"""
from __future__ import annotations

from .cache_events import ThreadTracker, epoch
from .pricing import cost_for

COMPACTION_DROP_RATIO = 0.5       # cache_read falling below 50% of previous turn...
COMPACTION_MIN_PREV_READ = 20_000  # ...from at least this many tokens = compaction

_Z = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
      "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0}


def rates(model, pricing, memo) -> tuple:
    """(cache_read, create_5m, create_1h, input, output) USD per token for ``model`` (0 if unpriced)."""
    r = memo.get(model)
    if r is None:
        def one(field):
            return (cost_for(model or "", {**_Z, field: 1_000_000}, pricing)["usd"] or 0.0) / 1e6
        r = memo[model] = (one("cache_read_tokens"), one("cache_create_5m_tokens"),
                           one("cache_create_1h_tokens"), one("input_tokens"), one("output_tokens"))
    return r


def write_rate(c5, c1, r5, r1, tier=None) -> float:
    """Blended cache-write rate for a turn that wrote ``c5`` 5m and ``c1`` 1h tokens."""
    n = c5 + c1
    if n > 0:
        return (c5 * r5 + c1 * r1) / n
    return r1 if tier == "1h" else r5


def load_thread(conn, pricing, memo, session_id, agent_id):
    """Ordered assistant turns of one thread with per-turn rates, cost and expiry verdict."""
    turns, index = [], {}
    sql = """
      SELECT uuid, timestamp, model, input_tokens, output_tokens, cache_read_tokens,
             cache_create_5m_tokens, cache_create_1h_tokens
        FROM messages
       WHERE session_id=? AND COALESCE(agent_id,'')=? AND type='assistant'
       ORDER BY timestamp, rowid
    """
    tracker = ThreadTracker()
    for r in conn.execute(sql, (session_id, agent_id)):
        rd, r5, r1, ri, ro = rates(r["model"], pricing, memo)
        c5, c1 = r["cache_create_5m_tokens"], r["cache_create_1h_tokens"]
        inp, cr = r["input_tokens"], r["cache_read_tokens"]
        usd = inp * ri + r["output_tokens"] * ro + cr * rd + c5 * r5 + c1 * r1
        st = tracker.step(epoch(r["timestamp"] or ""), r["model"], inp, cr, c5, c1)
        index[r["uuid"]] = len(turns)
        turns.append({"uuid": r["uuid"], "ts": r["timestamp"], "read": rd,
                      "create": write_rate(c5, c1, r5, r1, st.tier),
                      "usd": usd, "cr": cr, "model": r["model"],
                      "cc": c5 + c1, "ctx": st.ctx, "prev_ctx": st.prev_ctx,
                      "expired": st.expired, "tier": st.tier, "gap_s": st.gap_s or 0.0})
    return turns, index


def is_compaction(turns, i) -> bool:
    if i <= 0:
        return False
    p, t = turns[i - 1], turns[i]
    if not (p["cr"] >= COMPACTION_MIN_PREV_READ and t["cr"] < p["cr"] * COMPACTION_DROP_RATIO):
        return False
    # A TTL expiry also zeroes cache reads; it is a compaction only if the prompt shrank too.
    return (not t["expired"]) or t["ctx"] < COMPACTION_DROP_RATIO * t["prev_ctx"]


def is_expiry(turns, i) -> bool:
    """Counts as an expiry event for carried content (an expired turn that is not a compaction)."""
    return turns[i]["expired"] and not is_compaction(turns, i)


class Tables:
    """Prefix sums over a thread: read-rate sum on warm turns, write-rate sum / count / idle seconds
    on expiry turns, and the next compaction index for each turn."""
    __slots__ = ("read", "rewrite", "events", "gap", "nxt")

    def __init__(self, turns):
        n = len(turns)
        read, rew, ev, gap = [0.0] * (n + 1), [0.0] * (n + 1), [0] * (n + 1), [0.0] * (n + 1)
        nxt_idx = [n] * (n + 1)
        comp = [is_compaction(turns, i) for i in range(n)]
        for i, t in enumerate(turns):
            ex = t["expired"] and not comp[i]
            read[i + 1] = read[i] + (0.0 if ex else t["read"])
            rew[i + 1] = rew[i] + (t["create"] if ex else 0.0)
            ev[i + 1] = ev[i] + (1 if ex else 0)
            gap[i + 1] = gap[i] + (t["gap_s"] if ex else 0.0)
        for i in range(n - 1, -1, -1):
            nxt_idx[i] = i if comp[i] else nxt_idx[i + 1]
        self.read, self.rewrite, self.events, self.gap, self.nxt = read, rew, ev, gap, nxt_idx


def cost_of(tokens, k, turns, tb):
    """(create, carry, expiry, carry_turns, expiry_events, expiry_gap_s) for ``tokens`` after turn k."""
    n = len(turns)
    if not tokens or k + 1 >= n:
        return 0.0, 0.0, 0.0, 0, 0, 0.0
    create = tokens * turns[k + 1]["create"]
    start = k + 2
    if start >= n:
        return create, 0.0, 0.0, 0, 0, 0.0
    end = tb.nxt[start]
    return (create, tokens * (tb.read[end] - tb.read[start]), tokens * (tb.rewrite[end] - tb.rewrite[start]),
            max(0, end - start), tb.events[end] - tb.events[start], tb.gap[end] - tb.gap[start])
