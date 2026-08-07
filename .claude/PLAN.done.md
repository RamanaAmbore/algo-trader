# Plan: Real-time tickBus LTP flash + multi-broker holdings fix

## Context

Two confirmed gaps from audit:

**Gap 1 — LTP flash is poll-driven (30 s lag) on PerformancePage and Derivatives**
MarketPulse already wires LTP cells to `tickBus` (SSE-driven, fires per tick). Every other
surface — PerformancePage Positions/Holdings ag-Grid + Derivatives Spot + CandidateLegRow
spans — uses `createTickFlash` poll-diff (30 s cadence). LTP flash on those pages lags
up to 30 s.

**Gap 2 — Holdings total excludes Dhan/Groww during cold-boot window**
`_build_kite_conn_map` (`connections.py:~1531`) wraps EVERY account as `KiteConnection`
regardless of broker type. Until `rebuild_from_db` overwrites `conn`, Dhan account's
`broker.holdings()` calls `KiteConnection.holdings()` with no valid token → throws →
empty DataFrame. Dhan/Groww holdings silently zero out in the TOTAL until DB rebuild completes.
Also: `holdings.py:471` error string hardcoded as "Broker (Kite) returned no holdings data"
even when the failing account is Dhan.

## Task

**Flash**: Subscribe to `tickBus` on PerformancePage and Derivatives so LTP cells flash
immediately on each SSE tick. P&L cells keep existing poll-diff — they aggregate multiple
legs and cannot key to a single symbol.

**Holdings**: Fix `_build_kite_conn_map` to call `_build_conn_for_row` (same as
`rebuild_from_db`) so Dhan/Groww connections are correct from startup. Fix "Kite" error string.

## Agents

- backend: skip
- frontend: **Two surfaces — one agent pass.**

  **Surface A — PerformancePage (`frontend/src/lib/PerformancePage.svelte`)**

  Read the full `_seedFlash` function and LTP `cellClass` callback on `last_price` column
  (search `_perfFlash`, `pnlClsFlash`). Also read MarketPulse.svelte lines 1530-1570 and
  2155-2240 for the existing tickBus pattern (`_ltpFlashUp/Down` Sets, `_ltpFlashTimers`
  Map, `onMount` subscribe, `onDestroy` unsub, `refreshCells` call).

  Apply the same pattern:
  1. Add `let _perfLtpFlashUp = $state(new Set()); let _perfLtpFlashDown = $state(new Set());`
     and `const _perfLtpTimers = new Map();` alongside existing flash state.
  2. In `onMount`, subscribe to `tickBus`. On each `{ sym, dir }` event, find all rows in
     `rawPositions` / `rawHoldings` where `tradingsymbol.toUpperCase() === sym`. For each
     match: add sym to the appropriate Set (clear opposite Set for that sym first), set a
     300 ms timer to remove it, reassign the Sets (`= new Set(...)`) to trigger reactivity.
     After updating, call `posGridApi?.refreshCells({ columns: ['last_price'] })` and
     `holdGridApi?.refreshCells({ columns: ['last_price'] })`. Store unsub; call in `onDestroy`.
  3. Update the LTP `cellClass` callback: check `_perfLtpFlashUp.has(sym)` → return
     `'ltp-flash-up'`, check `_perfLtpFlashDown.has(sym)` → return `'ltp-flash-down'`, else
     fall through to `_perfFlash.classOf`. `sym` = `params.data?.tradingsymbol?.toUpperCase()`.
  4. Verify `posGridApi` / `holdGridApi` variable names via `onGridReady` in the file.

  **Surface B — Derivatives (`frontend/src/routes/(algo)/admin/derivatives/+page.svelte`)**

  Read lines 1025-1130 (flash + leg $effect + spot batchQuote $effect). Check how
  `_underlyingQuotes`/`_batchQuotes` is keyed and what `candidates` (or equivalent) holds
  per-leg rows. Check `symbolStore.svelte.js` lines 100-130 for `getSnapshot` export.

  In `onMount`, add:
  ```js
  const _derivTickUnsub = tickBus.subscribe(({ sym }) => {
    const root = sym.toUpperCase();
    // 1. Spot LTP cell
    if (root in _underlyingQuotes || _underlyingRoots?.has(root)) {
      flash.update(`${root}:ltp`, getSnapshot(root)?.ltp ?? null);
    }
    // 2. CandidateLegRow LTP cells
    for (const c of candidates) {
      if ((c.symbol ?? '').toUpperCase() === root) {
        const k = `${c.account ?? ''}|${c.symbol ?? ''}`;
        flash.update(`leg:${k}:ltp`, getSnapshot(root)?.ltp ?? null);
      }
    }
  });
  // onDestroy: _derivTickUnsub();
  ```
  Adapt variable names to match the file. No CSS changes — existing scoped `leg-ltp-up/down`
  keyframes (0.22α/450ms) handle the visual; Spot LTP uses global `tf-up/tf-down`.

  For every file you change, you MUST write or update at least one test covering the changed
  behaviour. Frontend changes → add/update a Playwright spec in `frontend/e2e/`.

- broker: Fix `connections.py` cold-boot and `holdings.py` error string.

  **`backend/brokers/connections.py`**: Read `_build_kite_conn_map` (~line 1531) and
  `_build_conn_for_row` (used by `rebuild_from_db`). Replace the per-account
  `KiteConnection(...)` call with `_build_conn_for_row(row)` so Dhan/Groww get the correct
  adapter from startup. If `_broker_id_map` is not yet seeded when this runs, also seed it
  from the YAML secrets (check `_load_secrets` or equivalent for Dhan/Groww account entries).

  **`backend/api/routes/holdings.py:471`** (or `broker_apis.py` if the string lives there):
  Change `"Broker (Kite) returned no holdings data"` → `"Broker returned no holdings data"`.

  Add a pytest test in `backend/tests/broker/` verifying that the cold-boot path produces
  the correct connection type for a Dhan-typed account (not `KiteConnection`).

- doc: skip
- backend-test: skip (broker agent owns its tests)
- playwright: Write `frontend/e2e/ltp_flash_realtime.spec.js`:
  1. Stale-code: `PerformancePage.svelte` contains `tickBus.subscribe` and `_perfLtpFlashUp`
  2. Stale-code: derivatives `+page.svelte` contains `tickBus.subscribe` and
     `flash.update.*ltp` inside the subscribe block
  3. Regression guard: `PerformancePage.svelte` still contains `_perfFlash.classOf` or
     `pnlClsFlash` (poll-diff P&L flash not removed)
  4. Regression guard: `CandidateLegRow.svelte` still contains `leg-ltp-up` keyframe
  5. Live DOM: navigate to `dev.ramboq.com/admin/perf`, login via `loginAsAdmin(page)`,
     verify ag-Grid rows render (`.ag-row` count > 0), skip gracefully if 0.

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message
feat(flash+holdings): real-time tickBus LTP flash on PerformancePage + Derivatives; fix Dhan/Groww cold-boot holdings gap

## Done when
- PerformancePage LTP column flashes `ltp-flash-up/down` on each SSE tick
- Derivatives Spot LTP + CandidateLegRow LTP call `flash.update()` on each tickBus event
- `_build_kite_conn_map` produces correct connection type for Dhan/Groww accounts from startup
- `holdings.py` error string no longer says "Kite"
- svelte-check 0 errors, pytest passing, Playwright stale-code guards passing
