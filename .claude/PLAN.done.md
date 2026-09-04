# Plan: Fix all 19 audit findings — order ticket, chain tab, chart/symbol/virtual roots

## Task

Fix all 19 issues found in the 2026-09-04 audit of the derivatives page order ticket,
chain tab, and chart/symbol/virtual root surfaces. Grouped into three parallel streams:
backend (orders + options routes), frontend chain (OptionChainTab + OrderKnobsRow + chainQuotes),
and frontend chart (ChartWorkspace + OhlcvTooltip + resolveUnderlying).

---

## Agents

- backend: Fix all 7 backend issues in `backend/api/routes/orders_helpers.py`,
  `orders_place.py`, and `options.py`. Specific fixes:

  **#1 P1** `orders_helpers.py:34` — Add `"NCO"` and `"BCD"` to `_EXCHANGES` set.
  Currently `{"NSE","BSE","NFO","CDS","MCX","BFO"}` — missing NCO and BCD causes every
  NCO/BCD order to return HTTP 400 before reaching any broker.

  **#2 P1** `orders_place.py:100` — `_opl_price_from_broker` hardcodes
  `f"NFO:{tradingsymbol.upper()}"` as the LTP lookup key regardless of actual exchange.
  Fix: use the actual exchange from the order data in the key
  (e.g. `f"{exchange}:{tradingsymbol.upper()}"` where exchange comes from the order context).
  Verify what arguments the function receives and use the correct exchange field.

  **#3 P1** `options.py:2753-2760` — `chain_quotes` returns `rows=[]` on cold start when
  `_CHAIN_QUOTES_CLOSED_CACHE` has no entry for (underlying, expiry). Fix: stale-while-
  revalidate — when market is closed and cache miss, return last-known data if available,
  or return a structured response with `rows=[], stale=True, message="No chain data until
  next market open"` instead of an empty list that silently breaks the grid. The frontend
  must receive a non-empty signal to show a helpful state rather than an apparently-ready
  empty grid.

  **#6 P2** `orders_place.py:1244,1746` — `_ticket_check_mcx_lot_cache` return value
  `_mcx_ls_for_translate` is computed but never consumed (`_ls_for_translate` is derived
  independently at line 1764). Fix: remove the dead call to `_ticket_check_mcx_lot_cache`
  (and its WARNING log) from the live placement path, OR wire its return value into
  `_ls_for_translate`. Verify whether the function is called anywhere else before removing.

  **#9 P2** `options.py:2140` — `_CHAIN_QUOTES_CLOSED_CACHE` is an unbounded dict.
  `_CHAIN_SYM_CACHE` has a 64-entry LRU cap at line 2221. Apply the same LRU eviction
  pattern to `_CHAIN_QUOTES_CLOSED_CACHE` (cap at 128 entries — larger because it stores
  full strike-grid payloads keyed by (underlying, expiry)).

  **#10 P2** `options.py:2749` — `chain_quotes` calls `_any_segment_open()` directly
  instead of the canonical `closed_hours_or_broker()` gate from `snapshot_gate.py`.
  Add a block comment at line 2749 explaining the intentional bypass: chain quotes have
  no DB snapshot fallback (unlike positions/holdings), so `closed_hours_or_broker()` would
  call the broker even when closed. The deviation is correct but must be documented.

  **#19 P3** `options.py:2893` — Cache key defaults `exchange` to `"NFO"` when the
  param is empty/missing. An MCX call with no exchange param writes an NFO-keyed entry;
  a subsequent NFO call hits stale MCX data. Fix: default to `"NSE"` (not `"NFO"`) when
  exchange is absent, OR make the caller always pass exchange. Verify the call sites.

  For every file you change or create, you MUST write or update at least one test that
  covers the changed behaviour. This is mandatory — not optional.
  - `backend/api/` change → add/update a pytest test in `backend/tests/` covering the changed lines
  No change ships without a corresponding test update. If you add a helper function, test it.
  If you fix a branch, test that branch. The test must exercise the exact lines you changed.

