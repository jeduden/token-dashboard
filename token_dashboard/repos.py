"""Per-repository roll-up of the per-worktree project rows.

Resolution happens at query time from the distinct (project_slug, cwd) pairs;
no schema change and no rescan. Cost is O(distinct slugs), not O(messages).
"""
from __future__ import annotations

from .db import connect, project_summary, project_name_for
from .repo_resolve import resolve_paths, encode_slug

_SUM_FIELDS = ("sessions", "turns", "input_tokens", "output_tokens",
               "billable_tokens", "cache_read_tokens")


def _slug_path(slug: str, cwds) -> str:
    """The directory Claude Code was launched in: the cwd ancestor that encodes
    to the slug, else the first cwd (sessions may cd elsewhere later)."""
    for cwd in sorted(cwds):
        trimmed = cwd.rstrip("/\\")
        sep = "\\" if "\\" in trimmed else "/"
        parts = trimmed.split(sep)
        for i in range(len(parts), 0, -1):
            cand = sep.join(parts[:i])
            if cand and encode_slug(cand) == slug:
                return cand
    return sorted(cwds)[0]


def slug_repo_map(conn, slugs=None) -> dict:
    """{slug: {key, name, root, confidence, cwd}} for every slug (or `slugs`)."""
    by_slug = {}
    for r in conn.execute(
        "SELECT project_slug, cwd FROM messages WHERE cwd IS NOT NULL AND cwd != '' "
        "GROUP BY project_slug, cwd"
    ):
        by_slug.setdefault(r["project_slug"], []).append(r["cwd"])
    paths = {s: _slug_path(s, c) for s, c in by_slug.items()}
    resolved = resolve_paths(paths.values())
    out = {}
    for slug, path in paths.items():
        info = resolved.get(path)
        if info:
            out[slug] = dict(info, cwd=path)
    for slug in (slugs if slugs is not None else ()):
        out.setdefault(slug, _fallback(slug))
    if slugs is not None:
        out = {s: out[s] for s in slugs}
    return out


def _fallback(slug):
    return {"key": "slug:" + (slug or ""), "name": project_name_for(None, slug or ""),
            "root": None, "confidence": "slug-only", "cwd": None}


def repo_for_slugs(db_path, slugs) -> dict:
    """{slug: {repo, repo_name}} -- small helper for other modules."""
    with connect(db_path) as c:
        m = slug_repo_map(c, list(slugs))
    return {s: {"repo": i["key"], "repo_name": i["name"]} for s, i in m.items()}


def annotate_projects(db_path, rows) -> list:
    with connect(db_path) as c:
        m = slug_repo_map(c, [r["project_slug"] for r in rows])
    for r in rows:
        i = m[r["project_slug"]]
        r["repo"], r["repo_name"], r["repo_confidence"] = i["key"], i["name"], i["confidence"]
    return rows


def group_rows(rows, smap) -> list:
    groups = {}
    for r in rows:
        i = smap.get(r["project_slug"]) or _fallback(r["project_slug"])
        g = groups.get(i["key"])
        if g is None:
            g = groups[i["key"]] = {
                "repo": i["key"], "repo_name": i["name"], "repo_root": None,
                "repo_confidence": "slug-only", "_confs": set(),
                **{f: 0 for f in _SUM_FIELDS}, "worktrees": [],
            }
        g["_confs"].add(i["confidence"])
        if g["repo_root"] is None and i["root"]:
            g["repo_root"] = i["root"]
        for f in _SUM_FIELDS:
            g[f] += r.get(f) or 0
        g["worktrees"].append({
            "project_slug": r["project_slug"], "project_name": r.get("project_name"),
            "cwd": i["cwd"], "sessions": r.get("sessions") or 0,
            "billable_tokens": r.get("billable_tokens") or 0, "share": 0.0,
        })
    out = []
    for g in groups.values():
        confs = g.pop("_confs")
        g["repo_confidence"] = ("git" if confs == {"git"} else
                                "path-heuristic" if "path-heuristic" in confs or "git" in confs
                                else "slug-only")
        total = g["billable_tokens"]
        for w in g["worktrees"]:
            w["share"] = (w["billable_tokens"] / total) if total else 0.0
        g["worktrees"].sort(key=lambda w: -w["billable_tokens"])
        g["worktree_count"] = len(g["worktrees"])
        out.append(g)
    out.sort(key=lambda g: -g["billable_tokens"])
    return out


def repo_summary(db_path, since=None, until=None) -> list:
    rows = project_summary(db_path, since, until)
    with connect(db_path) as c:
        smap = slug_repo_map(c, [r["project_slug"] for r in rows])
    return group_rows(rows, smap)
