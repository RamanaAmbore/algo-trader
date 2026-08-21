# Plan: Fix day P&L = 0 for overnight NFO positions (stale LTP fingerprint)

## Context

Root cause confirmed via code investigation. Two separate bugs:

**Bug B — primary (causes 0 day P&L for ALL INFY overnight positions):**
Kite REST positions API returns `last_price = close_price` for NFO/MCX positions when
the WS tick hasn't arrived yet (identical stale fingerprint previously observed for MCX
CRUDEOIL). The existing `_override_stale_ltp_from_ticker` in `positions.py` only rescues
when KiteTicker has a live tick for that symbol (diff > 0.005 epsilon). When no ticker
tick is available AND broker REST LTP = close_price, `positions_policy` in `ltp_patch.py`
returns `Decision()` (no-op). Result: `day_change_val = 0` for ALL surfaces.

Fix location: `backend/brokers/broker_apis.py:_bmd_build_key_index`. The existing missing-row
mask only includes `close <= 0 OR last <= 0` rows. Overnight F&O positions with
`last_price = close_price > 0` never enter the PriceBroker.quote() batch. Extending the
mask triggers a batched quote fetch → fresh LTP → `_bmd_recompute_derived` rewrites
`day_change_val = (fresh_ltp - cls) × qty`.

`_bmd_recompute_derived` already handles this correctly: `_should_overwrite = _valid_p &
(_existing_dcv == 0)` fires for stale rows (existing dcv = 0 by construction), recomputes
with the fresh LTP.

**Bug A — secondary (causes wrong value, not 0, when closePx = 0):**
`livePositionDayPnl` in `nav.js:188`: fallback path `(live − avg) × qty` fires for
overnight positions when `closePx = 0`. For overnight positions `avg` is the entry cost
from prior sessions, not yesterday's close → shows lifetime P&L instead of 0. Fix: add
`oq === 0` guard so only today's new positions (where avg = entry = correct reference)
use this formula. Overnight positions with `closePx = 0` fall through to
`baseDayPnlForPosition` → 0 ("unknown" until broker populates close_price).

## Task

1. **Backend (Bug B):** Extend `_bmd_build_key_index` to detect overnight F&O positions
   with the stale fingerprint and add them to the backfill batch.

2. **Frontend (Bug A):** Add `oq === 0` guard to `livePositionDayPnl` at nav.js:188.

## Agents

- broker: Fix `_bmd_build_key_index` in `backend/brokers/broker_apis.py`.

  After the existing `_missing = _cls_missing | _ltp_missing` computation (around line 2116),
  ADD a stale-fingerprint extension:

  ```python
  # Stale fingerprint: overnight F&O position where Kite REST LTP = close_price
  # (WS tick not yet received; day_change_val collapses to 0 when ltp = cls).
  # Extend the backfill batch to include these rows so PriceBroker.quote() can
  # deliver a fresh LTP and _bmd_recompute_derived can fix day_change_val.
  _FO_EXCH = {'NFO', 'MCX', 'CDS', 'BFO'}
  if ('overnight_quantity' in df.columns and 'exchange' in df.columns
          and 'last_price' in df.columns and 'close_price' in df.columns):
      _oq_s   = pd.to_numeric(df['overnight_quantity'], errors='coerce').fillna(0)
      _exch_s = df['exchange'].fillna('').astype(str).str.upper()
      _ltp_s  = pd.to_numeric(df['last_price'],  errors='coerce').fillna(0)
      _cls_s  = pd.to_numeric(df['close_price'], errors='coerce').fillna(0)
      _stale_fp = (
          (_oq_s != 0) &
          (_exch_s.isin(_FO_EXCH)) &
          (_ltp_s > 0) & (_cls_s > 0) &
          ((_ltp_s - _cls_s).abs() <= 0.005)
      )
      _missing = _missing | _stale_fp

  if not _missing.any():
      return None, [], []
  ```

  No changes to `_bmd_patch_rows` or `_bmd_recompute_derived` — both handle stale-fp rows
  correctly as-is:
  - `_bmd_patch_rows`: writes fresh `last_price` from `ltp_lookup`; `close_price` from
    OHLC.close = same as REST value (effectively no-op on close_price)
  - `_bmd_recompute_derived`: `_should_overwrite = _valid_p & (existing_dcv == 0)` = True
    for stale rows; rewrites `(fresh_ltp - cls) × qty`

  Also add a test in `backend/tests/broker/test_ltp_oscillation_fixes.py`:
  - Test: DataFrame with `overnight_quantity=5, exchange='NFO', last_price=150.0,
    close_price=150.0` → `_bmd_build_key_index` returns a mask with True for that row
  - Test: DataFrame with `overnight_quantity=0, exchange='NFO', last_price=150.0,
    close_price=150.0` (new position today) → mask = False (NOT in backfill batch)
  - Test: DataFrame with `overnight_quantity=5, exchange='NSE', last_price=150.0,
    close_price=150.0` (equity, not F&O) → mask = False

- frontend: Fix `livePositionDayPnl` in `frontend/src/lib/data/nav.js` line 188.

  Current guard:
  ```js
  if (marketOpen && live != null && closePx === 0 && avg > 0 && qty !== 0) {
      return (live - avg) * qty;
  }
  ```
  Required: extract `oq = Number(dcvRow?.overnight_quantity ?? 0)` from the dcvRow
  parameter. Add `&& oq === 0` to the condition so the fallback only fires for TODAY's
  new positions. Overnight positions with `closePx = 0` fall through to
  `baseDayPnlForPosition(dcvRow)` → 0 (honest "unknown" when no prior close).

  Update JSDoc for `livePositionDayPnl` to document the `oq` check.

  Also write a Vitest unit test in `frontend/src/lib/__tests__/data/nav.test.js`:
  - overnight position (oq > 0), closePx = 0, live tick present → must return 0
  - intraday position (oq = 0), closePx = 0, live tick present → must return (live - avg) × qty

- backend-test: skip (covered in broker agent)
- playwright: skip (unit test covers it)
- doc: skip

## Tests
- pytest: yes (broker layer — `_bmd_build_key_index` stale fingerprint tests)
- svelte-check: yes
- playwright: no (Vitest unit test)

## Commit message
fix(positions): rescue stale NFO LTP fingerprint + guard livePositionDayPnl overnight

Kite REST positions API returns last_price = close_price for overnight NFO/MCX
positions before WS ticks arrive. _override_stale_ltp_from_ticker only rescues when
the ticker has a live sample (diff > 0.005 epsilon); non-subscribed symbols fall
through and day_change_val stays 0 for all surfaces.

Extend _bmd_build_key_index to detect the stale fingerprint (overnight F&O position
where abs(last_price - close_price) <= 0.005 and both > 0) and add those rows to the
PriceBroker.quote() backfill batch. _bmd_recompute_derived already handles the recompute
correctly (should_overwrite = True when existing_dcv = 0).

Also add oq === 0 guard to livePositionDayPnl fallback (nav.js:188) so overnight
positions with closePx = 0 return 0 (unknown) instead of (live - avg) × qty (lifetime P&L).

## Done when
- `_bmd_build_key_index` includes overnight NFO/MCX rows with `last_price ≈ close_price` in backfill mask
- Unit tests: stale-fp row → in mask; new-position row (oq=0) → not in mask; equity row → not in mask
- `livePositionDayPnl` at nav.js:188 has `&& oq === 0` condition
- Vitest: overnight + closePx=0 + live → 0; intraday + closePx=0 + live → (live-avg)×qty
- pytest passes, svelte-check 0 errors
