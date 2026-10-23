"""Messages, tokens and cost per day or hour, split by session.

Timestamps in the transcripts are UTC ISO strings, so buckets are UTC like the
other per-day views. Empty buckets between the first and last are filled in so a
chart does not squash a gap of days into adjacent bars.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .db import connect, best_project_name, _range_clause
from .pricing import cost_for

# bucket -> (length of the timestamp prefix, step, label format, max buckets)
BUCKETS = {
    "day":  (10, timedelta(days=1),  "%Y-%m-%d",       400),
    "hour": (13, timedelta(hours=1), "%Y-%m-%dT%H",    24 * 31),
}


def _fill(first: str, last: str, bucket: str) -> list:
    _, step, f, cap = BUCKETS[bucket]
    t, end = datetime.strptime(first, f), datetime.strptime(last, f)
    if (end - t) / step >= cap:
        raise ValueError(f"range too long for {bucket} buckets (max {cap}); use a shorter range or day buckets")
    out = []
    while t <= end:
        out.append(t.strftime(f))
        t += step
    return out


def message_buckets(db_path, pricing, bucket="day", since=None, until=None, sessions=None) -> dict:
    if bucket not in BUCKETS:
        raise ValueError("bucket must be 'day' or 'hour'")
    n = BUCKETS[bucket][0]
    rng, args = _range_clause(since, until)
    sess_sql, sess_args = "", []
    if sessions:
        # placeholder list is built from len(sessions); values are still bound
        sess_sql = " AND session_id IN (%s)" % ",".join("?" * len(sessions))
        sess_args = list(sessions)
    sql = f"""
      SELECT substr(timestamp, 1, {int(n)}) AS b, session_id, project_slug, model,
             COUNT(*) AS messages,
             COALESCE(SUM(input_tokens),0)           AS input_tokens,
             COALESCE(SUM(output_tokens),0)          AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)      AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens
        FROM messages
       WHERE type IN ('user', 'assistant') AND timestamp IS NOT NULL {rng} {sess_sql}
       GROUP BY b, session_id, project_slug, model
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, (*args, *sess_args))]
        names = {}
        for slug in {r["project_slug"] for r in rows}:
            cwds = [x["cwd"] for x in c.execute(
                "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
                (slug,))]
            names[slug] = best_project_name(cwds, slug)

    cells, per_session = {}, {}  # (bucket, sid) -> stats ; sid -> totals
    for r in rows:
        usd = cost_for(r["model"], r, pricing)["usd"] if r["model"] else None
        tokens = r["input_tokens"] + r["output_tokens"]
        for d, k in ((cells, (r["b"], r["session_id"])), (per_session, r["session_id"])):
            s = d.setdefault(k, {"messages": 0, "tokens": 0, "cost_usd": 0.0})
            s["messages"] += r["messages"]; s["tokens"] += tokens; s["cost_usd"] += usd or 0
        ps = per_session[r["session_id"]]
        ps["project_name"] = names[r["project_slug"]]
        ps["first"] = min(ps.get("first", r["b"]), r["b"])
        ps["last"] = max(ps.get("last", r["b"]), r["b"])

    keys = _fill(min(r["b"] for r in rows), max(r["b"] for r in rows), bucket) if rows else []
    out = []
    for k in keys:
        by = {sid: {**v, "cost_usd": round(v["cost_usd"], 6)}
              for (b, sid), v in cells.items() if b == k}
        out.append({"t": k, "messages": sum(v["messages"] for v in by.values()),
                    "tokens": sum(v["tokens"] for v in by.values()),
                    "cost_usd": round(sum(v["cost_usd"] for v in by.values()), 6),
                    "sessions": by})
    sess = [{"session_id": sid, **v, "cost_usd": round(v["cost_usd"], 6)} for sid, v in per_session.items()]
    sess.sort(key=lambda s: s["cost_usd"], reverse=True)
    return {"bucket": bucket, "buckets": out, "sessions": sess}
