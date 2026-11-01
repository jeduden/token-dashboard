"""When you and your agents were working: active hours and parallel sessions.

A session's messages (any thread) are merged into segments: two messages closer than
`IDLE_GAP_S` belong to the same stretch of work; a longer silence ends the segment. Active
time is the sum of segment lengths and wall-clock time is first to last message, so a
session left open overnight counts little. Days are UTC like the other per-day views.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .db import connect, best_project_name, _range_clause

IDLE_GAP_S = 600


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts[:19]).replace(tzinfo=timezone.utc).timestamp()


def _segments(times: list, gap: float) -> list:
    segs, start, prev = [], times[0], times[0]
    for t in times[1:]:
        if t - prev > gap:
            segs.append((start, prev))
            start = t
        prev = t
    segs.append((start, prev))
    return segs


def _peak(segs: list) -> int:
    """Most segments open at once. Segments touching at a point do not overlap."""
    ev = sorted([(a, 1) for a, _ in segs] + [(b, -1) for _, b in segs], key=lambda e: (e[0], e[1]))
    cur = best = 0
    for _, d in ev:
        cur += d
        best = max(best, cur)
    return best


def _by_day(segs: list) -> dict:
    """{YYYY-MM-DD: [(start_min, end_min)]} with segments cut at UTC midnight."""
    out: dict = {}
    for a, b in segs:
        t = a
        while True:
            day0 = t - t % 86400
            end = min(b, day0 + 86400)
            out.setdefault(datetime.fromtimestamp(day0, timezone.utc).strftime("%Y-%m-%d"), []).append(
                (round((t - day0) / 60, 1), round((end - day0) / 60, 1)))
            if end >= b:
                break
            t = day0 + 86400
    return out


def activity(db_path, since=None, until=None, gap_s: int = IDLE_GAP_S) -> dict:
    rng, args = _range_clause(since, until)
    sessions: dict = {}
    with connect(db_path) as c:
        for r in c.execute(f"SELECT session_id, project_slug, timestamp FROM messages "
                           f"WHERE timestamp IS NOT NULL {rng} ORDER BY session_id, timestamp", args):
            s = sessions.setdefault(r["session_id"], {"slug": r["project_slug"], "t": []})
            s["t"].append(_epoch(r["timestamp"]))
        names = {}
        for slug in {s["slug"] for s in sessions.values()}:
            cwds = [x["cwd"] for x in c.execute(
                "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL", (slug,))]
            names[slug] = best_project_name(cwds, slug)

    all_segs, wall, active, days = [], 0.0, 0.0, {}
    for sid, s in sessions.items():
        segs = _segments(s["t"], gap_s)
        all_segs += segs
        wall += s["t"][-1] - s["t"][0]
        active += sum(b - a for a, b in segs)
        for day, spans in _by_day(segs).items():
            d = days.setdefault(day, {"date": day, "sessions": []})
            d["sessions"].append({"session_id": sid, "project_name": names[s["slug"]], "segments": spans,
                                  "active_min": round(sum(b - a for a, b in spans), 1)})
    by_day = []
    for day in sorted(days):
        d = days[day]
        d["sessions"].sort(key=lambda x: x["segments"][0][0])
        d["peak_concurrent"] = _peak([(a, b) for x in d["sessions"] for a, b in x["segments"]])
        d["active_min"] = round(sum(x["active_min"] for x in d["sessions"]), 1)
        by_day.append(d)
    return {
        "idle_gap_min": gap_s // 60,
        "overall": {"sessions": len(sessions), "wall_hours": round(wall / 3600, 1),
                    "active_hours": round(active / 3600, 1),
                    "peak_concurrent": _peak(all_segs) if all_segs else 0},
        "by_day": by_day,
    }
