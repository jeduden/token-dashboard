"""Token sums grouped by (model, long-context band, speed), priced once per group.

Rows are added one by one; `cost` prices each distinct group, so a prompt with
thousands of turns costs a handful of price lookups instead of thousands.
"""
from __future__ import annotations

from .pricer import Pricer, USAGE_KEYS, prompt_tokens


class UsageSums:
    __slots__ = ("groups", "turns")

    def __init__(self):
        self.groups: dict = {}
        self.turns = 0

    def add(self, pricer: Pricer, row) -> None:
        self.turns += 1
        key = (row["model"] or "", pricer.band_of(prompt_tokens(row)), row["speed"])
        g = self.groups.get(key)
        if g is None:
            g = self.groups[key] = dict.fromkeys(USAGE_KEYS, 0)
        for k in USAGE_KEYS:
            g[k] += row[k] or 0

    def cost(self, pricer: Pricer) -> float:
        total = 0.0
        for (model, band, speed), g in self.groups.items():
            if model:
                total += pricer.price(model, g, band, speed)["usd"] or 0.0
        return total

    def tokens(self) -> dict:
        t = dict.fromkeys(USAGE_KEYS, 0)
        for g in self.groups.values():
            for k in USAGE_KEYS:
                t[k] += g[k]
        return t

    def models(self) -> dict:
        """{model: output+input tokens}, to name the dominant model."""
        out: dict = {}
        for (model, _, _), g in self.groups.items():
            out[model] = out.get(model, 0) + g["input_tokens"] + g["output_tokens"]
        return out
