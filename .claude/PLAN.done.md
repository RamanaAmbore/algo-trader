# Plan: Snapshot/book audit — remaining P2 fixes + coverage uplift

## Task

Deploy the 5 P1/P2 backend fixes + exchange-schedule grid alignment already
completed by agents. Then fix 3 remaining P2 issues and add targeted test
coverage for the snapshot/book/close-price paths identified by the audit.

### What's already done (uncommitted, staged for commit)

| File | Change |
|---|---|
| `exchange_clock.py` | NON-MCX close_time migration (fixes DB no-op) + fail-open for unknown exchange |
| `daily_snapshot.py` | sparkline row missing `lots`/`lot_size`/`previous_close` keys |
| `positions_helpers.py` | closed overnight day_pnl = 0 bug |
| `positions.py` | dead `pairs` set-comprehension removed |
| `settings/+page.svelte` | weekdays column + phantom th + td flex fix |
| `test_exchange_clock.py` | new tests for migration + fail-open |
| `test_sparkline_snapshot.py` | new UPSERT-keys test |
| `test_positions_helpers.py` | new closed-overnight day_pnl test |

### Remaining P2 issues to fix in this plan

**P2-A — `connections.py:1730` blocking sleep in async startup**
`import time as _t; _t.sleep(2)` inside `rebuild_from_db` (an async function
decorated with `@ssot_fetch`) blocks the event loop during Dhan stagger.
Fix: `await asyncio.sleep(2)`.
File: `backend/brokers/connections.py:1730`

**P2-B — `_override_stale_close_for_holdings`: 0.0 written before DB query**
`raw['previous_close'] = 0.0` is set unconditionally (line 420) before the
try/except. On DB failure the function returns early, leaving `previous_close=0.0`
for all rows — the broker live path then sends 0.0 to the frontend as the prior
close, zeroing the Day P&L %.
Fix: defer the `raw['previous_close'] = 0.0` init to AFTER a successful query
so on DB failure the column is either absent (broker will leave it) or retains
the broker-supplied stale BHAV value (better than 0.0).
Specifically: remove line 420, add `raw['previous_close'] = raw.get('previous_close', 0.0)`
ONLY within the success path. Rows with no snapshot entry still get 0.0 in
the loop at line 468.
File: `backend/api/routes/holdings.py:420`

**P2-C — `_is_exchange_open_at`: vestigial `now_ist` parameter**
Parameter is accepted but ignored since the exchange_clock delegation refactor.
Callers that pass `now_ist` for test-time control no longer have that ability —
they must mock `exchange_clock.is_exchange_open` instead. Remove the parameter
to make the interface honest and avoid confusion.
File: `backend/api/algo/daily_snapshot.py` — function `_is_exchange_open_at`
Also update all internal callers.

### Coverage uplift (broker + backend-test agents)

Four uncovered paths — add pytest tests only (no prod code changes):

| Path | Test location |
|---|---|
| `exchange_clock.sessions_with_snapshot_time_now` — time exactly at boundary, time outside window, multiple rows | `backend/tests/test_exchange_clock.py` |
| `exchange_clock.settlement_cutoff_for` — MCX gate, before reset time (yesterday boundary), after reset time | `backend/tests/test_exchange_clock.py` |
| `holdings._overlay_snapshot_for_closed_exchanges` — closed exchange gets snapshot price, open exchange gets live price | `backend/tests/test_holdings_overlay.py` (new) |
| `holdings._override_stale_close_for_holdings` — DB failure path (no crash, previous_close stays absent/0) | `backend/tests/test_holdings_overlay.py` (new) |

## Agents

- backend: fix P2-B (`holdings.py`) and P2-C (`daily_snapshot.py`)
- broker: fix P2-A (`connections.py`)
- backend-test: add coverage for `sessions_with_snapshot_time_now`, `settlement_cutoff_for`, `_overlay_snapshot_for_closed_exchanges`, DB-failure path
- doc: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(snapshot): remaining P2 — async sleep, holdings 0.0 prev_close on DB fail, vestigial now_ist; coverage uplift for clock/overlay paths

## Done when

- All pytest pass (broker cov ≥ 80%, api cov ≥ 45%)
- `connections.py` Dhan stagger uses `await asyncio.sleep(2)`
- `_override_stale_close_for_holdings` no longer writes `previous_close=0.0` before the DB round-trip
- `_is_exchange_open_at` signature has no `now_ist` parameter
- `sessions_with_snapshot_time_now`, `settlement_cutoff_for`, overlay, and DB-failure paths have explicit test coverage
