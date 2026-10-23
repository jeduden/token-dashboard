"""Combined feed of the most recent messages across sessions, plus a per-session
summary of that same window so sessions can be compared side by side.

The feed is the last `limit` messages by timestamp over *all* sessions (optionally
narrowed to `sessions`). Each session is then summarised over only the messages
that landed in the window, so a long-idle session shows little and a busy one a lot.
"""
from __future__ import annotations

import json

from .db import connect, best_project_name, _range_clause
from .pricing import cost_for

SERIES_TURNS = 30  # per-session sparkline length (assistant turns)


def _context(r) -> int:
    return (r["input_tokens"] or 0) + (r["cache_read_tokens"] or 0) \
        + (r["cache_create_5m_tokens"] or 0) + (r["cache_create_1h_tokens"] or 0)


def _tool_names(raw) -> list:
    if not raw:
        return []
    try:
        return [t.get("name") for t in json.loads(raw) if t.get("name")]
    except (ValueError, AttributeError):
        return []


def recent_feed(db_path, pricing, limit=100, since=None, until=None, sessions=None) -> dict:
    rng, args = _range_clause(since, until)
    sess_sql, sess_args = "", []
    if sessions:
        # placeholder list is built from len(sessions); values are still bound
        sess_sql = " AND session_id IN (%s)" % ",".join("?" * len(sessions))
        sess_args = list(sessions)
    sql = f"""
      SELECT uuid, session_id, project_slug, cwd, type, timestamp, model, is_sidechain,
             input_tokens, output_tokens, cache_read_tokens,
             cache_create_5m_tokens, cache_create_1h_tokens,
             prompt_text, tool_calls_json
        FROM messages
       WHERE type IN ('user', 'assistant') {rng} {sess_sql}
       ORDER BY timestamp DESC
       LIMIT ?
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, (*args, *sess_args, limit))]
        names = {}
        for slug in {r["project_slug"] for r in rows}:
            cwds = [x["cwd"] for x in c.execute(
                "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
                (slug,))]
            names[slug] = best_project_name(cwds, slug)

    messages, by_session = [], {}
    for r in rows:  # newest first
        is_asst = r["type"] == "assistant"
        usd = cost_for(r["model"], r, pricing)["usd"] if is_asst and r["model"] else None
        ctx = _context(r) if is_asst else None
        m = {
            "uuid": r["uuid"], "session_id": r["session_id"],
            "project_name": names[r["project_slug"]], "type": r["type"],
            "timestamp": r["timestamp"], "model": r["model"],
            "is_sidechain": bool(r["is_sidechain"]),
            "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
            "cache_read_tokens": r["cache_read_tokens"],
            "context_tokens": ctx, "cost_usd": usd,
            "text": r["prompt_text"], "tools": _tool_names(r["tool_calls_json"]),
        }
        messages.append(m)
        by_session.setdefault(r["session_id"], []).append(m)

    summaries = [_summarise(sid, msgs) for sid, msgs in by_session.items()]
    summaries.sort(key=lambda s: s["last"], reverse=True)
    return {"messages": messages, "sessions": summaries}


def _summarise(sid: str, newest_first: list) -> dict:
    asst = [m for m in newest_first if m["type"] == "assistant"]
    asst_old_first = list(reversed(asst))[-SERIES_TURNS:]
    ctx = [m["context_tokens"] for m in asst]
    cache_read = sum(m["cache_read_tokens"] or 0 for m in asst)
    prompt_in = sum(m["context_tokens"] or 0 for m in asst)
    models = sorted({m["model"] for m in asst if m["model"]})
    tool_counts = {}
    for m in asst:
        for t in m["tools"]:
            tool_counts[t] = tool_counts.get(t, 0) + 1
    return {
        "session_id": sid,
        "project_name": newest_first[0]["project_name"],
        "first": newest_first[-1]["timestamp"], "last": newest_first[0]["timestamp"],
        "messages": len(newest_first), "assistant_turns": len(asst),
        "tokens": sum((m["input_tokens"] or 0) + (m["output_tokens"] or 0) for m in asst),
        "cost_usd": round(sum(m["cost_usd"] or 0 for m in asst), 6),
        "context_now": ctx[0] if ctx else None,
        "context_max": max(ctx) if ctx else None,
        "cache_hit": (cache_read / prompt_in) if prompt_in else None,
        "models": models,
        "top_tools": sorted(tool_counts, key=tool_counts.get, reverse=True)[:3],
        "series_context": [m["context_tokens"] for m in asst_old_first],
        "series_cost": [m["cost_usd"] or 0 for m in asst_old_first],
    }
