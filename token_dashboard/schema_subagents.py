"""``subagent_spawns`` table: which Agent/Task call started which subagent, and why.

One row per spawning tool call. The call (a ``tool_use`` block) supplies the
description / type / name / start of the prompt; the matching ``tool_result``
record supplies the ``agentId`` that tags the subagent's own transcript
(``messages.agent_id``). Databases scanned before this table existed hold no
rows, so a one-time replay (same mechanism as the compactions backfill) re-reads
every transcript without deleting anything.
"""
from __future__ import annotations

BACKFILL_KEY = "subagents_backfill_v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS subagent_spawns (
  tool_use_id    TEXT PRIMARY KEY,
  session_id     TEXT NOT NULL,
  description    TEXT,
  subagent_type  TEXT,
  name           TEXT,
  prompt_head    TEXT,
  timestamp      TEXT,
  agent_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_spawns_session ON subagent_spawns(session_id);
CREATE INDEX IF NOT EXISTS idx_spawns_agent   ON subagent_spawns(agent_id);
"""


def ensure_subagents(conn) -> None:
    conn.executescript(SCHEMA)
    if conn.execute("SELECT 1 FROM meta WHERE k=?", (BACKFILL_KEY,)).fetchone():
        return
    conn.execute("UPDATE files SET bytes_read = 0, mtime = 0")  # mtime 0 never matches: full re-read
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, '1')", (BACKFILL_KEY,))
    conn.commit()
