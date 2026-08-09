# Plan: fix(snapshot): MCX day_pnl lot-scale, holdings prev_batch, flat-row hygiene + stale code

## Context

Comprehensive audit of positions / holdings / snapshot / MCX vs equity surfaces surfaced:

**P1 — MCX `day_pnl` wrong for brand-new positions** (`daily_snapshot.py:481–506`, `positions_helpers.py:320–323`)
`_snap_compute_day_pnl` calls `decomposed_intraday_pnl(oq, ltp, cls, bq, bv, sv, sq)`.
For MCX (CRUDEOIL etc.), Kite ships `oq`/`bq`/`sq` in **LOTS** but `ltp`/`cls` in **per-unit** price
and `bv`/`sv` as **absolute ₹** (lot_size already multiplied in). All three formula components
are therefore off by `multiplier` (lot_size = 100 for CRUDEOIL). The error is masked for
overnight MCX positions that already have a prior `daily_book` row — `build_row_from_snapshot_raw`
recomputes `computed_day_pnl = (ltp - prev_ltp) * effective_qty` using correct contract qty.
Exposed only when `prev_close_val is None` (brand-new MCX position, no prior row) — fallback
hits the stored `day_pnl` which is off by 100×.

**P2 — Holdings snapshot has no yesterday-LTP anchor** (`holdings.py:39–56`)
`_HOLDINGS_SNAPSHOT_SQL` has no `prev_batch` CTE. `_build_holding_row_from_snapshot` falls
back to stored `day_pnl_f` when `previous_close IS NULL` — same stale-value risk as positions.
Closed-hours holdings show wrong day P&L.

**P3 — `_apply_flat_row_hygiene` inconsistency** (`positions.py:357–376`)
Zeros `day_change` and `day_change_percentage` for `qty=0` rows but NOT `day_change_val`.
Frontend reads `day_change_val` for NavStrip P-slot; a residual value from a closed position
shows phantom P&L.

**Stale code — `_is_broker_outage` duplicated 3×**
Identical function in `positions.py:168`, `holdings.py:226`, `funds.py:33`. Should live in one
shared helper (e.g., `backend/api/routes/_route_helpers.py` or existing `positions_helpers.py`).

**Stale code — `positionsPnlFiltered` not exported in `nav.js:206`**
Function is used by test scripts that import it — but it isn't exported. Either add the export
or confirm tests import via a re-export barrel. Minor; add export.

## Task

Fix all five findings in a single pass:
1. Pass `multiplier` (lot_size) from `payload_json` into `_snap_compute_day_pnl`; scale `oq`,
   `bq`, `sq` by multiplier before `decomposed_intraday_pnl` so stored `day_pnl` is in ₹.
2. Add `prev_batch` CTE to `_HOLDINGS_SNAPSHOT_SQL`; update `_build_holding_row_from_snapshot`
   to accept `prev_ltp` and compute day P&L from price diff × qty (mirror positions pattern).
3. In `_apply_flat_row_hygiene`, also set `day_change_val = 0.0` for `qty=0` rows.
4. Move `_is_broker_outage` to `positions_helpers.py`; remove the two duplicate definitions;
   update import in `funds.py` and `holdings.py`.
5. Export `positionsPnlFiltered` from `nav.js`.

Out of scope (separate plans):
- P2: `_fetch_positions_direct()` async refactor (complex background.py surgery)
- P2: Dhan MCX multiplier gap (requires Dhan fixture data investigation)

## Agents

- backend: Four changes in `backend/api/`:
    (a) `algo/daily_snapshot.py` — update `_snap_compute_day_pnl(r, ltp_val, close_price, qty, multiplier=1)`: multiply `oq`, `bq`, `sq` by `multiplier` before passing to `decomposed_intraday_pnl`; also multiply `qty_f` by `multiplier` for the `naive_day_pnl` fallback. Read `multiplier` from `r.get("multiplier", 1)` in `_positions_rows` and pass down via `_snap_position_eod_vals`.
    (b) `routes/positions.py` — in `_apply_flat_row_hygiene`, set `day_change_val=0.0` for `qty=0` rows (alongside existing day_change/day_change_percentage zeros).
    (c) `routes/holdings.py` — add `prev_batch` CTE to `_HOLDINGS_SNAPSHOT_SQL` (same shape as positions: find most-recent `daily_book` row per (account,symbol) BEFORE `latest_batch.max_at`, 7-day lookback). Update `_build_holding_row_from_snapshot` to unpack `prev_ltp` and use `(ltp - prev_ltp) * qty` as `day_pnl` when `prev_ltp > 0`. Update the SELECT column count / tuple unpacking.
    (d) `routes/positions_helpers.py` — move `_is_broker_outage` definition here (already has other helpers). Remove the body from `positions.py` and `holdings.py`; keep only an import alias if callers reference it locally. In `funds.py`, import from `positions_helpers` instead.

- frontend: `frontend/src/lib/data/nav.js` — add `export` keyword to `positionsPnlFiltered` (line ~206).

- broker: skip

- doc: Update `docs/specs/BROKER_SPEC.md` — add note on MCX lot-scale fix in §snapshot write path. Update `docs/specs/PULSE_SPEC.md` — mention holdings prev_batch CTE parity with positions.

- backend-test: Write/update tests covering:
    (1) `test_snap_compute_day_pnl_mcx_lot_scale` — verify stored `day_pnl` = 100 × naive result for CRUDEOIL-like row (multiplier=100)
    (2) `test_positions_rows_mcx_multiplier_passed_to_snap` — `_positions_rows` with MCX raw row containing `"multiplier": 100` produces correctly-scaled `day_pnl`
    (3) `test_holdings_snapshot_prev_batch_day_pnl` — mock DB returning prev_ltp row; `_build_holding_row_from_snapshot` uses price diff × qty
    (4) `test_apply_flat_row_hygiene_zeros_day_change_val` — `qty=0` row → `day_change_val == 0.0`
    (5) `test_is_broker_outage_single_import` — import from `positions_helpers`; verify positions.py, holdings.py, funds.py no longer define their own copy

- playwright: skip

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message

fix(snapshot): MCX day_pnl lot-scale, holdings prev_batch CTE, flat-row hygiene, _is_broker_outage SSOT

## Done when

- `_snap_compute_day_pnl` scales oq/bq/sq by multiplier; MCX CRUDEOIL test shows 100× correct day_pnl
- `_HOLDINGS_SNAPSHOT_SQL` has prev_batch CTE; closed-hours holdings day P&L computed from price diff
- `_apply_flat_row_hygiene` zeros `day_change_val` for qty=0 rows
- `_is_broker_outage` defined once in `positions_helpers.py`; removed from positions.py, holdings.py, funds.py
- `positionsPnlFiltered` exported in nav.js
- All 5 new pytest tests pass; svelte-check 0 errors
