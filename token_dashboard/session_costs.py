"""Estimated USD for sessions and for single turns, using the shared pricing table."""
from __future__ import annotations

from .db import connect
from .pricer import Pricer, PROMPT_SQL
from .pricing import cost_for
from .whatif import WhatIf, combine, row_alts


def add_turn_costs(turns: list, pricing: dict, targets=None) -> list:
    """Set `cost_usd` on each assistant row of `session_turns` (None for other rows).

    With `targets`, assistant rows also get `alt_costs`.
    """
    pricer = Pricer(pricing) if targets else None
    for t in turns:
        priced = t["type"] == "assistant" and t["model"]
        t["cost_usd"] = cost_for(t["model"], t, pricing, per_request=True)["usd"] if priced else None
        if targets:
            t["alt_costs"] = row_alts(pricer, targets, t["model"], t) if priced else dict.fromkeys(targets)
    return turns


def add_session_costs(db_path, pricing: dict, sessions: list, targets=None) -> list:
    """Set `cost_usd` (total), `cost_main_usd` and `cost_sub_usd` on each row of `recent_sessions`.

    With `targets` (vendor ids or `model:<id>`), also `alt_costs` for the total and
    `alt_costs_main` / `alt_costs_sub`, plus `alt_unpriced_tokens` for models with no peer.
    """
    ids = [s["session_id"] for s in sessions]
    if not ids:
        return sessions
    pricer = Pricer(pricing)
    band_sql, band_args = pricer.band_sql(PROMPT_SQL)
    # band_sql is internal; ids and thresholds are bound
    sql = f"""
      SELECT session_id, model, is_sidechain, speed, {band_sql} AS band,
             COALESCE(SUM(input_tokens),0)           AS input_tokens,
             COALESCE(SUM(output_tokens),0)          AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)      AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' AND model IS NOT NULL AND session_id IN ({",".join("?" * len(ids))})
       GROUP BY session_id, model, is_sidechain, speed, band
    """
    w = WhatIf(pricer, targets or [])
    with connect(db_path) as c:
        for r in c.execute(sql, (*band_args, *ids)):
            w.add(r["model"], r, r["band"], r["speed"], key=(r["session_id"], r["is_sidechain"]))
    for s in sessions:
        main, side = w.result((s["session_id"], 0)), w.result((s["session_id"], 1))
        s["cost_main_usd"], s["cost_sub_usd"] = main["cost_usd"], side["cost_usd"]
        s["cost_usd"] = round(main["cost_usd"] + side["cost_usd"], 6)
        if targets:
            s["alt_costs_main"], s["alt_costs_sub"] = main["alt_costs"], side["alt_costs"]
            both = combine([main, side], targets)
            s["alt_costs"], s["alt_unpriced_tokens"] = both["alt_costs"], both["alt_unpriced_tokens"]
    return sessions
