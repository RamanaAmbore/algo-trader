# Plan: Fix holdings snapshot previous_close not stored — day % uses wrong denominator

## Task
`daily_snapshot.py:_holdings_rows()` at line 446 stores `"previous_close": None` for every
holdings row written to `daily_book`. The positions path (line 548) correctly stores
`float(r["close_price"])` — Kite's prior-session official settlement price. Because
`previous_close` is always NULL for holdings, `_build_holding_row_from_snapshot()` in
`holdings.py` falls back to the `avg_cost` denominator instead of yesterday's close, producing
inflated day-% values for holdings bought at a low cost basis (e.g. SIEMENS, WAAREEENER).

One-line fix in `daily_snapshot.py` + a pytest covering the corrected snapshot write.

## Agents

- backend: Fix `backend/api/algo/daily_snapshot.py` line 446:
  Change `"previous_close": None` to
  `"previous_close": float(r["close_price"]) if r.get("close_price") else None`
  This mirrors the positions path at line 548. No other changes needed.
  File: `backend/api/algo/daily_snapshot.py`

- frontend: skip
- broker: skip
- doc: skip

- backend-test: Add a pytest in `backend/tests/test_holdings_fetch_helpers.py` (already exists)
  or in a suitable existing test file. The test must verify that:
  1. `_holdings_rows()` from `daily_snapshot.py` stores `previous_close` = Kite's `close_price`
     when `close_price` is present in the broker row (not None)
  2. When `close_price` is absent/zero, `previous_close` is stored as None
  Check what test infrastructure exists for `daily_snapshot.py` before writing — look for
  existing tests that mock `_holdings_rows` or `_snap_holding_eod_vals`.
  Files: `backend/tests/test_holdings_fetch_helpers.py` or adjacent file

- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(snapshot): store previous_close from Kite close_price in holdings snapshot — fixes day-% denominator

## Post-deploy DB backfill
After deploy, run on prod to fix existing NULL previous_close for today's holdings rows:
```sql
UPDATE daily_book
SET previous_close = (payload_json->>'close_price')::NUMERIC
WHERE kind = 'holdings'
  AND previous_close IS NULL
  AND payload_json->>'close_price' IS NOT NULL
  AND (payload_json->>'close_price')::NUMERIC > 0;
```
Then trigger a fresh snapshot so the updated values flow through to the closed-hours response.

## Done when
- `daily_book` holdings rows have `previous_close` = Kite's prior-session close (not NULL)
- `day_change_percentage` in closed-hours holdings snapshot uses close-based denominator
- pytest passes for new test + existing holdings test suite
- DB backfill run on prod + snapshot refreshed so page shows correct day %
