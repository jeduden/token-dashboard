"""What each prompt cost, counting everything that ran because of it.

A prompt is a `user` row of the main thread that carries text. Every assistant turn
after it, on the main thread and in subagents, belongs to it until the next prompt in
that session. Subagents that keep running after you send another prompt are charged to
the later prompt. Messages that agents or tasks send into the session (see
`prompt_kinds`) start prompts too and are labelled with their `kind`.
"""
from __future__ import annotations

from .db import connect, _range_clause
from .pricer import Pricer
from .prompt_kinds import classify
from .usage_sums import UsageSums

TEXT_HEAD = 2000
SORTS = ("tokens", "cost", "recent")


def _billable(t: dict) -> int:
    return t["input_tokens"] + t["output_tokens"] + t["cache_create_5m_tokens"] + t["cache_create_1h_tokens"]


def collect(db_path, pricing, since=None, until=None) -> list:
    """Every prompt in range with its main and subagent usage (unsorted, priced)."""
    pricer = Pricer(pricing)
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT uuid, session_id, project_slug, timestamp, type, agent_id, model, speed,
             input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
             CASE WHEN type = 'user' AND agent_id IS NULL THEN substr(prompt_text, 1, {int(TEXT_HEAD)}) END AS head,
             CASE WHEN type = 'user' AND agent_id IS NULL THEN prompt_chars END AS chars
        FROM messages
       WHERE type IN ('user', 'assistant') {rng}
       ORDER BY session_id, timestamp, rowid
    """
    out, cur, sid = [], None, None
    with connect(db_path) as c:
        for r in c.execute(sql, args):
            if r["session_id"] != sid:
                sid, cur = r["session_id"], None
            if r["type"] == "user":
                if r["head"] is not None:
                    cur = {"user_uuid": r["uuid"], "session_id": sid, "project_slug": r["project_slug"],
                           "timestamp": r["timestamp"], "prompt_text": r["head"], "prompt_chars": r["chars"],
                           "kind": classify(r["head"]), "main": UsageSums(), "sub": UsageSums(), "agents": set()}
                    out.append(cur)
                continue
            if cur is None:
                continue
            if r["agent_id"]:
                cur["sub"].add(pricer, r)
                cur["agents"].add(r["agent_id"])
            else:
                cur["main"].add(pricer, r)
    for p in out:
        main, sub = p.pop("main"), p.pop("sub")
        tm, ts = main.tokens(), sub.tokens()
        models = main.models() or sub.models()
        p.update({
            "model": max(models, key=models.get) if models else None,
            "api_calls": main.turns + sub.turns, "subagent_calls": len(p.pop("agents")),
            "cost_main_usd": round(main.cost(pricer), 6), "cost_sub_usd": round(sub.cost(pricer), 6),
            "tokens_main": _billable(tm), "tokens_sub": _billable(ts),
            "billable_tokens": _billable(tm) + _billable(ts),
            "cache_read_tokens": tm["cache_read_tokens"] + ts["cache_read_tokens"],
        })
        p["cost_usd"] = round(p["cost_main_usd"] + p["cost_sub_usd"], 6)
        p["estimated_cost_usd"] = p["cost_usd"]
    return out


def prompt_costs(db_path, pricing, limit=50, sort="tokens", since=None, until=None, kind=None) -> list:
    """The top `limit` prompts. `kind`: `human` (people only), `automated`, or a single kind name."""
    rows = [p for p in collect(db_path, pricing, since, until) if p["api_calls"]]
    if kind == "human":
        rows = [p for p in rows if p["kind"] is None]
    elif kind == "automated":
        rows = [p for p in rows if p["kind"] is not None]
    elif kind:
        rows = [p for p in rows if p["kind"] == kind]
    key = {"cost": "cost_usd", "recent": "timestamp"}.get(sort, "billable_tokens")
    rows.sort(key=lambda p: p[key], reverse=True)
    return rows[:limit]
