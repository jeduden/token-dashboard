# Known Limitations

None of these are blockers — the dashboard still gives you useful information. They're the rough edges you'll notice if you look hard.

## Skills token counts are partial, and costs are estimates

The Skills route shows every skill Claude Code invoked, how many times, across how many sessions, and when. The **tokens-per-call** column is populated only for skills whose `SKILL.md` lives under `~/.claude/skills/`, `~/.claude/scheduled-tasks/`, or `~/.claude/plugins/`.

The Costs tab (and `/api/skill-costs`) now attributes cost per skill and per tool call, because each call row stores its `tool_use_id` and links to its result. For skills outside the scanned roots the load size is estimated from the jump in `cache_create` on the following turn and flagged `estimated`. All Costs figures are estimates: tokens are chars/4, results are assumed to be written to cache once and re-read at the cache-read rate on every later turn of the same thread until a detected compaction (cache_read dropping below 50% of the previous turn), and re-written at the cache-write price on every later turn that is a cache TTL expiry (see "Cache expiry in the Costs model" below). Calls scanned before `tool_use_id` existed show as "unattributed" until a rescan; the migration clears and replays all transcripts once.

The per-tool drill-down (`/api/tool-costs/params`) groups calls with heuristics, not by parsing full tool input: Bash by the first real command (a small quote-aware tokenizer skips leading `cd <path> &&`/`;`, `VAR=x`, `sudo`/`env`/`time`/`nohup`/`nice`/`timeout N`/`command`/`exec` and recurses into `bash -c '...'`; path-like words become their basename), by that command plus its next word, or by the whole pipeline (`git log | head`); `xargs`, aliases/functions, loop bodies (`for`/`case` are keyed as such) and `$(...)` substitutions are not resolved, and malformed or truncated commands fall back to the first word; Read/Edit/Write by lower-cased file extension or parent directory; Grep/Glob by the pattern's leading word-token; WebFetch by host. Only the stored target field (first 500 chars; 200 in output) is available, so other parameters (limits, offsets, flags) are invisible. At most 20,000 distinct keys are tracked per request; further keys are merged into an "other" row and `truncated` is set. Calls without a linked result are counted as unattributed and left out of the ranking. A "repeat" is any call whose exact (session, target) pair was already seen earlier in that session (agent threads share the session). Quadrant labels are relative to the tool itself, using cost-weighted thresholds that are returned in `thresholds` and shown in the UI: "expensive" means cost per call at or above the tool's overall average (total cost / attributed calls); "frequent" means calls at or above the 75th percentile of calls among the keys that make up the top 80% of the tool's cost, with at least 3 calls. A plain median over all keys was not used because the long tail of tiny keys drags it to nearly zero and labels every top key alike.

The quadrant wording is "high volume" / "lower volume" (machine keys `frequent_*` / `rare_*` are unchanged for API compatibility): volume is relative to the tool's own heaviest keys, so a key with thousands of calls can still be "lower volume" in a tool with a much heavier key.

Cross-cutting drill-down dimensions (valid for every tool): `project` (key = project slug, label = pretty name), `repo` (worktrees rolled up like the Projects tab, git-based when the checkout still exists, otherwise path heuristics), `day` and `hour_of_day` (UTC, from the stored timestamp; the per-day `time` section is UTC too), `session` (rows are capped by `limit`; label is project name plus the first 8 id characters), `model` and `result_size`. `model` is the model of the turn that consumes the result (the one whose cache rates price it), not the model that issued the call; calls with no following turn have no cost and group under "(unknown)". `result_size` buckets are <500, 500-2k, 2-8k, 8-20k and >=20k result tokens (tokens are chars/4; calls whose result tokens are unknown count as <500). Repo roll-up is resolved at query time and shares the Projects tab's limits (moved or deleted checkouts fall back to the slug). Every grouping sums to the same tool total as `/api/tool-costs`; unattributed calls are excluded from rows and reported in `totals.unattributed_calls`. These modes also apply to the `Skill` tool.

Parallel tool calls are streamed as one JSONL line per `tool_use` block, all sharing a message id. The snapshot dedup keeps every block with a distinct `tool_use_id` in `tool_calls` (and counts tokens once, from the final snapshot). The per-message `tool_calls_json` summary shown in the session view still lists only the final line's blocks, so it can show fewer tools than the Costs tab counts. Databases built before this fix undercount parallel calls until a rescan.

## Cache expiry in the Costs model

