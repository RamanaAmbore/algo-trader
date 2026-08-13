# Plan: Centralize positions day P&L + rationalize poll cycles

## Context

### Problem: Position day P&L drift between PositionStrip and Pulse

PositionStrip and the Pulse positions grid independently compute day P&L from different-aged data:
- PositionStrip: `_livePositionsToday $derived.by` reads `positionsStore` (5s) + live ticks
- Pulse: `mergePositionRows` reads `pulsePositionsStore` (10s) + live ticks (via `livePositionDayPnl` in ctx)

Result: up to 10s drift — P∆ pill in PositionStrip and TOTAL row in Pulse show different numbers.

### Recent fixes already committed (do NOT redo these)

- `2eadec21` — `mergePositionRows` now calls `livePositionDayPnl` via `posCtx` (Pulse positions grid is correct formula-wise; drift remains due to data age)
- `b136e785` — NavStrip P∆ slot uses `livePositionDayPnl` (not naive `(ltp−close)×qty`)
- `ab62d117` — `pulseHoldingsStore` added to book poller (holdings covered; positions still missing)

### Remaining work

Create `positionsDayPnlStore.svelte.js` as the single computation source. PositionStrip and Pulse both read from it — zero drift. Add `pulsePositionsStore` to book poller so Pulse's base data is also 5s fresh. Rationalize poll cycles to reduce cadence variation across the app.

---

## Audit: All Frontend Refresh Cycles (before fixes)

### Positions / holdings — 4 cadence cycles, all hitting the same endpoint

| Timer cycle | Cadence | Writes to |
|---|---|---|
| Book poller (layout-global) | 5s / 30s hidden / 30min closed | `positionsStore`, `holdingsStore`, `pulseHoldingsStore`, `fundsStore` |
| Pulse `loadPulse` (`_TICK_PULSE=2`) | 10s | `pulsePositionsStore`, `pulseHoldingsStore` |
| Dashboard `loadHero` | 15s | `positionsStore`, `holdingsStore`, `fundsStore` |
| Derivatives `loadPositions` | 30s | `positionsStore`, `holdingsStore` |

### All 22 timer cycles (before)

| # | Timer cycle | Cadence | Component |
|---|---|---|---|
| 1 | Book poller | 5s | marketDataStores |
| 2 | Pulse `_runTick` (movers 30s, sparklines 60s, quotes 30s internal) | 5s base | MarketPulse |
| 3 | Pulse settings poll | 60s | MarketPulse |
| 4 | Pulse closed-hours sparkline | 30s | MarketPulse |
| 5 | Dashboard `loadHero` | 15s | Dashboard |
| 6 | Dashboard `_fetchEquity` | 15s | Dashboard |
| 7 | Derivatives `loadStrategy` | 5s | Derivatives |
| 8 | Derivatives `loadPositions` | 30s | Derivatives |
| 9 | Derivatives `loadUnderlyingQuotes` | 30s | Derivatives |
| 10 | Derivatives `loadSimStatus` | 30s | Derivatives |
| 11 | Derivatives chain quotes (modal-only) | 5s | Derivatives |
| 12 | PositionStrip `_load` | 30s | PositionStrip |
| 13 | PositionStrip market boundary detector | 30s | PositionStrip |
| 14 | Broker connection health | **15s** | layout |
| 15 | Broker auth health | 30s | layout |
| 16 | Market status / holidays | 5min | layout |
| 17 | pollSim | **4s** | layout |
| 18 | pollPaper | **4s** | layout |
| 19 | pollReplay | 5s | layout |
| 20 | pollChase | 5s | layout |
| 21 | Execution mode | 30s | layout |
| 22 | Cache purge (in-memory) | 60s | stores |

**Before: 22 timer cycles, 7 cadence cycles (4s, 5s, 10s, 15s, 30s, 60s, 5min)**

---

## Confirmed: LTP background animation threshold = 0.1%

