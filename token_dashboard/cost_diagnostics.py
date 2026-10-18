"""Rule-based diagnostics over cost_insights aggregates.

Each diagnostic: {rule, severity ('info'|'warn'|'high'), text, evidence}.
Thresholds are named constants; no result text is ever inspected (only sizes).
"""
from __future__ import annotations

REREAD_MIN = 3                    # same file Read >= N times in one thread
BIG_OUTPUT_TOKENS = 8_000         # Bash/Grep/Glob result above this is "huge"
BIG_OUTPUT_TOOLS = ("Bash", "Grep", "Glob")
ERROR_RATE_MIN = 0.25             # tool error rate above this...
ERROR_MIN_CALLS = 5               # ...with at least this many attributed calls
SKILL_CARRY_SHARE = 0.7           # carry > 70% of a skill's total cost
SKILL_MIN_SESSIONS = 3            # ...and used in at least this many sessions
DOMINANT_SHARE = 0.2              # one call > 20% of its thread's total spend
DOMINANT_MIN_THREAD_USD = 0.25    # ignore trivial threads
EXPIRY_HEAVY_SHARE = 0.25         # expiry (re-write after idle gap) >= 25% of an item's cost...
EXPIRY_MIN_USD_CALL = 0.05        # ...and at least this many dollars for one call
EXPIRY_MIN_USD = 0.50             # ...or for a whole tool / skill
MAX_EXAMPLES = 5
EXPIRY_SUGGESTION = ("Compact or clear before a long pause, finish the task in one sitting, or keep "
                     "outputs short: every re-write after an idle gap pays for them again.")


def _d(rule, severity, text, evidence=None):
    return {"rule": rule, "severity": severity, "text": text, "evidence": evidence or {}}


def flag_call(call: dict) -> None:
    """Attach per-call diagnostics (call['flags'])."""
    f = call["flags"]
    rt = call["result_tokens"] or 0
    if call["tool_name"] in BIG_OUTPUT_TOOLS and rt > BIG_OUTPUT_TOKENS:
        f.append(_d("big_output", "warn",
                    f"{call['tool_name']} returned ~{rt:,} tokens; consider head/limit or a narrower pattern."))
    if call.get("read_n", 0) >= REREAD_MIN:
        f.append(_d("reread", "warn",
                    f"Same file read {call['read_n']} times in this session."))
    if call["is_error"]:
        f.append(_d("error", "info", "Call returned an error."))
    if call["usd"] > 0 and call["expiry_usd"] >= EXPIRY_MIN_USD_CALL \
            and call["expiry_usd"] >= EXPIRY_HEAVY_SHARE * call["usd"]:
        pct = round(100 * call["expiry_usd"] / call["usd"])
        f.append(_d("expiry_heavy", "warn",
                    f"~{pct}% of this call's cost is re-writing it to the cache after "
                    f"{call['expiry_events']} idle gap(s) longer than the cache lifetime.",
                    {"expiry_events": call["expiry_events"], "expiry_cost_usd": round(call["expiry_usd"], 6),
                     "expiry_gap_total_s": round(call["expiry_gap_s"], 1)}))
    tu = call["thread_usd"]
    if tu >= DOMINANT_MIN_THREAD_USD and call["usd"] > DOMINANT_SHARE * tu:
        pct = round(100 * call["usd"] / tu)
        f.append(_d("dominant_call", "high",
                    f"This one call is ~{pct}% of its session's spend."))


def note_big_output(big: dict, c: dict) -> None:
    rt = c["result_tokens"] or 0
    if c["tool_name"] not in BIG_OUTPUT_TOOLS or rt <= BIG_OUTPUT_TOKENS:
        return
    b = big.setdefault(c["tool_name"], {"count": 0, "tokens": 0, "examples": []})
    b["count"] += 1
    b["tokens"] += rt
    b["examples"].append({"session_id": c["session_id"], "timestamp": c["timestamp"],
                          "target": (c["target"] or "")[:200], "result_tokens": rt})
    b["examples"].sort(key=lambda e: -e["result_tokens"])
    del b["examples"][MAX_EXAMPLES:]


