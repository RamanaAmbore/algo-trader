# Plan: NavStrip P-slot Day P&L zero after MCX close + desktop ALGO/bull alignment

## Task
Two fixes:
1. **NavStrip P-slot 1 (Day P&L) shows 0 after MCX close** — the closed-hours snapshot path
   in `_positions_snapshot()` uses the wrong `close_price`. After MCX close, the broker sets
   `previous_close = today_settlement` and `ltp = today_settlement`, so the formula
   `pnl - oq*(close_price - avg)` collapses to 0. The fix is to use yesterday's LTP from
   `daily_book` as `close_price` instead of the snapshot's stale `previous_close` column,
   and to anchor the "previous batch" lookup on `captured_at` (not the `date` column, which
   has UTC/IST edge cases).
2. **Desktop ALGO text not vertically centred with bull** — `.algo-vert` in the desktop
   navbar has `align-self: stretch` making it full navbar height (48px), but relies on
   `align-items: center` inside a `writing-mode: vertical-lr` element, which centres
   horizontally (cross-axis in vertical writing mode), not vertically. The mobile version
   works because a wrapping `<div class="flex items-center">` gives correct centering.

## Agents
- backend: Fix `backend/api/routes/positions.py:_positions_snapshot()`:
  Merge the two separate SQL queries into one combined CTE query that includes a `prev_batch`
  CTE anchored on `captured_at < lb.max_at AND captured_at >= lb.max_at - INTERVAL '2 days'`
  (replacing the `date < :today_date` filter), and LEFT JOINs `prev_batch` into the main
  SELECT so `prev_ltp` and `prev_settlement_pnl` come back with each row directly.
  Then in the Python loop, swap the preference order at lines 158-162: use `prev_ltp`
  (yesterday's settlement from daily_book) FIRST; fall back to snapshot's `previous_close`
  only when `prev_ltp` is None. This fixes both the date-filter bug and the wrong-value bug.

- frontend: Fix `frontend/src/routes/(algo)/+layout.svelte` desktop navbar:
  In the desktop section (line 941 block, `hidden lg:flex items-center gap-1 h-12`), wrap
  `<span class="algo-vert">ALGO</span>` and `<button class="algo-brand">` together in
  `<div class="flex items-center h-full">`. This mirrors the mobile structure (line 1148
  wrapper div) which already works correctly. No CSS changes needed — the wrapper gives
  `algo-vert`'s `align-self: stretch` a same-height parent to stretch to, then both
  elements align by the parent's `items-center`.

- broker: skip
- doc: skip
- backend-test: Update `backend/tests/test_broker_connection_events.py` — no. Update
  `backend/tests/` — check if `test_snapshot_*` tests cover the `prev_ltp` preference path.
  If `test_snapshot_day_change_extras_fallback.py` exists, add a test case for the
  `prev_ltp > 0` branch to confirm `close_price = prev_ltp` (not snapshot `previous_close`)
  when both are present. Otherwise add it to the nearest positions-snapshot test file.
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(snapshot): use prev_ltp as close_price after MCX close; fix desktop ALGO/bull alignment

## Done when
1. `_positions_snapshot()` LEFT JOINs `prev_batch` CTE; `prev_close_val` prefers `prev_ltp`
2. Desktop navbar wraps ALGO + bull brand in `<div class="flex items-center h-full">`
3. pytest green (snapshot prev_ltp test passes)
4. svelte-check 0 errors

## Critical files
- `backend/api/routes/positions.py` lines 56–178 (full `_positions_snapshot` function)
- `frontend/src/routes/(algo)/+layout.svelte` lines 941–951 (desktop nav inner block)
