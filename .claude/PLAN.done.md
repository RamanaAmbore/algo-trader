# Plan: snapshot ltp=0 corruption + positions P&L unification (all states, all brokers) + exchange_clock date-override-first

## Task

Four bug areas:

**A — Snapshot ltp=0 corruption (primary — holdings shows -13k instead of +1L on Saturday)**
Root cause: MCX 23:45 IST snapshot fires Saturday (18:15 UTC < 18:30 UTC = Saturday midnight
cutoff). Dhan returns `last_price=0` for NSE holdings at MCX settlement time. UPSERT
`COALESCE(EXCLUDED.ltp, daily_book.ltp)` treats 0 as non-NULL → overwrites good Friday 15:45
settlement LTP. Rolling-shift also fires: `previous_close = old_ltp (Friday settlement)`.
Reader selects 23:45 batch as latest. Shows `ltp=0, previous_close=Friday_settlement` →
`(0 - prev_close) × qty` = large negative day P&L.

Three-layer defense (details in agent section below). Critical: the writer guard (Fix 2) must
NOT skip the row entirely — it must set `ltp_val = None` instead, so the row is written (batch
timestamp updates for the account) but UPSERT NULLIF preserves the existing ltp. Skipping entirely
would mean NSE rows are absent from the 23:45 batch; the reader joins on `captured_at = max_at`
and would miss all NSE holdings for accounts where MCX drove `max_at` to 23:45.

**B — Positions P&L: opened, partially closed, fully closed — all states, all brokers**

*Snapshot write path (daily_snapshot.py)*: `total_pnl = r.get("pnl")` captures only unrealised.
For partially-closed and fully-closed positions, realised is lost.
Fix at the snapshot writer chokepoint: `total_pnl = r.get("pnl") + r.get("realised")`.
All three broker adapters (Kite, Dhan, Groww) already set a separate `"realised"` field in their
normalised position dicts, so this single fix covers all brokers.

*Live path (broker_apis.py `_enrich_positions`)*: `pnl` expression uses broker's `"pnl"` field
directly. Kite raw API: `pnl` = unrealised only, `realised` = separate. Dhan adapter: `"pnl" =
pnl_calc = (ltp-avg)×qty` (unrealised). Groww adapter: `"pnl" = unrealised_pnl`.
Fix: in `_enrich_positions`, when computing the `pnl` expression, add `realised` column:
`total = broker_pnl + broker_realised.fill_null(0)`. This fixes Kite, Dhan, Groww in one place.

**Do NOT change adapter `"pnl"` fields** — that would double-count when `_enrich_positions`
adds `realised` again. Keep adapter `"pnl"` = unrealised, `"realised"` = realised (current state).

*Snapshot reader (positions_helpers.py)*: `build_row_from_snapshot_raw` computes:
- `qty=0` (fully closed): uses stored `day_pnl` ✓ (already correct)
- `qty>0`: naive `(ltp - prev_close) × qty` — wrong for new-today positions (oq=0)

Fix with universal formula using `overnight_quantity` from `payload_json`:
`day_pnl = total_pnl - (prev_close - avg) × oq`
Handles ALL states when `prev_close > 0`:
- Overnight open (oq=qty): `(ltp-avg)*oq - (prev_close-avg)*oq = (ltp-prev_close)*oq` ✓
- New today (oq=0): `(ltp-avg)*qty - 0 = (ltp-entry)*qty` ✓
- Partial close: `(ltp-avg)*remaining + realised - (prev_close-avg)*oq` ✓
- Fully closed intraday (qty=0, oq=0): `realised - 0 = realised` ✓
- Fully closed overnight (qty=0, oq>0): `realised - (prev_close-avg)*oq = (exit-prev_close)*oq` ✓

Also fix `overnight_quantity` field in the returned PositionRow: currently set to `qty_i`
(wrong for partially-closed), must be read from `payload_json.overnight_quantity`.

