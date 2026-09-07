# Plan: Fix stale prev_close — daily_book.ltp for holdings AND positions

## Context

Holdings day P&L% shows 100% for HFCL, E2E, and any holding where
`daily_book.previous_close = 0` (Kite BHAV copy not yet distributed or missing).

`_override_stale_close_for_holdings` in `holdings.py` queries
`COALESCE(daily_book.previous_close, ltp)` as `ref_close`. When `previous_close = 0`,
COALESCE returns 0. The epsilon check `|0 − close_price| ≤ 0.005` passes → no patch →
`close_price = 0` propagates → day P&L% = 100%.

Same root cause can hit positions: `_override_stale_close_from_snapshot` in `positions.py`
must also use `daily_book.ltp` directly (not `COALESCE(previous_close, …)`).

Fix for both: use `daily_book.ltp` (our own settlement snapshot, reliable) as `ref_close`
instead of Kite's `previous_close` / BHAV copy. Same invariant documented in CLAUDE.md:
"Never use daily_book.previous_close. Use daily_book.ltp."

## Agents
- backend: Apply `daily_book.ltp` fix to BOTH `_override_stale_close_for_holdings`
  (holdings.py) AND verify/fix `_override_stale_close_from_snapshot` (positions.py).
  Add pytest covering the 0-prev_close case for holdings; verify positions test exists.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: skip (backend agent writes tests)
- playwright: skip

## Fix — holdings.py + positions.py

**Files:**
- `backend/api/routes/holdings.py` — `_override_stale_close_for_holdings`
- `backend/api/routes/positions.py` — `_override_stale_close_from_snapshot`

In both functions, find the query that computes `ref_close`. Change any occurrence of:
```sql
COALESCE(daily_book.previous_close, ...)
-- or: daily_book.previous_close
```
to:
```sql
daily_book.ltp
```

`daily_book.ltp` = the LTP we captured at settlement from our own snapshot
(`captured_at < 08:00 IST`, DESC per account+symbol). Reliable regardless of Kite's
BHAV distribution schedule.

Add a pytest that mocks a holding row with `close_price = 0` and
`daily_book.ltp = <valid settlement price>`, and asserts:
- returned holding has patched `close_price` = `daily_book.ltp`
- day P&L% is not 100%

Verify an equivalent test already covers the positions path; add one if missing.

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(holdings+positions): use daily_book.ltp (not COALESCE previous_close) as stale close_price reference

## Done when
1. HFCL and E2E (and any holding/position with previous_close=0) show correct day P&L%
2. Holdings and positions day P&L totals are accurate
3. pytest passes with new/updated tests covering the 0-prev_close case
