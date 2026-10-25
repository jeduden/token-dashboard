"""Which subagent did what in one session.

Subagent transcripts are tagged with an agent id (``messages.agent_id``). The
spawning Agent/Task call (``subagent_spawns``, linked through the id its result
reports) says why it was started; older rows without that link fall back to the
name embedded in the id (``a<name>-<hex>``) and the first prompt the agent received.
"""
from __future__ import annotations

import json
import re

from .db import connect
from .pricing import cost_for

NAMED_ID = re.compile(r"^a?(.+?)-[0-9a-f]{12,}$")
PROMPT_HEAD = 300


def _label(spawn, agent_id: str, first_prompt) -> str:
    for v in ((spawn or {}).get("description"), (spawn or {}).get("name"), (spawn or {}).get("subagent_type")):
        if v:
            return v
    m = NAMED_ID.match(agent_id)
    if m:
        return m.group(1)
    if first_prompt:
        return " ".join(first_prompt.split())[:60]
    return "agent " + agent_id[:8]


def session_subagents(db_path, pricing, session_id: str) -> list:
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(
            """SELECT agent_id, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens,
                      cache_create_5m_tokens, cache_create_1h_tokens, speed, tool_calls_json,
                      CASE WHEN type = 'user' THEN substr(prompt_text, 1, ?) END AS head
                 FROM messages WHERE session_id = ? AND agent_id IS NOT NULL
                ORDER BY timestamp""", (PROMPT_HEAD, session_id))]
        spawns = {r["agent_id"]: dict(r) for r in c.execute(
            "SELECT * FROM subagent_spawns WHERE session_id = ? AND agent_id IS NOT NULL ORDER BY timestamp",
            (session_id,))}

    agents = {}
    for r in rows:
        a = agents.setdefault(r["agent_id"], {
            "agent_id": r["agent_id"], "first": r["timestamp"], "last": r["timestamp"], "turns": 0,
            "cost_usd": 0.0, "tokens": 0, "context_max": 0, "tools": {}, "first_prompt": None, "models": set()})
        a["last"] = r["timestamp"]
        if r["type"] == "user" and r["head"] and not a["first_prompt"]:
            a["first_prompt"] = r["head"]
        if r["type"] != "assistant":
            continue
        a["turns"] += 1
        a["tokens"] += r["input_tokens"] + r["output_tokens"]
        a["context_max"] = max(a["context_max"], r["input_tokens"] + r["cache_read_tokens"]
                               + r["cache_create_5m_tokens"] + r["cache_create_1h_tokens"])
        if r["model"]:
            a["models"].add(r["model"])
            a["cost_usd"] += cost_for(r["model"], r, pricing, per_request=True)["usd"] or 0.0
        try:
            for t in json.loads(r["tool_calls_json"] or "[]"):
                if t.get("name"):
                    a["tools"][t["name"]] = a["tools"].get(t["name"], 0) + 1
        except (ValueError, TypeError, AttributeError):
            pass

    out = []
    for aid, a in agents.items():
        sp = spawns.get(aid)
        out.append({
            "agent_id": aid, "label": _label(sp, aid, a["first_prompt"]),
            "subagent_type": (sp or {}).get("subagent_type"),
            "description": (sp or {}).get("description"),
            "prompt_head": (sp or {}).get("prompt_head") or a["first_prompt"],
            "linked": sp is not None,  # False: label guessed from the id / first prompt
            "spawned_at": (sp or {}).get("timestamp"),
            "first": a["first"], "last": a["last"], "turns": a["turns"],
            "tokens": a["tokens"], "cost_usd": round(a["cost_usd"], 6), "context_max": a["context_max"],
            "models": sorted(a["models"]),
            "tools": sorted(a["tools"].items(), key=lambda kv: kv[1], reverse=True)[:5],
        })
    out.sort(key=lambda x: x["first"])
    return out
