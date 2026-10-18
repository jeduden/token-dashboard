"""JSONL transcript walker + parser."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional, Tuple, Union

from .db import connect
from .compaction_parse import store_compaction


CHECKPOINT_BYTES = 8 * 1024 * 1024

INSERT_MSG = """
INSERT OR REPLACE INTO messages (
  uuid, parent_uuid, session_id, project_slug, cwd, git_branch, cc_version, entrypoint,
  type, is_sidechain, agent_id, timestamp, model, stop_reason, prompt_id, message_id,
  input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
  prompt_text, prompt_chars, tool_calls_json
) VALUES (
  :uuid, :parent_uuid, :session_id, :project_slug, :cwd, :git_branch, :cc_version, :entrypoint,
  :type, :is_sidechain, :agent_id, :timestamp, :model, :stop_reason, :prompt_id, :message_id,
  :input_tokens, :output_tokens, :cache_read_tokens, :cache_create_5m_tokens, :cache_create_1h_tokens,
  :prompt_text, :prompt_chars, :tool_calls_json
)
"""

INSERT_TOOL = """
INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, is_error, timestamp, tool_use_id, result_chars)
VALUES (:message_uuid, :session_id, :project_slug, :tool_name, :target, :result_tokens, :is_error, :timestamp, :tool_use_id, :result_chars)
"""


_TARGET_FIELDS = {
    "Read":      "file_path",
    "Edit":      "file_path",
    "Write":     "file_path",
    "Glob":      "pattern",
    "Grep":      "pattern",
    "Bash":      "command",
    "WebFetch":  "url",
    "WebSearch": "query",
    "Task":      "subagent_type",
    "Skill":     "skill",
}


def _usage(rec: dict) -> dict:
    u = (rec.get("message") or {}).get("usage") or {}
    cc = u.get("cache_creation") or {}
    return {
        "input_tokens":           int(u.get("input_tokens") or 0),
        "output_tokens":          int(u.get("output_tokens") or 0),
        "cache_read_tokens":      int(u.get("cache_read_input_tokens") or 0),
        "cache_create_5m_tokens": int(cc.get("ephemeral_5m_input_tokens") or 0),
        "cache_create_1h_tokens": int(cc.get("ephemeral_1h_input_tokens") or 0),
    }


def _prompt_text(rec: dict) -> Tuple[Optional[str], Optional[int]]:
    if rec.get("type") != "user" or rec.get("isCompactSummary"):
        # The compaction summary is Claude's own text, not a prompt: never stored.
        return None, None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content, len(content)
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = "".join(parts) if parts else None
        return text, (len(text) if text else None)
    return None, None


def _target(name: str, inp: dict) -> Optional[str]:
    field = _TARGET_FIELDS.get(name)
    if field and isinstance(inp, dict):
        v = inp.get(field)
        if isinstance(v, str):
            return v[:500]
    return None


def _extract_tools(rec: dict) -> List[dict]:
    out = []
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name") or "unknown"
        target = _target(name, block.get("input") or {})
        out.append({
            "tool_name":     name,
            "target":        target,
            "result_tokens": None,
            "is_error":      0,
            "timestamp":     rec.get("timestamp"),
            # Links this call to its _tool_result row (same tool_use_id).
            "tool_use_id":   block.get("id") if isinstance(block.get("id"), str) else None,
            "result_chars":  None,
        })
    return out


def _extract_results(rec: dict) -> List[dict]:
    out = []
    content = (rec.get("message") or {}).get("content")
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        body = block.get("content")
        if isinstance(body, str):
            chars = len(body)
        elif isinstance(body, list):
            chars = sum(len(p.get("text", "")) for p in body if isinstance(p, dict))
        else:
            chars = 0
        out.append({
            "tool_name":     "_tool_result",
            "target":        block.get("tool_use_id"),
            "result_tokens": chars // 4,
            "is_error":      1 if block.get("is_error") else 0,
            "timestamp":     rec.get("timestamp"),
            # Size only; the result text itself is never stored (privacy).
            "tool_use_id":   block.get("tool_use_id") if isinstance(block.get("tool_use_id"), str) else None,
            "result_chars":  chars,
        })
    return out


def parse_record(rec: dict, project_slug: str) -> Tuple[dict, List[dict]]:
    """Return (message_row, [tool_call_rows])."""
    msg_obj = rec.get("message") or {}
    text, chars = _prompt_text(rec)
    msg = {
        "uuid":         rec.get("uuid"),
        "parent_uuid":  rec.get("parentUuid"),
        "session_id":   rec.get("sessionId"),
        "project_slug": project_slug,
        "cwd":          rec.get("cwd"),
        "git_branch":   rec.get("gitBranch"),
        "cc_version":   rec.get("version"),
        "entrypoint":   rec.get("entrypoint"),
        "type":         rec.get("type"),
        "is_sidechain": 1 if rec.get("isSidechain") else 0,
        "agent_id":     rec.get("agentId"),
        "timestamp":    rec.get("timestamp"),
        "model":        msg_obj.get("model"),
        "stop_reason":  msg_obj.get("stop_reason"),
        "prompt_id":    rec.get("promptId"),
        "message_id":   msg_obj.get("id"),
        "prompt_text":  text,
        "prompt_chars": chars,
        "tool_calls_json": None,
        **_usage(rec),
    }
    tools = _extract_tools(rec)
    tools.extend(_extract_results(rec))
    if tools:
        msg["tool_calls_json"] = json.dumps(
            [{"name": t["tool_name"], "target": t["target"]} for t in tools if t["tool_name"] != "_tool_result"]
        )
    for t in tools:
        t["message_uuid"] = msg["uuid"]
        t["session_id"]   = msg["session_id"]
        t["project_slug"] = project_slug
    return msg, tools


def _project_slug(file_path: Path, projects_root: Path) -> str:
    rel = file_path.relative_to(projects_root)
    return rel.parts[0]


def _drop_tool_calls(conn, uuids, tool_use_ids) -> None:
    """Delete ``uuids``' tool_calls that a newer record supersedes.

    A row is superseded when the new record carries the same ``tool_use_id``
    (a re-streamed copy of that block) or when it has no id to compare. Rows
    with a different id are sibling blocks of the same response and stay.
    """
    marks = ",".join("?" * len(uuids))
    sql = f"DELETE FROM tool_calls WHERE message_uuid IN ({marks}) AND (tool_use_id IS NULL"
    args = list(uuids)
    if tool_use_ids:
        sql += f" OR tool_use_id IN ({','.join('?' * len(tool_use_ids))})"
        args += tool_use_ids
    conn.execute(sql + ")", args)


def _evict_prior_snapshots(conn, session_id: str, message_id: str, keep_uuid: str,
                           tool_use_ids=()) -> None:
    """Remove older streaming snapshots for the same (session_id, message_id).

    Claude Code writes 2–3 JSONL lines per assistant response (partial → final)
    with identical message.id but distinct top-level uuids. Only the final
    tally matches billing, so earlier snapshots must be replaced, not summed.
    Parallel tool calls are written one content block per line, so a snapshot
    can hold tool_use blocks that no later line repeats: those tool_calls are
    re-parented onto ``keep_uuid`` instead of deleted.
    Uses idx_messages_msgid / idx_tools_msg, so each call is index-only.
    """
    old = [r[0] for r in conn.execute(
        "SELECT uuid FROM messages WHERE session_id=? AND message_id=? AND uuid!=?",
        (session_id, message_id, keep_uuid),
    )]
    if not old:
        return
    placeholders = ",".join("?" * len(old))
    _drop_tool_calls(conn, old, list(tool_use_ids))
    conn.execute(f"UPDATE tool_calls SET message_uuid=? WHERE message_uuid IN ({placeholders})",
                 [keep_uuid, *old])
    conn.execute(f"DELETE FROM messages WHERE uuid IN ({placeholders})", old)


def scan_file(path: Path, project_slug: str, conn, start_byte: int = 0,
              checkpoint=None, checkpoint_bytes: int = CHECKPOINT_BYTES) -> dict:
    """Ingest new lines from a JSONL file starting at ``start_byte``.

    Returns message/tool counts plus ``end_offset`` — the byte offset just
    past the last fully-parsed line. Callers persist ``end_offset`` as the
    file's high-water mark so a line partially flushed at EOF gets re-read
    once it completes.

    ``checkpoint(end_offset)``, if given, is called roughly every
    ``checkpoint_bytes`` of input so the caller can persist the high-water
    mark and commit rows together (resumable on huge files).
    """
    msgs = tools = 0
    end_offset = start_byte
    last_ckpt = start_byte
    with open(path, "rb") as fb:
        if start_byte:
            fb.seek(start_byte)
        while True:
            raw = fb.readline()
            if not raw:
                break  # EOF
            if not raw.endswith(b"\n"):
                # Partial line — Claude Code is mid-flush. Leave the
                # high-water mark behind the line start so we re-read it
                # once the write completes.
                break
            line_end = fb.tell()
            try:
                line = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                end_offset = line_end
                continue
            if not line:
                end_offset = line_end
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                end_offset = line_end
                continue
            if not isinstance(rec, dict) or "uuid" not in rec or "type" not in rec:
                end_offset = line_end
                continue
            msg, tlist = parse_record(rec, project_slug)
            if not msg["session_id"] or not msg["timestamp"]:
                end_offset = line_end
                continue
            if rec.get("type") == "system":
                store_compaction(conn, rec, project_slug)
            call_ids = [t["tool_use_id"] for t in tlist if t.get("tool_use_id")]
            if msg["message_id"]:
                _evict_prior_snapshots(conn, msg["session_id"], msg["message_id"],
                                       msg["uuid"], call_ids)
            conn.execute(INSERT_MSG, msg)
            # tool_calls has no natural unique key; clear this uuid's prior
            # copies of the calls re-inserted below so full rescans stay
            # idempotent. Re-parented sibling blocks (other ids) are kept.
            _drop_tool_calls(conn, [msg["uuid"]], call_ids)
            for t in tlist:
                conn.execute(INSERT_TOOL, t)
                tools += 1
            msgs += 1
            end_offset = line_end
            if checkpoint and end_offset - last_ckpt >= checkpoint_bytes:
                checkpoint(end_offset)
                last_ckpt = end_offset
    return {"messages": msgs, "tools": tools, "end_offset": end_offset}


def _put_file_row(conn, path, mtime, offset) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO files (path, mtime, bytes_read, scanned_at) VALUES (?, ?, ?, ?)",
        (str(path), mtime, offset, time.time()),
    )


def scan_dir(projects_root: Union[str, Path], db_path: Union[str, Path], progress=None) -> dict:
    """Scan every JSONL under ``projects_root``, newest files first.

    Commits once per file (and every CHECKPOINT_BYTES inside huge files), with
    the files-table high-water mark written in the same transaction as the
    rows it covers, so an interrupted scan resumes where it stopped.
    ``progress(files_done, files_total, bytes_done, bytes_total)`` is optional.
    """
    root = Path(projects_root)
    totals = {"messages": 0, "tools": 0, "files": 0}
    if not root.is_dir():
        return totals
    entries = []
    for p in root.rglob("*.jsonl"):
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, p))
    entries.sort(key=lambda e: e[0], reverse=True)
    with connect(db_path) as conn:
        known = {r["path"]: (r["mtime"], r["bytes_read"])
                 for r in conn.execute("SELECT path, mtime, bytes_read FROM files")}
        todo = []
        for mtime, size, p in entries:
            row = known.get(str(p))
            if row and row[0] == mtime and row[1] == size:
                continue
            offset = row[1] if row and size > row[1] else 0
            todo.append((mtime, size, offset, p))
        files_total = len(todo)
        bytes_total = sum(size - off for _, size, off, _ in todo)
        files_done = bytes_done = 0
        if progress:
            progress(0, files_total, 0, bytes_total)
        for mtime, size, offset, p in todo:
            slug = _project_slug(p, root)

            def ckpt(off, p=p, mtime=mtime):
                _put_file_row(conn, p, mtime, off)
                conn.commit()

            try:
                sub = scan_file(p, slug, conn, start_byte=offset, checkpoint=ckpt)
            except OSError:
                conn.rollback()
                files_done += 1
                bytes_done += size - offset
                continue
            # Persist the byte offset of the last fully-parsed line (not
            # st_size) so a partial line mid-flush is retried on the next
            # scan instead of being skipped over.
            _put_file_row(conn, p, mtime, sub["end_offset"])
            conn.commit()
            totals["messages"] += sub["messages"]
            totals["tools"]    += sub["tools"]
            totals["files"]    += 1
            files_done += 1
            bytes_done += size - offset
            if progress:
                progress(files_done, files_total, bytes_done, bytes_total)
    return totals
