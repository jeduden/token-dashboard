"""Resolve working-directory paths to a repository identity.

No subprocess, no network. Two evidence levels:

* "git"            -- the path exists on disk and a `.git` entry was read
                      (directory, or a `gitdir:` file for linked worktrees,
                      submodules and bare-repo worktrees).
* "path-heuristic" -- the path is gone (deleted worktrees are common) but its
                      string matches a worktree layout whose base repo is
                      proven by another known path or by a repo on disk.
* "slug-only"      -- nothing better; the directory stands for itself.

A false merge of unrelated repos is worse than not merging, so heuristics
only fire when the base is proven.
"""
from __future__ import annotations

import os
import posixpath
import re
from typing import Dict, Iterable, Optional

MAX_DEPTH = 64
_WIN_RE = re.compile(r"^[A-Za-z]:[\\/]|\\")
_WT_SUFFIXES = ("-worktrees", ".worktrees")


def encode_slug(path: str) -> str:
    """Claude Code's project-slug encoding: every non-alphanumeric char -> `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


class _Stop(Exception):
    """A `.git` entry exists but cannot be trusted/read; do not climb further."""


def _is_win(p: str) -> bool:
    return bool(_WIN_RE.search(p))


def _split(p: str):
    """-> (sep, parts) for a posix or Windows-style path, trailing seps dropped."""
    if _is_win(p):
        parts = [x for x in p.replace("/", "\\").split("\\")]
        while len(parts) > 1 and parts[-1] == "":
            parts.pop()
        return "\\", parts
    n = posixpath.normpath(p)
    if n == "/":
        return "/", [""]
    return "/", n.split("/")


def _join(sep: str, parts) -> str:
    return sep.join(parts)


def _key(sep: str, path: str) -> str:
    return path.lower() if sep == "\\" else path


def _base_name(p: str) -> str:
    sep, parts = _split(p)
    return parts[-1] if parts else p


def _clean_name(name: str) -> str:
    return name[:-4] if name.endswith(".git") and len(name) > 4 else name


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(4096).decode("utf-8", "replace")
    except OSError as e:
        raise _Stop(str(e))


def _root_from_gitfile(d: str, gfile: str) -> str:
    text = _read(gfile)
    m = re.match(r"\s*gitdir:\s*(.+?)\s*$", text.split("\n", 1)[0])
    if not m or "\x00" in m.group(1):
        raise _Stop("not a gitdir file")
    gd = m.group(1)
    gd = posixpath.normpath(gd if os.path.isabs(gd) else os.path.join(d, gd))
    common = None
    try:
        with open(os.path.join(gd, "commondir"), "rb") as f:
            c = f.read(1024).decode("utf-8", "replace").strip()
        if c:
            common = os.path.normpath(os.path.join(gd, c))
    except OSError:
        pass
    if common is None and os.path.basename(os.path.dirname(gd)) == "worktrees":
        common = os.path.dirname(os.path.dirname(gd))
    if common is None:
        return d  # submodule / separate-git-dir checkout: its own repo
    return os.path.dirname(common) if os.path.basename(common) == ".git" else common


def _is_bare(d: str) -> bool:
    return (os.path.isfile(os.path.join(d, "HEAD")) and os.path.isdir(os.path.join(d, "objects"))
            and os.path.isdir(os.path.join(d, "refs")))


def _git_lookup(start: str, exact: bool = False) -> Optional[str]:
    """Repo root for `start` (or the nearest ancestor), or None. Never raises."""
    try:
        d = os.path.normpath(start)
        for _ in range(MAX_DEPTH):
            g = os.path.join(d, ".git")
            if os.path.isdir(g):
                return d
            if os.path.isfile(g):
                return _root_from_gitfile(d, g)
            if _is_bare(d):
                return os.path.dirname(d) if os.path.basename(d) == ".git" else d
            parent = os.path.dirname(d)
            if exact or parent == d:
                return None
            d = parent
    except (_Stop, OSError, ValueError):
        return None
    return None


def _exists(p: str) -> bool:
    try:
        return os.path.isdir(p)
    except (OSError, ValueError):
        return False


def _first_existing_ancestor(p: str) -> Optional[str]:
    d = p
    for _ in range(MAX_DEPTH):
        if _exists(d):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def _info(sep, path, confidence, root=None, name=None):
    return {"key": _key(sep, path), "name": name or _clean_name(_base_name(path)),
            "root": root, "confidence": confidence}


def _marker_bases(sep, parts):
    """Candidate base-repo paths implied by a worktree directory layout."""
    out = []
    for i, seg in enumerate(parts):
        if i >= 1 and seg == "worktrees" and parts[i - 1] == ".claude" and i >= 2 and len(parts) > i + 1:
            out.append(parts[:i - 1])
        elif i >= 1 and seg == ".worktrees" and len(parts) > i + 1:
            out.append(parts[:i])
        elif i >= 1:
            for suf in _WT_SUFFIXES:
                if seg.endswith(suf) and len(seg) > len(suf) and len(parts) > i + 1:
                    out.append(parts[:i] + [seg[:-len(suf)]])
    return [_join(sep, b) for b in out if len(b) >= 2]


def _sibling_bases(sep, parts):
    """`<repo>-<branch>` siblings: candidate bases, longest prefix first."""
    out = []
    for j in range(len(parts), 1, -1):
        name = parts[j - 1]
        for m in reversed([m.start() for m in re.finditer("-", name)]):
            if m > 0:
                out.append(_join(sep, parts[:j - 1] + [name[:m]]))
    return out


def resolve_paths(paths: Iterable[Optional[str]]) -> Dict[str, dict]:
    """Map each usable path to {key, name, root, confidence}.

    `root` is set only for git-confirmed resolution. Unusable inputs
    (None/empty/blank) are omitted.
    """
    uniq = []
    for p in paths:
        if isinstance(p, str) and p.strip() and p not in uniq:
            uniq.append(p)
    results: Dict[str, dict] = {}
    git_roots: Dict[str, str] = {}   # key -> root
    pending = []
    for p in uniq:
        sep, parts = _split(p)
        norm = _join(sep, parts)
        root = None
        if not (sep == "\\" and os.name != "nt") and _exists(norm):
            root = _git_lookup(norm)
        if root:
            root = os.path.normpath(root)
            git_roots[root] = root
            results[p] = _info("/", root, "git", root=root)
        else:
            pending.append((p, sep, parts, norm))
    known = {_key(sep, norm) for _, sep, _, norm in pending} | set(git_roots)
    exact_cache: Dict[str, bool] = {}

    def proven(sep, base, strict):
        k = _key(sep, base)
        if k in git_roots:
            return True
        if not strict and k in known:
            return True
        if sep == "\\" and os.name != "nt":
            return False
        if base not in exact_cache:
            r = _git_lookup(base, exact=True) if _exists(base) else None
            exact_cache[base] = bool(r) and os.path.normpath(r) == os.path.normpath(base)
        return exact_cache[base]

    for p, sep, parts, norm in pending:
        info = None
        if len(parts) <= MAX_DEPTH and not _exists(norm):
            cands = [(b, False) for b in _marker_bases(sep, parts)]
            cands += [(b, True) for b in _sibling_bases(sep, parts)]
            for base, strict in cands:
                if _key(sep, base) != _key(sep, norm) and proven(sep, base, strict):
                    info = _info(sep, base, "path-heuristic")
                    break
            if info is None and not (sep == "\\" and os.name != "nt"):
                anc = _first_existing_ancestor(norm)
                root = _git_lookup(anc) if anc else None
                if root:
                    info = _info("/", os.path.normpath(root), "path-heuristic")
        results[p] = info or _info(sep, norm, "slug-only")
    return results
