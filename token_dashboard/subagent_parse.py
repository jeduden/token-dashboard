"""Spawn records for subagents, from Agent/Task ``tool_use`` blocks and their results.

Only short metadata is kept: description, type, name and the first
``PROMPT_HEAD`` characters of the prompt. The subagent's output is never read.
"""
from __future__ import annotations

from typing import Optional

PROMPT_HEAD = 300
SPAWN_TOOLS = ("Agent", "Task")

UPSERT = """
INSERT INTO subagent_spawns (tool_use_id, session_id, description, subagent_type, name, prompt_head, timestamp, agent_id)
VALUES (:tool_use_id, :session_id, :description, :subagent_type, :name, :prompt_head, :timestamp, :agent_id)
ON CONFLICT(tool_use_id) DO UPDATE SET
  description   = COALESCE(excluded.description,   description),
  subagent_type = COALESCE(excluded.subagent_type, subagent_type),
  name          = COALESCE(excluded.name,          name),
  prompt_head   = COALESCE(excluded.prompt_head,   prompt_head),
  timestamp     = COALESCE(excluded.timestamp,     timestamp),
  agent_id      = COALESCE(excluded.agent_id,      agent_id)
"""


def _s(v, limit: int = 500) -> Optional[str]:
    return v[:limit] if isinstance(v, str) and v else None


def store_spawns(conn, rec: dict, session_id: str) -> None:
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use" and b.get("name") in SPAWN_TOOLS and isinstance(b.get("id"), str):
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            conn.execute(UPSERT, {
                "tool_use_id": b["id"], "session_id": session_id,
                "description": _s(inp.get("description")), "subagent_type": _s(inp.get("subagent_type")),
                "name": _s(inp.get("name")), "prompt_head": _s(inp.get("prompt"), PROMPT_HEAD),
                "timestamp": rec.get("timestamp"), "agent_id": None})
        elif b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
            res = rec.get("toolUseResult")
            if not isinstance(res, dict):
                continue
            aid = _s(res.get("agentId")) or _s(res.get("agent_id"))
            if not aid:
                continue
            conn.execute(UPSERT, {
                "tool_use_id": b["tool_use_id"], "session_id": session_id,
                "description": _s(res.get("description")), "subagent_type": _s(res.get("agentType") or res.get("agent_type")),
                "name": _s(res.get("name")), "prompt_head": _s(res.get("prompt"), PROMPT_HEAD),
                "timestamp": None, "agent_id": aid})
