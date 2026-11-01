"""Tell a person's prompt from a message another agent or a task sent into the session.

Claude Code records both as `user` rows. Team agents announce themselves going idle
(`idle_notification`), teammates send messages, and finished background tasks report
back (`<task-notification>`); each starts a full-context turn that costs like a prompt.
"""
from __future__ import annotations

from typing import Optional

TEAMMATE = "Another Claude session sent a message"


def classify(text: Optional[str]) -> Optional[str]:
    """None for a person's prompt, else `idle_notification`, `teammate_message` or `task_notification`."""
    if not text:
        return None
    head = text[:600]
    if head.startswith(TEAMMATE):
        return "idle_notification" if '"type":"idle_notification"' in head.replace(" ", "") else "teammate_message"
    if head.lstrip().startswith("<task-notification"):
        return "task_notification"
    return None
