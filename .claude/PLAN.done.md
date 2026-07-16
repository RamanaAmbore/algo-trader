# Plan: Chart OHLCV auto-heal — demand backfill + partial-bars retry

## Context

Charts show same data for 6M and 1Y because:
1. DB has only ~6M of OHLCV history for the symbol
2. The existing self-heal (`_ohlcv_self_heal_if_needed`) fires, calls broker with `bypass_cache=True`, but broker also returns only ~180 bars → `partial=True` is set in the response
3. **Frontend bug**: `partial=True` is ONLY handled in `_handleEmptyBars` (fires when `bars.length === 0`). When bars > 0 but partial, the partial flag is silently ignored — no retry is scheduled, `_histRetrying` is set to `false`, and the chart settles on the incomplete dataset.
4. **Backend gap**: when `partial=True` is returned, no background fill is triggered. The existing broker refresh cycle (`backfill_ohlcv_daily` in `background.py`) runs on a fixed schedule for a fixed symbol set — chart demand never feeds into it.

The fix is two-sided: backend fires a demand backfill into the existing broker refresh infrastructure; frontend retries after 5 s when `partial=True` with non-empty bars.

## Agents

- backend: Fix `backend/api/routes/options_helpers.py`:
  1. Add module-level `_DEMAND_FILL_ACTIVE: set[tuple[str, str]] = set()` (debounce — don't fire two fills for the same symbol concurrently).
  2. Add async function `_ohlcv_demand_fill(sym: str, exch: str, days: int) -> None`:
     ```python
     async def _ohlcv_demand_fill(sym: str, exch: str, days: int) -> None:
         key = (sym, exch)
         if key in _DEMAND_FILL_ACTIVE:
             return
         _DEMAND_FILL_ACTIVE.add(key)
         try:
             from backend.api.persistence.backfill import backfill_ohlcv_daily
             await backfill_ohlcv_daily([(sym, exch)], target_days=days + 5, max_concurrent=1)
             logger.info("ohlcv demand fill complete: %s/%s target=%d", sym, exch, days + 5)
         except Exception as exc:
             logger.warning("ohlcv demand fill %s/%s: %s", sym, exch, exc)
         finally:
             _DEMAND_FILL_ACTIVE.discard(key)
     ```
  3. In `_historical_ohlcv_store`, right after the `_still_partial` check (after the `HistoricalResponseCls` is built), fire the demand fill:
     ```python
     if _still_partial:
         import asyncio as _asyncio
         _asyncio.create_task(_ohlcv_demand_fill(sym, resolved_exch, days))
     ```
     This integrates with the existing broker refresh cycle — `backfill_ohlcv_daily` uses
     the same `get_or_fetch_daily(bypass_cache=True)` path and write-back pipeline.

- frontend: Fix 2 files:
  1. **`frontend/src/lib/api.js`** — add `fresh=false` param to `fetchOptionsHistorical`:
     ```javascript
     export async function fetchOptionsHistorical(symbol,
                                                   { days = 30,
                                                     interval = 'day',
                                                     exchange = undefined,
                                                     fresh = false } = {}) {
       const p = new URLSearchParams({ symbol: String(symbol), days: String(days), interval: String(interval) });
       if (exchange) p.set('exchange', String(exchange));
       if (fresh)    p.set('fresh', '1');
       return _get(`/options/historical?${p}`, { auth: true });
     }
     ```

  2. **`frontend/src/lib/ChartWorkspace.svelte`** — handle `partial=True` with non-empty bars:
     a. Add `_partialRetryFired: Set<string> = new Set()` alongside `_emptyRetryFired`.
     b. Add `let _partialRetryTimer = null` alongside `_emptyRetryTimer`.
     c. Add `_histPartial = $state(false)` reactive cell (shows "Loading more history..." badge near range pills when true).
     d. Modify `_loadHistorical` signature: `async function _loadHistorical(force = false, fresh = false)`.
        Pass `fresh` to the fetch call: `fetchOptionsHistorical(fetchSym, { days: _chartDays, exchange: fetchExch, fresh })`.
     e. In the `else` branch (bars > 0, lines ~758-763), BEFORE `_histRetrying = false`, add:
        ```javascript
        if (hist?.partial && !_partialRetryFired.has(retryKey)) {
          _partialRetryFired.add(retryKey);
          _histPartial = true;
          if (_partialRetryTimer) clearTimeout(_partialRetryTimer);
          _partialRetryTimer = setTimeout(() => {
            _histPartial = false;
            _loadHistorical(true, true);  // force=true, fresh=true
          }, 5000);
        } else {
          _histRetrying = false;
          _histPartial  = false;
        }
        ```
     f. In the catch block and in the symbol-change `clearData` path, also clear `_histPartial = false` and cancel `_partialRetryTimer`.
     g. In the template, near the range pills, add a small "Loading more history…" text when `_histPartial`:
        ```html
        {#if _histPartial}
          <span class="chart-partial-hint">Loading more history…</span>
        {/if}
        ```
        Style: `font-size: 11px; color: var(--text-faint); opacity: 0.8` — non-intrusive.

- broker: skip
- doc: skip
- backend-test: skip

## Tests
- pytest: no (no new server logic that needs unit testing — the demand fill delegates to existing `backfill_ohlcv_daily` which is already tested)
- svelte-check: yes
- playwright: no

## Commit message
feat(chart): OHLCV auto-heal — demand backfill on partial + frontend retry for partial-bars

## Done when
- `_historical_ohlcv_store` fires `asyncio.create_task(_ohlcv_demand_fill(...))` when `_still_partial=True`
- `_ohlcv_demand_fill` calls `backfill_ohlcv_daily` (existing broker refresh infrastructure) with debounce
- `fetchOptionsHistorical` accepts `fresh` param and appends `?fresh=1` when true
- `ChartWorkspace` schedules a 5-second retry when `hist.partial=true` AND `bars.length > 0`
- `_histPartial` state shows "Loading more history…" badge during the wait
- Retry uses `fresh=true` to bypass server cache so updated DB bars are served
- svelte-check 0 errors
