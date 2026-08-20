# Plan: fix(holdings): 5 holdings/positions day P&L bugs — qty priority, previous_close NULL, short case2, Polars two-pass

## Context

Audit identified 5 bugs causing holdings to show 0 day P&L and wrong holding values.
None from the last commit (chain tab fix). Introduced across May–Aug commits.

Root causes:
- P1-A/P1-C: `opening_quantity` used before `quantity` in two places (May + Jul)
- P1-B: `previous_close = NULL` in DB → fallback `close_price = ltp` → post-settlement guard zeros P&L (Aug 15)
- P2-A: Case 2 backstop misses short overnight positions — `_oq > 0` should be `_oq != 0` (Aug 16)
- P2-B: CC refactor left `cur_val` reading `pl.col("pnl")` in the same `with_columns` pass — fails for Dhan/Groww where `pnl` column doesn't exist in input frame (Aug 19)

## Agents

- backend: Fix P1-A, P1-B, P2-A in three files (see detail below)
- broker: Fix P2-B in `backend/brokers/broker_apis.py` — split Polars into two passes (see detail below)
- frontend: Fix P1-C in `frontend/src/lib/data/pulseUnified.js:526`
- backend-test: Add tests covering all five fixes (see test detail below)
- playwright: skip

## Fix detail

### P1-A — `backend/api/algo/daily_snapshot.py:468`
```python
# BEFORE (wrong — opening_quantity = pre-sell qty for partial sells)
"qty": int(r.get("opening_quantity") or r.get("quantity") or 0),
# AFTER
"qty": int(r.get("quantity") or r.get("opening_quantity") or 0),
```

### P1-B — `backend/api/algo/daily_snapshot.py:473–476`
Add `ltp_val` as last-resort fallback so `previous_close` is never NULL on insert.
NULL in DB means `_build_holding_row_from_snapshot` falls back `close_price = ltp`,
triggering the `|ltp−close| ≤ 0.005` guard → routes to stale `day_change_val = 0`.
```python
# BEFORE
"previous_close": (
    (prev_ltp_map or {}).get((account, symbol, "holdings"))
    or (float(r["close_price"]) if r.get("close_price") else None)
),
# AFTER — ltp_val as last resort so the column is never NULL
"previous_close": (
    (prev_ltp_map or {}).get((account, symbol, "holdings"))
    or (float(r["close_price"]) if r.get("close_price") else None)
    or ltp_val
),
```
Note: `ltp_val` as fallback means same-day buys get `previous_close = ltp` — day P&L = 0
which is correct (no prior session exists). Better than NULL which breaks the formula path.

### P2-A — `backend/api/algo/pnl_math.py:209`
```python
# BEFORE — misses short overnight positions (oq < 0)
_case2 = (_oq > 0) & (_dcv == 0) & (_pnl != 0) & (_cls > 0) & (_avg > 0)
# AFTER
_case2 = (_oq != 0) & (_dcv == 0) & (_pnl != 0) & (_cls > 0) & (_avg > 0)
```
Frontend already uses `oq !== 0` correctly in `nav.js:108`. This aligns backend.

### P2-B — `backend/brokers/broker_apis.py` — split `_enrich_holdings` into two Polars passes

`_build_holdings_curval_exprs` uses `pl.col("pnl")`. Polars evaluates all expressions in a
single `with_columns` call against the **input** frame — not sibling aliases.
When `has_pnl=False` (Dhan/Groww), `pnl` doesn't exist in the input frame → ColumnNotFoundError.

Fix in `_enrich_holdings` (~line 1692): split into two `with_columns` passes:

**Pass 1** — pnl only (must land in the frame before pass 2 reads it):
```python
lf = pl.from_pandas(df, nan_to_null=True)
if has_ltp and has_avg and has_qty:
    lf = lf.with_columns([_build_holdings_pnl_expr(lf, has_pnl)])
    has_pnl = True  # now genuinely in the frame
```

**Pass 2** — everything else (cur_val, pnl_percentage, price_change, day_change_val):
```python
exprs2 = _build_holdings_computed_exprs(
    lf, has_ltp, has_avg, has_qty, has_close,
    has_pnl, has_invval, has_dcv, cols,
)
if exprs2:
    lf = lf.with_columns(exprs2)
_enrich_holdings_writeback(df, lf)
```

Also update `_build_holdings_computed_exprs` to remove the pnl append block (lines 1611–1613)
since pass 1 now handles it. The helper should only build pass-2 exprs.

### P1-C — `frontend/src/lib/data/pulseUnified.js:526`
```js
// BEFORE
const heldQty = Number(r.opening_quantity) || Number(r.quantity) || 0;
// AFTER
const heldQty = Number(r.quantity) || Number(r.opening_quantity) || 0;
```

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

### Test detail (backend-test agent)

New file **`backend/tests/broker/test_holdings_pnl_bugs.py`**:

1. `test_daily_snapshot_qty_uses_quantity_not_opening_quantity` — partial-sell holding where
   `quantity=50, opening_quantity=100`; assert snapshot row written with `qty=50`

2. `test_daily_snapshot_previous_close_never_null` — holding with no prev_ltp_map entry and
   `close_price=0`; assert `previous_close = ltp_val` (not None)

3. `test_enrich_holdings_two_pass_dhan_no_pnl_column` — DataFrame without `pnl` column
   (Dhan path); assert `_enrich_holdings` completes without error and `cur_val > 0`

4. `test_case2_backstop_fires_for_short_overnight` — raw df with
   `overnight_quantity=-10, day_change_val=0, pnl=-500, close_price=100, average_price=105`;
   assert `apply_day_change_backstop` sets `day_change_val != 0`

## Commit message
fix(holdings): qty priority + previous_close NULL + short case2 + Polars two-pass

## Done when
- `venv/bin/python -m radon cc backend/ -s -n D 2>/dev/null` — still empty (no CC regression)
- 5348 tests pass (no regressions); 4 new tests added
- `opening_quantity` not used before `quantity` in daily_snapshot.py or pulseUnified.js
- `previous_close` never NULL in daily_book writes
- `pnl_math.py` Case 2 uses `_oq != 0`
- `_enrich_holdings` does two Polars passes; no ColumnNotFoundError for Dhan/Groww