**C — Holdings P&L: correct prev_close, correct ltp, all three brokers**
Holdings day P&L formula `(ltp - previous_close) × qty` is already correct. The problem is
corrupted data. After Fix A: `ltp = Friday_settlement` (preserved by UPSERT NULLIF), `previous_close
= Thursday_settlement` (rolling-shift guard prevents corruption when ltp=0). Holdings day P&L
shows Friday's gain correctly on Saturday.

No special formula change for holdings — the three-layer snapshot fix (Fixes 1+2+4) is
broker-agnostic and covers Kite, Dhan, Groww accounts.

**D — exchange_clock date-override-first (unified row model)**
Single `ExchangeSchedule` table, unified row model:
- Default rows: `date=NULL, weekdays=[0,1,2,3,4]` — seed rows and migration already in code ✓
- Override rows: `date=YYYY-MM-DD, weekdays=NULL` — operator-created
  - `open_time IS NULL` → closed (holiday)
  - `open_time IS NOT NULL` → open with custom hours (Muhurat, early close)

`_effective_gate_rows(gate)`: if any row exists for (gate, today) → absolute priority,
suppresses all default rows. No override today → apply default rows with weekday filter.
Remove `is_open` flag from session logic; rely solely on `open_time` presence.

## Agents

- backend: Fix `daily_snapshot.py`, `holdings.py`, `positions.py`,
  `positions_helpers.py`, and `exchange_clock.py`. No schema change.

  ### daily_snapshot.py

  **Fix 1 — UPSERT: NULLIF(ltp, 0) prevents zero overwriting good data (line 831)**
  ```sql
  -- Before:
  ELSE COALESCE(EXCLUDED.ltp, daily_book.ltp)
  -- After:
  ELSE COALESCE(NULLIF(EXCLUDED.ltp, 0), daily_book.ltp)
  ```
  Also guard the `previous_close` rolling-shift (line 835) — do not roll when ltp=0:
  ```sql
  -- Before:
  CASE WHEN EXCLUDED.ltp IS NOT NULL AND (daily_book.ltp IS NULL OR EXCLUDED.ltp != daily_book.ltp)
       THEN daily_book.ltp ELSE daily_book.previous_close END
  -- After:
  CASE WHEN EXCLUDED.ltp IS NOT NULL AND EXCLUDED.ltp != 0
            AND (daily_book.ltp IS NULL OR EXCLUDED.ltp != daily_book.ltp)
       THEN daily_book.ltp ELSE daily_book.previous_close END
  ```

  **Fix 2 — Writer: set ltp_val = None for ltp=0 non-mid-session rows (do NOT skip/continue)**
  In `_holdings_rows`, after `ltp_val, day_pnl_v, total_pnl_v = _snap_holding_eod_vals(...)`,
  before the `_is_zero_payload_row` check:
  ```python
  if not mid_session and ltp_val is not None and ltp_val == 0.0:
      ltp_val = None  # neutralise bad ltp; UPSERT NULLIF will preserve existing ltp
      logger.debug(f"holdings: ltp=0 neutralised for {symbol} (non-mid-session)")
  ```
  Row is still written (so captured_at updates for the account), but UPSERT NULLIF
  preserves the existing ltp=150 from the prior settlement. Do NOT use `continue` —
  that would remove NSE rows from the 23:45 batch, causing the reader to miss them when
  the account's max_at is driven by MCX rows at 23:45.

  **Fix 3 — Unified total_pnl = pnl + realised (lines 540, 675) — covers Kite, Dhan, Groww**
  In `_snap_position_eod_vals` (line 540):
  ```python
  _unrealised = r.get("pnl")
  _realised   = r.get("realised")
  if _unrealised is not None or _realised is not None:
      total_pnl_v = float(_unrealised or 0) + float(_realised or 0)
  else:
      total_pnl_v = None
  ```
  In `_positions_rows` dict literal (line 675):
  ```python
  "total_pnl": (float(r.get("pnl") or 0) + float(r.get("realised") or 0))
               if (r.get("pnl") is not None or r.get("realised") is not None) else None,
  ```

  ### holdings.py

  **Fix 4 — Reader: row-level ltp > 0 filter in final WHERE (not just latest_batch)**
  The `latest_batch` CTE does `MAX(captured_at)` per account. If MCX rows with ltp>0
  are in the 23:45 batch, `max_at=23:45` even if NSE rows have ltp=0. The join then
  brings all 23:45 rows including NSE with ltp=0. A `latest_batch` filter alone is
  insufficient. Add row-level filter in the final WHERE clause:
  ```sql
  -- In the final SELECT...FROM daily_book WHERE:
  -- Before: (nothing for ltp)
  -- After: add:
  AND (db.ltp IS NULL OR db.ltp > 0)
  ```
  Also add `AND ltp > 0` to `latest_batch` (line 45) as secondary defense:
  ```sql
  WHERE kind = 'holdings' AND ltp IS NOT NULL AND ltp > 0
    AND captured_at < :snapshot_cutoff
  ```
  After Fix 1 (UPSERT NULLIF), ltp=0 can never be stored — but both guards protect
  against data written before this fix is deployed.

  ### positions.py

  **Fix 5 — Reader: row-level ltp > 0 filter in final WHERE**
  Same reasoning as Fix 4. Positions `latest_batch` (line 255):
  ```sql
  WHERE kind = 'positions' AND ltp IS NOT NULL AND ltp > 0
    AND captured_at < :snapshot_cutoff
  ```
  And in the final WHERE clause (line ~286):
  ```sql
  -- Add:
  AND (db.ltp IS NULL OR db.ltp > 0)
  ```
  (alongside the existing `AND (db.ltp IS NULL OR NOT (db.ltp = 0 AND ...))` guard
  which is narrower — replace or supplement it with the simpler `ltp IS NULL OR ltp > 0`)

  ### positions_helpers.py

  **Fix 6 — `build_row_from_snapshot_raw`: universal day_pnl + correct overnight_qty**
  Read `overnight_quantity` from `payload_json` and apply universal formula for all states.
  Replace the current `computed_day_pnl` block (lines ~313–321):

  ```python
  import json as _j
  _pj = _j.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
  _oq  = float(_pj.get("overnight_quantity") or 0)   # true opening qty (may be 0 for new-today)
  _avg = float(avg_cost) if avg_cost else 0.0
  _total = float(total_pnl) if total_pnl is not None else 0.0

  # Universal formula — correct for all position states:
  #   Overnight open (oq=qty):  (ltp-prev)*oq                    ✓
  #   New today (oq=0):         (ltp-entry)*qty  [prev factor=0] ✓
  #   Partial close (oq>qty>0): (ltp-avg)*qty + realised - (prev-avg)*oq ✓
  #   Closed intraday (oq=0):   realised                         ✓
  #   Closed overnight (oq>0):  (exit-prev)*oq                   ✓
  if actual_previous_close and actual_previous_close > 0:
      computed_day_pnl = _total - (actual_previous_close - _avg) * _oq
  else:
      computed_day_pnl = day_pnl  # fallback when prev_close unavailable
  ```

  Pass `_oq` into `build_snapshot_position_row` as `overnight_quantity` parameter:
  Add `overnight_quantity: int | None = None` keyword arg to `build_snapshot_position_row`.
  In the PositionRow construction: `overnight_quantity=int(_oq) if overnight_quantity is not None else qty_i`.

  ### exchange_clock.py

  **Fix 7 — `_is_within_session`: remove `is_open` flag, rely solely on `open_time`**
  ```python
  def _is_within_session(row: "ExchangeSchedule") -> bool:
      if row.open_time is None or row.close_time is None:
          return False
      now_t = _now_ist().time().replace(second=0, microsecond=0)
      return row.open_time <= now_t < row.close_time
  ```

  **Fix 8 — Add `_effective_gate_rows(gate)`: date override has absolute precedence**
  ```python
  def _effective_gate_rows(gate: str) -> list["ExchangeSchedule"]:
      upper = gate.upper()
      today = _now_ist().date()
      overrides = [r for r in _CACHE if r.gate.upper() == upper and r.date == today]
      if overrides:
          return overrides  # suppresses default rows entirely
      default_rows = [r for r in _CACHE if r.gate.upper() == upper and r.date is None]
      return [r for r in default_rows if _row_matches_now(r)]
  ```

  **Fix 9 — Refactor `is_exchange_open(exchange)`**
  ```python
  def is_exchange_open(exchange: str) -> bool:
      if not _CACHE:
          return True
      gate = _exchange_to_gate(exchange)
      if gate is None:
          return True
      return any(_is_within_session(r) for r in _effective_gate_rows(gate))
  ```

  **Fix 10 — Refactor `get_today_gate_sessions(gate)`**
  ```python
  def get_today_gate_sessions(gate: str) -> list["ExchangeSchedule"]:
      return [r for r in _effective_gate_rows(gate) if r.open_time is not None]
  ```

  **Fix 11 — Refactor `is_any_segment_open(exchanges)`**
  ```python
  def is_any_segment_open(exchanges: list[str] | None = None) -> bool:
      if not _CACHE:
          return True
      upper_set = {e.upper() for e in exchanges} if exchanges else None
      gates = {r.gate for r in _CACHE}
      for gate in gates:
          for row in _effective_gate_rows(gate):
              if upper_set is not None:
                  row_exchs = {e.upper() for e in (row.exchanges or [])}
                  if not row_exchs.intersection(upper_set):
                      continue
              if _is_within_session(row):
                  return True
      return False
  ```

  **Fix 12 — Refactor `sessions_with_snapshot_time_now(tolerance_minutes)`**
  ```python
  def sessions_with_snapshot_time_now(tolerance_minutes: int = 1) -> list["ExchangeSchedule"]:
      now_t = _now_ist().time().replace(second=0, microsecond=0)
      delta = timedelta(minutes=tolerance_minutes)
      matched = []
      for gate in {r.gate for r in _CACHE}:
          for row in _effective_gate_rows(gate):
              if row.open_time is None or row.snapshot_time is None:
                  continue
              snap_dt = datetime.combine(datetime.today(), row.snapshot_time)
              now_dt  = datetime.combine(datetime.today(), now_t)
              if abs((snap_dt - now_dt).total_seconds()) <= delta.total_seconds():
                  matched.append(row)
      return matched
  ```

  **Fix 13 — Refactor `settlement_cutoff_for(gate)`: use default row's open_time**
  ```python
  async def settlement_cutoff_for(gate: str) -> datetime:
      await refresh()
      reset_time = time(8, 0)
      for row in _CACHE:
          if row.gate.upper() == gate.upper() and row.date is None and row.open_time:
              reset_time = row.open_time
              break
      now_ist = _now_ist()
      today_reset = now_ist.replace(
          hour=reset_time.hour, minute=reset_time.minute, second=0, microsecond=0
      )
      if now_ist >= today_reset:
          return today_reset
      return today_reset - timedelta(days=1)
  ```

