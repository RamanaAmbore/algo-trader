# Plan: Fix MCX day P&L, NavBreakdown SSOT, chain timeout + animation audit fixes

## Context

**Group 1 — MCX settlement bugs (from prev session)**
1. Position day P&L = 0 after MCX close — `_positions_snapshot_mode` uses `today_ist_midnight`
   in `prev_batch` CTE. After midnight rolls, it selects the MCX settlement snapshot as the
   "previous" reference → delta ≈ 0. Fix: use 08:00 IST boundary cutoff (same as live path).
2. NavBreakdown TOTAL row mismatch — computes `Σ baseDayPnlForPosition` but NavStrip reads
   `positionsDayPnlStore.total` (`_pulseTotal ?? _store.total`). Fix: use store directly.
3. Chain snapshot hangs — `_chain_snapshot_batch_quote` calls `broker.quote(keys)` with no timeout.
   Fix: `asyncio.wait_for(..., timeout=10.0)`.

**Group 2 — Animation audit findings**
- Derivatives day% column animates incorrectly (threshold applied to relative change of a
  percent value, not the actual price move). Spot LTP should animate on >0.1% LTP change.
- day% is never updated from tickBus → day% flash only fires on 30s poll, not real-time ticks.
- NavStrip underline (cell-freshness-pulse::after) is missing: CSS keyframe absent from app.css,
  `createFreshnessShimmer` never instantiated, tick-bus shimmer in PositionStrip.svelte cut off.
- TOTAL rows in MarketPulse ag-Grid and Dashboard positions grid excluded from all animation
  via `_isTotal` / `account === 'TOTAL'` guards. User wants 0.1% threshold flash on TOTAL row numbers.

## Task

Six targeted fixes across backend + frontend. No refactoring beyond the minimum.

## Agents

### backend
Files: `backend/api/routes/positions.py`, `backend/api/routes/options_helpers.py`

**positions.py — `_positions_snapshot_mode` (line ~210):**
1. Replace the two separate `_ts_indian()` calls with one: `_now_ist = _ts_indian()`, then
   `_today_ist = _now_ist.date()`, `_today_ist_midnight = _now_ist.replace(hour=0,minute=0,second=0,microsecond=0)`.
2. Add local `from datetime import timedelta` import (same pattern as `_override_stale_close_from_snapshot` line 866).
3. Compute `_today_ist_8am = _today_ist_midnight + timedelta(hours=8)` and
   `_prev_batch_cutoff = _today_ist_8am if _now_ist >= _today_ist_8am else _today_ist_8am - timedelta(days=1)`.
4. In `prev_batch` CTE SQL (line ~250): `:today_ist_midnight` → `:prev_batch_cutoff`.
5. In `.bindparams(...)` (line ~267): replace `today_ist_midnight=_today_ist_midnight` with
   `prev_batch_cutoff=_prev_batch_cutoff`; keep `today_ist=_today_ist` unchanged.

**options_helpers.py — `_chain_snapshot_batch_quote` (line ~780):**
Wrap `asyncio.to_thread(get_market_data_broker().quote, keys)` with
`asyncio.wait_for(..., timeout=10.0)`; catch `asyncio.TimeoutError` → log warning, return `{}, key_meta`.
Same pattern as `chain-quotes` in `options.py:2241`.

Tests required: add/update pytest in `backend/tests/` for both changed behaviours.

### frontend (NavBreakdown + TOTAL-row SSOT)
File: `frontend/src/lib/NavBreakdown.svelte`

1. Add import: `import { positionsDayPnlStore } from '$lib/data/positionsDayPnlStore.svelte.js';`
2. Change `_pTotal` (line ~214): set `dayPnl: positionsDayPnlStore.total` (remove the
   `_pByAcct.reduce(...)` expression for dayPnl — keep lifetimePnl and expiryPnl reduces unchanged).
3. Update component comment line 8: `Day P&L (positionsDayPnlStore.total — matches NavStrip P:1)`.

