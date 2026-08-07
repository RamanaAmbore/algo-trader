# Plan: Holdings + Positions SSOT — correctness, closure, stale-code cleanup

## Context

Three parallel audit agents reviewed holdings.py, positions.py, broker_apis.py, background.py,
snapshot_gate.py, pnl_math.py, nav.js, and PerformancePage.svelte against six dimensions:
market closure, snapshot/LKG behavior, prev-close accuracy, closed-position persistence,
day P&L formula correctness, and stale code. Six correctness defects found (3 P1, 3 P2),
three robustness gaps (P2), and six cleanup items (P3). All fixes are mechanical — no
architectural change.

---

## Task

Fix all P1 defects, all P2 robustness gaps, and the P3 cleanup items flagged in the audit.

---

## Agents

- broker: Fix `backend/brokers/broker_apis.py`:
  (a) Line 2263-2268 `_bmd_recompute_derived` — only overwrite `day_change_val` when the
      existing value is zero or NaN; do NOT overwrite pre-existing Dhan/Groww decomposed values.
      Guard: `if row['day_change_val'] == 0 or pd.isna(row['day_change_val'])` (or polars
      equivalent) before applying `(ltp - close) * opening_quantity`.
  (b) Lines 562-582 `_record_lkg_frame` + callers — remove the `not df.empty` guard at call
      sites (broker_apis.py ~1426 and ~1799). Allow writing an empty frame to the LKG cache
      with a fresh timestamp, so `_stale_substitute_frame` can detect "account has no positions"
      vs "account was never fetched". Update the misleading docstring at lines 562-572 to
      describe what the function actually does.
  (c) Lines 2375-2377 `_fetch_margins_local` exception handler — add
      `df_margins.attrs['fetch_failed'] = True` after the exception is caught, matching the
      pattern in holdings (line ~1409) and positions.
  (d) Lines 1891-1903 — remove dead variables `_dcp_expr` and `_pnl_pct_expr` (assigned but
      never referenced in any subsequent `with_columns()` call).
  (e) Line 1721 — replace `globals()['_KITE_VALUE_UNIT_LOGGED'] = True` with the direct module-
      level assignment `_KITE_VALUE_UNIT_LOGGED = True`.
  Write/update tests in `backend/tests/broker/` covering (a) and (b): one test verifying that
  a row with an existing non-zero day_change_val is NOT overwritten by _bmd_recompute_derived,
  and one test verifying that writing an empty LKG frame allows _stale_substitute_frame to
  return empty rather than the prior session frame.

- backend: Fix three files:
  1. `backend/api/routes/holdings.py` line 555-558 `_snapshot_fn` — set `as_of=None` (not
     `as_of=timestamp_display()`) when `_holdings_snapshot()` returns None. The correct pattern
     is in positions.py line ~955-959. Also fix line 119 `_build_holding_row_from_snapshot`:
     set `close_price` from the `previous_close` column in the snapshot query result, not from
     `ltp_f`.
  2. `backend/api/background.py` line 127 `_bg_holdings_add_pct` — change denominator from
     `cur_val` to `cur_val - day_change_val` (matching the route formula in
     `holdings.py:_compute_summary_df`). Also fix lines 152-179 `_fetch_positions_direct`:
     call `_override_stale_close_from_snapshot(raw)` and `_override_stale_ltp_from_ticker(raw)`
     BEFORE `apply_day_change_backstop(raw)`, matching the order in
     `positions.py:_patch_raw_positions`.
  3. `backend/api/routes/positions_helpers.py` line 178 `resolve_snapshot_day_pct` — compute
     `close_price_f` before passing it as the denominator (currently uses `ltp_f`, which diverges
     from prev-close for F&O). Also line 242 — extract `product` from `payload_json` dict
     instead of hardcoding `"NRML"`.
  Write/update tests in `backend/tests/` covering: (1) holdings _snapshot_fn returns no as_of
  when DB is empty; (2) _build_holding_row_from_snapshot uses previous_close not ltp for
  close_price; (3) _bg_holdings_add_pct uses correct denominator.

- frontend: Fix `frontend/src/lib/data/nav.js` line 108 — change `oq > 0` to `oq !== 0` in
  `baseDayPnlForPosition` so that short overnight positions (oq < 0) also get the dcv fast-path
  and the Case 4 stale-close guard (close <= 0 → return 0). The formula at line 126
  (`pnl - oq*(close-avg)`) is already sign-correct for negaitve oq; only the guard predicates
  need updating. Also update the `livePositionDayPnl` function — it mirrors the same `oq > 0`
  guard for the ticker-rescue path; change to `oq !== 0` there too.
  Do NOT change PerformancePage snapshot indicator — scope excluded (requires backend as_of
  field plumbing which is a separate task).

- backend-test: skip (tests handled inline by broker and backend agents above)

- playwright: skip

---

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

---

## Commit message
fix(ssot): short-position day P&L guard, LKG phantom positions, snapshot as_of, bmd overwrite, bg pct denominator, stale-close overrides

## Done when
- `nav.js` `baseDayPnlForPosition` and `livePositionDayPnl`: predicate is `oq !== 0`
- `broker_apis.py` `_bmd_recompute_derived`: does not overwrite pre-existing non-zero day_change_val
- `broker_apis.py` LKG callers: empty frames written to cache; phantom positions eliminated
- `broker_apis.py` margins: `fetch_failed` flag set on exception
- `holdings.py` `_snapshot_fn`: `as_of=None` when snapshot is None
- `holdings.py` `_build_holding_row_from_snapshot`: `close_price` from `previous_close`
- `background.py` `_bg_holdings_add_pct`: denominator is `cur_val - day_change_val`
- `background.py` `_fetch_positions_direct`: stale-close + stale-ltp overrides applied before backstop
- `positions_helpers.py` `resolve_snapshot_day_pct`: `close_price_f` as denominator
- Dead vars `_dcp_expr`, `_pnl_pct_expr` removed; `globals()` pattern replaced
- pytest green, svelte-check 0 errors, coverage ≥ 80%
