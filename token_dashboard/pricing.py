"""Pricing table + plan-aware cost formatting."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Union

from .db import connect
from .pricer import Pricer, speed_of


def load_pricing(path: Union[str, Path]) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


_PRICERS: dict = {}  # id(pricing) -> (pricing, Pricer); the tuple keeps the dict alive so the id stays valid


def pricer_for(pricing: dict) -> Pricer:
    hit = _PRICERS.get(id(pricing))
    if hit is None or hit[0] is not pricing:
        if len(_PRICERS) >= 8:
            _PRICERS.clear()
        hit = _PRICERS[id(pricing)] = (pricing, Pricer(pricing))
    return hit[1]


def cost_for(model: str, usage, pricing: dict, per_request: bool = False) -> dict:
    """Return {usd, estimated, breakdown}. usd=None when no tier match.

    `usage` may be one request or a sum over many. Pass `per_request=True` only for a
    single request: it then also applies the request's `speed` (fast mode) and its
    long-context band. Summed usage is priced at the standard rates.
    """
    pr = pricer_for(pricing)
    if per_request:
        return pr.price(model, usage, band=None, speed=speed_of(usage))
    return pr.price(model, usage)


def get_plan(db_path: Union[str, Path], default: str = "api") -> str:
    with connect(db_path) as c:
        row = c.execute("SELECT v FROM plan WHERE k='plan'").fetchone()
    return row["v"] if row else default


def set_plan(db_path: Union[str, Path], plan: str) -> None:
    with connect(db_path) as c:
        c.execute("INSERT OR REPLACE INTO plan (k, v) VALUES ('plan', ?)", (plan,))
        c.commit()


def format_for_user(api_cost_usd: float, plan: str, pricing: dict) -> dict:
    p = pricing["plans"].get(plan, pricing["plans"]["api"])
    if plan == "api" or p["monthly"] == 0:
        return {"display_usd": api_cost_usd, "subtitle": None, "subscription_usd": None}
    return {
        "display_usd":      api_cost_usd,
        "subtitle":         f"You pay ${p['monthly']}/mo on {p['label']}",
        "subscription_usd": p["monthly"],
    }
