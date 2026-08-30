# Plan: Fix previous_close corruption — immutable in UPSERT, dynamic open-time trigger

## Root cause
The UPSERT rolling-shift in `_UPSERT_SQL` (`daily_snapshot.py:852-854`) overwrites
`previous_close` every time `ltp` changes:
```sql
previous_close = CASE WHEN EXCLUDED.ltp IS NOT NULL AND EXCLUDED.ltp != 0
                           AND (daily_book.ltp IS NULL OR EXCLUDED.ltp != daily_book.ltp)
                      THEN daily_book.ltp ELSE daily_book.previous_close END,
```
Positions get ~15 intraday writes per day. By 15:45 EOD, `previous_close` has been
rolled through every intraday ltp and ends up at the last intraday price (~407.4) instead
of Thursday's settlement (374.10).

Additionally, the writer uses `ltp_val` as a last resort for `previous_close` when
both `prev_ltp_map` and `close_price` are unavailable. This sets `previous_close = ltp`.

Three hardcoded `dtime(8, 0)` / `timedelta(hours=8)` references determine when
`previous_close` transitions to the new session. These must use the actual market open
time from `exchange_schedule` (default 08:00, overridden per date for special sessions).

## Correct design (user confirmed)
- `previous_close` is set ONCE — at the INSERT (via `prev_ltp_map` = yesterday's ltp).
- ON CONFLICT DO UPDATE must NEVER touch `previous_close`.
- `ltp` updates freely until settlement (NSE 15:45, MCX 00:15) — unaffected.
- Today's session open time is read from `exchange_schedule` at **startup** and again
  at **04:00 IST** (piggybacked on `_task_holiday_refresh`) — stored as module-level
  variable `_TODAY_NSE_OPEN` in `exchange_clock.py`.
- `fix_daily_book_prev_close` fires when `now.time() >= _TODAY_NSE_OPEN` (replacing
  the hardcoded `dtime(8, 0)` guard) and is the ONLY mechanism to update `previous_close`.
- If today has a holiday override (`open_time=None`), `_TODAY_NSE_OPEN` is `None` →
  fix does not fire → `previous_close` unchanged → correct for closed days.

## Agents

### broker: Five changes in `backend/api/algo/daily_snapshot.py`

1. **UPSERT SQL (`_UPSERT_SQL` line ~852)** — replace rolling-shift with immutable preserve:
   ```sql
   -- REMOVE:
   previous_close = CASE WHEN EXCLUDED.ltp IS NOT NULL AND EXCLUDED.ltp != 0
                              AND (daily_book.ltp IS NULL OR EXCLUDED.ltp != daily_book.ltp)
                         THEN daily_book.ltp ELSE daily_book.previous_close END,
   -- REPLACE WITH:
   previous_close = daily_book.previous_close,
   ```
   `ltp` update clause is unchanged — ltp continues to update freely.

2. **`_holdings_rows` writer (line ~470)** — remove `ltp_val` fallback:
   ```python
   # REMOVE the trailing `or ltp_val`:
   "previous_close": (
       (prev_ltp_map or {}).get((account, symbol, "holdings"))
       or (float(r["close_price"]) if r.get("close_price") else None)
       # None when unavailable — reader uses prev_batch ltp as safety net
   ),
   ```

3. **`_positions_rows` writer** — same removal if the positions writer also has a
   `ltp_val` fallback for `previous_close`. Check `_positions_build_row` and
   `_position_previous_close`: cap `previous_close_val` to `None` instead of `ltp_val`.

4. **`fix_daily_book_prev_close` (line 969)** — backup old value before overwriting + replace hardcoded `timedelta(hours=8)`:
   ```python
   # CURRENT:
   today_8am = midnight + timedelta(hours=8)
   if now_ist < today_8am:
   # REPLACE WITH:
   from backend.api.helpers import exchange_clock as _ec
   _open = _ec.get_nse_open_time()          # sync — set at startup + 04:00 IST
   if _open is None:
       return 0                             # holiday → no transition
   today_open = midnight.replace(hour=_open.hour, minute=_open.minute, second=0)
   if now_ist < today_open:
   ```

5. **`prev_ltp_map` boundary (line 1121-1123)** — replace hardcoded 08:00:
   ```python
   # CURRENT:
   _snap_midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
   _snap_8am = _snap_midnight + timedelta(hours=8)
   _before_session_open = now_ist < _snap_8am
   # REPLACE WITH:
   from backend.api.helpers import exchange_clock as _ec
   _open = _ec.get_nse_open_time()
   if _open is not None:
       _snap_open = now_ist.replace(hour=_open.hour, minute=_open.minute, second=0, microsecond=0)
       _before_session_open = now_ist < _snap_open
   else:
       _before_session_open = True          # holiday → behave as before-open
   ```

### broker (exchange_clock): Two additions in `backend/api/helpers/exchange_clock.py`

Add module-level variable and loader:
```python
# Module-level — set at startup and refreshed at 04:00 IST
_TODAY_NSE_OPEN: time | None = time(8, 0)  # default; None = holiday/closed

def get_nse_open_time() -> time | None:
    """Return today's NSE session open time. None = holiday (no transition fires)."""
    return _TODAY_NSE_OPEN

async def load_today_open_time() -> None:
    """Read today's effective NSE open time from exchange_schedule and cache it.

    Called at startup (from seed_and_warm) and at 04:00 IST daily (piggybacked
    on _task_holiday_refresh) so _TODAY_NSE_OPEN is always correct for the day.
    Default = time(8, 0) when cache is empty or no matching row found.
    """
    global _TODAY_NSE_OPEN
    await refresh()                         # ensure cache is warm
    sessions = get_today_gate_sessions("NON-MCX")
    if not sessions:
        _TODAY_NSE_OPEN = time(8, 0)        # no rows → fallback default
    elif sessions[0].open_time is None:
        _TODAY_NSE_OPEN = None              # holiday override — closed
    else:
        _TODAY_NSE_OPEN = sessions[0].open_time
```

Also call `await load_today_open_time()` at the end of the existing `seed_and_warm()`.

### backend (migration + model): `previous_close_backup` column on `daily_book`

**A. Alembic migration** — add nullable column (no backfill needed; NULL = "fix not yet run for this row"):
```sql
ALTER TABLE daily_book ADD COLUMN IF NOT EXISTS
    previous_close_backup DOUBLE PRECISION DEFAULT NULL;
```
Create as a new Alembic revision in `backend/alembic/versions/`.

**B. SQLAlchemy model** (`backend/api/models.py`, `DailyBook` class) — add:
```python
previous_close_backup: Mapped[Optional[float]] = mapped_column(Double, nullable=True)
```

**C. `fix_daily_book_prev_close` UPDATE** (`daily_snapshot.py` line ~995) — save old
`previous_close` into backup column before overwriting it:
```sql
UPDATE daily_book d
SET previous_close        = r.ref_close,
    previous_close_backup = COALESCE(d.previous_close_backup, d.previous_close)
    -- COALESCE preserves first backup; idempotent on server restart within same day
FROM prev_ref r
WHERE ...
```
`previous_close_backup` is never touched by `_UPSERT_SQL` — intraday writes leave it
NULL until the morning fix runs once.
This UPDATE covers **both** `kind='holdings'` and `kind='positions'` rows (the
`prev_ref` CTE filters `kind IN ('holdings', 'positions')`) — backup applies to both.

### backend: Three changes

**A. `backend/api/background.py` — piggyback onto `_task_holiday_refresh` (line ~2325)**

At the end of each successful daily holiday refresh, add:
```python
await exchange_clock.load_today_open_time()
logger.info("Background: 04:00 IST — NSE session open time loaded: %s",
            exchange_clock.get_nse_open_time())
```

**B. `backend/api/background.py` — dynamic trigger for `fix_daily_book_prev_close` (line 2093)**

Replace hardcoded `dtime(8, 0)` window:
```python
# CURRENT:
if dtime(8, 0) <= now.time() < dtime(8, 30) and _prev_close_fix_done != today:

# REPLACE WITH:
_nse_open_t = exchange_clock.get_nse_open_time()   # sync — set at startup + 04:00
_nse_close_t = dtime((_nse_open_t.hour * 60 + _nse_open_t.minute + 30) // 60,
                     (_nse_open_t.minute + 30) % 60) if _nse_open_t else None
if (_nse_open_t is not None
        and _nse_open_t <= now.time() < _nse_close_t
        and _prev_close_fix_done != today):
```

**C. `backend/api/routes/holdings.py` — reader safety net in `_build_holding_row_from_snapshot`**

Add `previous_close_backup` to `_HOLDINGS_SNAPSHOT_SQL` SELECT (alongside existing columns).

After computing `previous_close_f` (line ~155), add before `day_change_val` computation:
```python
# Safety net for legacy rows where previous_close was stamped = ltp (rolling-shift bug).
# Priority: previous_close_backup (saved by morning fix) > prev_ltp (prev_batch CTE).
backup_f = float(row.previous_close_backup) if row.previous_close_backup else 0.0
if previous_close_f <= 0 or (ltp_f > 0 and abs(previous_close_f - ltp_f) < 0.01):
    if backup_f > 0 and abs(backup_f - ltp_f) >= 0.01:
        previous_close_f = backup_f
    elif prev_ltp_f is not None and prev_ltp_f > 0:
        previous_close_f = prev_ltp_f
```
`prev_ltp_f` comes from the `prev_batch` CTE already in `_HOLDINGS_SNAPSHOT_SQL` (line ~60).

**D. `backend/api/routes/positions_helpers.py` — same safety net in `build_row_from_snapshot_raw`**

Add `previous_close_backup` to the positions snapshot SQL SELECT (whichever query feeds
`build_row_from_snapshot_raw` — check `_POSITIONS_SNAPSHOT_SQL` or equivalent).

In `build_row_from_snapshot_raw`, after reading `previous_close` from the row, add
the same safety net as holdings:
```python
# Safety net: use previous_close_backup or prior ltp when previous_close ≈ ltp.
backup_f = float(row.previous_close_backup) if getattr(row, 'previous_close_backup', None) else 0.0
if previous_close_f <= 0 or (ltp_f > 0 and abs(previous_close_f - ltp_f) < 0.01):
    if backup_f > 0 and abs(backup_f - ltp_f) >= 0.01:
        previous_close_f = backup_f
```
With the UPSERT fix, new rows will have correct `previous_close` after the morning fix;
this guard covers legacy corrupted rows already in `daily_book`.

- frontend: skip
- doc: skip

### backend-test: Add tests in `backend/tests/`

1. **UPSERT immutability**: Insert a row with `previous_close=374.10, ltp=374.10`, then
   UPSERT again with `ltp=407.50`. Assert `daily_book.previous_close` is still `374.10`.

2. **Writer `None` fallback**: Call holdings writer with `prev_ltp_map={}` and
   `close_price=0`. Assert stored `previous_close` is `None`, not ltp.

3. **Reader safety net**: Call `_build_holding_row_from_snapshot` with
   `previous_close=407.50, ltp=407.50, prev_ltp=374.10`. Assert returned
   `close_price` (= `previous_close_f`) is `374.10`.

4. **Positions formula**: Call `build_row_from_snapshot_raw` with
   `previous_close=374.10, ltp=407.50, average_price=350.00, opening_quantity=10`.
   Assert `day_change_val ≈ 334.0`.

5. **`load_today_open_time` — default and holiday**: Patch `exchange_clock._CACHE`
   with (a) normal NON-MCX row `open_time=time(8,0)`, (b) holiday override
   `open_time=None`. Assert `get_nse_open_time()` returns correct value in each case.

6. **`fix_daily_book_prev_close` — holiday no-op**: Patch `get_nse_open_time()` to
   return `None`. Call `fix_daily_book_prev_close(now_ist)`. Assert returns `0`
   immediately without touching the DB.

7. **`previous_close_backup` saved on fix**: After `fix_daily_book_prev_close` runs,
   assert `daily_book.previous_close_backup` equals the old `previous_close` value for
   both `kind='holdings'` and `kind='positions'` rows.
   Run fix a second time; assert `previous_close_backup` is unchanged (COALESCE guard).

8. **Positions reader safety net**: Call `build_row_from_snapshot_raw` with a row where
   `previous_close=407.50, ltp=407.50, previous_close_backup=374.10`. Assert
   `day_change_val` is computed using 374.10 (backup), not 407.50.

- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(snapshot): previous_close immutable in UPSERT; open-time loaded at startup + 04:00 IST

## Done when
- Multiple intraday writes on same date do NOT change `daily_book.previous_close`
- Holdings writer stores `None` (not ltp) when prev_ltp_map and close_price unavailable
- `exchange_clock._TODAY_NSE_OPEN` is populated at startup and refreshed at 04:00 IST
- `fix_daily_book_prev_close` and `prev_ltp_map` boundary use `get_nse_open_time()`
- On holiday (`open_time=None`): fix does not fire, prev_ltp_map behaves as before-open
- `_build_holding_row_from_snapshot` uses `prev_ltp_f` when `previous_close_f ≈ ltp_f`
- Positions day P&L formula computes correctly with stable `previous_close`
- `daily_book.previous_close_backup` holds the pre-fix value for both holdings and positions after morning fix runs
- `previous_close_backup` is NULL before the fix runs and immutable once set (per day)
- Holdings and positions readers use `previous_close_backup` as primary fallback when `previous_close ≈ ltp`
- All tests green
