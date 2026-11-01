---
name: dashboard-findings
description: Write plain-English findings and recommendations about the user's Claude Code token usage and save them to the Token Dashboard's Tips tab, from the terminal. The dashboard's own Generate button does the same without a terminal. Use when the user runs /dashboard-findings or asks for an analysis of their usage, cost or subagents.
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

3. **Write the findings.** Read `token_dashboard/findings_prompt.md`: it holds the JSON shape, the limits, what to look for and how to stay honest about the numbers (the Tips tab's Generate button gives the same text to Claude). Follow it, and write the JSON object it describes to `<scratchpad>/findings.json`. Do not quote prompt text beyond a few words: it can be private.

4. **Check the numbers.** Every figure must appear in `facts.json`. Costs are estimates; `attribution_note` explains why automated messages look worse than they are, so say "cost X after them", never "caused".

5. **Save** and report:
   ```sh
   python3 cli.py findings-save <scratchpad>/findings.json
   ```
   If it prints an error, fix the file and run it again. Then tell the user the findings are on the Tips tab (reload the page) and give a one-line summary of the top finding. Do not open a browser.

## Notes

- `python3 cli.py findings-clear` removes the saved findings; the Tips tab then shows only the rule-based checks.
- Do not send the facts anywhere else, and do not run other commands against the user's database.