Tests required: Playwright spec covering that NavBreakdown TOTAL day P&L matches NavStrip P:1 value.

### frontend-animations
Files:
- `frontend/src/app.css`
- `frontend/src/lib/data/tickFlash.svelte.js`
- `frontend/src/lib/PositionStrip.svelte`
- `frontend/src/lib/data/pulseColumns.js`
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
- `frontend/src/routes/(algo)/dashboard/+page.svelte`

**Fix A1+A2 — Derivatives spot LTP animation (not day%):**
In `derivatives/+page.svelte` tickBus handler (line ~1840–1858):
- Remove or stop updating `flash.update(':pct', ...)` from the tickBus path — day% is a derived
  metric and should not drive its own flash independent of price.
- The `:ltp` update already fires `tf-up/tf-down` on spot price column. Ensure the `flash.update(':ltp', ltp)`
  threshold check is against the absolute LTP value with `pctThreshold=0.001` (0.1%). Spot LTP
  column class should use `flash.classOf(':ltp')` — verify it already does and confirm no guard removes it.
- For the 30s poll path: update `:pct` with the current `day_pct` value ONLY from the poll diff
  (not tickBus), keeping day% flash tied to actual meaningful poll-period changes.

**Fix A3+A4 — NavStrip underline animation:**
In `app.css`: add the `cell-freshness-pulse::after` keyframe — a 1px gradient underline sweep
(left→right, sky-300/indigo-400 gradient, 0.6s ease, fires once). Example:
```css
@keyframes freshness-sweep {
  from { transform: scaleX(0); transform-origin: left; }
  to   { transform: scaleX(1); transform-origin: left; }
}
.cell-freshness-pulse::after {
  content: '';
  position: absolute;
  bottom: 0; left: 0; right: 0;
  height: 1px;
  background: linear-gradient(90deg, theme('colors.sky.300'), theme('colors.indigo.400'));
  animation: freshness-sweep 0.6s ease forwards;
}
```
In `PositionStrip.svelte`: instantiate `createFreshnessShimmer` (imported from tickFlash.svelte.js)
and wire it to the tickBus event (fire shimmer on each SSE LTP tick that passes 0.1% threshold
for any slot's underlying symbol). Apply `cell-freshness-pulse` class to the strip's border element
(the element that currently uses `ps-heartbeat`/`ps-poll-pulse`). The class should be added
transiently (remove after animation ends via `animationend` event or a short timeout).

**Fix A5+A6 — TOTAL row animation in ag-Grid surfaces:**
In `pulseColumns.js` (line ~50): remove the `_isTotal` guard that returns the base class. Allow
`tf-up/tf-down` classes to be applied to TOTAL row cells too. Add a `pctThreshold: 0.001` (0.1%)
gate so only changes ≥ 0.1% trigger animation (same threshold as non-total rows).
In `dashboard/+page.svelte` (line ~93): remove `account === 'TOTAL'` guard in the cellClass
function; allow the `_dashFlash` instance to track and flash TOTAL row values.

Tests required: at minimum one Playwright spec asserting animation class appears on TOTAL row
when a value changes, and one asserting NavStrip underline class fires on tick.

- broker: skip
- doc: skip
- backend-test: skip (backend agent writes its own tests)
- playwright: skip (animation agents write their own Playwright specs)

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(positions,chain,animations): MCX prev_batch cutoff + chain timeout + NavBreakdown SSOT + LTP/TOTAL/NavStrip animations

## Done when
- After MCX settlement (>00:15 IST), position day P&L shows correct non-zero values
- NavBreakdown TOTAL day P&L matches NavStrip P:1 exactly
- Chain snapshot returns within 10s on broker stall
- Derivatives spot LTP flashes tf-up/tf-down on >0.1% LTP tick; day% no longer fires independently
- NavStrip shows gradient underline sweep on SSE LTP ticks
- MarketPulse and Dashboard TOTAL rows flash tf-up/tf-down on ≥0.1% value change
- All pytest green, svelte-check 0 errors
