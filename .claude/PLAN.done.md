# Plan: Fix holdings 100% day P&L% — gate-filter + missed-snapshot guard

## Context

**Root cause (primary):**

`snapshot_daily_book` has no gate/exchange filter. When triggered by MCX close at 23:45 IST,
it fetches ALL broker holdings (NSE + MCX) and UPSERTs everything. This overwrites the
correct NON-MCX 15:45 snapshot for NSE symbols (E2E, HFCL) with data captured when
`close_price=0` (NSE BHAV not published at 23:45 IST), giving `day_pnl = ltp × qty = 56718`.
`day_change_pct = 56718 / (previous_close × qty) × 100 = 100%`.

**Root cause (secondary — missed-snapshot):**

When a deployment happens during the 15:45 or 23:45 close window (snapshot missed), the
startup snapshot fires afterward with `close_price=0` from Kite (BHAV lag). `_snap_holding_eod_vals`
computes `day_pnl = ltp × qty` — same 100% bug. `fix_daily_book_prev_close` at 08:00 patches
`previous_close` but does NOT recompute `day_pnl`. Priority 1 in `_compute_holding_day_change`
returns the wrong stored `day_pnl` directly (non-zero check).

**Full timeline (Monday trading day — normal):**
1. **15:45 IST** — NON-MCX close snapshot fires → ALL holdings written (NSE + MCX):
   NSE E2E: `ltp=630.20, close_price=601.55` (Fri BHAV available), `day_pnl=2578.5` ✓
2. **23:45 IST** — MCX close snapshot fires → ALL holdings UPSERTed again (no filter):
   NSE E2E: `close_price=0` (Mon BHAV not published until Tue 08:00), `day_pnl=56718` ✗
   Overwrites the correct 15:45 row on `(date=Mon, account, E2E, holdings)`.
3. **~01:11 IST Tue** — Service restart startup snapshot → NEW row `date=Tue`:
   rolling-shifts `previous_close = prior ltp = 630.20`, `day_pnl=56718` ✗
4. **08:00 IST Tue** — `fix_daily_book_prev_close` fixes `previous_close` in `date=Tue`
   rows to Monday settlement = 630.20. No change (was already 630.20). `day_pnl` not touched.
5. **09:15 IST Tue** — NSE opens → live path bypasses snapshot → 100% disappears.

**Missed-snapshot timeline (deployment at 15:45 on trading day):**
1. **15:45 IST** — NON-MCX snapshot missed (service restarting).
2. **~16:00 IST** — Startup snapshot fires → NSE E2E: `close_price=0` → guard: `day_pnl=NULL` ✓ (new)
3. **23:45 IST** — MCX snapshot fires → gate filter: NSE symbols SKIPPED ✓ (new)
4. **08:00 IST Tue** — `fix_daily_book_prev_close` patches `previous_close=settlement`
   AND recomputes `day_pnl=(ltp − previous_close) × qty` for NULL rows ✓ (new)
5. **09:15 IST Tue** — NSE opens → live path takes over.
   Pre-market window shows ~0% instead of 100% — correct and safe.

Non-trading days: `fix_daily_book_prev_close` returns early (no-op) — no impact.

**Fix 1 — Gate filter (primary):**
Pass `gate` through `trigger_close_snapshot` → `_snapshot_fire` → `snapshot_daily_book`.
Filter `_holdings_rows` / `_positions_rows` to only write symbols in the gate's exchange set.

**Fix 2 — Missed-snapshot guard (secondary — two parts):**
- **Write-time:** In `_snap_holding_eod_vals` and `_snap_position_eod_vals`, if `close_price == 0`
  (or null/NaN), store `day_pnl = None` (NULL) instead of `ltp × qty`. Prevents garbage persistence.
- **Fix-time:** In `fix_daily_book_prev_close`, after patching `previous_close`, run a second
  UPDATE: `SET day_pnl = (ltp - previous_close) * quantity WHERE day_pnl IS NULL AND previous_close > 0`
  (scoped to same date+account+source batch). This recomputes correct `day_pnl` using BHAV at 08:00.

Exchange membership:
- NON-MCX: `{"NSE", "BSE", "NFO", "BFO", "CDS"}`
- MCX: `{"MCX"}`
- `gate=None` (startup snapshot, admin manual): no filter — write everything (existing behaviour)

## Task

**Files to change:**

1. **`backend/api/algo/daily_snapshot.py`**:
   - `snapshot_daily_book`: add optional `gate: str | None = None` param.
   - Derive `gate_exchanges: set[str]` from gate name (NON-MCX → {"NSE","BSE","NFO","BFO","CDS"},
     MCX → {"MCX"}, None/empty → no filter).
   - Pass `gate_exchanges` to `_holdings_rows` and `_positions_rows`; skip rows not in set.
   - `_snap_holding_eod_vals`: if `close_price == 0 or close_price is None`, set `day_pnl_v = None`
     (do not compute `ltp × qty`).
   - `_snap_position_eod_vals`: same guard for positions `day_change_val`.
   - `fix_daily_book_prev_close`: after existing `previous_close` UPDATE, add second UPDATE:
     `SET day_pnl = (ltp - previous_close) * quantity WHERE day_pnl IS NULL AND previous_close > 0`
     scoped to the same `date + account + source` batch being fixed. Only runs on trading days
     (existing guard: `_open=None` → returns 0 early).

2. **`backend/api/background.py`**:
   - `trigger_close_snapshot(gate)`: forward `gate` to `_snapshot_fire(label, gate=gate)`.
   - `_snapshot_fire(label, ..., gate=None)`: forward `gate` to `snapshot_daily_book(gate=gate)`.
   - Startup snapshot (`_snapshot_fire("startup")`): no gate → writes everything (keep as-is).

3. **`backend/tests/test_holdings_close_override.py`** (or new `test_daily_snapshot.py`)**:
   - `test_mcx_snapshot_does_not_write_nse_holdings`: `snapshot_daily_book(gate="MCX")` with
     NSE+MCX holdings → assert only MCX symbols written.
   - `test_zero_close_price_writes_null_day_pnl`: holdings row with `close_price=0` →
     assert `day_pnl=None` in the UPSERT (not `ltp × qty`).
   - `test_fix_daily_book_recomputes_null_day_pnl`: row with `day_pnl=NULL, previous_close=0`
     → after `fix_daily_book_prev_close` with patched `previous_close=601.55` → assert
     `day_pnl = (ltp - 601.55) × qty`.

## Agents
- backend: Implement both fixes in `daily_snapshot.py` and `background.py` as described above.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: Add all three tests in `backend/tests/test_holdings_close_override.py`
  (or `test_daily_snapshot.py`).
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(snapshot): gate-filter by exchange + null day_pnl guard for zero close_price — prevents 100% holdings P&L%

## Done when
- MCX 23:45 snapshot writes only MCX positions; NSE holdings are skipped
- NON-MCX 15:45 snapshot writes only NSE/BSE holdings; MCX positions are skipped
- Startup snapshot (gate=None) writes all — behaviour unchanged
- Snapshot with `close_price=0` writes `day_pnl=NULL` (not `ltp × qty`)
- `fix_daily_book_prev_close` at 08:00 recomputes `day_pnl` for NULL rows using BHAV
- E2E and HFCL show correct day P&L% after Monday MCX close; pre-market shows ~0% (not 100%) if snapshot missed
- All three new tests pass; pytest green
