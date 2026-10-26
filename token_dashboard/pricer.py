"""Prices token usage against the rate table.

Rates are $ per million tokens. For one model the Pricer picks, in order:

* the `fast` rate set when the turn ran in fast mode and the model has one;
* the `long_context` rate set when the request's prompt was larger than the entry's
  threshold (a "band" says how many of the table's thresholds the prompt exceeded);
* the standard rates.

A model id missing from the table falls back to its tier's rates (flagged
`estimated`); an id with no tier is unpriced (`usd` None). Usage dicts and
sqlite3 rows work alike. Aggregated sums must not be priced with a band: the
threshold applies to one request, so group by band first.
"""
from __future__ import annotations

from bisect import bisect_left
from typing import Optional

from .rates import BUCKETS, canonical_id, tier_from_name

# a request's prompt size, for grouping rows by long-context band in SQL
PROMPT_SQL = "(input_tokens + cache_read_tokens + cache_create_5m_tokens + cache_create_1h_tokens)"
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens",
              "cache_create_5m_tokens", "cache_create_1h_tokens")


def prompt_tokens(usage) -> int:
    return (usage["input_tokens"] + usage["cache_read_tokens"]
            + usage["cache_create_5m_tokens"] + usage["cache_create_1h_tokens"])


def speed_of(usage) -> Optional[str]:
    try:
        return usage["speed"]
    except (KeyError, IndexError):
        return None


class Pricer:
    def __init__(self, pricing: dict):
        self.pricing = pricing
        self.thresholds = sorted({m["long_context"]["above_prompt_tokens"]
                                  for m in pricing["models"].values()
                                  if isinstance(m.get("long_context"), dict)})
        self._memo: dict = {}

    def band_of(self, prompt: int) -> int:
        """How many long-context thresholds a prompt of this size exceeds."""
        return bisect_left(self.thresholds, prompt)

    def rates(self, model: str, band: int = 0, speed: Optional[str] = None) -> Optional[dict]:
        """{bucket: $/Mtok, "estimated": bool, "model": id} or None when unpriced."""
        key = (model, band, speed)
        if key not in self._memo:
            self._memo[key] = self._resolve(model or "", band, speed)
        return self._memo[key]

    def _resolve(self, model: str, band: int, speed: Optional[str]) -> Optional[dict]:
        models = self.pricing["models"]
        mid = model if model in models else canonical_id(model)
        entry, estimated = models.get(mid), False
        if entry is None:
            tier = tier_from_name(model)
            entry = self.pricing.get("tier_fallback", {}).get(tier) if tier else None
            if entry is None:
                return None
            mid, estimated = f"{tier} (fallback)", True
        use = entry
        if speed == "fast" and isinstance(entry.get("fast"), dict):
            use = entry["fast"]
        else:
            lc = entry.get("long_context")
            if isinstance(lc, dict) and band > self.thresholds.index(lc["above_prompt_tokens"]):
                use = lc
        return {**{b: use[b] for b in BUCKETS}, "estimated": estimated, "model": mid}

    def class_of(self, model: str) -> Optional[str]:
        """fable / opus / sonnet / haiku, from the table when the model is listed, else from its name."""
        models = self.pricing["models"]
        e = models.get(model) or models.get(canonical_id(model))
        return (e or {}).get("class") or tier_from_name(model)

    def resolve(self, model: str, target: str) -> Optional[str]:
        """The model that stands in for `model` on `target`, or None. See `resolve_for`."""
        return self.resolve_for(model, target)[0]

    def resolve_for(self, model: str, target: str) -> tuple:
        """(model id, speed) pricing `model` on `target`.

        A target is a vendor id, `model:<id>`, or either with a `:fast` suffix. `:fast` uses the
        peer's `fast_model` (a separate high-speed model) or its `fast` rate set; a peer with
        neither stays at standard rates. Returns (None, None) when there is no peer.
        """
        models = self.pricing["models"]
        fast = target.endswith(":fast")
        base = target[:-5] if fast else target
        if base.startswith("model:"):
            mid = base[6:] if base[6:] in models else None
        else:
            cls = self.pricing.get("classes", {}).get(self.class_of(model) or "", {})
            mid = cls.get(base)
        if mid is None or not fast:
            return mid, None
        entry = models[mid]
        if entry.get("fast_model"):
            return entry["fast_model"], None
        return mid, ("fast" if isinstance(entry.get("fast"), dict) else None)

    def band_sql(self, prompt_expr: str) -> tuple:
        """(CASE expression, params) yielding the long-context band of a request's prompt size."""
        ts = list(reversed(self.thresholds))
        if not ts:
            return "0", []
        whens = "".join(f" WHEN {prompt_expr} > ? THEN {len(ts) - i}" for i in range(len(ts)))
        return f"CASE{whens} ELSE 0 END", ts

    def price(self, model: str, usage, band: Optional[int] = 0, speed: Optional[str] = None) -> dict:
        """{usd, estimated, breakdown}. `band=None` derives it from the usage's prompt size."""
        if band is None:
            band = self.band_of(prompt_tokens(usage))
        r = self.rates(model, band, speed)
        if r is None:
            return {"usd": None, "estimated": True, "breakdown": {}}
        bd = {
            "input":           usage["input_tokens"]           * r["input"]           / 1_000_000,
            "output":          usage["output_tokens"]          * r["output"]          / 1_000_000,
            "cache_read":      usage["cache_read_tokens"]      * r["cache_read"]      / 1_000_000,
            "cache_create_5m": usage["cache_create_5m_tokens"] * r["cache_create_5m"] / 1_000_000,
            "cache_create_1h": usage["cache_create_1h_tokens"] * r["cache_create_1h"] / 1_000_000,
        }
        return {"usd": round(sum(bd.values()), 6), "estimated": r["estimated"], "breakdown": bd}
