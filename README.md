# Token Dashboard

A local dashboard that reads the JSONL transcripts Claude Code writes to `~/.claude/projects/` and turns them into per-prompt cost analytics, tool/file heatmaps, subagent attribution, cache analytics, project comparisons, and a rule-based tips engine.

**Everything runs locally.** No data leaves your machine — no telemetry, no API calls for your data, no login.

![Overview tab — totals and daily charts](docs/images/dashboard-overview-top.jpg)

![Overview tab — per-project, per-model, top tools, recent sessions](docs/images/dashboard-overview-bottom.jpg)

## What this is useful for

- Seeing which of your prompts are expensive (surprise: they usually involve large tool results).
- Comparing token usage across projects you've worked on.
- Spotting wasteful patterns — the same file read twenty times in a session, a tool call returning 80k tokens.
- Understanding what a "cache hit" actually saves you.
- If you're on Pro or Max, confirming you're getting your money's worth in API-equivalent dollars.

## Prerequisites

- **Python 3.8 or newer** — already installed on macOS and most Linux. On Windows: `winget install Python.Python.3.12` or download from python.org.
- **Claude Code** — installed and with at least one session run. The dashboard reads those sessions. If you just installed Claude Code and haven't used it yet, run at least one prompt first.
- **A web browser.** Any modern one.

No `pip install`. No Node.js. No build step.

## Quickstart

```bash
git clone https://github.com/nateherkai/token-dashboard.git
cd token-dashboard
python3 cli.py dashboard
```

> On Windows, if `python3` isn't on your PATH, substitute `py -3` for `python3` in every command below.

