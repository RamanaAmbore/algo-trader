# Plan: prev_close pipeline — remove settlement snapshots, 08:00 fixup for both MCX+NSE

## Context

Multiple `previous_close` bugs traced to settlement snapshots (16:15 NSE, 00:15 MCX) creating
date/timing complexity that corrupts the reference price pipeline. Simplify: one snapshot per
exchange at close, prev_close updated at 08:00 next trade day for both.

---

## Proposed Architecture

### One rule for all exchanges — no MCX special-casing

| Step | When | All exchanges (MCX and non-MCX identical) |
|---|---|---|
| Close snapshot | NSE 15:45 / MCX 23:45 IST | Captures `ltp` (EOD price). Sets `previous_close = broker.close_price` (prior session's settlement already in Kite) |
| Settlement snapshot | — | **REMOVED for both** |
| 08:00 fixup | Next trading day 08:00 IST | Calls broker API once for all positions + holdings. Reads `close_price` (BHAV for NSE, MCX official settlement for MCX — both available at 08:00). Writes `previous_close` uniformly. No MCX-specific logic. |

**Settlement price source — Kite's `close_price` field:**
At 08:00 IST, `positions.close_price` and `holdings.close_price` from Kite carry the official
settlement price for all exchanges:
- NSE: BHAV-confirmed settlement (Zerodha loads at ~08:00 IST)
- MCX: official 00:15 settlement (available in Kite API well before 08:00)

One broker API call at 08:00 covers both. No exchange-specific branching.

**Snapshot time `previous_close` (initial value):**
At snapshot time (15:45 / 23:45), Kite's `close_price` = prior session's settlement (set at the
PREVIOUS 08:00, unchanged during the current session). Use this directly as `previous_close` for
new rows — no pre-load query from daily_book needed.

**08:00 fixup** (edge case only): corrects rows created between 00:30–07:59 IST (maintenance
restart before BHAV loads). Single broker API call, all exchanges, uniform logic.

**No MCX-specific code anywhere** — no date-offset, no midnight cutoff, no overnight epsilon
guard, no exchange checks in any of these paths.

### `previous_close_backup` invariant

Written once per (date, account, symbol): the value of `previous_close` at first INSERT.
- Currently NOT written during UPSERT — only by `fix_daily_book_prev_close`.
- Fix: add to UPSERT so backup is set from day 1 of a row's life.
- Survives code redeployment (DB-side data, not code-side logic).

### No runtime corruption detection

`_resolve_previous_close` fires when `|previous_close − ltp| < 0.01`. At 08:00 session open, ltp
equals previous_close (no movement yet) — this is **valid**, not corruption. With settlement
snapshots removed and `fix_daily_book_prev_close` as the only writer of `previous_close`, the
stored value is always correct. Corruption detection is unnecessary and actively harmful.

---

## Inconsistencies — Current Code vs Proposed Design

### I1 — Settlement snapshots still exist in scheduler
- **File**: `backend/api/background.py` — `_task_daily_snapshot()`
- **Current**: Triggers `trigger_settlement_capture("NON-MCX")` at 16:15 IST and
  `trigger_settlement_capture("MCX")` at 00:15 IST via `_snapshot_probe_nse_mcx()`
- **Proposed**: Remove both. `_snapshot_probe_nse_mcx` should only fire `trigger_close_snapshot`,
  never `trigger_settlement_capture`.
- **Fix**: Delete `trigger_settlement_capture` calls (and the function if unused elsewhere).
  Remove session detection logic that routes to settlement vs close path.

### I2 — `snapshot_daily_book` has unused `settled` / `market_open=False` params
- **File**: `backend/api/algo/daily_snapshot.py` — `snapshot_daily_book(settled=True, market_open=False)`
- **Current**: `settled=True` is passed by settlement snapshots. Downstream code checks `settled`
  to prefer settlement ltp.
- **Proposed**: With settlement snapshots removed, `settled` is always `False`. Parameter and
  any `settled`-conditional logic become dead code.
- **Fix**: Remove `settled` parameter and any `if settled:` branches from `snapshot_daily_book`.
  Keep `market_open` param if used for other logic; verify and remove if not.

### I3 — `_resolve_previous_close` false-positive corruption guard
- **File**: `backend/api/routes/positions_helpers.py:33–51`
- **Current**: `abs(pc_f - ltp_f) < 0.01` → substitute `previous_close_backup`. Fires at
  session open when `ltp = previous_close` is valid.
- **Proposed**: Delete the function. `build_row_from_snapshot_raw` (line 367) reads
  `previous_close` directly: `actual_previous_close = _pc_raw if _pc_raw > 0 else 0.0`.
- **Fix**: Delete `_resolve_previous_close`; update call site.

### I4 — UPSERT does not write `previous_close_backup` at INSERT time
- **File**: `backend/api/algo/daily_snapshot.py:819–845` (UPSERT SQL)
- **Current**: `previous_close_backup` not in INSERT column list. Only written by
  `fix_daily_book_prev_close`. Rows created before 08:00 fixup have `backup = NULL`.
- **Fix**: Add to UPSERT:
  ```sql
  -- INSERT columns: add previous_close_backup
  -- INSERT values:  :previous_close  (same value as previous_close on first insert)
  -- ON CONFLICT:    previous_close_backup = COALESCE(daily_book.previous_close_backup, daily_book.previous_close)
  ```

### I5 — `fix_daily_book_prev_close` reads `daily_book.ltp` instead of broker settlement price; has MCX-specific overnight mode
- **File**: `daily_snapshot.py:1129–1137` (new-session pre-load SQL), `963–996` (update SQL)
- **Current**: New-session mode reads `yesterday's daily_book.ltp`; overnight mode (ε=0.005) exists
  for MCX settlement rows. Both are wrong direction: self-reading ltp instead of querying broker,
  and MCX-specific branching that should not exist.
- **Proposed**: `fix_daily_book_prev_close` at 08:00 calls `broker.get_positions()` and
  `broker.get_holdings()` for each account — one pass, all exchanges. Reads `close_price`
  (settlement price, available for both NSE and MCX at 08:00). Updates any `date = today` rows.
  No overnight branch. No MCX-specific logic. No exchange checks.
- **Fix**:
  - Remove overnight branch and epsilon guard entirely.
  - Remove pre-load SQL query (daily_book self-read).
  - Add broker API calls (all accounts, all exchanges); build `(account, symbol) → close_price` map.
  - UPDATE `daily_book` rows for `date = today` using that map — uniform for all exchanges.

### I6 — Pre-load query uses `daily_book.ltp` instead of broker `close_price`; has MCX/non-MCX branching
- **File**: `daily_snapshot.py:1101–1147` (`prev_ltp_map` pre-load block)
- **Current**: Pre-load branches on session time (overnight vs new-session), reading either
  `previous_close` or `ltp` from daily_book depending on exchange and time. MCX and non-MCX take
  different paths. All of this complexity exists to approximate settlement — none of it is needed.
- **Proposed**: Snapshot already has the Kite broker response. Use `position.close_price` /
  `holding.close_price` directly as `previous_close` for new rows. At any snapshot time (15:45
  or 23:45), Kite's `close_price` = prior session's settlement (loaded at the PREVIOUS 08:00,
  unchanged during session). Same logic for MCX and non-MCX — no branching.
- **Fix**: In `_holdings_rows` and `_positions_rows`, replace `prev_ltp_map.get(...)` with
  `float(row.close_price or 0)` from the broker row. Delete `prev_ltp_map` construction and
  all pre-load SQL. Delete the new-session vs overnight branch entirely.

---

## Changes Required

### `backend/api/background.py`
1. Remove `trigger_settlement_capture("NON-MCX")` call and its scheduling logic (~16:15 IST branch)
2. Remove `trigger_settlement_capture("MCX")` call and its scheduling logic (~00:15 IST branch)
3. Remove `trigger_settlement_capture` function definition if it has no other callers
4. Simplify `_snapshot_probe_nse_mcx`: only close snapshot path remains

### `backend/api/algo/daily_snapshot.py`
5. Remove `settled` parameter from `snapshot_daily_book`; remove any `if settled:` branches
6. UPSERT SQL: add `previous_close_backup` to INSERT columns + ON CONFLICT clause (I4)
7. `fix_daily_book_prev_close`:
   - Remove overnight branch and all pre-load SQL (I5)
   - At 08:00: call `broker.get_positions()` + `broker.get_holdings()` per account
   - Build `(account, symbol) → close_price` map
   - UPDATE `daily_book` rows where `date = today` using broker settlement prices
8. `_holdings_rows` and `_positions_rows`:
   - Remove `prev_ltp_map` parameter and lookup (I6)
   - Set `previous_close = float(row.close_price or 0)` from the broker DataFrame row directly

### `backend/api/routes/positions_helpers.py`
8. Delete `_resolve_previous_close` (lines 33–51)
9. `build_row_from_snapshot_raw` line 367: replace `_resolve_previous_close(...)` with
   `actual_previous_close = float(_pc_raw) if _pc_raw and float(_pc_raw) > 0 else 0.0`

---

## Agents

- backend: changes 1–9 above across `background.py`, `daily_snapshot.py`, `positions_helpers.py`
- backend-test: add/update pytest:
  - `fix_daily_book_prev_close` calls broker API at 08:00, uses `close_price` not `daily_book.ltp`
  - `_holdings_rows` / `_positions_rows` use `row.close_price` as `previous_close` (not pre-load map)
  - UPSERT sets `previous_close_backup` on first INSERT, preserves on conflict
  - `build_row_from_snapshot_raw` uses direct read (no `_resolve_previous_close`)
  - Verify no settlement snapshot calls remain in background task

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(snapshot): remove settlement snapshots, use broker.close_price as settlement reference in snapshots + 08:00 fixup, delete _resolve_previous_close false-positive guard, write previous_close_backup at UPSERT INSERT time

## Done when
- No settlement snapshot triggers remain in `background.py`
- `snapshot_daily_book` has no `settled` parameter
- `fix_daily_book_prev_close` calls broker API at 08:00; uses `close_price` not `daily_book.ltp`
- Pre-load query removed; `_holdings_rows`/`_positions_rows` use `row.close_price` directly
- `_resolve_previous_close` deleted; no references remain
- `previous_close_backup` written at INSERT time in UPSERT
- All pytest pass, broker cov ≥ 80%, api cov ≥ 45%
