"""The facts bundle Claude reads to write findings (`cli.py findings-data`).

Everything is computed from the local database. Shares are of the window's estimated
cost. Prompt text is cut to 200 characters. The bundle also carries the rule-based
findings, so the writer can see what the fixed rules already say and go beyond them.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from .agent_types import agent_types
from .findings import findings
from .findings_data import gather

ATTRIBUTION = ("A prompt is charged for every turn after it in its session, main thread and subagents, until the next "
               "prompt. Subagents still running when a later prompt (or an agent's idle notice) arrives are charged "
               "to that later one, so automated messages look more expensive than they are: the figure is what ran "
               "after them, not what they caused.")


def _share(x: float, total: float) -> float:
    return round(100 * x / total, 1) if total else 0.0


def facts(db_path, pricing, days: int = 7, now: Optional[datetime] = None) -> dict:
    g = gather(db_path, pricing, days, now)
    total, agents = g["total"], g["agents"]
    now_dt = now or datetime.now(timezone.utc)
    prev_since = (now_dt - timedelta(days=2 * days)).strftime("%Y-%m-%dT%H:%M:%S")
    prev = agent_types(db_path, pricing, prev_since, g["since"], runs=1)["totals"]["cost_usd"]

    auto: dict = {}
    for p in g["prompts"]:
        if p["kind"]:
            a = auto.setdefault(p["kind"], {"count": 0, "cost_usd": 0.0})
            a["count"] += 1
            a["cost_usd"] += p["cost_usd"]
    auto_cost = sum(a["cost_usd"] for a in auto.values())
    top_prompts = sorted(g["prompts"], key=lambda p: p["cost_usd"], reverse=True)[:10]
    act = g["activity"]
    return {
        "window_days": days, "since": g["since"], "until": g["now"],
        "cost_usd": round(total, 2), "previous_window_cost_usd": round(prev, 2),
        "main_cost_usd": agents["totals"]["main_cost_usd"], "subagent_cost_usd": agents["totals"]["sub_cost_usd"],
        "subagent_share_pct": agents["totals"]["sub_share_pct"], "subagent_runs": agents["totals"]["runs"],
        "projects": [{"name": p["name"], "cost_usd": round(p["cost"], 2), "share_pct": _share(p["cost"], total),
                      "sessions": p["sessions"]} for p in g["projects"][:8]],
        "agent_types": [{k: t[k] for k in ("type", "runs", "turns", "cost_usd", "share_pct", "avg_run_usd", "max_run_usd")}
                        for t in agents["by_type"][:10]],
        "top_runs": [{"label": r["label"], "type": r["type"], "project": r["project_name"], "turns": r["turns"],
                      "cost_usd": r["cost_usd"], "share_pct": r["share_pct"], "peak_context_tokens": r["context_max"],
                      "session_id": r["session_id"]} for r in agents["runs"][:8]],
        "top_prompts": [{"text": " ".join(p["prompt_text"].split())[:200], "kind": p["kind"] or "person",
                         "cost_usd": round(p["cost_usd"], 2), "cost_subagents_usd": round(p["cost_sub_usd"], 2),
                         "share_pct": _share(p["cost_usd"], total), "api_calls": p["api_calls"],
                         "subagent_calls": p["subagent_calls"], "session_id": p["session_id"]} for p in top_prompts],
        "automated_messages": {"cost_usd": round(auto_cost, 2), "share_pct": _share(auto_cost, total),
                               "by_kind": {k: {"count": v["count"], "cost_usd": round(v["cost_usd"], 2),
                                               "share_pct": _share(v["cost_usd"], total)} for k, v in auto.items()}},
        "cache": {"hit_pct": None if g["cache"]["hit_pct"] is None else round(g["cache"]["hit_pct"], 1),
                  "breaks_over_100k_uncached_tokens": g["cache"]["breaks"], "of_them_in_subagents": g["cache"]["sub_breaks"]},
        "activity": {**act["overall"], "idle_gap_min": act["idle_gap_min"],
                     "active_hours_per_day": {d["date"]: round(d["active_min"] / 60, 1) for d in act["by_day"]}},
        "attribution_note": ATTRIBUTION,
        "rule_findings": findings(db_path, pricing, days, now_dt)["findings"],
    }


def dumps(db_path, pricing, days: int = 7) -> str:
    return json.dumps(facts(db_path, pricing, days), indent=1)