Every Costs figure is `create + carry + expiry`. `create` is the result written to cache once at the next turn, priced with the 5m / 1h mix that turn actually wrote. `carry` is the cache-read rate on later turns that found the cache warm. `expiry` (`expiry_cost_usd`, with `expiry_events` and `expiry_gap_total_s`) is the full cache-write price of the result on each later turn that re-wrote the whole prefix after an idle gap, again priced by that turn's 5m / 1h mix; afterwards the result is carried at the read rate again. It is the whole re-write cost, not just the premium over reading. The decision is the Cache tab's (`token_dashboard/cache_events.py`): the gap since the previous turn of the thread exceeded the lifetime of the tier last written (5 min for 5m writes, 1 h for 1h writes; any turn refreshes it) and the turn's usage looks like a miss (at least 50% of the prompt, 2,000+ tokens, was created or uncached) with no model change. Remaining approximations:

- It is a heuristic. Transcripts never say why a cache was missed, so a real expiry whose turn looks warm is not counted, and a prefix change after a long gap is counted as expiry.
- It depends on observed usage: a long gap followed by a turn that still read most of its prompt (for example another client kept the cache warm) is not an expiry.
- It is per thread (session + agent). Sub-agents sharing a parent's cache, and parallel sessions keeping each other's cache warm, are not modelled.
- An expiry turn also zeroes cache reads, so it is not treated as a compaction unless the prompt itself shrank below half of the previous prompt; a compaction that coincides with a long idle gap but does not shrink the prompt is therefore carried on.
- The carried tokens are chars/4 estimates and are priced individually; they are not capped at the tokens that turn really wrote.
- For estimated skill loads (not in a scanned root) the previous prompt size is subtracted from the cache-write jump on an expiry turn, so a long idle gap does not inflate the load size.
- The Cache tab's "what would the 1h cache change?" figure assumes the same turns with every 5m write priced at the 1h rate and every 5m expiry with an idle gap of at most one hour avoided; it ignores second-order effects (later refreshes, changed behaviour) and 1h writes that already happened.

## Cost for Pro / Max / Max-20x users is shown as API-equivalent, not subscription value

The Settings route lets you select your pricing plan, but the Overview cost number is always the API-equivalent (what the same usage would have cost on pay-per-token rates). If you're on Pro you pay a flat $20/month regardless of how much of that API-equivalent number you rack up. We don't do "subscription ROI" math yet — Anthropic doesn't publish per-plan rate limits as public JSON, and faking it would be worse than not doing it.

## Cowork sessions are invisible

If you use Claude's Cowork mode (server-side sessions, not local `claude` CLI), those sessions don't write JSONL to `~/.claude/projects/` and the dashboard can't see them.

## Non-standard model names get tier-fallback pricing

