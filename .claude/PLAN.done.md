# Plan: LogPanel zerodha label + header book-refresh time + closed-hours adaptive poller

## Context
Three related issues:
1. Conn tab shows `zerodha_kite` (internal ID) — user wants `zerodha`. `.lp-conn-acct` wastes whitespace at `min-width: 5rem`.
2. Page header (`AlgoTimestamp`) shows the MarketPulse LTP-update time — user wants it to show the last book (holdings/positions) fetch time instead.
3. Adaptive book-poller doesn't switch to 30 min during closed hours when no daily_book snapshot exists yet: `_snapshot_fn()` returns `HoldingsResponse` without `as_of`, causing the gate to fall through to the broker path and the frontend to keep `_holdingsSnapshotAt = null`, staying at 5 s cadence.

## Task

**Fix 1 — zerodha display label + account column**
`frontend/src/lib/LogPanel.svelte` line ~1622: remap `zerodha_kite` → `zerodha` for display.
`frontend/src/lib/LogPanel.svelte` line ~2368: `.lp-conn-acct` `min-width: 5rem` → `3.5rem`.

**Fix 2 — Page header shows book refresh time**
`frontend/src/lib/data/marketDataStores.svelte.js`:
- Line 43: add `lastRefreshAt` to import from `$lib/stores`
- In `_tickBookPollers()` (line ~730), after `_bookPollerTick++`, add: `lastRefreshAt.set(Date.now())`

`frontend/src/lib/MarketPulse.svelte` — remove the three `lastRefreshAt.set(pulseLastUpdate)` calls at lines 2626, 2717, 2730. The book poller covers all pages via the layout so the header stays accurate. Manual RefreshButton clicks still set `lastRefreshAt` (those calls stay).

**Fix 3 — Reliable closed-hours `as_of` signal**
`backend/api/routes/holdings.py` `_snapshot_fn()` lines 553-556:
Change:
```python
if snap is None:
    return HoldingsResponse(rows=[], summary=[],
                            refreshed_at=timestamp_display())
```
To:
```python
if snap is None:
    return HoldingsResponse(rows=[], summary=[],
                            refreshed_at=timestamp_display(),
                            as_of=timestamp_display())
```
`_snapshot_fn` is only called by the gate when market is closed, so adding `as_of` here is semantically correct. This ensures `_holdingsSnapshotAt` is non-null whenever the gate routes to the snapshot path, even if the DB has no snapshot yet.

## Files to modify
- `frontend/src/lib/LogPanel.svelte` — line ~1622 (template) + line ~2368 (CSS)
- `frontend/src/lib/data/marketDataStores.svelte.js` — line 43 import + line ~730 lastRefreshAt set
- `frontend/src/lib/MarketPulse.svelte` — remove 3× `lastRefreshAt.set(pulseLastUpdate)` at lines 2626, 2717, 2730
- `backend/api/routes/holdings.py` — line ~554 add `as_of=timestamp_display()`

## Agents
- backend: In `backend/api/routes/holdings.py`, inside `_snapshot_fn()` (around line 553), change `HoldingsResponse(rows=[], summary=[], refreshed_at=timestamp_display())` to `HoldingsResponse(rows=[], summary=[], refreshed_at=timestamp_display(), as_of=timestamp_display())`. This ensures the frontend adaptive poller sees a non-null `as_of` whenever the gate routes to the snapshot path, even if no daily_book snapshot exists yet.
- frontend: Three sub-tasks in one agent pass:
  (A) `frontend/src/lib/LogPanel.svelte` line ~1622: change `{ev.broker_id || '—'}` to `{ev.broker_id === 'zerodha_kite' ? 'zerodha' : (ev.broker_id || '—')}`. Line ~2368: `.lp-conn-acct` `min-width: 5rem` → `min-width: 3.5rem`.
  (B) `frontend/src/lib/data/marketDataStores.svelte.js` line 43: change `import { marketAwareInterval, visibleInterval } from '$lib/stores';` to `import { marketAwareInterval, visibleInterval, lastRefreshAt } from '$lib/stores';`. Then in `_tickBookPollers()` after `_bookPollerTick++` (line ~730), add `lastRefreshAt.set(Date.now());`.
  (C) `frontend/src/lib/MarketPulse.svelte` lines 2626, 2717, 2730: remove `lastRefreshAt.set(pulseLastUpdate);` at each of the three sites. Keep all surrounding code unchanged.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): zerodha label, header book-refresh time, closed-hours adaptive poller

## Done when
- Conn tab shows "zerodha" not "zerodha_kite"
- Account and broker columns closer (no excess gap)
- Page header refresh time updates on book poll cadence (5 s open, 30 min closed), not on LTP ticks
- Holdings API always returns `as_of` when market is closed, so adaptive poller reliably switches to 30 min
- svelte-check 0 errors
