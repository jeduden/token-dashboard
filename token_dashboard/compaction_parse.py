"""Compaction events: ``compact_boundary`` records -> rows of the ``compactions`` table.

Claude Code writes one ``type:"system", subtype:"compact_boundary"`` record per
compaction (auto or manual), followed by a user record flagged
``isCompactSummary`` that carries the summary text. Privacy: only numbers, the
trigger and ids are kept. The boundary's ``content``, the summary text and the
``preCompactDiscoveredTools`` list are never read into a row.

Fields differ between Claude Code versions, so every metadata value is
optional and validated; a missing one is stored as NULL.
"""
from __future__ import annotations

from typing import Optional

from .cache_events import epoch

TRIGGERS = ("auto", "manual")

INSERT_COMPACTION = """
INSERT OR REPLACE INTO compactions (
  uuid, session_id, project_slug, cwd, git_branch, timestamp, trigger, is_sidechain,
  agent_id, pre_tokens, post_tokens, duration_ms, dropped_tokens,
  cumulative_dropped_tokens, gap_before_s, cc_version
) VALUES (
  :uuid, :session_id, :project_slug, :cwd, :git_branch, :timestamp, :trigger, :is_sidechain,
  :agent_id, :pre_tokens, :post_tokens, :duration_ms, :dropped_tokens,
  :cumulative_dropped_tokens, :gap_before_s, :cc_version
)
"""


def _count(v) -> Optional[int]:
    """A non-negative integer count, else None (bools and strings are rejected)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        return None
    return int(v)


def _text(v) -> Optional[str]:
    return v if isinstance(v, str) and v else None


def parse_compaction(rec: dict, project_slug: str) -> Optional[dict]:
    """Row for a ``compact_boundary`` record, or None for anything else."""
    if rec.get("type") != "system" or rec.get("subtype") != "compact_boundary":
        return None
    uuid, sid, ts = rec.get("uuid"), rec.get("sessionId"), rec.get("timestamp")
    if not (isinstance(uuid, str) and uuid and isinstance(sid, str) and sid and isinstance(ts, str) and ts):
        return None
    md = rec.get("compactMetadata")
    if not isinstance(md, dict):
        md = {}
    pre, post = _count(md.get("preTokens")), _count(md.get("postTokens"))
    trigger = md.get("trigger")
    agent = _text(rec.get("agentId"))
    return {
        "uuid": uuid, "session_id": sid, "project_slug": project_slug,
        "cwd": _text(rec.get("cwd")), "git_branch": _text(rec.get("gitBranch")),
        "timestamp": ts,
        "trigger": trigger if trigger in TRIGGERS else None,
        "is_sidechain": 1 if (rec.get("isSidechain") or agent) else 0,
        "agent_id": agent,
        "pre_tokens": pre, "post_tokens": post,
        "duration_ms": _count(md.get("durationMs")),
        # cumulativeDroppedTokens runs over the whole session, so the per-event drop is pre - post.
        "dropped_tokens": (max(0, pre - post) if pre is not None and post is not None else None),
        "cumulative_dropped_tokens": _count(md.get("cumulativeDroppedTokens")),
        "gap_before_s": None,
        "cc_version": _text(rec.get("version")),
    }


def idle_gap_before(conn, row: dict) -> Optional[float]:
    """Seconds the thread sat idle before the compaction, or None without a prior assistant turn.

    Measured from the thread's last assistant turn to the next user record (the user coming
    back, or the tool result of a mid-turn auto-compaction), or to the boundary itself when no
    user record intervenes. The compact-summary record and the boundary come later, so they
    never count. Uses idx_messages_session.
    """
    sid, aid, ts = row["session_id"], row["agent_id"] or "", row["timestamp"]
    last = conn.execute(
        "SELECT MAX(timestamp) FROM messages WHERE session_id=? AND COALESCE(agent_id,'')=? "
        "AND type='assistant' AND timestamp < ?", (sid, aid, ts)).fetchone()[0]
    if last is None:
        return None
    back = conn.execute(
        "SELECT MIN(timestamp) FROM messages WHERE session_id=? AND COALESCE(agent_id,'')=? "
        "AND type='user' AND timestamp > ? AND timestamp <= ?", (sid, aid, last, ts)).fetchone()[0]
    a, b = epoch(last), epoch(back or ts)
    return None if a is None or b is None else max(0.0, round(b - a, 3))


def store_compaction(conn, rec: dict, project_slug: str) -> bool:
    """Insert (or replace, by uuid) the row for ``rec``; False when it is not a compaction."""
    row = parse_compaction(rec, project_slug)
    if row is None:
        return False
    row["gap_before_s"] = idle_gap_before(conn, row)
    conn.execute(INSERT_COMPACTION, row)
    return True
