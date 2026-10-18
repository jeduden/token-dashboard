"""Shared prompt-cache expiry decision, used by the Cache tab and the Costs model.

One ``ThreadTracker`` walks the assistant turns of ONE thread in time order and
answers, per turn: was this a cache miss, and if so was the cause a cache TTL
expiry (idle gap since the previous turn longer than the lifetime of the tier
last written)?  Every turn that reads or writes the cache refreshes the TTL, so
only the gap to the immediately preceding turn matters.

Everything is a HEURISTIC: transcripts do not record why a write happened.  An
expiry is only detected when the turn's observed usage looks like a miss (see
``is_miss``); a gap longer than the TTL whose usage shows warm reads is not
counted.  Turns with no token usage are ignored (they neither count nor advance
the tracker's state).
"""
from __future__ import annotations

from datetime import date
from typing import Dict, Optional

# ---- tunable heuristics -----------------------------------------------------
TTL_5M_S = 5 * 60            # default prompt-cache lifetime
TTL_1H_S = 60 * 60           # extended lifetime (1h cache writes)
MISS_SHARE = 0.5             # miss if (created + uncached input) / prompt >= this
MIN_MISS_TOKENS = 2000       # ignore tiny prompts / small incremental writes
PREFIX_DROP_RATIO = 0.5      # cache_read < this * previous prompt size => prefix changed

TTL_CAUSES = {"ttl_expired_5m": "5m", "ttl_expired_1h": "1h"}

_ORD_CACHE: Dict[str, int] = {}


def epoch(ts) -> Optional[float]:
    """Fast ISO-8601 (UTC) -> seconds; None if unparseable. Day ordinals memoized."""
    try:
        day = ts[:10]
        o = _ORD_CACHE.get(day)
        if o is None:
            o = date(int(day[:4]), int(day[5:7]), int(day[8:10])).toordinal()
            if len(_ORD_CACHE) > 20000:
                _ORD_CACHE.clear()
            _ORD_CACHE[day] = o
        s = int(ts[11:13]) * 3600 + int(ts[14:16]) * 60 + float(ts[17:23].rstrip("Z+-") or 0)
        return o * 86400 + s
    except (ValueError, IndexError, TypeError):
        return None


def classify(gap_s, prev_model, model, last_tier, read, prev_ctx) -> str:
    """Heuristic cause for a miss turn. `last_tier` is '5m'/'1h'/None."""
    if prev_model and model and prev_model != model:
        return "model_switch"
    if gap_s is not None:
        if last_tier == "1h" and gap_s > TTL_1H_S:
            return "ttl_expired_1h"
        if last_tier == "5m" and gap_s > TTL_5M_S:
            return "ttl_expired_5m"
    if prev_ctx <= 0:
        return "unknown"
    if read < PREFIX_DROP_RATIO * prev_ctx:
        return "prefix_changed"
    return "new_content"


def is_miss(read, created, inp) -> bool:
    total = read + created + inp
    fresh = created + inp
    return total > 0 and fresh >= MIN_MISS_TOKENS and fresh / total >= MISS_SHARE


class Step:
    """Verdict for one turn.

    first      first usable turn of the thread (cold start, never a miss)
    miss       the turn re-created / left uncached most of its prompt
    cause      classify() result for a miss, else None
    expired    miss whose cause is a TTL expiry
    tier       '5m' / '1h': the tier whose lifetime elapsed (None unless expired)
    gap_s      seconds since the previous usable turn (None if unknown / first)
    prev_ctx   previous usable turn's prompt size (read + created + input)
    ctx        this turn's prompt size
    c5, c1     tokens written this turn at the 5m / 1h tier
    """
    __slots__ = ("first", "miss", "cause", "expired", "tier", "gap_s", "prev_ctx", "ctx", "c5", "c1")

    def __init__(self, first=False, miss=False, cause=None, tier=None, gap_s=None,
                 prev_ctx=0, ctx=0, c5=0, c1=0):
        self.first, self.miss, self.cause = first, miss, cause
        self.expired = cause in TTL_CAUSES
        self.tier, self.gap_s, self.prev_ctx, self.ctx, self.c5, self.c1 = tier, gap_s, prev_ctx, ctx, c5, c1


_IDLE = Step()


class ThreadTracker:
    """Feed one thread's turns in order with ``step``; keeps the previous-turn state."""

    def __init__(self):
        self.p_ts = self.p_model = self.last_tier = None
        self.p_ctx = 0
        self.seen = False

    def step(self, ts_s, model, inp, read, c5, c1) -> Step:
        """``ts_s`` is seconds (see ``epoch``) or None. Returns the turn's Step."""
        created = c5 + c1
        ctx = read + created + inp
        if ctx <= 0:
            return _IDLE
        first = not self.seen
        miss = (not first) and is_miss(read, created, inp)
        gap = (ts_s - self.p_ts) if (ts_s is not None and self.p_ts is not None) else None
        cause = classify(gap, self.p_model, model, self.last_tier, read, self.p_ctx) if miss else None
        s = Step(first, miss, cause, TTL_CAUSES.get(cause), gap, self.p_ctx, ctx, c5, c1)
        self.seen = True
        self.p_ts, self.p_model, self.p_ctx = ts_s, model, ctx
        if c1 > 0:
            self.last_tier = "1h"
        elif c5 > 0:
            self.last_tier = "5m"
        return s
