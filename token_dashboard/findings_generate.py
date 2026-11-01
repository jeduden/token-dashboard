"""Generate findings on demand by asking Claude, through the user's installed `claude` CLI.

The dashboard makes no network call of its own. When the user confirms in the Tips tab, the
facts bundle (see `findings_facts`) is piped to `claude -p` with every tool switched off,
no session saved and a temporary working directory, so the model can only answer in text.
That text is checked by `findings_store` and saved. Nothing runs unless the user asks.

`TOKEN_DASHBOARD_CLAUDE` names the executable (default: `claude` on PATH);
`TOKEN_DASHBOARD_FINDINGS_MODEL` picks a model (default: the CLI's own).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from . import findings_store
from .findings_facts import dumps

PROMPT = Path(__file__).with_name("findings_prompt.md")
TIMEOUT_S = 240
MAX_BUDGET_USD = "1.00"


class Unavailable(RuntimeError):
    """No `claude` executable to run."""


class Failed(RuntimeError):
    """The CLI ran but gave nothing usable."""


def executable() -> Optional[str]:
    return shutil.which(os.environ.get("TOKEN_DASHBOARD_CLAUDE") or "claude")


def build_prompt(facts_json: str) -> str:
    return PROMPT.read_text(encoding="utf-8") + "\nFacts (JSON, estimates from local data):\n" + facts_json + "\n"


def _parse(text: str) -> dict:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        raise Failed("Claude's answer contained no JSON object")
    try:
        return json.loads(text[a:b + 1])
    except ValueError as e:
        raise Failed(f"Claude's answer was not valid JSON: {e}")


def generate(db_path, pricing, days: int = 7, include_prompts: bool = False) -> dict:
    """Run Claude over the facts and store the findings. Returns the saved document."""
    exe = executable()
    if not exe:
        raise Unavailable("the `claude` command was not found; install Claude Code or set TOKEN_DASHBOARD_CLAUDE")
    prompt = build_prompt(dumps(db_path, pricing, days, prompt_text=include_prompts))
    cmd = [exe, "-p", "--output-format", "json", "--no-session-persistence", "--tools", "",
           "--disable-slash-commands", "--max-budget-usd", MAX_BUDGET_USD]
    if os.environ.get("TOKEN_DASHBOARD_FINDINGS_MODEL"):
        cmd += ["--model", os.environ["TOKEN_DASHBOARD_FINDINGS_MODEL"]]
    with tempfile.TemporaryDirectory() as cwd:      # no CLAUDE.md or project settings from the repo
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_S, cwd=cwd)
        except subprocess.TimeoutExpired:
            raise Failed(f"Claude did not answer within {TIMEOUT_S} seconds")
    if r.returncode != 0:
        raise Failed("claude exited with an error: " + (r.stderr.strip() or r.stdout.strip())[:300])
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise Failed("claude printed something other than JSON")
    if not isinstance(out, dict) or out.get("is_error") or not isinstance(out.get("result"), str):
        raise Failed("claude reported an error: " + str((out or {}).get("result") or out)[:300])
    doc = _parse(out["result"])
    models = list((out.get("modelUsage") or {}))
    doc["meta"] = {"model": models[0] if models else "", "prompts_included": include_prompts,
                   **({"cost_usd": round(out["total_cost_usd"], 4)} if isinstance(out.get("total_cost_usd"), (int, float)) else {})}
    try:
        return findings_store.save(db_path, doc)
    except findings_store.Invalid as e:
        raise Failed(f"Claude's findings were rejected: {e}")


class Job:
    """At most one generation at a time; the Tips tab polls `status`."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {"running": False, "started": None, "finished": None, "error": None}

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self, db_path, pricing, days: int, include_prompts: bool) -> bool:
        """False when one is already running."""
        with self._lock:
            if self._state["running"]:
                return False
            self._state = {"running": True, "started": time.time(), "finished": None, "error": None}

        def work():
            err = None
            try:
                generate(db_path, pricing, days, include_prompts)
            except (Unavailable, Failed) as e:
                err = str(e)
            except Exception as e:              # keep the poller informed whatever happens
                err = f"unexpected error: {e}"
            with self._lock:
                self._state.update(running=False, finished=time.time(), error=err)

        threading.Thread(target=work, daemon=True).start()
        return True
