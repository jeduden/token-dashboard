"""Estimated USD for sessions and for single turns, using the shared pricing table."""
from __future__ import annotations

from .db import connect
from .pricing import cost_for


def add_turn_costs(turns: list, pricing: dict) -> list:
    """Set `cost_usd` on each assistant row of `session_turns` (None for other rows)."""
    for t in turns:
        t["cost_usd"] = cost_for(t["model"], t, pricing)["usd"] if t["type"] == "assistant" and t["model"] else None
    return turns


def add_session_costs(db_path, pricing: dict, sessions: list) -> list:
    """Set `cost_usd` (total), `cost_main_usd` and `cost_sub_usd` on each row of `recent_sessions`."""
    ids = [s["session_id"] for s in sessions]
    if not ids:
        return sessions
    sql = """
      SELECT session_id, model, is_sidechain,
             COALESCE(SUM(input_tokens),0)           AS input_tokens,
             COALESCE(SUM(output_tokens),0)          AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)      AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' AND model IS NOT NULL AND session_id IN (%s)
       GROUP BY session_id, model, is_sidechain
    """ % ",".join("?" * len(ids))  # placeholder list only; ids are bound
    totals = {}  # (session, is_sidechain) -> usd
    with connect(db_path) as c:
        for r in c.execute(sql, ids):
            usd = cost_for(r["model"], dict(r), pricing)["usd"] or 0.0
            k = (r["session_id"], r["is_sidechain"])
            totals[k] = totals.get(k, 0.0) + usd
    for s in sessions:
        main, side = totals.get((s["session_id"], 0), 0.0), totals.get((s["session_id"], 1), 0.0)
        s["cost_main_usd"], s["cost_sub_usd"] = round(main, 6), round(side, 6)
        s["cost_usd"] = round(main + side, 6)
    return sessions
