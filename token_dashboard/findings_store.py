"""Findings written by Claude (the /dashboard-findings skill), stored for the Tips tab.

The skill reads `cli.py findings-data`, writes findings, and saves them with
`cli.py findings-save`. This module checks the file before anything is stored: kinds,
lengths and links are bounded, because the text is shown on a page. The dashboard itself
never calls a model; it only shows what the last save contains.
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from .db import connect

KINDS = ("bad", "good", "info")
MAX_FINDINGS, MAX_RECS = 8, 6
LIMITS = {"fig": 14, "subject": 90, "text": 400}
LINK = re.compile(r"^#/[a-z-]+(/[A-Za-z0-9._%-]+)?(\?[A-Za-z0-9=&._%,-]*)?$")
SCHEMA = """
CREATE TABLE IF NOT EXISTS findings_notes (
  id           INTEGER PRIMARY KEY CHECK (id = 1),
  generated_at REAL NOT NULL,
  window_days  INTEGER NOT NULL,
  since        TEXT,
  payload      TEXT NOT NULL
);
"""


class Invalid(ValueError):
    pass


def _text(d: dict, key: str, required: bool = True) -> str:
    v = d.get(key)
    if v is None and not required:
        return ""
    if not isinstance(v, str) or (required and not v.strip()):
        raise Invalid(f"{key} must be a non-empty string")
    if len(v) > LIMITS.get(key, 600):
        raise Invalid(f"{key} is longer than {LIMITS.get(key, 600)} characters")
    return v.strip()


def _link(d: dict) -> str:
    v = d.get("link")
    if not v:
        return ""
    if not isinstance(v, str) or not LINK.match(v):
        raise Invalid(f"link {v!r} is not an in-app link like #/agents")
    return v


def validate(doc) -> dict:
    """The cleaned document, or Invalid with a message saying what to fix."""
    if not isinstance(doc, dict):
        raise Invalid("the file must be a JSON object")
    fs, rs = doc.get("findings"), doc.get("recommendations", [])
    if not isinstance(fs, list) or not fs:
        raise Invalid("findings must be a non-empty list")
    if not isinstance(rs, list) or len(fs) > MAX_FINDINGS or len(rs) > MAX_RECS:
        raise Invalid(f"at most {MAX_FINDINGS} findings and {MAX_RECS} recommendations")
    out_f = []
    for f in fs:
        if not isinstance(f, dict) or f.get("kind") not in KINDS:
            raise Invalid(f"each finding needs a kind of {', '.join(KINDS)}")
        out_f.append({"kind": f["kind"], "fig": _text(f, "fig"), "subject": _text(f, "subject"),
                      "text": _text(f, "text"), "link": _link(f)})
    out_r = []
    for r in rs:
        if not isinstance(r, dict):
            raise Invalid("each recommendation must be an object with text")
        out_r.append({"text": _text(r, "text"), "link": _link(r)})
    days = doc.get("window_days", 7)
    if not isinstance(days, int) or not 1 <= days <= 3650:
        raise Invalid("window_days must be a whole number of days")
    since = doc.get("since")
    return {"window_days": days, "since": since if isinstance(since, str) else None,
            "findings": out_f, "recommendations": out_r}


def save(db_path, doc) -> dict:
    clean = validate(doc)
    with connect(db_path) as c:
        c.executescript(SCHEMA)
        c.execute("INSERT OR REPLACE INTO findings_notes (id, generated_at, window_days, since, payload) VALUES (1,?,?,?,?)",
                  (time.time(), clean["window_days"], clean["since"], json.dumps(clean)))
        c.commit()
    return clean


def load(db_path) -> Optional[dict]:
    with connect(db_path) as c:
        c.executescript(SCHEMA)
        r = c.execute("SELECT generated_at, payload FROM findings_notes WHERE id = 1").fetchone()
    if not r:
        return None
    doc = json.loads(r["payload"])
    doc["generated_at"] = r["generated_at"]
    doc["age_days"] = round((time.time() - r["generated_at"]) / 86400, 1)
    return doc


def clear(db_path) -> None:
    with connect(db_path) as c:
        c.executescript(SCHEMA)
        c.execute("DELETE FROM findings_notes")
        c.commit()
