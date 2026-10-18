"""Pure Bash command-key extraction for the cost drill-down (no shell execution).

``bash_key(target, mode)`` turns a stored Bash command string (capped at 500 chars,
so quotes may be unbalanced) into a grouping key:

* ``command``  - the first real command word (path basename)
* ``command2`` - the first real command plus its next word (``git status``)
* ``pipeline`` - every command in the line joined by its operator (``git log | head``)

"First real command" means after skipping: leading ``cd <path> &&`` / ``;`` (and
``pushd``/``export``/``set``/``unset``) setup commands that are followed by another
command, ``VAR=value`` assignments, wrappers (sudo, env, time, nohup, nice,
timeout N, command, exec) and ``bash|sh|zsh|dash -c '<script>'`` (recursed, depth 3).
``xargs`` is deliberately NOT stripped: it is its own cost pattern and the
interesting command is an argument, not a prefix. Known gaps: no alias/function
resolution, ``for``/``case`` loop bodies are keyed as ``for``/``case``, and
substitutions ``$(...)`` are opaque words. Any failure falls back to the old
behaviour (first non-assignment whitespace word).
"""
from __future__ import annotations

import re

NONE_KEY = "(none)"
MAX_INPUT = 2000          # parse at most this many chars (targets are <= 500 anyway)
MAX_DEPTH = 3
MAX_PIPELINE = 6
SETUP = {"cd", "pushd", "popd", "export", "set", "unset"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
KEYWORDS = {"if", "then", "do", "else", "elif", "while", "until", "!", "{", "time"}
# wrapper -> (options that take an argument, positional args to skip after options)
WRAPPERS = {
    "sudo": ({"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "-r", "-t"}, 0),
    "env": ({"-u", "-C", "-S"}, 0), "time": ({"-f", "-o"}, 0), "nohup": (set(), 0),
    "nice": ({"-n"}, 0), "timeout": ({"-s", "-k"}, 1), "command": (set(), 0), "exec": ({"-a"}, 0),
}
_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_REDIR = re.compile(r"^[0-9]*(?:&>>?|>>?&?|<<<|<)(.*)$", re.S)
_SUBCMD = re.compile(r"^[A-Za-z][\w-]*$")
# tools whose second word is a subcommand (used by the pipeline mode only)
MULTIPLEXERS = {"git", "npm", "npx", "yarn", "pnpm", "gh", "docker", "cargo", "go", "kubectl", "pip",
                "pip3", "brew", "systemctl", "apt", "apt-get", "make", "uv", "poetry", "terraform",
                "helm", "aws", "gcloud", "az", "dotnet", "bundle", "rake", "mvn", "gradle"}
_DASH_C = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")


def _read_quoted(s, i, q):
    """Return (text, next_index) for a quote opened at s[i]; unbalanced -> to the end."""
    n, out, j = len(s), [], i + 1
    while j < n:
        c = s[j]
        if q == '"' and c == "\\" and j + 1 < n:
            out.append(s[j + 1] if s[j + 1] in '"\\$`' else c + s[j + 1])
            j += 2
            continue
        if c == q:
            return "".join(out), j + 1
        out.append(c)
        j += 1
    return "".join(out), n


def _read_subst(s, i):
    """``$(`` at s[i]: raw text through the matching ``)`` (or end of input)."""
    depth, j, n = 0, i + 1, len(s)
    while j < n:
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return s[i:j + 1], j + 1
        j += 1
    return s[i:], n


def _skip_heredocs(s, i, delims):
    """s[i] is just after a newline: skip body lines up to each pending delimiter."""
    n = len(s)
    for d in delims:
        while i < n:
            e = s.find("\n", i)
            e = n if e < 0 else e
            line = s[i:e].strip()
            i = min(e + 1, n)
            if line == d:
                break
    return i


def tokenize(s):
    """Split into ('w', word) / ('op', operator) tokens. Quotes are removed from words."""
    toks, n, i = [], len(s), 0
    buf, has, delims = [], False, []

    def flush():
        nonlocal buf, has
        if has:
            toks.append(("w", "".join(buf)))
        buf, has = [], False

    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 < n and s[i + 1] == "\n":
                i += 2
                continue
            if i + 1 < n:
                buf.append(s[i + 1]); has = True
            i += 2
        elif c in "'\"":
            t, i = _read_quoted(s, i, c)
            buf.append(t); has = True
        elif c == "$" and s[i + 1:i + 2] == "(":
            t, i = _read_subst(s, i)
            buf.append(t); has = True
        elif c == "`":
            e = s.find("`", i + 1)
            e = n if e < 0 else e
            buf.append(s[i:e + 1]); has = True
            i = e + 1
        elif c in " \t\r":
            flush(); i += 1
        elif c == "#" and not has:
            e = s.find("\n", i)
            i = n if e < 0 else e
        elif c == "\n":
            flush(); toks.append(("op", "\n")); i += 1
            if delims:
                i, delims = _skip_heredocs(s, i, delims), []
        elif c == "<" and s[i:i + 2] == "<<" and s[i:i + 3] != "<<<":
            flush(); i += 2
            if s[i:i + 1] == "-":
                i += 1
            while i < n and s[i] in " \t":
                i += 1
            j = i
            while j < n and s[j] not in " \t\r\n;|&<>()":
                j += 1
            delims.append(s[i:j].strip("'\""))
            i = j
        elif c in "<>":
            buf.append(c); has = True; i += 1
        elif c == "&" and (("".join(buf)).endswith((">", "<")) or s[i + 1:i + 2] == ">" and not has):
            buf.append(c); has = True; i += 1
        elif c in ";|&":
            flush()
            two = s[i:i + 2]
            op = two if two in ("&&", "||", ";;") else c
            toks.append(("op", op)); i += len(op)
        elif c in "()":
            flush(); toks.append(("op", c)); i += 1
        else:
            buf.append(c); has = True; i += 1
    flush()
    return toks


def _strip_redirects(words):
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
            continue
        m = _REDIR.match(w)
        if m:
            skip = m.group(1) == ""
            continue
        out.append(w)
    return out


def _unwrap(words):
    """Drop leading assignments, keywords and wrapper commands."""
    w = list(words)
    while w:
        h = w[0]
        if _ASSIGN.match(h) or h in KEYWORDS:
            w = w[1:]
        elif h in WRAPPERS and len(w) > 1:
            takes_arg, positional = WRAPPERS[h]
            if h == "command" and w[1] in ("-v", "-V"):
                break
            j = 1
            while j < len(w) and (w[j].startswith("-") or (h == "env" and _ASSIGN.match(w[j]))):
                j += 2 if w[j] in takes_arg else 1
            j += positional
            if j >= len(w):
                break
            w = w[j:]
        else:
            break
    return w


def _shell_script(w):
    """``bash -lc 'script'`` -> script text, else None."""
    if not w or basename(w[0]) not in SHELLS:
        return None
    for j in range(1, len(w) - 1):
        if _DASH_C.match(w[j]):
            return w[j + 1]
        if not w[j].startswith("-"):
            return None
    return None


def basename(word):
    if "/" in word:
        return word.rstrip("/").rsplit("/", 1)[-1] or word
    return word


def _commands(text, depth):
    """Flatten into [(words, op_after)], recursing into ``sh -c`` scripts."""
    segs, cur = [], []
    for kind, v in tokenize(text):
        if kind == "w":
            cur.append(v)
            continue
        if cur:
            segs.append((cur, v))
            cur = []
        elif segs and v in ("&&", "||", "|") and segs[-1][1] in ("\n", ";", "(", ")"):
            segs[-1] = (segs[-1][0], v)
    if cur:
        segs.append((cur, ""))
    out = []
    for words, op in segs:
        w = _unwrap(_strip_redirects(words))
        script = _shell_script(w) if depth < MAX_DEPTH else None
        inner = _commands(script, depth + 1) if script else []
        if inner:
            inner[-1] = (inner[-1][0], op if op else inner[-1][1])
            out.extend(inner)
        elif w:
            out.append((w, op))
    return out


def _drop_setup(cmds):
    out = []
    for idx, (w, op) in enumerate(cmds):
        if (w[0] in SETUP and op in ("&&", ";", "\n", "(", ")")
                and any(True for _ in cmds[idx + 1:])):
            continue
        out.append((w, op))
    return out or cmds


def _legacy(target):
    w = [x for x in (target or "").split() if not _ASSIGN.match(x)]
    return basename(w[0]) if w else NONE_KEY


def _name(words, two):
    head = basename(words[0])
    if two:
        return head + (" " + words[1] if len(words) > 1 else "")
    return head


def _pipeline(cmds):
    parts = []
    for k, (w, op) in enumerate(cmds[:MAX_PIPELINE]):
        n = basename(w[0])
        if n in MULTIPLEXERS and len(w) > 1 and _SUBCMD.match(w[1]):
            n += " " + w[1]
        parts.append(n)
        if k < min(len(cmds), MAX_PIPELINE) - 1:
            parts.append("|" if op == "|" else ";" if op in ("\n", ";", "(", ")", "") else op)
    return " ".join(parts) + (" ..." if len(cmds) > MAX_PIPELINE else "")


def bash_key(target, mode="command"):
    if not isinstance(target, str) or not target.strip():
        return NONE_KEY
    text = target.replace("\0", " ")[:MAX_INPUT]
    try:
        cmds = _drop_setup(_commands(text, 0))
        if not cmds:
            return _legacy(text) if any(k == "w" for k, _ in tokenize(text)) else NONE_KEY
        if mode == "pipeline":
            return _pipeline(cmds)
        return _name(cmds[0][0], mode == "command2")
    except Exception:
        return _legacy(text)
