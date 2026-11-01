"""Plain-English findings and recommendations over the last few days, from fixed rules.

Each finding is `{key, kind, fig, subject, text, link}`: `kind` is `bad`, `good` or `info`,
`fig` one short number, `subject` the project / agent / prompt the sentence is about and
`text` the rest of it. Percentages are shares of the period's estimated cost. Nothing is
sent anywhere: the sentences are templates filled from the local database.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .findings_data import gather, BREAK_TOKENS

SUB_SHARE_BAD = 50.0      # subagents' share of cost, %
RUN_SHARE_BAD = 10.0      # one subagent run's share, %
PROJECT_SHARE_BAD = 40.0  # one project's share, %
AUTOMATED_SHARE_BAD = 10.0
CACHE_HIT_GOOD, CACHE_HIT_BAD = 90.0, 70.0
BREAK_MIN = 20


def _usd(x: float) -> str:
    return "$" + format(x, ",.0f" if x >= 100 else ".2f")


def _pct(x: float) -> str:
    return f"{x:.0f}%" if x >= 10 else f"{x:.1f}%"


def findings(db_path, pricing, days: int = 7, now: Optional[datetime] = None) -> dict:
    g = gather(db_path, pricing, days, now)
    agents, prompts, total, since = g["agents"], g["prompts"], g["total"], g["since"]
    out = {"window_days": days, "since": since, "cost_usd": total, "findings": [], "recommendations": []}
    if total <= 0:
        return out
    f, rec = out["findings"], out["recommendations"]
    t = agents["totals"]

    if t["runs"]:
        kind = "bad" if t["sub_share_pct"] >= SUB_SHARE_BAD else "info"
        f.append({"key": "subagent-share", "kind": kind, "fig": _pct(t["sub_share_pct"]), "subject": "Subagents",
                  "text": f"cost {_usd(t['sub_cost_usd'])} of {_usd(total)} across {t['runs']} runs, "
                          f"{_usd(t['sub_cost_usd'] / t['runs'])} per run on average", "link": "#/agents"})
        big = agents["runs"][0]
        if big["share_pct"] >= RUN_SHARE_BAD:
            f.append({"key": "biggest-run", "kind": "bad", "fig": _pct(big["share_pct"]), "subject": big["label"],
                      "text": f"({big['type']}, {big['project_name']}) cost {_usd(big['cost_usd'])} in one run of {big['turns']:,} turns",
                      "link": f"#/sessions/{big['session_id']}"})
            rec.append({"text": f"Long-running agents are the lever: {big['label']} kept growing its context for {big['turns']:,} "
                                f"turns. Split work like this into shorter agents with a written handoff, so context resets "
                                f"instead of being re-read on every turn.", "link": "#/agents"})

    projects = g["projects"]
    if projects:
        top = projects[0]
        share = 100 * top["cost"] / total
        if share >= PROJECT_SHARE_BAD:
            rest = f", and {projects[1]['name']} another {_pct(100 * projects[1]['cost'] / total)}" if len(projects) > 1 else ""
            f.append({"key": "project-share", "kind": "bad", "fig": _pct(share), "subject": top["name"],
                      "text": f"used {_usd(top['cost'])} in {top['sessions']} session{'s' if top['sessions'] != 1 else ''}{rest}",
                      "link": "#/projects"})
            rec.append({"text": f"{top['name']} dominates the period. Compact or clear between phases there so each phase "
                                f"starts from a small context.", "link": "#/compactions"})

    auto = [p for p in prompts if p["kind"]]
    auto_cost = sum(p["cost_usd"] for p in auto)
    if auto and 100 * auto_cost / total >= AUTOMATED_SHARE_BAD:
        idle = [p for p in auto if p["kind"] == "idle_notification"]
        parts = [f"{len(auto):,} messages from agents and tasks, with the turns that followed them (including subagents still running), cost {_usd(auto_cost)}"]
        if idle:
            parts.append(f"{len(idle):,} of them idle notifications ({_usd(sum(p['cost_usd'] for p in idle))})")
        f.append({"key": "automated-prompts", "kind": "bad", "fig": _pct(100 * auto_cost / total),
                  "subject": "Automated messages", "text": ", ".join(parts), "link": "#/prompts?kind=automated"})
        if idle:
            rec.append({"text": "Idle notifications restart a full-context turn each time. Shut idle team agents down instead "
                                "of leaving them waiting.", "link": "#/prompts?kind=automated"})

    cache = g["cache"]
    if cache["breaks"] >= BREAK_MIN:
        f.append({"key": "cache-breaks", "kind": "info", "fig": f"{cache['breaks']:,}", "subject": "Cache breaks",
                  "text": f"turns sent or wrote over {BREAK_TOKENS // 1000}k uncached tokens, {cache['sub_breaks']:,} of them in subagents",
                  "link": "#/cache"})
    if cache["hit_pct"] is not None:
        kind = "good" if cache["hit_pct"] >= CACHE_HIT_GOOD else "bad" if cache["hit_pct"] < CACHE_HIT_BAD else "info"
        f.append({"key": "cache-hit", "kind": kind, "fig": f"{cache['hit_pct']:.1f}%", "subject": "Cache hit rate",
                  "text": "of prompt tokens were read from cache", "link": "#/cache"})

    act = g["activity"]["overall"]
    if act["sessions"]:
        f.append({"key": "activity", "kind": "info", "fig": f"{act['active_hours']:g} h", "subject": "Active time",
                  "text": f"in {act['sessions']} sessions over {act['wall_hours']:g} h of wall-clock time, "
                          f"up to {act['peak_concurrent']} sessions working at once", "link": "#/activity"})
    return out
