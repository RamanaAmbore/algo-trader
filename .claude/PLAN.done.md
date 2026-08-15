# Plan: Fix snapshot so Dhan holdings appear after weekend/holiday restarts

## Context

The `market_open=False` startup fix landed correctly in `background.py`. After a server restart on Sat Aug 15, the startup snapshot fires with `market_open=False`. But two bugs downstream still prevent Dhan from appearing in Pulse:

**Bug 1 — Admin trigger uses time-only `_is_exchange_open_at`**
`trigger_pnl_snapshot` in `admin.py:1721` computes:
```python
_market_open = _is_exchange_open_at("NSE", now_ist) or _is_exchange_open_at("MCX", now_ist)
```
On Saturday at 14:00 IST this returns `True` (time falls inside trading window even though markets are closed). Operator can't manually re-trigger a correct snapshot from the admin panel.

**Bug 2 — Dhan returns `last_price=0` on non-trading days; no fallback**
Dhan's API sometimes returns `last_price=0` on weekends when its market-data cache is cold. The backfill (`_backfill_market_data_dicts`) attempts to fix this but may also return 0 on non-trading days (no live quotes). With `market_open=False` → `mid_session=False`, `_snap_holding_eod_vals` returns `ltp_val=0`. `_is_zero_payload_row(avg_cost>0, ltp=0, pnl=0)` fires → all Dhan rows filtered → empty upsert (no-op) → prior NULL rows preserved → `ltp IS NOT NULL` filter still excludes Dhan.

**Gap in `_snap_all_filtered`**: only protects when BOTH holdings AND positions are filtered. On weekends `raw_p_count=0`, so the condition is always False → no warning emitted when all holdings silently drop.

## Files

- `backend/api/routes/admin.py` — `trigger_pnl_snapshot` + `SnapshotRequest`
- `backend/api/algo/daily_snapshot.py` — `_snap_holding_eod_vals`, `_snap_all_filtered`
- `backend/tests/test_snapshot_market_open.py` — extend with new tests
- `backend/tests/test_snapshot_holiday_fix.py` — extend with new tests

## Agents

- backend: Fix Bug 1 + Bug 2 + `_snap_all_filtered` gap
- backend-test: Add tests for all three fixes
- doc: skip
- frontend: skip
- playwright: skip

## Detailed changes

### backend/api/routes/admin.py

1. Add `market_open: Optional[bool] = None` to `SnapshotRequest` (msgspec Struct).
2. In `trigger_pnl_snapshot`: replace the `_is_exchange_open_at` block with:
   ```python
   import asyncio
   from backend.shared.helpers.date_time_utils import is_market_open
   if data.market_open is not None:
       _market_open = data.market_open
   else:
       _market_open = await asyncio.to_thread(is_market_open)
   ```
   This is holiday+weekend aware (not time-only). Operator can also force override via body `{"date":"today","market_open":false}`.

### backend/api/algo/daily_snapshot.py — `_snap_holding_eod_vals`

Add close_price fallback when `mid_session=False` and Dhan returns `last_price=0`:
```python
# existing: ltp_val = r.get("last_price")
# add after:
if not ltp_val:  # 0, None, or missing
    ltp_val = r.get("close_price") or r.get("previous_close")
```
This ensures the DB row gets a non-zero ltp (previous-session close), passes both `ltp IS NOT NULL` and the `NOT (ltp=0 AND ...)` guard, and is correct semantics for non-trading day display.

### backend/api/algo/daily_snapshot.py — `_snap_all_filtered`

Extend condition to also warn+protect when holdings are all filtered regardless of positions (weekend case):
```python
# Current:
if raw_h_count > 0 and len(h_rows) == 0 and raw_p_count > 0 and len(p_rows) == 0:
# Change to:
if raw_h_count > 0 and len(h_rows) == 0:
    logger.warning(
        f"Snapshot [{account}] date={target_date} — ALL "
        f"{raw_h_count} holdings rows filtered (bad payload / zero ltp). "
        f"Prior snapshot preserved. No upsert performed."
    )
    return True
if raw_p_count > 0 and len(p_rows) == 0:
    logger.warning(...)
    return True
return False
```

## Tests

- Admin endpoint: verify `market_open=None` uses `is_market_open()` (weekend → False), verify explicit `market_open=False` override works
- `_snap_holding_eod_vals`: when `last_price=0` and `close_price=1500.0`, `ltp_val` resolves to `1500.0`
- `_snap_all_filtered`: returns True when all holdings filtered + no positions (weekend scenario)

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(snapshot): admin trigger uses is_market_open(); close_price fallback when last_price=0

## Done when
- `POST /api/admin/pnl/snapshot {}` auto-detects weekend/holiday via `is_market_open()` → captures non-NULL ltp
- `POST /api/admin/pnl/snapshot {"market_open": false}` explicit override works
- Dhan holdings with `last_price=0` use `close_price` as ltp → appear in Pulse on non-trading days
- `_snap_all_filtered` emits warning and skips upsert when all holdings are zero-filtered (weekend, no positions)
- All new tests green
