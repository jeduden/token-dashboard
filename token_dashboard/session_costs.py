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
    """Set `cost_usd` on each row of `recent_sessions` (one grouped query per call)."""
    ids = [s["session_id"] for s in sessions]
    if not ids:
        return sessions
    sql = """
      SELECT session_id, model,
             COALESCE(SUM(input_tokens),0)           AS input_tokens,
             COALESCE(SUM(output_tokens),0)          AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)      AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' AND model IS NOT NULL AND session_id IN (%s)
       GROUP BY session_id, model
    """ % ",".join("?" * len(ids))  # placeholder list only; ids are bound
    totals = {}
    with connect(db_path) as c:
        for r in c.execute(sql, ids):
            usd = cost_for(r["model"], dict(r), pricing)["usd"] or 0.0
            totals[r["session_id"]] = totals.get(r["session_id"], 0.0) + usd
    for s in sessions:
        s["cost_usd"] = round(totals.get(s["session_id"], 0.0), 6)
    return sessions
