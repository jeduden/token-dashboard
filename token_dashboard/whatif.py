"""What the same token usage would have cost on other models.

`parse_targets` validates a `compare=` list. `WhatIf` accumulates usage per
(key, model, band, speed) and prices each distinct group once per target when the
result is read, so callers never loop over targets themselves.

A target is a vendor id (`openai`, `kimi`, `anthropic`), which maps each source
model to its class peer on that vendor, or `model:<id>`, which prices everything
at one model. A source model whose class has no peer on the vendor (Kimi has no
small model) is left out of that target's cost and its tokens are reported in
`alt_unpriced_tokens`. Other vendors have no fast mode: alternatives use standard rates.
"""
from __future__ import annotations

from typing import Optional

from .pricer import Pricer, USAGE_KEYS

MAX_TARGETS = 4
_BUCKET_KEYS = ("input", "output", "cache_read", "cache_create_5m", "cache_create_1h")


def parse_targets(pricing: dict, raw) -> list:
    """Split a comma list, drop blanks and repeats, and check each id. Raises ValueError."""
    ids = []
    for t in (raw.split(",") if isinstance(raw, str) else raw or []):
        t = t.strip()
        if t and t not in ids:
            ids.append(t)
    if len(ids) > MAX_TARGETS:
        raise ValueError(f"at most {MAX_TARGETS} comparison targets")
    vendors = {m.get("vendor") for m in pricing["models"].values()}
    for t in ids:
        ok = t in vendors or (t.startswith("model:") and t[6:] in pricing["models"])
        if not ok:
            raise ValueError(f"unknown comparison target {t!r}")
    return ids


def combine(results: list, targets: list) -> dict:
    """Sum the `alt_*` fields of several `WhatIf.result`s (e.g. a session's main and sub threads)."""
    alt, priced, unpriced = {}, {}, {}
    for t in targets:
        priced[t] = sum(r["alt_priced_tokens"][t] for r in results)
        unpriced[t] = sum(r["alt_unpriced_tokens"][t] for r in results)
        alt[t] = (round(sum(r["alt_costs"][t] or 0.0 for r in results), 6)
                  if priced[t] or not unpriced[t] else None)
    return {"alt_costs": alt, "alt_unpriced_tokens": unpriced, "alt_priced_tokens": priced}


def row_alts(pricer: Pricer, targets: list, model: Optional[str], usage) -> dict:
    """{target: usd or None} for one request. None when the model has no peer on that target."""
    out = {}
    for t in targets:
        m = pricer.resolve(model, t) if model else None
        out[t] = None if m is None else pricer.price(m, usage, band=None)["usd"]
    return out


class WhatIf:
    def __init__(self, pricer: Pricer, targets: list):
        self.pricer, self.targets = pricer, list(targets)
        self._sums: dict = {}  # (key, model, band, speed) -> {usage key: tokens}

    def add(self, model: Optional[str], usage, band: int = 0, speed: Optional[str] = None, key=None) -> None:
        s = self._sums.setdefault((key, model or "", band, speed), dict.fromkeys(USAGE_KEYS, 0))
        for k in USAGE_KEYS:
            s[k] += usage[k] or 0

    def keys(self) -> list:
        return list({k[0] for k in self._sums})

    def result(self, key=None) -> dict:
        """Totals for one key, or over everything when `key` is None."""
        actual, estimated, tokens = 0.0, False, 0
        by_bucket = dict.fromkeys(_BUCKET_KEYS, 0.0)
        alt = dict.fromkeys(self.targets, 0.0)
        priced = dict.fromkeys(self.targets, 0)
        unpriced = dict.fromkeys(self.targets, 0)
        resolved: dict = {}
        for (k, model, band, speed), s in self._sums.items():
            if key is not None and k != key:
                continue
            n = sum(s.values())
            tokens += s["input_tokens"] + s["output_tokens"]
            c = self.pricer.price(model, s, band, speed) if model else {"usd": None, "estimated": True, "breakdown": {}}
            if c["usd"] is not None:
                actual += c["usd"]
                estimated = estimated or c["estimated"]
                for b, v in c["breakdown"].items():
                    by_bucket[b] += v
            for t in self.targets:
                m = resolved[(model, t)] if (model, t) in resolved else resolved.setdefault(
                    (model, t), self.pricer.resolve(model, t) if model else None)
                if m is None:
                    unpriced[t] += n
                    continue
                priced[t] += n
                alt[t] += self.pricer.price(m, s, band, None)["usd"] or 0.0
        return {
            "cost_usd": round(actual, 6), "estimated": estimated, "tokens": tokens,
            "by_bucket": {b: round(v, 6) for b, v in by_bucket.items()},
            "alt_costs": {t: (round(alt[t], 6) if priced[t] or not unpriced[t] else None) for t in self.targets},
            "alt_unpriced_tokens": unpriced, "alt_priced_tokens": priced,
        }
