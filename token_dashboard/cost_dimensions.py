"""Cross-cutting grouping dimensions for the per-tool cost drill-down.

Valid for every tool (unlike the target-derived modes in ``cost_drilldown``):
project, repo, day, session, hour_of_day, model (the *consuming* turn's model,
the one that prices the result) and result_size (buckets of result tokens).

``DimContext`` is created once per request. ``observe`` is called for EVERY call
of a thread in the exact order ``cost_insights._process_thread`` emits them (it
only needs to count them to align the consuming-model list); ``key`` then maps a
tool call to its group key and ``describe`` adds label / link fields for a key.
Days and hours are UTC (taken from the stored ISO timestamps). No text is read.
"""
from __future__ import annotations

from . import cost_insights as ci
from .db import best_project_name
from .repos import slug_repo_map

UNKNOWN = "(unknown)"
# (key, label, exclusive upper bound in result tokens; None = unbounded)
BUCKETS = [("lt_500", "<500 tok", 500), ("500_2k", "500-2k tok", 2000), ("2k_8k", "2-8k tok", 8000),
           ("8k_20k", "8-20k tok", 20000), ("gt_20k", ">20k tok", None)]
DIMS = {
    "project": "project", "repo": "repository", "day": "day", "session": "session",
    "hour_of_day": "hour of day (UTC)", "model": "model (consuming turn)",
    "result_size": "result size",
}
_BUCKET_LABEL = {k: lab for k, lab, _ in BUCKETS}


def result_bucket(tokens) -> str:
    t = tokens or 0
    for key, _, hi in BUCKETS:
        if hi is None or t < hi:
            return key
    return BUCKETS[-1][0]


def day_of(ts) -> str:
    return ts[:10] if ts and len(ts) >= 10 else UNKNOWN


def hour_of(ts) -> str:
    h = ts[11:13] if ts and len(ts) >= 13 else ""
    return h if h.isdigit() else UNKNOWN


class DimContext:
    def __init__(self, conn, mode):
        self.mode = mode
        self.names, self.smap = {}, {}
        self.session_slug: dict = {}
        self._slug = None
        self._models: list = []
        self._i = 0
        if mode in ("project", "repo", "session"):
            cwds: dict = {}
            for r in conn.execute("SELECT project_slug, cwd FROM messages WHERE cwd IS NOT NULL AND cwd != '' "
                                  "GROUP BY project_slug, cwd"):
                cwds.setdefault(r[0], []).append(r[1])
            slugs = [r[0] for r in conn.execute("SELECT DISTINCT project_slug FROM messages")]
            self.names = {s: best_project_name(cwds.get(s), s or "") for s in slugs}
            self.smap = slug_repo_map(conn, slugs)

    def begin_thread(self, conn, sid, aid, since, until):
        if self.mode in ("project", "repo", "session"):
            r = conn.execute("SELECT project_slug FROM messages WHERE session_id=? LIMIT 1", (sid,)).fetchone()
            self._slug = r[0] if r else None
            self.session_slug[sid] = self._slug
        if self.mode == "model":
            self._models, self._i = self._consuming_models(conn, sid, aid, since, until), 0

    @staticmethod
    def _consuming_models(conn, sid, aid, since, until):
        """Model of the turn after each call's own turn, in cost_insights' row order."""
        turns = conn.execute("SELECT uuid, model FROM messages WHERE session_id=? AND COALESCE(agent_id,'')=? "
                             "AND type='assistant' ORDER BY timestamp, rowid", (sid, aid)).fetchall()
        index = {r[0]: i for i, r in enumerate(turns)}
        out = []
        for r in ci._tool_rows(conn, sid, aid):
            ts = r["timestamp"] or ""
            if (since and ts < since) or (until and ts >= until):
                continue
            k = index.get(r["message_uuid"])
            out.append(turns[k + 1][1] if k is not None and k + 1 < len(turns) else None)
        return out

    def observe(self, c):
        if self.mode == "model":
            c["model"] = self._models[self._i] if self._i < len(self._models) else None
            self._i += 1

    def key(self, c) -> str:
        m = self.mode
        if m == "project":
            return self._slug or UNKNOWN
        if m == "repo":
            return self.smap.get(self._slug, {}).get("key") or UNKNOWN
        if m == "session":
            return c["session_id"]
        if m == "day":
            return day_of(c["timestamp"])
        if m == "hour_of_day":
            return hour_of(c["timestamp"])
        if m == "model":
            return c.get("model") or UNKNOWN
        return result_bucket(c["result_tokens"])

    def describe(self, key) -> dict:
        m = self.mode
        if m == "project":
            r = self.smap.get(key) or {}
            return {"label": self.names.get(key, key), "project_slug": key,
                    "repo": r.get("key"), "repo_name": r.get("name")}
        if m == "repo":
            r = next((v for v in self.smap.values() if v["key"] == key), None)
            return {"label": r["name"], "repo": key, "repo_name": r["name"], "repo_root": r["root"]} if r else {}
        if m == "session":
            slug = self.session_slug.get(key)
            name = self.names.get(slug, UNKNOWN)
            return {"label": "%s · %s" % (name, key[:8]), "session_id": key,
                    "project": name, "project_slug": slug}
        if m == "hour_of_day" and key != UNKNOWN:
            return {"label": key + ":00"}
        if m == "result_size":
            return {"label": _BUCKET_LABEL.get(key, key)}
        return {}


class TimeSeries:
    """Per-day calls / cost of one tool, filled in the same pass as the group rows."""

    def __init__(self):
        self.days: dict = {}

    def add(self, c):
        d = self.days.setdefault(day_of(c["timestamp"]), {"calls": 0, "est_cost_usd": 0.0,
                                                          "carry_cost_usd": 0.0, "expiry_cost_usd": 0.0,
                                                          "unattributed_calls": 0})
        if not c["attributed"]:
            d["unattributed_calls"] += 1
            return
        d["calls"] += 1
        d["est_cost_usd"] += c["usd"]
        d["carry_cost_usd"] += c["carry_usd"]
        d["expiry_cost_usd"] += c["expiry_usd"]

    def to_list(self) -> list:
        return [{"day": k, "calls": v["calls"], "est_cost_usd": round(v["est_cost_usd"], 6),
                 "carry_cost_usd": round(v["carry_cost_usd"], 6),
                 "expiry_cost_usd": round(v["expiry_cost_usd"], 6), "unattributed_calls": v["unattributed_calls"]}
                for k, v in sorted(self.days.items())]