The command:
1. Scans `~/.claude/projects/` (first run can take 20–60 seconds on a heavy user's machine).
2. Starts a local server at http://127.0.0.1:8080.
3. Opens your default browser to that URL.

Leave it running; it re-scans every 30 seconds and pushes updates live. Stop with `Ctrl+C`.

## Where the data comes from

Claude Code writes one JSONL file per session here:

| OS | Path |
|---|---|
| macOS / Linux | `~/.claude/projects/<project-slug>/<session-id>.jsonl` |
| Windows | `C:\Users\<you>\.claude\projects\<project-slug>\<session-id>.jsonl` |

The dashboard never modifies those files — it only reads them and keeps a local SQLite cache at `~/.claude/token-dashboard.db`.

To point at a different location:

```bash
python3 cli.py dashboard --projects-dir /path/to/projects --db /path/to/cache.db
```

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `PORT` | `8080` | Port the local web server listens on |
| `HOST` | `127.0.0.1` | Bind address. Keep the default. Setting `0.0.0.0` exposes your entire prompt history to anyone on your local network — don't do this on any network you don't fully control (no coffee-shop Wi-Fi, no coworking spaces). |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Where to scan for session JSONL files |
| `TOKEN_DASHBOARD_DB` | `~/.claude/token-dashboard.db` | SQLite cache location |

Pricing lives in [`pricing.json`](pricing.json). Edit it directly if model prices change or to add a new plan.

## CLI reference

```bash
python3 cli.py scan          # populate / refresh the local DB, then exit
python3 cli.py today         # today's totals (terminal)
python3 cli.py stats         # all-time totals (terminal)
python3 cli.py tips          # active suggestions (terminal)
python3 cli.py dashboard     # scan + serve the UI at http://localhost:8080

# dashboard flags
python3 cli.py dashboard --no-open   # don't auto-open the browser
python3 cli.py dashboard --no-scan   # skip the initial scan (use cached DB only)
```

Change the port: `PORT=9000 python3 cli.py dashboard`.

## The 7 tabs

The dashboard is a single page with a hash-router tab bar across the top. Each tab is backed by its own JSON API under `/api/`:

- **Overview** — all-time input/output/cache tokens, sessions, turns, estimated cost on your chosen plan, daily work and cache-read charts, tokens-by-project, token share by model, top tools by call count, and recent sessions. This is the landing tab.
- **Prompts** — your most expensive user prompts ranked by tokens. Click any row to see the assistant response, tool calls made, and the size of each tool result.
- **Sessions** — turn-by-turn view of any single session, with per-turn tokens and tool calls.
- **Projects** — token spend per project, with a **By repo | By worktree** toggle (default: by repo). By repo rolls every git worktree of one repository into a single expandable row (`/api/repos`); by worktree is one row per directory Claude Code was started in (`/api/projects`, which also carries `repo`/`repo_name`). Honors the 7d/30d/90d/All tabs. Repo detection reads `.git` on disk and falls back to folder-name heuristics for deleted worktrees; see [limitations](docs/KNOWN_LIMITATIONS.md#repo-grouping-is-resolved-from-paths).
- **Skills** — which skills you invoke most often, and (where we can measure them) their token cost. See [limitations](docs/KNOWN_LIMITATIONS.md#skills-token-counts-are-partial).
- **Cache** — prompt-cache miss analytics (`/api/cache`): hit ratio, miss turns, cold starts, estimated wasted USD, daily read-vs-created tokens, a likely cause per miss (TTL expiry, model switch, prefix change), the worst sessions, and an idle-gap breakdown of cache expiries (how long the thread had been idle, 5m vs 1h expiries, and a simple estimate of what the 1h cache would change). Heuristic; see [limitations](docs/KNOWN_LIMITATIONS.md#cache-miss-analytics-are-heuristic).
- **Costs** — which tool calls and skills were most expensive (estimated: result tokens written to cache once, re-read from cache on every later turn until compaction, and written again when the cache expired after an idle gap; the "expiry" column is that re-write cost, using the same expiry decision as the Cache tab), plus diagnostics such as repeated file reads, huge Bash/Grep output and high error rates. Click a tool to drill into what drives its cost (`#/costs/<tool>`: group-by selector with "By parameter" modes such as command/extension/host and "By dimension" modes valid for every tool: project, repo, day, session (links to the session), hour of day, consuming model and result size; top-15 or per-day bar chart, cost-per-day trend, calls-vs-cost-per-call quadrant chart with "high volume"/"lower volume" x "expensive"/"cheap" labels, sortable table, repeat-call summary). API: `/api/tool-costs`, `/api/skill-costs`, `/api/tool-costs/calls`, `/api/tool-costs/params?tool=<name>&group=<auto|...>&since&until&limit` (response adds a `time.days` series and per-row `label`/`session_id`/`repo` fields where relevant). Click a skill to see what makes it expensive and how often (`#/costs/skill/<name>?range=&group=`: KPIs incl. rank vs the average skill, group-by project / repo / day / session / hour of day / model, average turns the skill stayed in context, daily timeline, same-session reloads, top sessions linking to the session view, diagnostics with suggestions). API: `/api/skill-costs/detail?skill=<name>&group=<project|repo|day|session|hour_of_day|model>&since&until&limit` (400 with an error message for a missing or unknown skill or group). Needs a rescan after upgrading (`tool_use_id` is stored per call). Each row of the "Most expensive calls" table opens a single-call view (`#/costs/call/<id>`): target, cost split into create vs carry, rank and percentile among that tool's calls, a cost-accrual chart over the later turns (marking why carrying stopped: session end, compaction or still ongoing), the other calls in the same parallel batch plus the three calls before/after, repeats of the same tool+target, and the diagnostics that fired with a suggestion. API: `/api/tool-costs/call?id=<tool_calls.id>` (400 for a missing/non-integer id, 404 for an unknown one; each call row of `/api/tool-costs/calls` carries `call_id`).
- **Tips** — rule-based suggestions for reducing token usage (repeated file reads, oversized tool results, low cache-hit rate, etc.).
- **Settings** — switch pricing between API / Pro / Max / Max-20x so cost figures everywhere else reflect your actual plan.

The Overview tab also has a built-in "What do these numbers mean?" panel that explains input/output/cache tokens in plain English.

## Troubleshooting

**"No data" or empty charts.** Run `python3 cli.py scan` once to populate the DB, then reload.

**Port 8080 already in use.** `PORT=9000 python3 cli.py dashboard`.

**Numbers look wrong / stuck.** The DB lives at `~/.claude/token-dashboard.db`. Delete it and re-run `python3 cli.py scan` to rebuild from scratch.

**Running the dashboard twice at the same time.** Don't — both processes will fight over the SQLite DB. Stop all instances before starting a new one.

## Accuracy note

Claude Code writes each assistant response 2–3 times to disk while it streams (the same API message gets snapshotted as output grows). The dashboard dedupes these by `message.id` so the final tally matches what the API actually billed. If you compare against another tool that sums every JSONL row, expect this dashboard's numbers to be lower — and closer to reality.

## Privacy

Nothing leaves your machine. No telemetry. No remote calls for your data. The browser fetches its JSON from `127.0.0.1`, and all JS/CSS/fonts are served from that same local server — ECharts is vendored into `web/`, and the UI falls back to system fonts rather than pulling from a font CDN. If you want to verify: `grep -r "https://" token_dashboard/ web/` — you'll find nothing.

## Tech stack

Python 3 (stdlib only) for the CLI, scanner, and HTTP server. SQLite for the local cache. Vanilla JS + ECharts for the UI, no build step. Dark theme, hash-based router, server-sent events for live refresh.

Data flow: `cli.py` → `token_dashboard/scanner.py` → SQLite DB; `token_dashboard/server.py` exposes `/api/*` JSON routes and serves `web/`.

## Further reading

- [`CLAUDE.md`](CLAUDE.md) — conventions and architecture overview (also picked up automatically by Claude Code)
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to develop and test
- [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) — rough edges
- [`docs/inspiration.md`](docs/inspiration.md) — prior art and how this project diverges

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Short version: fork, `python3 -m unittest discover tests` before opening a PR, keep it stdlib-only.

## License

[MIT](LICENSE).