- frontend: skip

- broker: Fix the live positions pnl path in `broker_apis.py` only.
  Do NOT change adapter `"pnl"` fields (that would double-count with this fix).

  ### broker_apis.py — `_enrich_positions` (~line 2141)
  In the `# ── pnl ──` block, when `"pnl"` is in cols, add `realised`:
  ```python
  # Before:
  if 'pnl' in cols:
      _broker_pnl = _col_f64_nullable(lf, 'pnl')
      _pnl_expr = (
          pl.when(_broker_pnl.is_not_null())
          .then(_broker_pnl)
          .otherwise(_pnl_calc)
      )
  # After:
  if 'pnl' in cols:
      _broker_pnl = _col_f64_nullable(lf, 'pnl')
      _broker_realised = (
          _col_f64_nullable(lf, 'realised').fill_null(0.0)
          if 'realised' in cols else pl.lit(0.0)
      )
      _pnl_expr = (
          pl.when(_broker_pnl.is_not_null())
          .then(_broker_pnl + _broker_realised)
          .otherwise(_pnl_calc)
      )
  ```
  Kite: `pnl(unrealised) + realised(raw API)` = total ✓
  Dhan: `pnl_calc(unrealised) + realisedProfit` = total ✓
  Groww: `unrealised_pnl + realised_pnl` = total ✓
  All three covered in one place. No adapter changes needed.

