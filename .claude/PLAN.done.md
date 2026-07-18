# Plan: Fix chart range selector, 3M/6M/1Y loading, and indicator multi-select

## Context
Three bugs all in `frontend/src/lib/ChartWorkspace.svelte`:

**Bug 1 — Range selector race condition:**
Two bidirectional `$effect` hooks sync `_chartDays` ↔ `chartStore.days`. Effect 1 (store→local,
declared first) reads `_chartDays` in `if (d !== _chartDays)` without `untrack()`, making it a
Svelte 5 reactive dependency. Clicking "3M" → `_setRange(90)` → `_chartDays = 90`. Both effects
schedule. Effect 1 fires first: reads `chartStore.days` (still 30), sees `30 !== 90`, resets
`_chartDays = 30`. Effect 2 then reads 30 and persists 30. Range reverts to 1M every time.
Fix: wrap `_chartDays` comparison in `untrack()` in Effect 1.

**Bug 2 — Empty retry timing too short:**
`_ohlcv_demand_fill` fires async after HTTP response — Kite + DB write for 90–365 days takes
2–10s. `_handleEmptyBars` retries once at 2300ms via `_emptyRetryFired` Set latch. Retry fires
too early, sees empty bars, latch prevents further retries → "No data available" permanently.
Fix: replace Set latch with count Map, allow 3 retries at 4s/8s/15s.

**Bug 3 — Indicator multi-select race condition:**
Same bidirectional `$effect` race as Bug 1, but for overlays. Effect 1 (store→local overlay sync,
~lines 421-428) reads `_overlays` in `JSON.stringify(_overlays)` without `untrack()`. When user
picks an indicator → `bind:value` writes `_overlays` → Effect 1 fires first, sees `chartStore.overlays`
unchanged, resets `_overlays` to old store value. Selection is lost before Effect 2 can persist it.
Fix: same `untrack()` pattern.

## Agents
- backend: skip
- frontend: Apply all three fixes in `frontend/src/lib/ChartWorkspace.svelte`:

  **Fix 1 — `untrack()` in days Effect 1 (~lines 369-373):**
  ```javascript
  $effect(() => {
      const d = chartStore.days;
      if (!_rangeHydrated) return;
      untrack(() => {
          if (d !== _chartDays) _chartDays = d;
      });
  });
  ```

  **Fix 2 — Multi-retry in `_handleEmptyBars`:**
  - Replace `const _emptyRetryFired = new Set()` (~line 614) with `const _emptyRetryCount = new Map()`
  - Rewrite gate: `(_emptyRetryCount.get(retryKey) ?? 0) >= 3` (max 3 retries)
  - On each retry: increment counter, use delay `[4000, 8000, 15000][count - 1]` (count after increment)
  - Range-change clear (~line 818): replace `_emptyRetryFired.clear()` with `_emptyRetryCount.clear()`
  - Update docblock comment (~lines 602-612) to reflect 3-retry/4s–15s timing

  **Fix 3 — `untrack()` in overlays Effect 1 (~lines 421-428):**
  ```javascript
  $effect(() => {
      const storeOverlays = chartStore.overlays;
      if (!_overlaysHydrated) return;
      untrack(() => {
          if (JSON.stringify(_overlays) !== JSON.stringify(storeOverlays)) {
              _overlays = storeOverlays.slice();
          }
      });
  });
  ```

  Commit as **three separate commits** (one defect per commit, per memory rule).
  `untrack` is already imported at line 44 — no new imports needed.

- broker: skip
- doc: skip
- backend-test: In `backend/tests/test_ohlcv_partial_range.py`:
  - Update `test_frontend_retry_delay_is_at_least_2300ms` — first retry is now 4000ms;
    update assertion + comment to validate ≥ 4000ms
  - Update `test_frontend_retry_delay_exceeds_backend_empty_cache_ttl` to validate
    first delay (4000ms) > empty cache TTL (2000ms)
  - Commit with Fix 2.
- playwright: Add `frontend/tests/chart-range-and-indicators.spec.ts` covering:
  1. **Range selector persistence** — navigate to /charts, click "3M" button, assert it stays
     active (not reverted to 1M), assert URL/store reflects 90 days, wait for chart to render
  2. **3M chart loads** — after clicking 3M, wait up to 20s for chart canvas/bars to appear
     (demand fill may need multiple retries); assert no "No data available" error message shown
  3. **Indicator multi-select persistence** — open indicators dropdown, select "SMA 20",
     then select "RSI", assert both remain checked in the dropdown; close and reopen dropdown,
     assert both are still checked (persisted to localStorage); assert only one selection is
     NOT lost after picking a second indicator

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message
Three commits:

1. `fix(chart): untrack _chartDays in store→local effect — range selector race`
2. `fix(chart): multi-retry backoff (4s/8s/15s) for 3M/6M/1Y demand-fill`
3. `fix(chart): untrack _overlays in store→local effect — indicator multi-select race`

## Done when
- Clicking 3M/6M/1Y range button stays selected (does not revert to 1M)
- 3M/6M/1Y charts load after demand fill completes (retried up to 3× at 4/8/15s)
- Selecting multiple indicators in the dropdown persists (no selection reset)
- All three Playwright specs pass
- `test_ohlcv_partial_range.py` passes with 4000ms floor assertion
- `svelte-check` reports 0 errors
