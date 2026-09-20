# Plan: Unify prev_close — eliminate close_price across the stack

## Context
`close_price` (broker API field) and `previous_close` (DB column, API response field) are
the same concept — prior session's settlement price. Two names for one thing creates
confusion and dual fallback chains. Unify to a single name: `prev_close`, matching how the
frontend store already names it internally.

**After this change:**
- `daily_book` columns: `prev_close` (was `previous_close`), `prev_close_backup` (was `previous_close_backup`)
- All broker adapter DataFrames: `prev_close` column (was `close_price`)
- API response schemas: single `prev_close: float` field (replaces both `close_price` and `previous_close`)
- Frontend reads only `p?.prev_close` — no fallback chain needed

## Agents

### broker
In `backend/brokers/`:

**`broker_apis.py`**: All references to `'close_price'` column in DataFrames → `'prev_close'`.
Includes `_col_f64("close_price")`, `_cols = ("day_change", "day_change_val", "close_price", ...)`,
`_stale_ltp_mask()` close-price equality check, `_bmd_patch_one_row()` / `_bmd_patch_rows()` patching,
backfill helpers. Also remove `close_price` from any output column lists and use `prev_close`.

**`adapters/dhan.py`**: Output dicts use `"close_price": close_price` → `"prev_close": close_price`
(lines ~1736, ~1750 for holdings and similar for positions).

**`adapters/groww.py`**: Output dicts use `"close_price": close` → `"prev_close": close`
(lines ~1493, ~1526 for holdings/positions).

**`adapters/kite.py`** (or wherever Kite positions/holdings are normalized): rename `close_price` →
`prev_close` in output DataFrame columns.

### backend
In `backend/api/`:

**`models.py`**: Rename `DailyBook.previous_close` → `DailyBook.prev_close` and
`DailyBook.previous_close_backup` → `DailyBook.prev_close_backup`.

**`database.py`**: Add migration function `_migrate_daily_book_rename_prev_close()` that runs:
```sql
ALTER TABLE daily_book RENAME COLUMN previous_close TO prev_close;
ALTER TABLE daily_book RENAME COLUMN previous_close_backup TO prev_close_backup;
```
Guard with `IF EXISTS` / check via `information_schema.columns`. Register in the startup
migration runner alongside existing migrations.

**`schemas.py`**: In `PositionRow` and `HoldingRow`:
- Remove `close_price: float` field
- Rename `previous_close: float = 0.0` → `prev_close: float = 0.0`

**`routes/positions.py`**:
- All `raw['close_price']` / `raw.at[idx, 'close_price']` → `raw['prev_close']`
- All `raw['previous_close']` → `raw['prev_close']`
- SQL queries on `daily_book`: `NULLIF(close_price, 0)` → remove (column doesn't exist; use only `ltp`)
  - `_fetch_snapshot_close_map` line 946/950: `COALESCE(NULLIF(ltp,0), NULLIF(close_price,0))` → just `NULLIF(ltp,0)` (close_price never existed in daily_book)
  - `_positions_snapshot` line 277: already uses `COALESCE(NULLIF(db.previous_close,0), NULLIF(db.close_price,0))` → `db.prev_close` (direct, no COALESCE needed; prev_close IS the correct column)
  - `_apply_second_pass_fallback` lines ~1027-1030: same cleanup
- Update `_ROW_COLS` to remove `close_price`, add `prev_close`
- `_patch_close_from_snapshot_map()`: update field names

**`routes/holdings.py`**:
- All `raw['close_price']` → `raw['prev_close']`
- All `raw['previous_close']` → `raw['prev_close']`
- `_override_stale_close_for_holdings()`: update field names throughout
- Update output column list

**`algo/pnl_math.py`**: All `close_price` references → `prev_close`.

**`algo/daily_snapshot.py`**:
- Line 72: `"prev_close": _f(r.get("close_price"))` → `"prev_close": _f(r.get("prev_close"))`
  (broker adapters now output `prev_close` directly)
- All other `close_price` references → `prev_close`

**`background.py`**: All `close_price` references → `prev_close`. Update `_fetch_settlement_map()`
to look for `prev_close` column in DataFrames.

**`algo/lot_ledger.py`**: `close_price` → `prev_close`.

### frontend
In `frontend/src/lib/`:

**`data/portfolioStore.svelte.js`**:
- Line 85: `Number(p?.previous_close) || Number(p?.close_price) || null` → `Number(p?.prev_close) || null`
- Line 226 (holdings): `Number(h?.previous_close) || Number(h?.close_price) || Number(h?.ohlc?.close) || null` → `Number(h?.prev_close) || null`

**`data/marketDataStores.svelte.js`**:
- Lines 105, 135: `close: r.close_price` — `close_price` here is from OHLC market data (not position prev_close). Leave as-is OR rename if this is the same field. Check context before changing.

**`derivatives/pageLoad.js`**:
- Lines 75, 110: `prev_close: Number(p?.previous_close) || Number(p?.close_price) || null` → `prev_close: Number(p?.prev_close) || null`

**`data/nav.js`**: Check and update any `close_price` / `previous_close` references.

### backend-test
Update ALL test files that reference `close_price`, `previous_close` in the context of
positions/holdings DataFrames or API schemas. Key files:
- `test_positions_prev_close.py` — update all SQL assertions to use `prev_close` not `previous_close`/`close_price`
- `test_coalesce_to_ltp_fix.py` — update SQL pattern assertions
- `test_market_window_pnl_edge_cases.py` — update `_make_position_df` / `_make_holding_df` fixtures (rename `close_price` param → `prev_close`, `previous_close` param → remove or merge)
- `test_holdings_snapshot_fixes.py` — rename `close_price` → `prev_close`
- Any other test that passes `close_price=` or `previous_close=` to position/holding fixtures
Do NOT change tests for market data OHLC (those `close` / `close_price` fields are different)

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
refactor(prev_close): unify close_price + previous_close → prev_close across stack; DB migration

## Done when
- `daily_book` table has `prev_close` column (migration runs on startup)
- All broker adapter DataFrames output `prev_close` instead of `close_price`
- API response has single `prev_close` field; `close_price` removed from schemas
- Frontend reads `p?.prev_close` only — no fallback chain
- `daily_book` SQL uses `prev_close` directly (no `close_price` references)
- pytest + svelte-check green