`stores.js:1384` — `export const ltpFlashPct = writable(0.1)` (default 0.1%, operator-configurable).  
`MarketPulse.svelte:1549` — tick skipped when `pct < _pctThreshold`.  
`symbolStore.svelte.js:293` — `_pct = |(_newLtp − _prevLtp) / _prevLtp × 100|`.

Row background flash fires only on ≥ 0.1% LTP move. Separate from the 250ms `_throttledTick` gate and poll-driven column flash (`threshold: 0.001`).

---

## Drift Sources

1. **positionsStore vs pulsePositionsStore** — same endpoint, different cache keys, poll rates 5s vs 10s. Book poller loads `pulseHoldingsStore` but NOT `pulsePositionsStore` — oversight.
2. **Day P&L double computation** — PositionStrip from `positionsStore` (5s age); Pulse from `pulsePositionsStore` (10s age). Different inputs → different totals.
3. **Why `pulsePositionsStore` is separate** — `createDataStore` deduplicates by `JSON.stringify(args)`. Pulse calls `load({ skipLtp: true })`; book poller calls `load()`. Different keys → two concurrent requests. `skipLtp` was meant to protect SSE LTPs, but `ltp_ts=0` arbitration already makes it redundant — polls can never overwrite a live SSE tick.

---

## Terminology

- **Cadence cycle** — the heartbeat frequency group. All timer cycles at the same interval belong to the same cadence cycle.
- **Timer cycle** — an individual `visibleInterval` / `marketAwareInterval` call. Changing its interval reassigns it to a different cadence cycle.

---

## Cycle Count: Before vs After

| Metric | Before | After |
|---|---|---|
| **Cadence cycles** | 7 (4s, 5s, 10s, 15s, 30s, 60s, 5min) | **4** (5s, 30s, 60s, 5min) |
| **Timer cycles** | 22 | **17** |
| **Positions/holdings callers** | 4 cadence cycles | **1** (book poller, 5s) |
| **Max positions data age** | 30s (Derivatives) | **5s** (all pages) |
| **Drift: PositionStrip vs Pulse P&L** | up to 10s | **0** |

**Reductions in timer cycles (5 total):**
- Pulse settings timer absorbed into `_runTick`: −1
- Dashboard `loadHero` + `_fetchEquity` merged: −1
- Derivatives `loadPositions` timer removed: −1
- Derivatives `loadUnderlyingQuotes` + `loadSimStatus` merged: −1
- PositionStrip `_load` timer removed: −1

**Cadence reassignments (no timer count change):**
- sim/paper: 4s → 5s
- Dashboard merged timer: 15s → 30s
- Broker connection health: 15s → 30s (Fix 11)

**4-cadence model after all fixes:**

| Cadence cycle | Timer cycles inside | Component |
|---|---|---|
| **5s** | Book poller (positions+holdings+funds) | marketDataStores |
| | Derivatives loadStrategy/Greeks | Derivatives |
| | Derivatives chain quotes (modal) | Derivatives |
| | pollSim, pollPaper (moved from 4s), pollReplay, pollChase | layout |
| **30s** | Pulse `_runTick` sub-tasks: movers, watchlist quotes, closed-hours sparkline | MarketPulse |
| | Broker conn health (moved from 15s), broker auth health | layout |
| | Execution mode, persist mode | layout |
| | PositionStrip market boundary detector | PositionStrip |
| | Derivatives: loadUnderlyingQuotes + loadSimStatus (merged) | Derivatives |
| | Dashboard: loadHero + _fetchEquity (merged, moved from 15s) | Dashboard |
| **60s** | Pulse `_runTick` sub-tasks: open-hours sparklines, settings (absorbed from Fix 9) | MarketPulse |
| | Cache purge (in-memory) | stores |
| **5min** | Market status / holidays | layout |

Within-component same-cadence timer cycles are merged where possible (Fixes 6, 8, 9). Cross-component same-cadence timers remain separate — they share the cadence cycle but cannot share one `setInterval` without a centralized scheduler (out of scope).

---

## Fixes

### Fix 1 — `positionsDayPnlStore.svelte.js` — SSOT for day P&L

