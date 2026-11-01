"""Subagent usage across all sessions: by type, and the most expensive single runs.

A run is one subagent (`messages.agent_id`) of one session. Its type, name and task come
from the Agent/Task call that started it (`subagent_spawns`); runs without that link are
typed `unknown` and labelled from the id (see `session_subagents`).
"""
from __future__ import annotations

from .db import connect, best_project_name, _range_clause
from .pricer import Pricer, PROMPT_SQL
from .session_subagents import _label, NAMED_ID

RUNS = 25


def _group_rows(c, pricer, agent_clause, rng, args):
    band_sql, band_args = pricer.band_sql(PROMPT_SQL)
    # agent_clause, rng and band_sql are internal; thresholds and the range are bound
    sql = f"""
      SELECT session_id, project_slug, COALESCE(agent_id, '') AS agent_id, model, speed, {band_sql} AS band,
             COUNT(*) AS turns, MIN(timestamp) AS first, MAX(timestamp) AS last,
             COALESCE(SUM(input_tokens),0)           AS input_tokens,
             COALESCE(SUM(output_tokens),0)          AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)      AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens,
             MAX({PROMPT_SQL}) AS context_max
        FROM messages
       WHERE type = 'assistant' AND {agent_clause} {rng}
       GROUP BY session_id, project_slug, agent_id, model, speed, band
    """
    return c.execute(sql, (*band_args, *args))


def _cost(pricer, r) -> float:
    return (pricer.price(r["model"], r, r["band"], r["speed"])["usd"] or 0.0) if r["model"] else 0.0


def _billable(r) -> int:
    return r["input_tokens"] + r["output_tokens"] + r["cache_create_5m_tokens"] + r["cache_create_1h_tokens"]


def agent_types(db_path, pricing, since=None, until=None, runs=RUNS) -> dict:
    pricer = Pricer(pricing)
    rng, args = _range_clause(since, until)
    with connect(db_path) as c:
        main_cost = sum(_cost(pricer, r) for r in _group_rows(c, pricer, "agent_id IS NULL", rng, args))
        spawns = {(r["session_id"], r["agent_id"]): dict(r) for r in c.execute(
            "SELECT * FROM subagent_spawns WHERE agent_id IS NOT NULL")}
        agents: dict = {}
        for r in _group_rows(c, pricer, "agent_id IS NOT NULL", rng, args):
            a = agents.setdefault((r["session_id"], r["agent_id"]), {
                "session_id": r["session_id"], "agent_id": r["agent_id"], "project_slug": r["project_slug"],
                "turns": 0, "cost_usd": 0.0, "billable_tokens": 0, "total_tokens": 0, "context_max": 0,
                "first": r["first"], "last": r["last"], "models": set()})
            a["turns"] += r["turns"]
            a["cost_usd"] += _cost(pricer, r)
            a["billable_tokens"] += _billable(r)
            a["total_tokens"] += _billable(r) + r["cache_read_tokens"]
            a["context_max"] = max(a["context_max"], r["context_max"])
            a["first"], a["last"] = min(a["first"], r["first"]), max(a["last"], r["last"])
            if r["model"]:
                a["models"].add(r["model"])
        names = {}
        for slug in {a["project_slug"] for a in agents.values()}:
            cwds = [x["cwd"] for x in c.execute(
                "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL", (slug,))]
            names[slug] = best_project_name(cwds, slug)

    sub_cost = sum(a["cost_usd"] for a in agents.values())
    total = main_cost + sub_cost
    by_type: dict = {}
    for key, a in agents.items():
        sp = spawns.get(key)
        named = NAMED_ID.match(a["agent_id"])
        a["type"] = (sp or {}).get("subagent_type") or (sp or {}).get("name") or (named.group(1) if named else "unknown")
        a["label"] = _label(sp, a["agent_id"], None)
        a["description"] = (sp or {}).get("description")
        a["project_name"] = names[a.pop("project_slug")]
        t = by_type.setdefault(a["type"], {"type": a["type"], "runs": 0, "turns": 0, "cost_usd": 0.0,
                                           "billable_tokens": 0, "total_tokens": 0, "max_run_usd": 0.0})
        t["runs"] += 1
        t["turns"] += a["turns"]
        t["cost_usd"] += a["cost_usd"]
        t["billable_tokens"] += a["billable_tokens"]
        t["total_tokens"] += a["total_tokens"]
        t["max_run_usd"] = max(t["max_run_usd"], a["cost_usd"])
    types = sorted(by_type.values(), key=lambda t: t["cost_usd"], reverse=True)
    share = lambda x: round(100 * x / total, 2) if total else 0.0
    for t in types:
        t["avg_run_usd"] = round(t["cost_usd"] / t["runs"], 6)
        t["share_pct"] = share(t["cost_usd"])
        t["cost_usd"], t["max_run_usd"] = round(t["cost_usd"], 6), round(t["max_run_usd"], 6)
    top = sorted(agents.values(), key=lambda a: a["cost_usd"], reverse=True)[:runs]
    for a in top:
        a["share_pct"] = share(a["cost_usd"])
        a["cost_usd"], a["models"] = round(a["cost_usd"], 6), sorted(a["models"])
    return {
        "totals": {"cost_usd": round(total, 6), "main_cost_usd": round(main_cost, 6),
                   "sub_cost_usd": round(sub_cost, 6), "sub_share_pct": share(sub_cost), "runs": len(agents)},
        "by_type": types, "runs": top,
    }
