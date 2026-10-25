"""``messages.speed``: `fast` / `standard` as reported in each turn's usage.

Fast mode (Opus 5.5 / 5 / 4.8) is billed at a premium, and the transcripts say
which turns used it. Databases scanned before the column existed hold NULL, which
is priced as standard; a one-time replay re-reads the transcripts still on disk.
"""
from __future__ import annotations

BACKFILL_KEY = "speed_backfill_v1"


def ensure_speed(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if "speed" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN speed TEXT")
    if conn.execute("SELECT 1 FROM meta WHERE k=?", (BACKFILL_KEY,)).fetchone():
        return
    conn.execute("UPDATE files SET bytes_read = 0, mtime = 0")  # mtime 0 never matches: full re-read
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, '1')", (BACKFILL_KEY,))
    conn.commit()
