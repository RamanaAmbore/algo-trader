# Plan: Skip startup snapshot on weekends/holidays to prevent Day P&L = 0

## Context

### Root cause (confirmed)

After the `previous_close` SSOT deploy (00:30 IST Saturday 2026-08-16), the service
restarted and `_task_daily_snapshot` fired its startup snapshot. The startup snapshot
ran `snapshot_daily_book(market_open=False)` with `target_date = today_indian() =
2026-08-16` (Saturday). This created 139 `daily_book` rows for `date = 2026-08-16`
with stale prices (LTP ≈ Friday close, `day_pnl ≈ 0`).

The `_POSITIONS_SNAPSHOT_SQL`'s `latest_batch` CTE picks `MAX(captured_at)` globally.
Saturday's rows (captured 00:32 IST) are 6 minutes newer than Friday's MCX-close rows
(captured 23:56 IST). So `latest_batch` → Saturday stale data. `prev_batch` →
Friday EOD. `Day P&L = Saturday_pnl - Friday_pnl ≈ 0` (prices unchanged).

### Immediate mitigation (already done)
`DELETE FROM daily_book WHERE date = '2026-08-16'` — 139 rows deleted on prod.
Day P&L is now live showing Friday's correct values.

### Recurrence scenarios

1. **Service restart on any Saturday/Sunday** — startup snapshot fires, creates
   stale weekend rows, Day P&L = 0 for the entire weekend.
2. **Service restart on a weekday NSE holiday** — same pattern, affects that
   holiday's data.
3. **MCX Saturdays** — MCX IS open Saturday 09:00–23:30. The 23:31 snapshot
   creates correct Saturday MCX rows. The startup snapshot at restart time (before
   MCX opens) creates stale rows that pollute `latest_batch` until 23:31.

---

## Files to change

- `backend/api/background.py` — `_task_daily_snapshot()` startup snapshot logic
  (lines ~1844–1856)

---

## Detailed change

In `_task_daily_snapshot`, after the `_probe_nse_mcx()` call but BEFORE the
`if _nse_open or _mcx_open` branch, add a weekend check:

```python
# Skip startup snapshot on weekends — the previous trading day's EOD data
# already lives in daily_book. Creating today's date rows with stale prices
# displaces the EOD rows in latest_batch (MAX captured_at), making Day P&L = 0.
_today_d = timestamp_indian().date()
if _today_d.weekday() >= 5:
    logger.info(
        "Background: skipping startup snapshot — weekend "
        "(existing EOD data serves closed-hours Pulse correctly)"
    )
else:
    # Optional: also check NSE holiday list to block weekday-holiday restarts.
    # For now, weekend-only guard covers the most common case (Saturday MCX
    # pre-session, Sunday NSE/MCX). Weekday holidays are less frequent.
    if _nse_open or _mcx_open:
        logger.info(
            f"Background: skipping startup daily snapshot — markets open "
            f"(NSE={_nse_open}, MCX={_mcx_open}). Settlement passes still fire."
        )
    else:
        await _fire_snapshot("startup", market_open=False)
```

Replace the existing block (lines ~1844–1856):
```python
_nse_open, _mcx_open = await _probe_nse_mcx(_now_ist)
if _nse_open or _mcx_open:
    logger.info(...)
else:
    await _fire_snapshot("startup", market_open=False)
```

With:
```python
_today_d = _now_ist.date()
_nse_open, _mcx_open = await _probe_nse_mcx(_now_ist)
if _today_d.weekday() >= 5:
    logger.info(
        "Background: skipping startup snapshot — weekend "
        "(existing EOD data serves closed-hours Pulse correctly)"
    )
elif _nse_open or _mcx_open:
    logger.info(
        f"Background: skipping startup daily snapshot — markets open "
        f"(NSE={_nse_open}, MCX={_mcx_open}). Settlement passes still fire."
    )
else:
    await _fire_snapshot("startup", market_open=False)
```

---

## Agents

- backend: Fix `_task_daily_snapshot` in `backend/api/background.py` as above
- frontend: skip
- broker: skip
- doc: skip
- backend-test: Add pytest test — startup snapshot skipped when `today.weekday() >= 5`
- playwright: skip

---

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

### Required test cases
- Mock `timestamp_indian()` to return a Saturday → verify `_fire_snapshot` NOT called
- Mock to return a Sunday → same
- Mock to return a weekday (non-holiday, market closed) → verify `_fire_snapshot` IS called
- Mock to return a weekday with market open → verify `_fire_snapshot` NOT called (existing guard)

---

## Commit message
fix(background): skip startup snapshot on weekends to prevent Day P&L = 0 after off-day restart

## Done when
- `_task_daily_snapshot` does NOT create daily_book rows for Saturday or Sunday on service restart
- Weekday non-holiday off-hours restarts still fire the startup snapshot (unchanged)
- Existing NSE/MCX settlement passes (16:15, 23:31, 00:15) unaffected
- pytest green
