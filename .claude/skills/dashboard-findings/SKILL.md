---
name: dashboard-findings
description: Write plain-English findings and recommendations about the user's Claude Code token usage and save them to the Token Dashboard's Tips tab. Use when the user runs /dashboard-findings or asks for an analysis of their usage, cost or subagents from the dashboard.
---

# Dashboard findings

Read the dashboard's local numbers, write 3-5 findings and 1-4 recommendations, and save them so the Tips tab shows them. Everything stays on this machine: the numbers come from the local database and the result goes back into it.

## Steps

1. **Get the facts.** Default window is 7 days; use the user's range if they gave one (`--days 30`). Write to the scratchpad directory, not the repo:
   ```sh
   python3 cli.py findings-data --days 7 > <scratchpad>/facts.json
   ```
   Add `--db <path>` if the user named a database. Run it from the repository root.

2. **Read** `facts.json`. Skim `cost_usd` against `previous_window_cost_usd`, `projects`, `agent_types`, `top_runs`, `top_prompts`, `automated_messages`, `cache`, `activity` and `rule_findings` (what the fixed rules already say: go beyond them, do not repeat them word for word).

3. **Write the findings** as JSON to `<scratchpad>/findings.json`:
   ```json
   {
     "window_days": 7,
     "since": "<facts.since>",
     "findings": [
       {"kind": "bad", "fig": "41%", "subject": "smalt-titan", "text": "used $222 of the week's $980 in just 3 sessions", "link": "#/projects"}
     ],
     "recommendations": [
       {"text": "Scope /weekly-status to fewer parallel agents: it started 7 subagents for 8% of the week.", "link": "#/agents"}
     ]
   }
   ```
   - `kind`: `bad` for waste or anomalies, `good` for a healthy signal, `info` for a neutral fact.
   - `fig`: one short figure, 14 characters at most: a % of the window's cost, a count, or a multiplier like `12×`. Prefer % of cost.
   - `subject`: the project, agent, skill or prompt the sentence is about, 90 characters at most. `text`: the rest of the sentence, one plain-English sentence, 400 characters at most. Name real projects, agents and numbers from the file.
   - `link`: optional in-app link, one of `#/agents`, `#/activity`, `#/prompts`, `#/projects`, `#/cache`, `#/compactions`, `#/costs`, `#/sessions/<session_id>`, `#/prompts?kind=automated`.
   - At most 8 findings and 6 recommendations, each recommendation tied to a specific row.

   Look for: a project, agent type or skill taking a disproportionate share of cost; a single run or prompt over about 10% of the window; subagent runs far above the average; automated messages (idle notices, task results) costing real money; cache hit rate below 85% or cache breaks clustering in one project; cost up sharply against the previous window; many hours of wall-clock time but few active ones (sessions left open). Include one `good` finding when something is healthy.

4. **Be honest about the numbers.** Costs are estimates from the price table. Read `attribution_note`: a prompt is charged with everything that ran after it, so automated messages look worse than the messages themselves are; say "cost X after them" and never "caused". Do not invent figures that are not in the file, and do not quote prompt text beyond a few words (it can be private).

5. **Save** and report:
   ```sh
   python3 cli.py findings-save <scratchpad>/findings.json
   ```
   If it prints an error, fix the file and run it again. Then tell the user the findings are on the Tips tab (reload the page) and give a one-line summary of the top finding. Do not open a browser.

## Notes

- `python3 cli.py findings-clear` removes the saved findings; the Tips tab then shows only the rule-based checks.
- Do not send the facts anywhere else, and do not run other commands against the user's database.
