# Pricing

All costs come from [`pricing.json`](../pricing.json) (schema 2). Nothing is fetched at run time; prices change only when the file changes.

## File layout

- `sources`: one entry per vendor with the official pricing page `url`, `as_of` (date checked), `verified` and a `note`.
- `classes`: the equivalence table (`fable`, `opus`, `sonnet`, `haiku`), naming each vendor's peer model (`null` for none).
- `models`: per model `vendor`, `class`, `source`, `as_of`, the rates in USD per million tokens (`input`, `output`, `cache_read`, `cache_create_5m`, `cache_create_1h`), and optionally `fast` (fast-mode rates), `long_context` (`threshold` and the rates above it), `verified: false` and a `note`.
- `plans`: how cost is displayed per subscription plan.

Model ids match by exact id first (date suffixes and `[1m]` are accepted), then by family prefix.

## Your own prices

Create `~/.claude/token-dashboard.pricing.json` (or point `TOKEN_DASHBOARD_PRICING` at a file) with the same shape, containing only what differs. Same-named models and classes replace the shipped ones; new ones are added. Restart the dashboard afterwards. Settings shows every rate in use.

## Updating the shipped prices

1. Use the vendor's official pricing page only (the `url` in `sources`), not third-party summaries. If a page cannot be read, leave the figure as it is and set `verified: false` with a `note`.
2. Edit the model's rates, and set its `as_of` and `source`. Update the source's `as_of`.
3. For a new model add `vendor`, `class` and the rates, and add it to `classes` if it becomes a peer.
4. Run `python3 -m unittest discover tests`. Tests build their prices from `tests/pricing_fixture.py`, so a price change should not break them; a failing test points at a structural problem.
5. Reload Settings: the table and the equivalent-model grid should show the new figures with no "stale" mark.

A source whose `as_of` is more than 90 days old is reported as stale by `/api/pricing/targets` and in Settings. Prices not confirmed against the vendor's page are shown with ≈.
