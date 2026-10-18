# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project overview

**Token Dashboard** — a local dashboard for Claude Code token usage, costs, and session history, built from the JSONL transcripts Claude Code writes to `~/.claude/projects/`. The feature list lives in `README.md`; the tabs are the routes registered in `web/app.js`.

Inspired by [phuryn/claude-usage](https://github.com/phuryn/claude-usage); see `docs/inspiration.md` for how this project differs.

## Architecture

- `cli.py` → `token_dashboard/scanner.py` → SQLite database (schema in `token_dashboard/db.py`)
- `token_dashboard/server.py` serves the JSON APIs, the SSE stream, and the static frontend in `web/`; its `do_GET` is the list of endpoints
- `web/` is vanilla JS with no build step; routes live in `web/routes/`

## Data source

Claude Code writes one JSONL file per session under `~/.claude/projects/<project-slug>/`. `token_dashboard/scanner.py` documents the record format it relies on and how scanning resumes incrementally.

## Conventions

- **Fully local.** No telemetry, no remote calls for user data. Tests run offline.
- **Stdlib only.** No `pip install`. If a new feature needs a third-party library, argue for it first — we're willing to pay ergonomics cost to keep install friction at zero.
- **SQLite parameter binding always.** Any f-string in a SQL statement must interpolate only internal, caller-controlled values (column names, placeholder lists). User-reachable values go through `?`.
- **Small files with clear responsibilities.** If a file grows past ~400 lines or accretes three distinct concerns, split it.
- **Streaming-snapshot dedup.** When adding scanner logic that joins the `messages` table, remember `(session_id, message_id)` is the dedup key, not `uuid`. See `scanner._evict_prior_snapshots` and the migration note in `db._migrate_add_message_id`.

## Customizing

Environment variables and defaults are documented in `README.md`. Pricing lives in `pricing.json`.

## Known limitations

See `docs/KNOWN_LIMITATIONS.md`.

## Verifying changes

```bash
python3 -m unittest discover tests        # all tests
python3 cli.py dashboard --no-open        # start the server (host/port: see README)
```