**New file:** `frontend/src/lib/data/positionsDayPnlStore.svelte.js`

Module-level singleton. Reads `positionsStore.value` (5s, book poller). Throttles at 4 Hz via `symbolTickCount.subscribe` + 250ms `setTimeout` + `isMarketOpen()` gate (same pattern as PositionStrip `_throttledTick`). Exports `{ total: number, byKey: { "EXCHANGE:SYM": number } }`.

- Key format: `${exchange}:${tradingsymbol.toUpperCase()}`
- Per-row: `livePositionDayPnl({ closePx, pollLtp, qty, avg, dcvRow }, untrack(() => getSnapshot(sym)?.ltp), { marketOpen: true })`

**`PositionStrip.svelte`:**
- Import `positionsDayPnlStore`
- Remove `_livePositionsToday $derived.by` block (lines 417–448)
- Replace all `_livePositionsToday` references with `positionsDayPnlStore.total`
- Keep `_throttledTick` setup (still needed by `_liveDeltaByRow`, `_liveHoldingsToday`, other deriveds)

**`MarketPulse.svelte`:**
- Import `positionsDayPnlStore`
- After `mergePositionRows` fills `byKey`, add a pass overriding each `row.day_pnl` from `positionsDayPnlStore.byKey[key]` when present
- TOTAL row accumulator picks up overridden values automatically

`pulseUnified.js` unchanged — `mergePositionRows` stays as cold-boot fallback.

### Fix 2 — Add `pulsePositionsStore` to book poller

**`marketDataStores.svelte.js`** (lines 723–727):

Add `pulsePositionsStore.load({ skipLtp: true })` to the book poller's `Promise.allSettled` batch. `pulseHoldingsStore` was already added in `ab62d117` — this completes the symmetric pair so both pulse stores refresh at 5s.

### Fix 3 — Remove positions/holdings load from Pulse `_runTick`

**`MarketPulse.svelte`:**

Remove `pulsePositionsStore.load()` and `pulseHoldingsStore.load()` from `loadPulse` (currently called at `_TICK_PULSE=2`, every 10s). Book poller covers both at 5s (Fix 2 + `ab62d117`). Pulse `_runTick` continues to own: `fundsStore`, movers, sparklines, watchlist-quotes, settings (after Fix 9).

Note: `mergePositionRows` already calls `livePositionDayPnl` via `posCtx` (commit `2eadec21`). Fix 1 adds a byKey override pass so the SSOT store value wins even if `_runTick` fires before `positionsDayPnlStore` updates.

### Fix 4 — Remove PositionStrip `_load` 30s timer

**`PositionStrip.svelte`** (line 239):

Remove `teardown = marketAwareInterval(_load, 30000)`. PositionStrip already reacts to `positionsStore.value` via `$effect` (lines 27–31); book poller keeps it fresh at 5s. Keep event-driven `_load()` calls: onMount (line 232), mode change (line 537), boundary trigger (line 349).

**Timer cycles: −1** (22→21)

### Fix 5 — Dashboard: strip positions/holdings from `loadHero`

**`dashboard/+page.svelte`** (lines 1175–1176):

Remove `positionsStore.load()` and `holdingsStore.load()` from `loadHero()`. Dashboard reads both reactively via `$effect` bridges (lines 180–182) — book poller keeps them fresh. `loadHero` retains: events fetch, NAV. Event-driven `loadHero()` calls (book_changed bus lines 1295/1359) untouched.

### Fix 6 — Dashboard: merge `loadHero` + `_fetchEquity` → one 30s timer

**`dashboard/+page.svelte`** (lines 1304, 1313):

Replace two 15s `visibleInterval` calls with one at 30s:
```js
_heroTeardown = visibleInterval(
  () => Promise.allSettled([loadHero(), _fetchEquity()]),
  30000, 'throttle:30000'
);
```
Mount already calls both together (lines 1110–1112). After Fix 5, `loadHero` only fetches events — 30s is sufficient. Eliminates the 15s cadence cycle (partially — see Fix 11).

