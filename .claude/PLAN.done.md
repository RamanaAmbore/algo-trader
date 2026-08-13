# Plan: Fix H slot (holdings day P&L) showing 0 after market close/settlement

## Context

After NSE settlement (~16:00 IST), Kite resets `last_price = close_price = settlement_price`
and `day_change_val = 0` for holdings. After 15:30, `closed_hours_or_broker` switches to the
snapshot path. The snapshot's `day_change_val` is supposed to reconstruct the correct figure
using `previous_close` from daily_book — but a priority bug causes it to use `prev_ltp` (from
the previous intraday snapshot, seconds apart) instead. Frontend `_liveHoldingsToday` falls
back to this near-zero `day_change_val` when symbolStore has no LTP for the holding.

P slot is correct: positions use `prev_settlement_pnl` via `_backfill_prev_settlement_pnl`.
Holdings have no equivalent — the snapshot reconstruction is the only source.

## Agents

- backend: Fix `_build_holding_row_from_snapshot` in `backend/api/routes/holdings.py` (lines 126–132). Swap priority so `previous_close` is used FIRST (mirrors positions_helpers.py lines 321–336). After the fix: `if previous_close_f > 0: day_change_val = (ltp_f - previous_close_f) * qty_i; elif prev_ltp_f is not None: day_change_val = (ltp_f - prev_ltp_f) * qty_i; else: day_change_val = day_pnl_f`. Also update comment at lines 120–126 to reflect the new priority rationale (matches positions_helpers.py comment).
- frontend: Fix `_liveHoldingsToday` in `frontend/src/lib/PositionStrip.svelte` (lines 426–440). When symbolStore has no LTP (`getSnapshot(sym)?.ltp = null`), use `h.last_price` as fallback before going to `day_change_val`. Add `Math.abs(holdLtp - close) > 0.005` guard so the formula is skipped when ltp ≈ close (post-settlement on live path — prevents computing (settlement - settlement) * qty = 0). After those two guards: fall to `day_change_val` from row (which after backend fix is `(today_ltp - previous_close) * qty` = correct).
  New logic:
  ```js
  const snapLtp = getSnapshot(sym)?.ltp;
  const holdLtp = (snapLtp != null && snapLtp > 0) ? snapLtp : Number(h?.last_price ?? 0);
  const close   = Number(h?.close_price || 0);
  const qty     = Number(h?.opening_quantity || h?.quantity || 0);
  const dcv     = Number(h?.day_change_val ?? 0);
  if (holdLtp > 0 && close > 0 && qty !== 0 && Math.abs(holdLtp - close) > 0.005) {
    s += (holdLtp - close) * qty;
  } else {
    s += dcv;
  }
  ```
- broker: skip
- doc: skip
- backend-test: Add test in `backend/tests/broker/` or `backend/tests/` (whichever is appropriate) verifying that `_build_holding_row_from_snapshot` uses `previous_close` over `prev_ltp` when both are available. Specifically: `previous_close=1000, prev_ltp=1002, ltp=1005, qty=10` → `day_change_val = (1005-1000)*10 = 50` not `(1005-1002)*10 = 30`.
- playwright: skip

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(holdings): snapshot day_change_val uses previous_close over prev_ltp; _liveHoldingsToday falls back to h.last_price

## Done when
- `_build_holding_row_from_snapshot` uses `previous_close` first when available
- `_liveHoldingsToday` uses `h.last_price` when symbolStore has no LTP
- H slot shows correct day P&L after NSE closes / settlement
- pytest passes; svelte-check 0 errors
