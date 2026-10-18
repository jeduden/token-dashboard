"""Idle-gap distribution of cache TTL expiries (the Cache tab's "gaps" section).

Answers: when the prompt cache expired, how long had the thread been idle, and
would a keep-alive or the 1h cache have helped?  Fed by ``cache_insights`` one
expired turn at a time; nothing here touches the database.
"""
from __future__ import annotations

from .cache_events import TTL_5M_S, TTL_1H_S

# (key, label, exclusive lower bound s, inclusive upper bound s); an expired gap is > 5 min.
BUCKETS = [
    ("5_10m", "5-10 min", TTL_5M_S, 600),
    ("10_30m", "10-30 min", 600, 1800),
    ("30_60m", "30-60 min", 1800, TTL_1H_S),
    ("1_2h", "1-2 h", TTL_1H_S, 7200),
    ("2_6h", "2-6 h", 7200, 21600),
    ("gt_6h", "over 6 h", 21600, None),
]


def bucket_key(gap_s) -> str:
    for key, _label, _lo, hi in BUCKETS:
        if hi is None or gap_s <= hi:
            return key
    return BUCKETS[-1][0]


class Gaps:
    def __init__(self):
        self.b = {k: [0, 0.0] for k, *_ in BUCKETS}
        self.tier = {"5m": [0, 0.0], "1h": [0, 0.0]}
        self.recoverable = 0.0       # waste of 5m expiries a 1h cache would have bridged
        self.covered = 0
        self.premium = 0.0           # extra cost of writing every 5m token at the 1h rate

    def expired(self, tier, gap_s, waste):
        if gap_s is not None:
            row = self.b[bucket_key(gap_s)]
            row[0] += 1
            row[1] += waste
        t = self.tier[tier]
        t[0] += 1
        t[1] += waste
        if tier == "5m" and gap_s is not None and gap_s <= TTL_1H_S:
            self.covered += 1
            self.recoverable += waste

    def wrote_5m(self, tokens, rates):
        """Accumulate the 1h-vs-5m write premium for ``tokens`` (rates = $/Mtok breakdown or None)."""
        if tokens and rates:
            self.premium += tokens * (rates["cache_create_1h"] - rates["cache_create_5m"]) / 1_000_000

    def to_dict(self) -> dict:
        net = self.recoverable - self.premium
        return {
            "buckets": [{"key": k, "label": lab, "turns": self.b[k][0],
                         "wasted_usd": round(self.b[k][1], 4)} for k, lab, _lo, _hi in BUCKETS],
            "by_tier": {t: {"turns": v[0], "wasted_usd": round(v[1], 4)} for t, v in self.tier.items()},
            "what_if_1h": {
                "expiries": self.covered, "recoverable_usd": round(self.recoverable, 4),
                "extra_write_cost_usd": round(self.premium, 4), "net_usd": round(net, 4),
                "note": ("If every 5m write had been a 1h write, the 5m expiries with an idle gap of "
                         "at most 1 hour would have been cache reads (recoverable), but all 5m tokens "
                         "written in the range would cost the 1h rate (extra write cost). Same "
                         "turns assumed; later effects and changed behaviour are not modelled."),
            },
        }
