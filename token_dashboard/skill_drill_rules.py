"""Rule-based diagnostics for one skill's drill-down (see skill_drilldown).

Each diagnostic: {rule, severity ('info'|'warn'|'high'), text, evidence, suggestion}.
Thresholds are named constants so they can be tuned and tested. Only sizes and
counts are inspected, never any text.
"""
from __future__ import annotations

from . import cost_diagnostics as dx

RELOAD_MIN = 2                  # >= N invocations in one session = reloaded_in_session
LONG_CARRY_TURNS = 40           # avg later turns the skill stays in context...
LONG_CARRY_SHARE = dx.SKILL_CARRY_SHARE  # ...and carry is > this share of its cost
SHORT_CARRY_TURNS = 3           # carried at most this many turns = "session ended soon"
BIG_DEFINITION_TOKENS = 5_000   # SKILL.md / load size above this is "big"
RARE_MAX_SESSIONS = 2           # used in at most this many sessions...
RARE_HEAVY_FACTOR = 2.0         # ...but costs >= this x the average skill per invocation
SEV_ORDER = {"high": 0, "warn": 1, "info": 2}


def _d(rule, severity, text, evidence, suggestion):
    return {"rule": rule, "severity": severity, "text": text, "evidence": evidence or {},
            "suggestion": suggestion}


def skill_rules(skill, t, avg_carry_turns, repeat, cmp, top_session) -> list:
    """t = totals dict, repeat = repeat section, cmp = {avg_cost_per_invocation}."""
    out = []
    usd = t["est_cost_usd"]
    if repeat["sessions_with_repeats"]:
        w = repeat["worst_session"]
        out.append(_d(
            "reloaded_in_session", "warn",
            f"Loaded {repeat['repeat_invocations']} extra time(s) within the same session "
            f"({repeat['sessions_with_repeats']} session(s)); each reload re-pays the full load "
            f"(${repeat['repeat_cost_usd']:.2f} of ${usd:.2f}).",
            {"session_id": w["session_id"], "invocations": w["invocations"]},
            "Once loaded, a skill stays in context: reuse it instead of invoking it again, "
            "or split it so only the needed part is loaded."))
    if usd > 0 and avg_carry_turns > LONG_CARRY_TURNS and t["carry_cost_usd"] > LONG_CARRY_SHARE * usd:
        pct = round(100 * t["carry_cost_usd"] / usd)
        out.append(_d(
            "long_carry", "warn",
            f"On average the skill stays in context for ~{round(avg_carry_turns)} later turns and "
            f"{pct}% of its cost is re-reading it.",
            top_session and {"session_id": top_session["session_id"]},
            "Invoke it later in the session, run it in a fresh or sub-agent session, or compact "
            "after use; a smaller SKILL.md also cuts every carried turn."))
    if t["est_load_tokens"] > BIG_DEFINITION_TOKENS:
        out.append(_d(
            "big_definition", "warn",
            f"Each load is ~{t['est_load_tokens']:,} tokens (limit {BIG_DEFINITION_TOKENS:,}).",
            {"est_load_tokens": t["est_load_tokens"]},
            "Trim SKILL.md or move rarely needed detail into referenced files that load on demand."))
    avg = cmp["avg_cost_per_invocation"]
    if (t["sessions"] <= RARE_MAX_SESSIONS and t["attributed_invocations"]
            and t["cost_per_invocation"] >= RARE_HEAVY_FACTOR * avg > 0):
        out.append(_d(
            "rarely_used_but_heavy", "info",
            f"Used in only {t['sessions']} session(s) but ${t['cost_per_invocation']:.2f} per "
            f"invocation, {t['cost_per_invocation'] / avg:.1f}x the average skill.",
            {"sessions": t["sessions"], "avg_cost_per_invocation": round(avg, 6)},
            "Consider uninstalling it, or keep it for sessions that need it."))
    if t["estimated"]:
        out.append(_d(
            "estimated_load", "info",
            "The load size is estimated from the cache-write jump on the next turn, not from an "
            "installed SKILL.md, so figures are approximate.",
            {"est_load_tokens": t["est_load_tokens"]},
            "Install it under ~/.claude/skills, scheduled-tasks or plugins to get an exact size."))
    out.extend(dict(d, suggestion=d.get("suggestion") or "") for d in dx.skill_diagnostics([{
        "skill": skill, "est_cost_usd": usd, "sessions": t["sessions"],
        "carry_cost_usd": t["carry_cost_usd"]}]))
    for d in out:
        if d["rule"] == "skill_carry":
            d["suggestion"] = "Consider a smaller SKILL.md or invoking it later in the session."
            d["evidence"] = {"skill": skill}
    if t["unattributed_invocations"]:
        out.append(_d(
            "unattributed", "info",
            f"{t['unattributed_invocations']} of {t['invocations']} invocations have no linked result "
            "and no known size, so they are counted but not costed.",
            {}, "Rescan after upgrading so tool_use_id is stored; install the skill for an exact size."))
    out.sort(key=lambda d: SEV_ORDER[d["severity"]])
    return out
