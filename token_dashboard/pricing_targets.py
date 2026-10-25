"""Vendors, equivalence classes and price provenance, for the Settings tab.

A class (fable / opus / sonnet / haiku) names one comparable model per vendor; the
mapping lives in pricing.json under `classes`. A vendor with no peer is `null`.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from .rates import BUCKETS

STALE_DAYS = 90
VENDOR_LABELS = {"anthropic": "Anthropic", "openai": "OpenAI", "kimi": "Kimi (Moonshot)"}


def _age_days(as_of: str, today: date) -> Optional[int]:
    try:
        return (today - datetime.strptime(as_of, "%Y-%m-%d").date()).days
    except (TypeError, ValueError):
        return None


def pricing_targets(pricing: dict, today: Optional[date] = None) -> dict:
    today = today or date.today()
    models = pricing["models"]
    vendors = []
    for m in models.values():
        if m["vendor"] not in vendors:
            vendors.append(m["vendor"])

    def member(mid):
        m = models.get(mid) if mid else None
        if not m:
            return None
        out = {"model": mid, "verified": m.get("verified", True), "as_of": m.get("as_of"),
               "derived": m.get("derived", []), "note": m.get("note"), "fast": m.get("fast")}
        out.update({b: m[b] for b in BUCKETS})
        return out

    classes = [{"id": cid, "label": c.get("label", cid),
                "members": {v: member(c.get(v)) for v in vendors}}
               for cid, c in pricing.get("classes", {}).items()]
    sources = {}
    for sid, s in pricing.get("sources", {}).items():
        age = _age_days(s.get("as_of"), today)
        sources[sid] = {**s, "age_days": age, "stale": age is None or age > STALE_DAYS}
    return {"vendors": [{"id": v, "label": VENDOR_LABELS.get(v, v)} for v in vendors],
            "classes": classes, "sources": sources}