**Timer cycles: −1** (21→20)

### Fix 7 — Derivatives: remove timed positions/holdings poll

**`derivatives/+page.svelte`** (line 3904):

Remove `posTeardown = visibleInterval(loadPositions, 30000, 'throttle:30000')`. Keep all event-driven `loadPositions({ fresh: true })` calls (lines 3765, 3795, 3834, 3933, 3940, 3951). Derivatives reads `positionsStore.value` reactively (line 3305).

**Timer cycles: −1** (20→19)

### Fix 8 — Derivatives: merge `loadUnderlyingQuotes` + `loadSimStatus` → one 30s timer

**`derivatives/+page.svelte`** (lines 3910, 3916):

Replace two separate `marketAwareInterval` calls (both 30s) with one:
```js
quotesTeardown = marketAwareInterval(
  () => Promise.allSettled([loadUnderlyingQuotes(), loadSimStatus()]),
  30000, 30_000
);
```
`loadUnderlyingQuotes` remains sequential internally (line 2715).

**Timer cycles: −1** (19→18)

### Fix 9 — Pulse settings: absorb into `_runTick`

**`MarketPulse.svelte`** (line 1467 — separate 60s `visibleInterval`):

Add `_TICK_SETTINGS = 12` counter gate inside `_runTick` (12 × 5s = 60s effective). Move settings fetch inside `_runTick`. Remove `stopTickSettingPoll = visibleInterval(...)`.

**Timer cycles: −1** (18→17)

### Fix 10 — Align sim/paper 4s → 5s

**`+layout.svelte`** (lines 861–864):

Change `_adaptiveInterval(pollSim, ...)` and `_adaptiveInterval(pollPaper, ...)` fast cadence from 4000 → 5000ms. Eliminates the 4s cadence cycle.

**Timer cycles: 0 change** (reassignment only)

### Fix 11 — Broker connection health 15s → 30s

**`stores.js` or `+layout.svelte`** (`startConnStatusPoller`, line 1275):

Change foreground cadence from 15000 → 30000ms. Fully eliminates the 15s cadence cycle. Broker connection status is a UI badge — 30s detection lag is acceptable.

**Timer cycles: 0 change** (reassignment only)

---

## Agents

- frontend: Fixes 1–11 (new positionsDayPnlStore; update PositionStrip, MarketPulse, marketDataStores, Dashboard, Derivatives, layout, stores)
- frontend-test: Vitest unit test for `positionsDayPnlStore` (mock `positionsStore.value` + `snapOf`, verify `total` + `byKey`)
- backend: skip
- broker: skip
- doc: skip
- playwright: no

## Tests

- pytest: no
- svelte-check: yes
- vitest: yes
- playwright: no

## Commit message

feat(stores): positionsDayPnlStore SSOT + rationalize frontend poll cycles (22→17 timers, 7→4 cadences)

## Done when

- `positionsDayPnlStore.svelte.js` exports `{ total, byKey }` from `positionsStore` + live ticks
- PositionStrip reads `positionsDayPnlStore.total`; `_livePositionsToday` derived removed
- Pulse positions grid `day_pnl` patched from store `byKey`; TOTAL row agrees with PositionStrip
- Book poller loads `pulsePositionsStore` at 5s (symmetric with `pulseHoldingsStore`)
- Pulse `_runTick` no longer loads positions/holdings; settings absorbed as `_TICK_SETTINGS=12`
- PositionStrip `marketAwareInterval(_load, 30000)` removed; event-driven `_load()` calls kept
- Dashboard `loadHero` stripped of positions/holdings; merged with `_fetchEquity` at 30s
- Derivatives `loadPositions` visibleInterval removed; event-driven calls kept
- Derivatives `loadUnderlyingQuotes` + `loadSimStatus` merged into one 30s timer
- sim/paper adaptive interval changed to 5000ms
- Broker connection health changed to 30s
- Timer cycles: **22 → 17** | Cadence cycles: **7 → 4** | Positions callers: **4 → 1**
- svelte-check 0 errors, vitest green
