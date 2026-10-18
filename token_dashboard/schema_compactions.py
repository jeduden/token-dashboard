"""``compactions`` / ``meta`` tables and the one-time replay that backfills them.

Databases scanned before ``compactions`` existed hold no compaction events. The
backfill makes the next scan re-read every transcript from byte 0 *without*
deleting anything: ``messages`` / ``tool_calls`` stay queryable during the
replay (which is idempotent, see scanner), unlike the older migrations that
clear them. A marker row in ``meta`` makes it run once.
"""
from __future__ import annotations

BACKFILL_KEY = "compactions_backfill_v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS compactions (
  uuid                       TEXT PRIMARY KEY,
  session_id                 TEXT NOT NULL,
  project_slug               TEXT NOT NULL,
  cwd                        TEXT,
  git_branch                 TEXT,
  timestamp                  TEXT NOT NULL,
  trigger                    TEXT,
  is_sidechain               INTEGER NOT NULL DEFAULT 0,
  agent_id                   TEXT,
  pre_tokens                 INTEGER,
  post_tokens                INTEGER,
  duration_ms                INTEGER,
  dropped_tokens             INTEGER,
  cumulative_dropped_tokens  INTEGER,
  gap_before_s               REAL,
  cc_version                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_compactions_session   ON compactions(session_id);
CREATE INDEX IF NOT EXISTS idx_compactions_timestamp ON compactions(timestamp);
"""


def ensure_compactions(conn) -> None:
    """Create the tables and, once, schedule a full replay of already-scanned files."""
    conn.executescript(SCHEMA)
    if conn.execute("SELECT 1 FROM meta WHERE k=?", (BACKFILL_KEY,)).fetchone():
        return
    # mtime 0 never equals a real mtime, so scan_dir re-reads each file from offset 0.
    conn.execute("UPDATE files SET bytes_read = 0, mtime = 0")
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, '1')", (BACKFILL_KEY,))
    conn.commit()