- frontend: Fix all 6 frontend chain/order-ticket issues in `OptionChainTab.svelte`,
  `OrderKnobsRow.svelte`, and `frontend/src/lib/data/chainQuotes.js`. Specific fixes:

  **#5 P2** `frontend/src/lib/order/OrderKnobsRow.svelte:58` — AMO variety is shown in
  the ticket dropdown for all accounts with no broker capability filter. Dhan
  (`backend/brokers/adapters/dhan.py:1309`) and Groww (`groww.py:953`) raise
  `NotImplementedError` for AMO at placement time. Fix: add `"AMO"` to the `capWarningFor`
  system OR filter AMO from the variety dropdown when the selected account's broker is
  Dhan or Groww. Check how other capability warnings (OCO, trailing stop, MCX GTT) are
  implemented in the frontend and use the same pattern.

  **#7 P2** `frontend/src/lib/order/OptionChainTab.svelte:504-522` — `refreshKey` effect
  fires `fetchOptionsSpot` with no abort protection. Double-tap on the Chain tab triggers
  two parallel spot fetches; last-to-resolve wins regardless of order. Fix: capture the
  current `refreshKey` value before the async call and discard the response if
  `refreshKey` has changed by the time it resolves. Pattern already exists at line 484
  in `_refreshChainSpot` — apply the same guard here.

  **#8 P2** `frontend/src/lib/order/OptionChainTab.svelte:803-804` —
  `loadInstruments().catch(() => {})` swallows all IDB errors. After `_instrumentsTimedOut`
  fires, the grid appears ready but is empty. Fix: in the catch handler, set a visible
  error state (e.g. `_instrumentsError = true`) and render an error message + "Retry"
  button in the chain tab. The retry should call `loadInstruments()` again.

  **#15 P3** `frontend/src/lib/order/OptionChainTab.svelte:430-431` — Comment reads
  "every 5 s" but poll interval is 30 seconds (`visibleInterval(..., 30000)` at line 466).
  Update the comment to match reality.

  **#16 P3** `frontend/src/lib/data/chainQuotes.js:50` — `depthAvail` default is
  semantically inverted: backend defaults absent field to `false` (no depth); frontend
  parses `row.ce_depth_available !== false` → absent field becomes `true`.
  Fix: change to `row.ce_depth_available === true` (explicit opt-in). Apply the same fix
  to `pe_depth_available` if present.

  **#17 P3** `frontend/src/lib/order/OptionChainTab.svelte:~1373` —
  `.chain-cell-no-depth` CSS class exists but the "(L)" last-price fallback indicator is
  never rendered. Fix: either (a) render `{#if !quote.ce.depthAvail}(L){/if}` in the
  bid/ask cell template, or (b) remove the dead CSS class if the indicator is not desired.
  Prefer option (a) — the indicator is operationally useful (tells operator they're
  seeing last price, not live bid/ask).

  For every file you change or create, you MUST write or update at least one test that
  covers the changed behaviour. This is mandatory — not optional.
  - `frontend/src/lib/data/` change → add/update a Vitest test in `frontend/src/lib/__tests__/`
  - `frontend/src/` UI change → add/update a Playwright spec in `frontend/tests/`
  No change ships without a corresponding test update.

- broker: Fix all 6 frontend chart/symbol/virtual-root issues in `ChartWorkspace.svelte`,
  `OhlcvTooltip.svelte`, and `frontend/src/lib/data/resolveUnderlying.js`. Specific fixes:

  **#4 P1** `frontend/src/lib/data/resolveUnderlying.js:102` — `CDS_CURRENCIES` set
  contains only `'USDINR'`. Add `'EURINR'`, `'GBPINR'`, `'JPYINR'` to match the backend
  `CDS_VIRTUAL_ROOTS` which has all four. These three currently route as NSE equities →
  wrong exchange sent to historical API → empty bars.

  **#11 P2** `frontend/src/lib/ChartWorkspace.svelte:776-779` — "Keep last-good bars"
  guard is a no-op. `_bars` and `chartStore` are cleared at line 776 before
  `_handleEmptyBars` is called with `prevBars` at line 779 — the store is already empty
  when the restore runs. Fix: restructure the ordering so `_handleEmptyBars(prevBars)`
  is evaluated and bars are restored BEFORE `_bars` is cleared, OR save the prevBars
  decision (restore vs clear) before clearing and then execute the decision. Read the
  surrounding code carefully to understand the intended semantics.

  **#12 P2** `frontend/src/lib/ChartWorkspace.svelte:762` — MCX spot overlay fetch
  (`fetchOptionsHistorical(_underlying, { days: _chartDays })`) sends no `exchange`
  argument. Backend defaults to `"NFO"`, causing cache miss and wrong exchange for every
  MCX chart load. Fix: pass `exchange` in the options object — for MCX underlyings
  (those in `MCX_COMMODITIES` from `resolveUnderlying.js`) pass `"MCX"`, otherwise `"NFO"`.
  Use the existing `resolveUnderlying` module to determine the exchange.

  **#13 P2** `frontend/src/lib/chart/OhlcvTooltip.svelte:30-33` — `new Date("YYYY-MM-DD")`
  parses as UTC midnight. For IST (+5:30) users this shows the previous calendar day in
  the tooltip. Fix: parse as `new Date(ts + "T00:00:00")` (local time) or use
  `ts.split('-')` to construct the date as `new Date(year, month-1, day)`.

  **#14 P2** `frontend/src/lib/data/resolveUnderlying.js:93-100` — `MCX_COMMODITIES`
  is missing `'CPO'` (present in backend `MCX_VIRTUAL_ROOTS` and `_MCX_LOT_OVERRIDES`).
  Also contains 8 discontinued contracts that no longer trade:
  GOLDMINI, SILVERMINI, ZINCMINI, LEADMINI, ALUMINI, CASTORSEED, KAPAS, CARDAMOM.
  Fix: add `'CPO'`; remove the 8 discontinued entries. Verify each against MCX's current
  active contract list before removing (read backend `MCX_VIRTUAL_ROOTS` as ground truth).

  **#18 P3** `frontend/src/lib/ChartWorkspace.svelte:513-518` — `_KITE_INDEX_TO_ROOT`
  local map has 5 entries but `resolveUnderlying.KITE_INDEX_QUOTE_KEY_TO_ROOT` has 7.
  SENSEX and BANKEX are absent from the local map → those index symbols won't resolve to
  virtual roots in the chart workspace. Fix: add SENSEX and BANKEX entries to
  `_KITE_INDEX_TO_ROOT` matching the values in `resolveUnderlying.KITE_INDEX_QUOTE_KEY_TO_ROOT`.

  Note: this agent is fronend-only (chart/symbol files). Do NOT touch OptionChainTab or
  OrderKnobsRow — those are handled by the `frontend` agent above.

  For every file you change or create, you MUST write or update at least one test that
  covers the changed behaviour. This is mandatory — not optional.
  - `frontend/src/lib/data/` change → add/update a Vitest test in `frontend/src/lib/__tests__/`
  - `frontend/src/` UI change → add/update a Playwright spec in `frontend/tests/`
  No change ships without a corresponding test update.

