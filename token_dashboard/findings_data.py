"""The numbers behind the findings: one pass of the existing views over a window.

`gather` is shared by the rule-based findings (`findings.py`) and by the facts bundle
that Claude reads to write findings (`findings_facts.py`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .activity import activity
from .agent_types import agent_types
from .db import connect, best_project_name, _range_clause
from .pricer import PROMPT_SQL
from .prompt_costs import collect

BREAK_TOKENS = 100_000    # a turn that wrote or sent this much uncached context


def _cache(db_path, since, until) -> dict:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COALESCE(SUM(cache_read_tokens),0) AS read,
             COALESCE(SUM({PROMPT_SQL}),0) AS prompt,
             COALESCE(SUM(CASE WHEN input_tokens + cache_create_5m_tokens + cache_create_1h_tokens > ? THEN 1 ELSE 0 END),0) AS breaks,
             COALESCE(SUM(CASE WHEN input_tokens + cache_create_5m_tokens + cache_create_1h_tokens > ? AND agent_id IS NOT NULL THEN 1 ELSE 0 END),0) AS sub_breaks
        FROM messages WHERE type = 'assistant' {rng}
    """
    with connect(db_path) as c:
        r = c.execute(sql, (BREAK_TOKENS, BREAK_TOKENS, *args)).fetchone()
    return {"hit_pct": 100 * r["read"] / r["prompt"] if r["prompt"] else None,
            "breaks": r["breaks"], "sub_breaks": r["sub_breaks"]}


def _project_shares(db_path, pricing, prompts, since, until) -> list:
    by: dict = {}
    for p in prompts:
        d = by.setdefault(p["project_slug"], {"cost": 0.0, "sessions": set()})
        d["cost"] += p["cost_usd"]
        d["sessions"].add(p["session_id"])
    with connect(db_path) as c:
        out = []
        for slug, d in by.items():
            cwds = [x["cwd"] for x in c.execute(
                "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL", (slug,))]
            out.append({"name": best_project_name(cwds, slug), "cost": d["cost"], "sessions": len(d["sessions"])})
    return sorted(out, key=lambda x: x["cost"], reverse=True)


def window(days: int, now: Optional[datetime] = None) -> tuple:
    now = now or datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return (now - timedelta(days=days)).strftime(fmt), now.strftime(fmt)


def gather(db_path, pricing, days: int = 7, now: Optional[datetime] = None) -> dict:
    since, now_iso = window(days, now)
    agents = agent_types(db_path, pricing, since, None, runs=10)
    prompts = [p for p in collect(db_path, pricing, since, None) if p["api_calls"]]
    return {
        "days": days, "since": since, "now": now_iso, "agents": agents, "prompts": prompts,
        "projects": _project_shares(db_path, pricing, prompts, since, None),
        "cache": _cache(db_path, since, None),
        "activity": activity(db_path, since, None),
        "total": agents["totals"]["cost_usd"],
    }
