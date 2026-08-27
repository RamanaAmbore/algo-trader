# Plan: Fix chain prices-poll hang + holdings day P&L snapshot bug

## Root cause — chain hang

`visibleInterval(withGuard(_refreshChainPrices), 5000)` fires `_loadPrices` every 5s.
`_loadPrices` calls `fetchChainQuotesPrices` → `broker.quote()` with 30s server timeout.
`_loadPrices` is fire-and-forget (not awaited), no in-progress guard.

Timeline: after 30s, there are 6 concurrent `asyncio.to_thread(broker.quote)` calls on the
server. Python's default thread pool executor has ~8–12 threads. After 8 polls (~40s), the
pool saturates. New `asyncio.to_thread()` calls block (including ALL other routes). The
server event loop stalls → all requests time out → browser appears completely frozen.

## Root cause — holdings day P&L oscillation

NSE closes at 15:35 IST but MCX closes at 00:30 IST. The holdings gate
`closed_hours_or_broker(exchange="NSE")` calls `is_any_segment_open()` (all segments), so
it stays on the **broker path** while MCX is open (15:35–00:30). After 00:30, it switches
to the **snapshot path**. Around the MCX close, requests alternate between the two paths →
two different `day_change_pct` values → oscillation visible in the UI.

The snapshot path reads `db.previous_close` from `daily_book`. The UPSERT rolling rule in
`daily_snapshot.py` sets `previous_close = old_ltp` every time ltp changes intraday. By
day-end, `db.previous_close` has drifted far from yesterday's true settlement LTP. The
broker path correctly calls `_override_stale_close_for_holdings` (queries
`daily_book.ltp WHERE captured_at < today_08:00`) → correct day P&L. The snapshot path
does NOT call this function → uses the drifted `db.previous_close` → wrong day P&L.

Secondary bug: `_recompute_day_change_pct` uses `close_price` as the percentage denominator
but `day_change_val` uses `previous_close` as the reference. When they diverge (epsilon
condition gates `close_price` sync in `_override_stale_close_for_holdings`), the displayed
percentage is inconsistent with the absolute day P&L value.

## Fixes

### Fix 1 — Add `_pricesFetching` guard to `_loadPrices`
**File**: `frontend/src/lib/order/OptionChainTab.svelte`

Add `let _pricesFetching = $state(false)`.

In `_loadPrices`:
```javascript
async function _loadPrices(u = '', e = '') {
  if (_pricesFetching) return;               // ← guard: skip if already in flight
  _pricesFetching = true;
  try {
    // ... existing body unchanged
  } finally {
    _pricesFetching = false;
  }
}
```

This caps concurrent server-side `broker.quote()` threads at 1 regardless of poll frequency.

### Fix 2 — Increase prices poll interval from 5s → 30s
**File**: `frontend/src/lib/order/OptionChainTab.svelte`

Change `visibleInterval(withGuard(_refreshChainPrices), 5000)` → `30000`.

Option prices don't meaningfully change in 5s intervals. 30s is sufficient and matches
the server's broker timeout, ensuring the previous fetch always completes before the
next one starts (belt-and-suspenders with Fix 1).

### Fix 3 — Reduce `asyncio.wait_for` timeout from 30s back to 12s
**File**: `backend/api/routes/options.py`

kiteconnect's `_default_timeout = 7` means `broker.quote()` can take AT MOST
7s of HTTP time + rate limiter wait (~3s worst case) = ~10s total.
A 30s timeout adds 20s of unnecessary thread hold time.
Change `timeout=30.0` → `timeout=12.0`. Still safe, never falsely fires.

### Fix 4 — Holdings gate: use NSE-specific segment check
**File**: `backend/api/helpers/snapshot_gate.py`

`is_any_segment_open()` returns True while MCX is open (until 00:30 IST), keeping the
holdings route on the broker path long after NSE equities are settled. Holdings are NSE
equities — they should enter snapshot mode when NSE closes (~15:35), not at MCX close.

In `closed_hours_or_broker` (or `_any_segment_open`), add an optional `exchanges`
parameter (default `None` = all). When provided, filter which segments are checked.
The holdings route passes `exchanges=["NSE"]` so the gate fires at NSE close.

Alternatively, expose a standalone `is_nse_open()` helper and use it directly in the
holdings route handler before calling `closed_hours_or_broker`.

### Fix 5 — Snapshot path: call `_override_stale_close_for_holdings` after building rows
**File**: `backend/api/routes/holdings.py`

In the snapshot branch of the holdings route handler (when `source="snapshot"`), call
`_override_stale_close_for_holdings(rows, session)` after building rows from
`_build_holding_row_from_snapshot`. This patches `previous_close` with the true
yesterday's settlement LTP (from `daily_book.ltp WHERE captured_at < today_08:00`)
in both paths — not just the broker path.

Currently the snapshot path skips this function entirely, leaving the drifted
`db.previous_close` as the reference for `day_change_val`.

### Fix 6 — Always sync `close_price = ref_close` in `_override_stale_close_for_holdings`
**File**: `backend/api/routes/holdings.py`

Remove the epsilon condition `abs(ref_close - current_close) > 0.005` that gates whether
`close_price` is patched. Always set `close_price = ref_close` so that
`_recompute_day_change_pct` (which uses `close_price` as the percentage denominator)
is consistent with `day_change_val` (which uses `previous_close` = `ref_close`).
When `close_price ≠ previous_close`, the displayed day% is wrong relative to the day P&L.

