# Plan: Fix chart range selector — x-axis anchored to requested range, not first bar

## Context

Chart range buttons (1M, 3M, 6M, 1Y) appear to do nothing for symbols with limited history
(e.g. BHEL). Root cause: `_dataXMin` in `ChartWorkspace.svelte` auto-fits the x-axis start
to the first bar's actual timestamp. When DB only has BHEL bars from Jun 9 onward, all range
requests return bars starting Jun 9 → chart x-axis starts at Jun 9 for 1M, 3M, 6M, and 1Y →
user sees no visual change when clicking range buttons.

Secondary/defensive issue: `_still_partial` in `options_helpers.py` only fires when
`_heal_attempted=True`. When all Kite accounts are rate-limited, `get_historical_brokers()`
returns [] → self-heal skipped → `_heal_attempted=False` → partial DB data returned as
"complete" with 60s TTL → no retry, no demand_fill. This prevents backfill from catching up.

## Agents

- backend: In `backend/api/routes/options_helpers.py` (line ~315), change `_still_partial` to
  NOT depend on `_heal_attempted`. Replace:
  ```python
  _still_partial = (
      _heal_attempted
      and len(store_bars) < self_heal_threshold * days
  )
  ```
  with:
  ```python
  _still_partial = len(store_bars) < int(self_heal_threshold * days)
  ```
  This ensures demand_fill is triggered when data is genuinely sparse, regardless of whether
  a heal was attempted (i.e., even when brokers were rate-limited). No other changes to
  options_helpers.py.

- frontend: In `frontend/src/lib/ChartWorkspace.svelte` (lines 921-924), change `_dataXMin`
  to anchor to the requested range start:
  
  Current:
  ```javascript
  const _dataXMin = $derived(_barXs.length ? Math.min(..._barXs) : 0);
  ```
  
  Replace with:
  ```javascript
  const _rangeStartMs = $derived(Date.now() - _chartDays * 86400 * 1000);
  const _dataXMin     = $derived(_barXs.length
      ? Math.min(Math.min(..._barXs), _rangeStartMs)
      : _rangeStartMs);
  ```
  
  This anchors the x-axis left edge to `now - chartDays` regardless of where actual bars
  start. Works for both daily and intraday (same component, `_chartDays` is set by
  `_setRange` for daily and by `_loadHistorical` calls for intraday).
  
  `_dataXMax` and zoom logic are unchanged.

- broker: skip

- doc: skip

- backend-test: Add test in `backend/tests/` for `_still_partial` logic:
  - Test that `_still_partial` is True when bar count < threshold × days, even when
    `_heal_attempted=False` (rate-limited scenario). Use `inspect.getsource` approach
    (static analysis of the condition) OR a unit test that patches `get_historical_brokers`
    to return [] and confirms `_ohlcv_demand_fill` is still scheduled.
  - File: `backend/tests/test_chart_range_partial.py`

- playwright: skip (x-axis position is not accessible text/attribute; visual change would
  require pixel comparison which is brittle — the fix is verified via code inspection and
  confirmed correct by design)

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message

fix(chart): anchor x-axis to requested range start; fix _still_partial when brokers rate-limited

Chart range buttons now correctly expand/contract the x-axis window even when symbol has
sparse historical data. Backend defensive fix ensures partial-data state is detected even
when all historical brokers are in rate-limit cool-off.

## Done when

1. BHEL 1M chart shows x-axis from ~Jun 15; 3M from ~Apr 15; 6M from ~Jan 15; 1Y from ~Jul '25.
   (Exact start = today − chartDays. Empty space shown before first available bar.)
2. Switching range buttons produces visually distinct x-axis spans immediately.
3. `_still_partial` logic does not reference `_heal_attempted` in `options_helpers.py`.
4. `test_chart_range_partial.py` passes.
5. `svelte-check` reports 0 errors.