- doc: skip

- backend-test: Write tests covering all fixes. Use `types.SimpleNamespace` for
  exchange_clock rows, patch `_now_ist`; dict rows for snapshot writer tests.

  **`backend/tests/test_snapshot_ltp_zero.py`** (new)

  1. `TestUpsertSqlNullIfGuard` — assert `_UPSERT_SQL` string contains
     `NULLIF(EXCLUDED.ltp, 0)` and `EXCLUDED.ltp != 0`.

  2. `TestHoldingsWriterNeutralisesZeroLtpNonMidSession` — call `_holdings_rows` with
     one row `{last_price=0, pnl=5000}`, `mid_session=False`. Assert result list has one
     entry with `ltp=None` (not skipped, not ltp=0). Validates "neutralise, don't skip."

  3. `TestHoldingsWriterAllowsZeroLtpMidSession` — same row but `mid_session=True`.
     Assert result list has one entry with `ltp=0.0` (mid-session, no neutralisation).

  4. `TestHoldingsReaderSqlHasRowLevelLtpFilter` — assert `_HOLDINGS_SNAPSHOT_SQL`
     contains `db.ltp IS NULL OR db.ltp > 0` (row-level filter in final WHERE).

  5. `TestPositionsReaderSqlHasRowLevelLtpFilter` — assert positions snapshot SQL
     contains row-level `ltp IS NULL OR ltp > 0` filter.

  6. `TestSnapPositionEodValsUnifiedPnlKite` — row `{pnl=5000, realised=2000}`.
     `_snap_position_eod_vals(r, mid_session=False, qty=10)` → `total_pnl_v == 7000`.

  7. `TestSnapPositionEodValsNoneRealised` — row `{pnl=5000, realised=None}`.
     Assert `total_pnl_v == 5000.0`.

  8. `TestSnapPositionEodValsBothNone` — row `{pnl=None, realised=None}`.
     Assert `total_pnl_v is None`.

  **`backend/tests/test_universal_day_pnl.py`** (new)
  All tests call `build_row_from_snapshot_raw` with a fabricated 13-column tuple.

  9. `TestUniversalFormula_OvernightOpen` — `total_pnl=800, prev_close=95, avg=90,
     payload.overnight_quantity=100, qty=100, ltp=98`.
     `day_pnl = 800 - (95-90)*100 = 300` == `(98-95)*100`. Assert `day_change_val == 300`.

  10. `TestUniversalFormula_NewTodayPosition` — `total_pnl=300, prev_close=92 (irrelevant),
      avg=95, payload.overnight_quantity=0, qty=100, ltp=98`.
      `day_pnl = 300 - (92-95)*0 = 300` == `(98-95)*100`. Assert `day_change_val == 300`.

  11. `TestUniversalFormula_FullyClosedOvernight` — `total_pnl=800 (realised from closing
      100 shares at 98), prev_close=95, avg=90, payload.overnight_quantity=100, qty=0`.
      `day_pnl = 800 - (95-90)*100 = 300` == `(98-95)*100`. Assert `day_change_val == 300`.

  12. `TestUniversalFormula_PartialClose` — `oq=100, qty=50, avg=90, prev_close=95, ltp=98,
      total_pnl=(98-90)*50+(97-90)*50=750, payload.overnight_quantity=100`.
      `day_pnl = 750 - (95-90)*100 = 250`. Assert `day_change_val == 250`.

  13. `TestOvernightQuantityFromPayload` — snapshot tuple with `payload_json='{"overnight_quantity":80}'`,
      `qty=50`. Assert `build_row_from_snapshot_raw(r).overnight_quantity == 80`.

  **`backend/tests/broker/test_enrich_positions_realised.py`** (new)

  14. `TestEnrichPositionsAddsRealised_Kite` — DataFrame with `pnl=5000, realised=2000`.
      After `_enrich_positions`, assert `pnl == 7000`.

  15. `TestEnrichPositionsNoRealisedColumn` — DataFrame without `realised` column.
      Assert `_enrich_positions` does not crash; `pnl` = broker_pnl (no realised added).

  16. `TestEnrichPositionsRealisedNullRow` — DataFrame with `pnl=5000, realised=NaN`.
      After `_enrich_positions`, assert `pnl == 5000` (NaN treated as 0).

  17. `TestEnrichPositionsDhanRow` — Dhan normalised row `{pnl=500 (unrealised), realised=3000}`.
      After `_enrich_positions`, assert `pnl == 3500`.

  18. `TestEnrichPositionsGrowwRow` — Groww normalised row `{pnl=4000 (unrealised_pnl), realised=1500}`.
      After `_enrich_positions`, assert `pnl == 5500`.

  **`backend/tests/test_exchange_clock.py`** (new)

  19. `TestHolidayOverrideBlocksDefault` — NON-MCX default (open 08:00–15:30,
      weekdays=[0,1,2,3,4]) + override (date=today, open_time=None). Time: 11:00 weekday.
      `is_exchange_open("NSE")` → False. `get_today_gate_sessions("NON-MCX")` → [].
      `sessions_with_snapshot_time_now()` → empty.

  20. `TestSpecialSessionCustomTimes` — NON-MCX override (date=today, open_time=18:00,
      close_time=21:00). At 19:00 → True; at 14:00 → False (default suppressed).

  21. `TestWeekendDefaultRowBlocked` — NON-MCX default (weekdays=[0,1,2,3,4]), Saturday.
      `is_exchange_open("NSE")` → False.

  22. `TestNoSnapshotOnHoliday` — override (open_time=None, no snapshot_time). Time: 15:45.
      `sessions_with_snapshot_time_now()` → empty.

  23. `TestMCXHolidayDoesNotAffectNSE` — MCX override (open_time=None) + NON-MCX default.
      Time: 11:00. `is_exchange_open("MCX")` → False, `is_exchange_open("NSE")` → True.

  24. `TestSpecialSessionSnapshot` — NON-MCX override (open_time=18:00, snapshot_time=21:15).
      Time: 21:15. `sessions_with_snapshot_time_now()` → returns override row.

  25. `TestSettlementCutoff` — NON-MCX default (open_time=08:00).
      At 10:00 → today 08:00. At 07:00 → yesterday 08:00.

- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(snapshot): ltp=0 NULLIF+neutralise+row-filter guards; universal day_pnl (all states); pnl+realised (Kite/Dhan/Groww); exchange_clock date-override-first

## Done when

**Holdings (all brokers, weekend-correct):**
- Saturday: holdings shows correct positive day P&L (ltp=150 preserved, prev_close=Thursday_settlement)
- ltp=0 MCX snapshot cannot overwrite Friday ltp (UPSERT NULLIF)
- previous_close not corrupted by ltp=0 rolling-shift
- Zero-ltp rows excluded from results (row-level WHERE filter)
- NSE holdings still appear in Saturday view (writer neutralises ltp, does not skip the row)

**Positions (all three states, all three brokers):**
- Overnight open: `day_pnl = (ltp - prev_close) × oq` ✓
- New today (oq=0): `day_pnl = (ltp - entry) × qty` ✓ (not prev_close-based)
- Partially closed: realised portion included in both `pnl` and `day_pnl` ✓
- Fully closed intraday: `day_pnl = realised` ✓ (not 0)
- Fully closed overnight: `day_pnl = (exit - prev_close) × oq` ✓
- `overnight_quantity` reflects true opening quantity from payload_json
- Kite, Dhan, Groww live pnl = unrealised + realised (via _enrich_positions)
- Snapshot write total_pnl = unrealised + realised (via daily_snapshot.py Fix 3)

**Exchange clock (unified row model):**
- Holiday override (open_time=NULL): `is_exchange_open` → False during trading hours
- Muhurat/early-close override: `is_exchange_open` respects custom hours
- MCX-only holiday does not affect NSE and vice versa
- Weekend → False (weekdays=[0,1,2,3,4], no override)
- `sessions_with_snapshot_time_now()` → empty for holiday gate (open_time=NULL)

**Tests:** All pytest pass (broker ≥ 80%, api ≥ 45%)
