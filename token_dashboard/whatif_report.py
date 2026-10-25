"""Cost of the recorded usage, with what-if costs, grouped by day, project, session or model."""
from __future__ import annotations

from .db import connect, best_project_name, _range_clause
from .pricer import Pricer
from .whatif import WhatIf, parse_targets

# group -> SQL expression (internal constants; never built from request input)
GROUPS = {"total": "'all'", "day": "substr(timestamp, 1, 10)", "project": "project_slug",
          "session": "session_id", "model": "model"}
PROMPT = "(input_tokens + cache_read_tokens + cache_create_5m_tokens + cache_create_1h_tokens)"
DEFAULT_LIMIT = 200


def whatif_report(db_path, pricing, group="total", compare="", since=None, until=None,
                  limit=DEFAULT_LIMIT) -> dict:
    if group not in GROUPS:
        raise ValueError("group must be one of " + ", ".join(GROUPS))
    targets = parse_targets(pricing, compare)
    pricer = Pricer(pricing)
    band_sql, band_args = pricer.band_sql(PROMPT)
    rng, rng_args = _range_clause(since, until)
    # `band_sql` and the group expression are internal; thresholds and the range are bound.
    sql = f"""
      SELECT {GROUPS[group]} AS k, model, speed, {band_sql} AS band,
             COALESCE(SUM(input_tokens),0)           AS input_tokens,
             COALESCE(SUM(output_tokens),0)          AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)      AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' AND model IS NOT NULL {rng}
       GROUP BY k, model, speed, band
    """
    w = WhatIf(pricer, targets)
    with connect(db_path) as c:
        for r in c.execute(sql, (*band_args, *rng_args)):
            w.add(r["model"], r, r["band"], r["speed"], key=r["k"])
        rows = []
        for k in w.keys():
            rows.append({"key": k, **w.result(k)})
        if group == "day":
            rows.sort(key=lambda r: r["key"])
        else:
            rows.sort(key=lambda r: r["cost_usd"], reverse=True)
            rows = rows[:max(1, int(limit))]
        if group == "project":
            for r in rows:
                cwds = [x["cwd"] for x in c.execute(
                    "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL", (r["key"],))]
                r["label"] = best_project_name(cwds, r["key"])
    return {"group": group, "targets": targets, "rows": rows, "total": w.result()}
