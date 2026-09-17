# Plan: Remove two-CTE — fix prev_close == ltp bug for positions + holdings

## Context

`_fetch_snapshot_close_map` (positions.py) has two query paths gated on `is_market_active_for_prev_close()`:
- True → single-query: `DISTINCT ON … ORDER BY captured_at DESC` before today_08 → correct
- False → two-CTE: skips the latest row and returns the one before it → **causes the bug**

The two-CTE was introduced to handle restart snapshots displacing correct EOD data on weekends/holidays. But `_task_daily_snapshot` already skips startup snapshot writes on weekends (lines 2089–2105) and holidays (lines 2106–2113). The only startup writes that fire are on trading days between 00:30–08:00 IST — and at that time MCX has closed (23:30 IST), so the tick buffer holds Wednesday's settlement LTP = correct prev_close. The two-CTE is solving a problem that no longer exists.

**Consequence of two-CTE on Thursday 07:19 IST (trading day, pre-08:00):**
1. `is_market_active_for_prev_close()` → False → two-CTE fires
2. `latest_batch` = Wednesday 23:45 settlement snapshot; `prev_batch` = nothing (CRUDEOIL rolled — only one row)
3. `snapshot_map` empty for CRUDEOIL
4. `_apply_second_pass_fallback` reads `daily_book.previous_close` from Wednesday 23:45 row — this column was written from Kite's `close_price` at 23:45 IST, before BHAV publishes at 00:15 IST, so `previous_close ≈ ltp`
5. Result: `previous_close == ltp` → day P&L = 0

## Task

Gainers/losers/watchlist/pinned use broker live-quote `ohlc.close` directly — BHAV is published by 00:15 IST, those surfaces are not affected. Only **positions** and **holdings** use `daily_book` for prev_close override, and both have the two-CTE bug.

### `backend/api/routes/positions.py`

**1. Remove two-CTE from `_fetch_snapshot_close_map` (lines 966–993)**

Replace the `if is_market_active_for_prev_close() … else …` with a single unconditional query. Remove the two-CTE branch and update the docstring.

```python
# After fix — one path only:
result = await session.execute(_sql_text("""
    SELECT DISTINCT ON (account, symbol)
           account, symbol,
           daily_book.ltp AS ref_close,
           total_pnl
    FROM daily_book
    WHERE kind = 'positions'
      AND ltp IS NOT NULL AND ltp > 0
      AND captured_at < :today_08
    ORDER BY account, symbol, captured_at DESC
"""), {"today_08": today_08})
```

Check if `is_market_active_for_prev_close` is used elsewhere in positions.py before removing the import.

**2. Fix `_apply_second_pass_fallback` (lines 1063–1068) to read `daily_book.ltp` instead of `daily_book.previous_close`**

```python
# Change to:
SELECT DISTINCT ON (account, symbol) account, symbol, ltp AS previous_close
FROM daily_book
WHERE kind = 'positions'
  AND ltp IS NOT NULL AND ltp > 0
  AND symbol = ANY(:syms)
ORDER BY account, symbol, captured_at DESC
```

Update docstring to reflect `ltp` is the source.

### `backend/api/routes/holdings.py`

**3. Remove two-CTE from `_override_stale_close_for_holdings` (lines ~461–507)**

Same pattern as positions: replace `if is_market_active_for_prev_close() … else …` with the single-query path only (`kind = 'holdings'`). Remove the two-CTE branch and update the docstring. Check if `is_market_active_for_prev_close` is used elsewhere in holdings.py before removing its import.

## Agents

- backend: Apply all three changes above. Files: `backend/api/routes/positions.py` (lines 919–1084) and `backend/api/routes/holdings.py` (lines ~414–507). See exact changes in Task section.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: Add/update tests covering:
  - `backend/tests/test_positions_route.py`: `_fetch_snapshot_close_map` with single-row symbol (new contract) — snapshot_map populated correctly (regression: two-CTE returned empty); `_apply_second_pass_fallback` reads `ltp` not `previous_close` (mock row with `previous_close != ltp`)
  - `backend/tests/test_holdings_route.py` (or equivalent): `_override_stale_close_for_holdings` with single-row symbol — snapshot_map populated correctly; no two-CTE branch
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(positions,holdings): remove two-CTE snapshot query — use single-query always; fix second-pass to read ltp not previous_close

## Done when

- `_fetch_snapshot_close_map` (positions) has one query path, no `is_market_active_for_prev_close` branch
- `_apply_second_pass_fallback` reads `ltp` not `previous_close`
- `_override_stale_close_for_holdings` has one query path, no `is_market_active_for_prev_close` branch
- New backend tests pass for both surfaces
- Full pytest suite green
