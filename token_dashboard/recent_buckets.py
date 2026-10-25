"""Messages, tokens and cost per day or hour, split by session.

Timestamps in the transcripts are UTC ISO strings, so buckets are UTC like the
other per-day views. Empty buckets between the first and last are filled in so a
chart does not squash a gap of days into adjacent bars.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .db import connect, best_project_name, _range_clause
from .pricer import Pricer, PROMPT_SQL
from .whatif import WhatIf, parse_targets

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


def message_buckets(db_path, pricing, bucket="day", since=None, until=None, sessions=None,
                    targets=None) -> dict:
    """With `targets`, buckets, per-bucket sessions and the session list also carry `alt_costs`."""
    if bucket not in BUCKETS:
        raise ValueError("bucket must be 'day' or 'hour'")
    targets = parse_targets(pricing, targets)
    n = BUCKETS[bucket][0]
    rng, args = _range_clause(since, until)
    sess_sql, sess_args = "", []
    if sessions:
        # placeholder list is built from len(sessions); values are still bound
        sess_sql = " AND session_id IN (%s)" % ",".join("?" * len(sessions))
        sess_args = list(sessions)
    pricer = Pricer(pricing)
    band_sql, band_args = pricer.band_sql(PROMPT_SQL)
    # n and band_sql are internal; thresholds, range and sessions are bound
    sql = f"""
      SELECT substr(timestamp, 1, {int(n)}) AS b, session_id, project_slug, model, speed,
             {band_sql} AS band,
             COUNT(*) AS messages,
             COALESCE(SUM(input_tokens),0)           AS input_tokens,
             COALESCE(SUM(output_tokens),0)          AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)      AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens
        FROM messages
       WHERE type IN ('user', 'assistant') AND timestamp IS NOT NULL {rng} {sess_sql}
       GROUP BY b, session_id, project_slug, model, speed, band
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, (*band_args, *args, *sess_args))]
        names = {}
        for slug in {r["project_slug"] for r in rows}:
            cwds = [x["cwd"] for x in c.execute(
                "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
                (slug,))]
            names[slug] = best_project_name(cwds, slug)

    w = WhatIf(pricer, targets)
    cells, per_session = {}, {}  # (bucket, sid) -> stats ; sid -> totals
    for r in rows:
        tokens = r["input_tokens"] + r["output_tokens"]
        for d, k in ((cells, (r["b"], r["session_id"])), (per_session, r["session_id"])):
            s = d.setdefault(k, {"messages": 0, "tokens": 0})
            s["messages"] += r["messages"]; s["tokens"] += tokens
        if r["model"]:
            for key in (("cell", r["b"], r["session_id"]), ("session", r["session_id"]), ("bucket", r["b"])):
                w.add(r["model"], r, r["band"], r["speed"], key=key)
        ps = per_session[r["session_id"]]
        ps["project_name"] = names[r["project_slug"]]
        ps["first"] = min(ps.get("first", r["b"]), r["b"])
        ps["last"] = max(ps.get("last", r["b"]), r["b"])

    def costs(key, v):
        res = w.result(key)
        v["cost_usd"] = res["cost_usd"]
        if targets:
            v["alt_costs"], v["alt_unpriced_tokens"] = res["alt_costs"], res["alt_unpriced_tokens"]
        return v

    keys = _fill(min(r["b"] for r in rows), max(r["b"] for r in rows), bucket) if rows else []
    out = []
    for k in keys:
        by = {sid: costs(("cell", b, sid), dict(v)) for (b, sid), v in cells.items() if b == k}
        out.append(costs(("bucket", k), {
            "t": k, "messages": sum(v["messages"] for v in by.values()),
            "tokens": sum(v["tokens"] for v in by.values()), "sessions": by}))
    sess = [costs(("session", sid), {"session_id": sid, **v}) for sid, v in per_session.items()]
    sess.sort(key=lambda s: s["cost_usd"], reverse=True)
    return {"bucket": bucket, "targets": targets, "buckets": out, "sessions": sess}
