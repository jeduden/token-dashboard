"""Per-turn cost and token timeline for one session, with prompt and compaction
markers and breakdowns by token type, turn kind and model.

A turn is one (deduplicated) assistant message. Its cost is split by token type
using the same rates as everywhere else; `kind` says what the turn did:
`subagent` (sidechain), `tools` (called at least one tool) or `text` (plain reply).
"""
from __future__ import annotations

import json

from .db import connect, best_project_name
from .pricing import cost_for

KINDS = ("text", "tools", "subagent")
MAX_CAUSES = 8


def _tools(raw) -> list:
    try:
        return [t["name"] for t in json.loads(raw) if t.get("name")] if raw else []
    except (ValueError, TypeError, AttributeError):
        return []


def session_timeline(db_path, pricing, session_id: str) -> dict:
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(
            """SELECT uuid, agent_id, type, timestamp, model, is_sidechain, input_tokens, output_tokens,
                      cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
                      prompt_text, prompt_chars, tool_calls_json, project_slug
                 FROM messages WHERE session_id = ? ORDER BY timestamp ASC""", (session_id,))]
        calls = {}
        for r in c.execute(
                """SELECT message_uuid, tool_name, target, result_tokens, is_error
                     FROM tool_calls WHERE session_id = ?""", (session_id,)):
            calls.setdefault(r["message_uuid"], []).append(dict(r))
        comps = [dict(r) for r in c.execute(
            """SELECT timestamp, trigger, pre_tokens, post_tokens FROM compactions
                WHERE session_id = ? ORDER BY timestamp""", (session_id,))]
        slug = rows[0]["project_slug"] if rows else ""
        cwds = [r["cwd"] for r in c.execute(
            "SELECT DISTINCT cwd FROM messages WHERE session_id=? AND cwd IS NOT NULL", (session_id,))]

    turns, prompts = [], []
    by_type = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_create": 0.0}
    by_kind = {k: {"turns": 0, "cost_usd": 0.0, "tokens": 0} for k in KINDS}
    by_model, cum = {}, 0.0
    pending, last = [], {}  # prompts since the last main-thread turn; last turn per thread
    for r in rows:
        if r["type"] == "user":
            if r["prompt_text"]:
                p = {"t": r["timestamp"], "chars": r["prompt_chars"], "text": r["prompt_text"][:200]}
                prompts.append(p); pending.append(p)
            continue
        if r["type"] != "assistant":
            continue
        cf = cost_for(r["model"], r, pricing) if r["model"] else {"usd": None, "breakdown": {}}
        bd = cf["breakdown"]
        split = {"input": bd.get("input", 0), "output": bd.get("output", 0),
                 "cache_read": bd.get("cache_read", 0),
                 "cache_create": bd.get("cache_create_5m", 0) + bd.get("cache_create_1h", 0)}
        usd = cf["usd"] or 0.0
        cum += usd
        tools = _tools(r["tool_calls_json"])
        kind = "subagent" if r["is_sidechain"] else ("tools" if tools else "text")
        create = r["cache_create_5m_tokens"] + r["cache_create_1h_tokens"]
        ctx = r["input_tokens"] + r["cache_read_tokens"] + create
        for k, v in split.items():
            by_type[k] += v
        by_kind[kind]["turns"] += 1; by_kind[kind]["cost_usd"] += usd
        by_kind[kind]["tokens"] += r["input_tokens"] + r["output_tokens"]
        mk = by_model.setdefault(r["model"] or "unknown", {"turns": 0, "cost_usd": 0.0})
        mk["turns"] += 1; mk["cost_usd"] += usd
        thread = r["agent_id"] or ("side" if r["is_sidechain"] else "")
        prev = last.get(thread)
        # What grew the context: the user's prompt(s) and the results of the tools the
        # previous turn of this thread called (they are fed back in on this turn).
        results = sorted(calls.get(prev["uuid"], []) if prev else [],
                         key=lambda x: x["result_tokens"] or 0, reverse=True)
        caused = {
            "prompts": [] if r["is_sidechain"] else pending,
            "tool_results": [{"tool": x["tool_name"], "target": x["target"],
                              "result_tokens": x["result_tokens"], "is_error": bool(x["is_error"])}
                             for x in results[:MAX_CAUSES]],
            "other_result_tokens": sum(x["result_tokens"] or 0 for x in results[MAX_CAUSES:]),
        }
        if not r["is_sidechain"]:
            pending = []
        delta = ctx - (prev["ctx"] if prev else 0)
        last[thread] = {"uuid": r["uuid"], "ctx": ctx}
        turns.append({
            "t": r["timestamp"], "model": r["model"], "kind": kind, "tools": tools,
            "context_delta": delta, "caused": caused,
            "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
            "cache_read_tokens": r["cache_read_tokens"], "cache_create_tokens": create,
            "context_tokens": ctx, "cost_usd": round(usd, 6),
            "cost_split": {k: round(v, 6) for k, v in split.items()},
            "cum_cost_usd": round(cum, 6),
        })

    rnd = lambda d: {k: (round(v, 6) if isinstance(v, float) else v) for k, v in d.items()}
    return {
        "session_id": session_id,
        "project_name": best_project_name(cwds, slug) if rows else "",
        "turns": turns, "prompts": prompts, "compactions": comps,
        "summary": {
            "cost_usd": round(cum, 6),
            "by_token_type": {k: round(v, 6) for k, v in by_type.items()},
            "by_kind": {k: rnd(v) for k, v in by_kind.items()},
            "by_model": {k: rnd(v) for k, v in by_model.items()},
        },
    }