If a transcript references a model ID not in `pricing.json` (e.g. a future snapshot that isn't in our table yet), cost is estimated from the tier substring (`opus` / `sonnet` / `haiku`) in the name. The UI marks these as `estimated: true`. If the model name contains none of those substrings, cost is reported as null.

## First scan can be slow

The first `python3 cli.py scan` on a heavy user's machine can read tens of MB across hundreds of JSONLs. Subsequent scans are incremental (mtime + byte-offset tracking in the `files` table), so they're fast.

## Running two dashboards against the same DB

Both will fight over the SQLite file and you'll see inconsistent numbers and occasional `database is locked` errors. Only run one at a time. If you want to view the dashboard from a second device, use `HOST=0.0.0.0` on the one running machine and point the second device's browser at it.

## Cache-miss analytics are heuristic

The Cache tab (`/api/cache`) infers misses from token counts alone; transcripts never record why a prompt cache was or was not hit. A "miss" is a non-first assistant turn where at least 50% of the prompt (and 2,000+ tokens) was cache-created or uncached input. The first turn of each session / subagent thread is a "cold start", not a miss. Causes come from simple rules (model change, time gap vs. the last 5m/1h cache write, sharp drop in cache reads; shared with the Costs model through `token_dashboard/cache_events.py`) and can be wrong: a "prefix changed" label may really be a TTL expiry we could not see, and subagent threads that actually share a parent's cache are still counted as cold starts. "Estimated wasted $" is the price difference between cache-creating and cache-reading the re-created tokens (capped at the previous turn's prompt size) and uses the same pricing table as the rest of the dashboard, including tier-fallback estimates. Thresholds are named constants at the top of `token_dashboard/cache_events.py`. The `gaps` section buckets the idle gap before TTL-expired turns (5-10 min, 10-30 min, 30-60 min, 1-2 h, 2-6 h, over 6 h; gap = time since the thread's previous usable turn) and splits expiries into 5m and 1h.

## Repo grouping is resolved from paths

The Projects tab's "By repo" view (`/api/repos`) groups worktrees by reading the filesystem at query time (no subprocess, no schema change). Each project's launch directory (the cwd ancestor that encodes to the project slug) is resolved with one of three confidence levels, reported as `repo_confidence`:

- `git`: the directory still exists and a `.git` entry was read. A `.git` directory is a repo root; a `.git` file with `gitdir: <repo>/.git/worktrees/<n>` (or a `commondir` file) is a linked worktree of that repo; a bare repo's worktrees group under the bare directory; a submodule (`gitdir: .../.git/modules/<x>`) is its own repo, never merged into its parent. `repo_root` is set.
- `path-heuristic`: the directory is gone (deleted worktrees are common), so only its path string is used. It is merged into a repo only when the base is proven: Claude Code's `<repo>/.claude/worktrees/<n>`, `<repo>/.worktrees/<n>`, `<repo>-worktrees/<n>` and `<repo>.worktrees/<n>` need the base to be another known project directory or a repo on disk; the sibling style `<repo>-<branch>` needs the base to be a repo that exists on disk. A repo row containing any such member is marked `path-heuristic`.
- `slug-only`: nothing could be proven (no cwd recorded, not a git checkout, unreadable `.git`); the directory stands alone.

Known weaknesses: a deleted sibling directory that merely looks like `<existing-repo>-<something>` is merged into that repo even if it was an unrelated project; a project whose sessions `cd` into other repos is attributed wholly to its launch directory's repo; paths are compared after `normpath` without resolving symlinks, so the same repo reached via two symlinked paths shows up twice; Windows-style paths recorded on another OS can only use the string heuristics. Sessions counts are summed across worktrees, so a session that appears under two project slugs counts twice in the repo row.

## Skill drill-down is an estimate along dimensions, not arguments

`/api/skill-costs/detail` (the `#/costs/skill/<name>` view) explains one skill's cost by project, repository, day, session, hour of day (UTC) and model. Skill invocations store no arguments, so these dimensions are all there is. It reuses the Costs cost model, so every caveat above applies. In addition:

- The load size is the catalog `tokens_per_call` when the skill is installed under a scanned root, else the (flagged `estimated`) cache-write jump on the next turn. The API reports only the root category (`skills`, `scheduled-tasks`, `plugins`, `other`), never the install path.
- "Model" is the model of the first later turn that re-reads the skill; a model switch mid-session is not split. Carry turns stop at the first detected compaction or the end of the thread.
- A "reload" is a second invocation of the same skill in the same session (all threads of it, including sub-agents; sub-agents have their own context, so a reload there is not always wasteful).
- Invocations with no linked result and no known size are counted in `unattributed_invocations` and excluded from rows and rankings. Totals match `/api/skill-costs`; per-group rows cover attributed invocations only.
- "Rank / percentile vs the average skill" compares within the selected date range.
- Rows are capped at 5,000 distinct keys per request (overflow merged into one "other" row, `truncated: true`).

## Single-call drill-down is an estimate on top of an estimate

`/api/tool-costs/call` prices one call with the same model as the rest of the Costs tab (tokens are chars/4; create cost once at the next turn, carry cost at the consuming turn's cache-read rate, expiry cost where the cache expired after an idle gap (timeline entries are `kind` `read` or `rewrite` with `gap_s`), compaction cutoff as a >50% cache-read drop from at least 20,000 tokens), so every limitation above applies. Specifics: the carry-end reason `ongoing` means the thread's last turn is under 30 minutes old (a guess, since transcripts do not say whether a session is finished); `compaction_detected` is inferred from cache-read counts only. Rank and percentile are by estimated cost among the tool's attributed calls over all time (not the selected range); ties share the better rank. Computing them takes one pass over every thread that used the tool: about 1.2 s cold on a synthetic 200k-message database, then cached for 2 minutes (and invalidated when the database file changes). "Previous/next" calls come from the same thread only (a subagent's calls are not interleaved with the main thread's), and calls in the same assistant message are listed as siblings, not as neighbours. "Same target" is an exact match on the stored target (first 500 chars); a call with no target has no repeats. Calls with no linked result are shown with zero cost, no rank and an `unattributed` diagnostic. The stored target is the only parameter text available: no result text exists in the database.