def find_rereads(session_id, reads, read_tokens) -> list:
    out = []
    for target, n in reads.items():
        if n >= REREAD_MIN:
            out.append({"session_id": session_id, "target": target[:200], "count": n,
                        "wasted_tokens": read_tokens[target] * (n - 1) // n})
    return out


def tool_diagnostics(agg, tool_rows: list) -> list:
    out = []
    for name, b in agg.big.items():
        ex = b["examples"][0]
        out.append(_d("big_output", "warn",
                      f"{b['count']} {name} call(s) returned more than {BIG_OUTPUT_TOKENS:,} tokens "
                      f"(largest ~{ex['result_tokens']:,}). Use head/limit or narrower patterns.",
                      {"tool": name, "examples": b["examples"]}))
    if agg.rereads:
        top = sorted(agg.rereads, key=lambda r: -r["count"])[:MAX_EXAMPLES]
        out.append(_d("reread", "warn",
                      f"{len(agg.rereads)} file(s) were Read {REREAD_MIN}+ times within one session; "
                      "most re-read: " + f"{top[0]['count']}x.",
                      {"tool": "Read", "examples": top}))
    for r in tool_rows:
        er = r["error_rate"]
        if er is not None and er > ERROR_RATE_MIN and r["attributed_calls"] >= ERROR_MIN_CALLS:
            out.append(_d("error_rate", "high" if er > 0.5 else "warn",
                          f"{r['tool_name']} fails {round(er * 100)}% of the time "
                          f"({r['attributed_calls']} calls); failed calls still cost context.",
                          {"tool": r["tool_name"]}))
    for r in tool_rows:
        d = _expiry_diag("tool", r["tool_name"], r)
        if d:
            out.append(d)
    if agg.unattributed:
        out.append(_d("unattributed", "info",
                      f"{agg.unattributed} of {agg.total_calls} calls have no linked result yet "
                      "(scanned before tool_use_id was stored, or result missing); a rescan fills this in.",
                      {}))
    sev = {"high": 0, "warn": 1, "info": 2}
    out.sort(key=lambda d: sev[d["severity"]])
    return out


def _expiry_diag(kind: str, name: str, r: dict):
    """expiry_heavy for a tool or skill row (needs est_cost_usd, expiry_cost_usd, expiry_events)."""
    usd, ex = r.get("est_cost_usd") or 0.0, r.get("expiry_cost_usd") or 0.0
    if usd <= 0 or ex < EXPIRY_MIN_USD or ex < EXPIRY_HEAVY_SHARE * usd:
        return None
    pct = round(100 * ex / usd)
    d = _d("expiry_heavy", "warn",
           f"{kind.capitalize()} '{name}': ~{pct}% of its cost (${ex:,.2f}) is re-writing results to the "
           f"cache after {r.get('expiry_events', 0)} idle gap(s) longer than the cache lifetime "
           "(5 min, or 1 h for 1h writes).",
           {kind: name, "expiry_cost_usd": round(ex, 6), "expiry_events": r.get("expiry_events", 0)})
    d["suggestion"] = EXPIRY_SUGGESTION
    return d


def skill_diagnostics(skill_rows: list) -> list:
    out = []
    for s in skill_rows:
        d = _expiry_diag("skill", s["skill"], s)
        if d:
            out.append(d)
        if (s["est_cost_usd"] > 0 and s["sessions"] >= SKILL_MIN_SESSIONS
                and s["carry_cost_usd"] > SKILL_CARRY_SHARE * s["est_cost_usd"]):
            pct = round(100 * s["carry_cost_usd"] / s["est_cost_usd"])
            out.append(_d("skill_carry", "warn",
                          f"Skill '{s['skill']}' is loaded in {s['sessions']} sessions and "
                          f"{pct}% of its cost is re-reading it on later turns; "
                          "consider a smaller SKILL.md or invoking it later in the session.",
                          {"skill": s["skill"]}))
    return out
