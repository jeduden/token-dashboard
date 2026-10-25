"""Pricing-file schema 2: validation, `null` bucket filling, and the local override.

A model entry carries the five bucket rates ($ per million tokens). A bucket set to
`null` means the vendor has no such charge; it is billed at the `input` rate and
listed under the entry's `derived` so the UI can say so. Schema-1 files (no
`schema` key) are accepted unchanged.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Optional, Union

BUCKETS = ("input", "output", "cache_read", "cache_create_5m", "cache_create_1h")
SECTIONS = ("models", "tier_fallback", "plans", "classes", "sources")
OVERRIDE_ENV = "TOKEN_DASHBOARD_PRICING"


def default_override_path() -> Path:
    return Path(os.environ.get(OVERRIDE_ENV) or Path.home() / ".claude" / "token-dashboard.pricing.json")


def _rate_set(where: str, e: dict) -> None:
    """Fill null buckets with the input rate (recording them) and check the rest."""
    if not isinstance(e.get("input"), (int, float)):
        raise ValueError(f"{where}: input rate is required")
    derived = []
    for b in BUCKETS:
        if e.get(b) is None:
            if b == "output":
                raise ValueError(f"{where}: output rate is required")
            e[b] = e["input"]
            derived.append(b)
        elif not isinstance(e[b], (int, float)) or e[b] < 0:
            raise ValueError(f"{where}: {b} must be a non-negative number")
    if derived:
        e["derived"] = derived


def normalize(pricing: dict) -> dict:
    """Return a validated copy with null buckets filled. Raises ValueError."""
    p = copy.deepcopy(pricing)
    strict = int(p.get("schema") or 1) >= 2
    for name, e in p.get("models", {}).items():
        _rate_set(f"models.{name}", e)
        if isinstance(e.get("fast"), dict):
            _rate_set(f"models.{name}.fast", e["fast"])
        lc = e.get("long_context")
        if isinstance(lc, dict):
            if not isinstance(lc.get("above_prompt_tokens"), int):
                raise ValueError(f"models.{name}.long_context: above_prompt_tokens is required")
            _rate_set(f"models.{name}.long_context", lc)
        if strict:
            for k in ("vendor", "class", "source", "as_of"):
                if not e.get(k):
                    raise ValueError(f"models.{name}: missing {k}")
            if e["source"] not in p.get("sources", {}):
                raise ValueError(f"models.{name}: unknown source {e['source']!r}")
    for name, e in p.get("tier_fallback", {}).items():
        _rate_set(f"tier_fallback.{name}", e)
    for cls, members in p.get("classes", {}).items():
        for vendor, model in members.items():
            if vendor != "label" and model is not None and model not in p["models"]:
                raise ValueError(f"classes.{cls}.{vendor}: unknown model {model!r}")
    return p


def merge_override(base: dict, override: dict) -> dict:
    """Entry-level merge: an override entry replaces the base entry of the same key."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if k in SECTIONS and isinstance(v, dict):
            out.setdefault(k, {}).update(copy.deepcopy(v))
        elif k not in SECTIONS:
            out[k] = copy.deepcopy(v)
    return out


def load_with_override(path: Union[str, Path], override: Optional[Union[str, Path]] = None) -> dict:
    """Load and validate `path`, then layer the override file over it when it exists.

    A missing override is normal. An unreadable or invalid one is reported on stderr
    and ignored, so a typo never takes the dashboard down.
    """
    base = normalize(json.loads(Path(path).read_text(encoding="utf-8")))
    ov = Path(override) if override else default_override_path()
    if not ov.is_file():
        return base
    try:
        return normalize(merge_override(base, json.loads(ov.read_text(encoding="utf-8"))))
    except (OSError, ValueError) as e:
        print(f"token-dashboard: ignoring pricing override {ov}: {e}", file=sys.stderr)
        return base