### Fix 7 — Positions: per-row exchange gating for NFO/NSE closed rows
**File**: wherever `_overlay_snapshot_for_closed_exchanges` lives (holdings.py or snapshot_gate.py)

Problem: NFO (options/futures on NSE) closes ~15:30 IST but MCX stays open until 00:30.
During 15:30–00:30, the positions gate stays on the broker path (MCX keeps
`is_any_segment_open()` True). `_overlay_snapshot_for_closed_exchanges` already patches
`last_price` and `cur_val` for closed-exchange rows, but does NOT fix `day_change_val`
or `day_change_pct`. Result: NFO position rows show stale/wrong day% until MCX closes.

A portfolio-wide exchange filter is wrong here — MCX positions are still live. Fix per-row:
in `_overlay_snapshot_for_closed_exchanges`, for each row whose exchange is closed, compute:

```python
ref_close = daily_book.ltp WHERE account=row.account AND symbol=row.symbol
            AND captured_at < today_08:00 ORDER BY captured_at DESC LIMIT 1
day_change_val = (snap_ltp - ref_close) × qty   # snap_ltp already overlaid
day_change_pct = day_change_val / abs(ref_close × qty) × 100
close_price = ref_close                          # keep denominator consistent
```

This means NFO rows get correct `day_change_val`/`day_change_pct` immediately after NFO
closes (15:30), while MCX rows continue on live broker data unaffected. The existing
`latest_snapshot_ltp_map("positions")` call already provides `snap_ltp`; add a parallel
`ref_close_map` query (same pattern as `_override_stale_close_for_holdings`) to get the
settlement LTP for closed-exchange rows only.

---

## Agents
- frontend: Fix 1 + Fix 2 — In `OptionChainTab.svelte`:
  (1) Add `let _pricesFetching = $state(false)` state variable.
  (2) Wrap the body of `_loadPrices` with `if (_pricesFetching) return;` guard at entry,
  `_pricesFetching = true` before the try, and `_pricesFetching = false` in the finally.
  (3) Change the `visibleInterval(..., 5000)` that calls `_refreshChainPrices` to `30000`.
  Do NOT change any other intervals or logic. Update the existing Playwright test
  (chain_tab_api_driven.spec.js Test 9) to reflect 30s poll interval if it asserts timing.
- backend: Fix 3 + Fix 4 + Fix 5 + Fix 6 —
  (a) `backend/api/routes/options.py`: change `timeout=30.0` → `timeout=12.0` in
  `_chain_quotes_batch_quote`. Update warning log to say "12s". Update test in
  `test_chain_quotes.py` that asserts the literal timeout string.
  (b) `backend/api/helpers/snapshot_gate.py`: add an `exchanges` keyword parameter to
  `_any_segment_open()` (default `None` = all segments). When provided, filter segments
  to only those whose exchange name is in the list. Update `closed_hours_or_broker` to
  accept and forward an optional `segment_exchanges` kwarg. Holdings route passes
  `segment_exchanges=["NSE"]`. Write test asserting NSE-only gate fires when NSE is
  closed but MCX is still open (mock `_any_segment_open` / segment schedule).
  (c) `backend/api/routes/holdings.py`: in the snapshot path (after
  `_build_holding_row_from_snapshot`), call `_override_stale_close_for_holdings(rows, session)`.
  Also in `_override_stale_close_for_holdings`, remove the epsilon condition gating
  `close_price` sync — always set `close_price = ref_close` unconditionally.
  Write/update pytest tests covering:
  - Snapshot path calls `_override_stale_close_for_holdings` (mock and verify)
  - `close_price` always equals `ref_close` after the override (no epsilon gate)
  - NSE-only gate fires correctly when MCX is open but NSE is closed
  (d) Extend `_overlay_snapshot_for_closed_exchanges` (positions broker path): for each
  row whose exchange is closed, also compute and overlay `day_change_val`, `day_change_pct`,
  and `close_price` using `ref_close` from `daily_book.ltp WHERE captured_at < today_08:00`.
  Build a `ref_close_map` (account, symbol) → ltp using the same query pattern as
  `_override_stale_close_for_holdings`. Apply only to closed-exchange rows; open MCX rows
  are untouched. Add pytest test: mock one NFO row (exchange closed) + one MCX row (open);
  assert NFO row gets patched `day_change_val`/`day_change_pct` and MCX row does not.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(chain+holdings): chain hang guard + 30s poll + 12s timeout; holdings day% oscillation after MCX close

## Done when
1. `_pricesFetching` guard in `_loadPrices` prevents concurrent broker.quote() calls
2. Prices poll at 30s (was 5s)
3. `asyncio.wait_for(timeout=12.0)` in `_chain_quotes_batch_quote` (was 30.0)
4. Holdings route gate uses NSE-only segment check → snapshot served at 15:35 IST not 00:30
5. Snapshot path calls `_override_stale_close_for_holdings` → correct `previous_close`
6. `close_price` always synced to `ref_close` → day% denominator consistent with day P&L value
7. Positions broker path: NFO rows get correct `day_change_val`/`day_change_pct` after NFO close; MCX rows unaffected
8. pytest passes; svelte-check 0 errors