- backend-test: Write pytest tests for all 7 backend fixes. Specifically:
  - Test that NCO and BCD are accepted by `_validate_ticket_enums` (orders_helpers.py fix #1)
  - Test that `_opl_price_from_broker` uses the correct exchange-prefixed key for MCX symbols
    (orders_place.py fix #2) — mock the LTP buffer with an MCX-keyed entry and verify it's found
  - Test that `chain_quotes` with a cold cache and closed market returns a non-empty structured
    response (not bare empty list) with a `stale=True` or `message` field (options.py fix #3)
  - Test that `_CHAIN_QUOTES_CLOSED_CACHE` is evicted after exceeding the cap (fix #9)
  - Test the exchange default correction in the options cache key (fix #19)
  - Tests go in `backend/tests/test_orders_place.py` and `backend/tests/test_chain_quotes.py`
    (create if not existing)

  For every file you change or create, you MUST write or update at least one test that
  covers the changed behaviour. This is mandatory — not optional.

- doc: skip
- playwright: skip

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(audit): all 19 findings — NCO/BCD exchange gate, MCX LTP key, chain cold-cache,
AMO capability filter, IDB error UX, depthAvail inversion, CDS currencies,
MCX chart exchange, tooltip UTC date, resolveUnderlying CPO + discontinued cleanup

## Done when

1. `_EXCHANGES` includes NCO and BCD — NCO/BCD orders no longer return 400 (fix #1)
2. `_opl_price_from_broker` uses actual exchange in LTP key — MCX MARKET orders resolve (fix #2)
3. Chain cold-start returns structured response, not bare `[]` — off-market basket works (fix #3)
4. `_ticket_check_mcx_lot_cache` dead call removed or wired (fix #6)
5. `_CHAIN_QUOTES_CLOSED_CACHE` has LRU cap (fix #9)
6. Canonical gate bypass documented in chain_quotes (fix #10)
7. Options cache key default corrected (fix #19)
8. AMO filtered from dropdown for Dhan/Groww accounts (fix #5)
9. refreshKey spot fetch race guarded (fix #7)
10. IDB error shows user-visible error + retry (fix #8)
11. Comment updated to 30s (fix #15)
12. depthAvail uses `=== true` (fix #16)
13. (L) indicator rendered or dead CSS removed (fix #17)
14. CDS_CURRENCIES has all 4 pairs (fix #4)
15. Last-good bars guard actually restores bars (fix #11)
16. MCX chart fetch passes exchange (fix #12)
17. Tooltip date parses as local time (fix #13)
18. MCX_COMMODITIES has CPO, no discontinued contracts (fix #14)
19. `_KITE_INDEX_TO_ROOT` has SENSEX + BANKEX (fix #18)
20. pytest green, broker cov ≥ 80%, api cov ≥ 45%, svelte-check 0 errors
