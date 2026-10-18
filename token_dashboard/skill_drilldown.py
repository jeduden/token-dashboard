"""Per-skill cost drill-down: what makes one skill expensive, and how often.

A Skill invocation has no stored arguments, so its "parameters" are dimensions:
project, repo, day, session, hour of day and the model that re-read it (see
GROUPS). The cost model is ``cost_insights``' (load cost once on the next turn,
then carry cost on every later turn until compaction/session end); this module
only plugs a collector into ``cost_insights._process_thread`` like
``cost_drilldown`` does. ``avg_carry_turns`` (how many later turns the skill
stayed in context) is the key diagnostic: a cheap skill loaded early in a long
session is expensive.

Streaming: one thread in memory at a time. State is one small accumulator per
distinct key (capped at KEY_TRACK_CAP, overflow in OTHER_KEY) and per
session / day of this skill; other skills only contribute a cost total for the
"vs the average skill" comparison. No prompt text is ever read.
Invocations without a linked result or a known size are counted in
``totals.unattributed_invocations`` and not ranked. Hours are UTC. "Repeat" is
per session (all threads of it, including sub-agents).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .db import connect, _range_clause, best_project_name
from . import cost_insights as ci
from .repos import slug_repo_map
from .skill_drill_rules import (skill_rules, SHORT_CARRY_TURNS, LONG_CARRY_TURNS,  # noqa: F401
                                BIG_DEFINITION_TOKENS, RELOAD_MIN)  # re-exported for tests/tuning

KEY_TRACK_CAP = 5_000
OTHER_KEY = "(other: key cap reached)"
CACHE_TTL_SECONDS = 20.0
TOP_SESSIONS = 10
GROUPS = [("project", "project"), ("repo", "repository"), ("day", "day"), ("session", "session"),
          ("hour_of_day", "hour of day (UTC)"), ("model", "model")]
CATALOG_ROOTS = ("skills", "scheduled-tasks", "plugins")

_cache: dict = {}


def _day(ts):
    return ts[:10] if ts else "(unknown)"


_KEY = {
    "project": lambda c: c["project_slug"] or "(unknown)",
    "repo": lambda c: c["project_slug"] or "(unknown)",       # merged by repo afterwards
    "day": lambda c: _day(c["timestamp"]),
    "session": lambda c: c["session_id"],
    "hour_of_day": lambda c: (c["timestamp"][11:13] + ":00") if c["timestamp"] else "(unknown)",
    "model": lambda c: c["model"] or "(unknown)",
}


def resolve_group(group):
    if not group:
        return "project"
    if group not in _KEY:
        raise ValueError("unknown group %r for skills" % group)
    return group


def _acc():
    return {"inv": 0, "usd": 0.0, "carry": 0.0, "expiry": 0.0, "events": 0, "turns": 0, "sessions": set(),
            "first": None, "last": None, "slug": None}


def _add(a, c):
    a["inv"] += 1
    a["usd"] += c["usd"]
    a["carry"] += c["carry_usd"]
    a["expiry"] += c["expiry_usd"]
    a["events"] += c["expiry_events"]
    a["turns"] += c["carry_turns"]
    a["sessions"].add(c["session_id"])
    ts = c["timestamp"]
    if ts:
        a["first"] = ts if a["first"] is None else min(a["first"], ts)
        a["last"] = ts if a["last"] is None else max(a["last"], ts)
    a["slug"] = a["slug"] or c["project_slug"]


def _split():
    return {n: {"invocations": 0, "est_cost_usd": 0.0, "carry_cost_usd": 0.0, "expiry_cost_usd": 0.0}
            for n in ("short", "medium", "long")}


class _Collector:
    """Duck-types the aggregate that ``cost_insights._process_thread`` fills."""

    def __init__(self, skill, mode):
        self.skill, self.fn = skill, _KEY[mode]
        self.rows: dict = {}
        self.truncated = False
        self.sources: set = set()
        self.total_calls = 0
        self.unattributed = 0
        self.big: dict = {}
        self.rereads: list = []
        self.all_usd: dict = {}            # every skill -> [usd, invocations]
        self.n = self.n_attr = 0
        self.load = 0
        self.usd = self.carry = self.expiry = 0.0
        self.events = 0
        self.sessions: set = set()
        self.estimated = False
        self.first = self.last = None
        self.by_session: dict = {}
        self.by_day: dict = {}
        self.split = _split()

    def add(self, c):
        self.total_calls += 1
        if c["tool_name"] != "Skill" or not c["target"]:
            return
        tot = self.all_usd.setdefault(c["target"], [0.0, 0])
        tot[0] += c["usd"]
        tot[1] += 1
        if c["target"] != self.skill:
            return
        self.n += 1
        self.load += c["tokens"]
        self.sessions.add(c["session_id"])
        self.estimated = self.estimated or c["estimated"]
        ts = c["timestamp"]
        if ts:
            self.first = ts if self.first is None else min(self.first, ts)
            self.last = ts if self.last is None else max(self.last, ts)
        day = self.by_day.setdefault(_day(ts), {"inv": 0, "usd": 0.0})
        day["inv"] += 1
        day["usd"] += c["usd"]
        s = self.by_session.get(c["session_id"])
        if s is None:
            s = self.by_session[c["session_id"]] = dict(_acc(), first_usd=c["usd"])
        _add(s, c)
        if not c["attributed"]:
            self.unattributed += 1
            return
        self.n_attr += 1
        self.usd += c["usd"]
        self.carry += c["carry_usd"]
        self.expiry += c["expiry_usd"]
        self.events += c["expiry_events"]
        t = c["carry_turns"]
        b = self.split["short" if t <= SHORT_CARRY_TURNS else "long" if t >= LONG_CARRY_TURNS else "medium"]
        b["invocations"] += 1
        b["est_cost_usd"] += c["usd"]
        b["carry_cost_usd"] += c["carry_usd"]
        b["expiry_cost_usd"] += c["expiry_usd"]
        key = self.fn(c)[:ci.TARGET_MAX_CHARS]
        a = self.rows.get(key)
        if a is None:
            if len(self.rows) >= KEY_TRACK_CAP:
                self.truncated, key = True, OTHER_KEY
                a = self.rows.get(key)
            if a is None:
                a = self.rows[key] = _acc()
        _add(a, c)

    def add_rereads(self, found):
        pass


def _skill_exists(conn, skill):
    return conn.execute("SELECT 1 FROM tool_calls WHERE tool_name = 'Skill' AND target = ? LIMIT 1",
                        (skill,)).fetchone() is not None


def _collect(db_path, pricing, skill, mode, since, until, catalog):
    try:
        st = os.stat(db_path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    key = (str(db_path), skill, mode, since, until, sig, id(pricing), id(catalog))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    col = _Collector(skill, mode)
    memo: dict = {}
    rng, args = _range_clause(since, until, "t.timestamp")
    with connect(db_path) as conn:
        if not _skill_exists(conn, skill):
            raise ValueError("unknown skill %r" % skill)
        threads = [(r[0], r[1]) for r in conn.execute(f"""
            SELECT DISTINCT m.session_id, COALESCE(m.agent_id,'')
              FROM tool_calls t JOIN messages m ON m.uuid = t.message_uuid
             WHERE t.tool_name = 'Skill' {rng}
             ORDER BY m.session_id""", args)]
        for sid, aid in threads:
            ci._process_thread(conn, pricing, memo, catalog, sid, aid, since, until, col)
    _cache.clear()
    _cache[key] = (time.time(), col)
    return col


def _root_category(path):
    """Scanned root name only ('skills' | 'scheduled-tasks' | 'plugins'), never the path."""
    parts = Path(path or "").parts
    if ".claude" in parts:
        i = len(parts) - 1 - parts[::-1].index(".claude")
        if i + 1 < len(parts) and parts[i + 1] in CATALOG_ROOTS:
            return parts[i + 1]
    return "other"


def _catalog_info(entry):
    if not entry:
        return {"installed": False, "root": None, "tokens": None}
    return {"installed": True, "root": _root_category(entry.get("path")), "tokens": entry.get("tokens")}


def _r(x, n=6):
    return round(x, n)


def _row(key, a, total_usd, cum):
    n = a["inv"]
    return {
        "key": key, "invocations": n, "sessions": len(a["sessions"]),
        "est_cost_usd": _r(a["usd"]), "load_cost_usd": _r(a["usd"] - a["carry"] - a["expiry"]),
        "carry_cost_usd": _r(a["carry"]), "expiry_cost_usd": _r(a["expiry"]),
        "expiry_share": round(a["expiry"] / a["usd"], 4) if a["usd"] else 0.0,
        "expiry_events": a["events"], "cost_per_invocation": _r(a["usd"] / n) if n else 0.0,
        "avg_carry_turns": round(a["turns"] / n, 2) if n else 0.0,
        "share": round(a["usd"] / total_usd, 4) if total_usd else 0.0,
        "cumulative_share": round(cum / total_usd, 4) if total_usd else 0.0,
        "first_seen": a["first"], "last_seen": a["last"],
    }


def _merge_repos(conn, rows):
    smap = slug_repo_map(conn, list(rows))
    out = {}
    for slug, a in rows.items():
        info = smap.get(slug) or {"key": "slug:" + slug, "name": slug}
        m = out.get(info["key"])
        if m is None:
            m = out[info["key"]] = dict(_acc(), repo_name=info["name"])
        m["inv"] += a["inv"]; m["usd"] += a["usd"]; m["carry"] += a["carry"]; m["turns"] += a["turns"]
        m["expiry"] += a["expiry"]; m["events"] += a["events"]
        m["sessions"] |= a["sessions"]
        for f, fn in (("first", min), ("last", max)):
            vals = [v for v in (m[f], a[f]) if v]
            m[f] = fn(vals) if vals else None
    return out


def _decorate(conn, rows, slug_of):
    """Add project (and repo) names to output rows; ``slug_of(row)`` gives its project slug."""
    names, smap = {}, {}
    slugs = sorted({slug_of(r) for r in rows if slug_of(r)})
    if slugs:
        smap = slug_repo_map(conn, slugs)
    for r in rows:
        slug = slug_of(r)
        if not slug:
            continue
        if slug not in names:
            cwds = [x["cwd"] for x in conn.execute(
                "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL", (slug,))]
            names[slug] = best_project_name(cwds, slug)
        r["project_slug"], r["project_name"] = slug, names[slug]
        r["repo"], r["repo_name"] = smap[slug]["key"], smap[slug]["name"]


def skill_detail(db_path, pricing, skill, since=None, until=None, limit=50, group="project",
                 catalog=None) -> dict:
    if not skill:
        raise ValueError("skill is required")
    mode = resolve_group(group)
    if catalog is None:
        from .skills import cached_catalog
        catalog = cached_catalog()
    col = _collect(db_path, pricing, skill, mode, since, until, catalog)
    total_usd = col.usd
    accs = col.rows
    names = {}
    with connect(db_path) as conn:
        if mode == "repo":
            accs = _merge_repos(conn, accs)
            names = {k: a["repo_name"] for k, a in accs.items()}
        ranked = sorted(accs.items(), key=lambda kv: (-kv[1]["usd"], -kv[1]["inv"], kv[0]))
        cum, rows = 0.0, []
        for k, a in ranked:
            cum += a["usd"]
            rows.append(_row(k, a, total_usd, cum))
        rows = rows[:limit]
        if mode == "repo":
            for r in rows:
                r["repo"], r["repo_name"] = r["key"], names[r["key"]]
        sess_rank = sorted(col.by_session.items(), key=lambda kv: (-kv[1]["usd"], kv[0]))
        top = [dict(_row(sid, a, total_usd, 0), session_id=sid, carry_turns=a["turns"])
               for sid, a in sess_rank[:TOP_SESSIONS]]
        for t in top:
            for f in ("key", "share", "cumulative_share", "sessions", "first_seen", "last_seen"):
                t.pop(f)
        if mode == "project":
            _decorate(conn, rows, lambda r: r["key"])
        elif mode == "session":
            _decorate(conn, rows, lambda r: col.by_session[r["key"]]["slug"] if r["key"] in col.by_session else None)
        _decorate(conn, top, lambda r: col.by_session[r["session_id"]]["slug"])
    n_all, n_attr = col.n, col.n_attr
    others = col.all_usd
    skills_total = len(others)
    rank = 1 + sum(1 for v in others.values() if v[0] > col.usd + 1e-12)
    not_more = sum(1 for v in others.values() if v[0] <= col.usd + 1e-12)
    all_usd = sum(v[0] for v in others.values())
    all_inv = sum(v[1] for v in others.values())
    totals = {
        "invocations": n_all, "attributed_invocations": n_attr,
        "unattributed_invocations": col.unattributed, "sessions": len(col.sessions),
        "est_load_tokens": round(col.load / n_all) if n_all else 0, "estimated": col.estimated,
        "est_cost_usd": _r(col.usd), "load_cost_usd": _r(col.usd - col.carry - col.expiry),
        "carry_cost_usd": _r(col.carry), "expiry_cost_usd": _r(col.expiry), "expiry_events": col.events,
        "expiry_share": round(col.expiry / col.usd, 4) if col.usd else 0.0,
        "cost_per_invocation": _r(col.usd / n_all) if n_all else 0.0,
        "first_used": col.first, "last_used": col.last,
        "rank": rank, "skills_total": skills_total,
        "cost_percentile": round(not_more / skills_total, 4) if skills_total else 0.0,
        "avg_skill_cost_usd": _r(all_usd / skills_total) if skills_total else 0.0,
        "vs_average": round(col.usd * skills_total / all_usd, 2) if all_usd else None,
    }
    reps = [(sid, a) for sid, a in col.by_session.items() if a["inv"] >= RELOAD_MIN]
    worst = max(reps, key=lambda x: (x[1]["usd"] - x[1]["first_usd"], x[0]), default=None)
    repeat_inv = sum(a["inv"] - 1 for _, a in reps)
    repeat_usd = sum(a["usd"] - a["first_usd"] for _, a in reps)
    repeat = {
        "repeat_invocations": repeat_inv, "sessions_with_repeats": len(reps),
        "repeat_cost_usd": _r(repeat_usd),
        "share_of_invocations": round(repeat_inv / n_all, 4) if n_all else 0.0,
        "share_of_cost": round(repeat_usd / col.usd, 4) if col.usd else 0.0,
        "worst_session": worst and {"session_id": worst[0], "invocations": worst[1]["inv"],
                                    "repeat_cost_usd": _r(worst[1]["usd"] - worst[1]["first_usd"])},
        "carry_split": {k: {**v, "est_cost_usd": _r(v["est_cost_usd"]), "carry_cost_usd": _r(v["carry_cost_usd"]),
                                       "expiry_cost_usd": _r(v["expiry_cost_usd"])}
                        for k, v in col.split.items()},
        "carry_split_note": f"short: <= {SHORT_CARRY_TURNS} later turns (session ended soon); "
                            f"long: >= {LONG_CARRY_TURNS} later turns; expiry = re-write after an idle gap",
    }
    avg_turns = sum(a["turns"] for a in col.by_session.values()) / n_attr if n_attr else 0.0
    cmp = {"avg_cost_per_invocation": all_usd / all_inv if all_inv else 0.0}
    return {
        "skill": skill, "group": mode,
        "available_groups": [{"key": k, "label": lbl} for k, lbl in GROUPS],
        "totals": totals, "avg_carry_turns": round(avg_turns, 2),
        "catalog": _catalog_info(catalog.get(skill)),
        "distinct_keys": len(col.rows), "truncated": col.truncated, "rows": rows,
        "repeat": repeat,
        "invocation_timeline": [{"day": d, "invocations": v["inv"], "est_cost_usd": _r(v["usd"])}
                                for d, v in sorted(col.by_day.items())],
        "top_sessions": top,
        "diagnostics": skill_rules(skill, totals, avg_turns, repeat, cmp, top[0] if top else None),
        "meta": {**ci._meta(col), "key_cap": KEY_TRACK_CAP,
                 "note": "Estimates; hours are UTC; repeats are counted per session."},
    }
