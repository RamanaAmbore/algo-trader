# Plan: Fix Snapshot Cutoff — Positions Day P&L = 0 + Holdings Oscillation (Any Non-Trading Day)

## Context

**Bug report (2026-09-06, Saturday, market closed):**
- Positions Day P&L = -0.00 (should be ~2.03L)
- Holdings Day P&L oscillating between ~2K and ~89K (correct is ~89K)

**Root cause — affects any non-trading day (weekends, single holidays, Diwali runs):**

`_fetch_snapshot_close_map` (positions) and `_override_stale_close_for_holdings` (holdings)
query `daily_book` with `captured_at < settlement_cutoff_for()` (= today 08:00 IST).
`DISTINCT ON + DESC` returns the **most recent** snapshot before that boundary.

On any non-trading day, the most recent snapshot = prior trading day's settlement (e.g. Fri 23:45).
Broker REST also returns ltp frozen at that same price. Result: `close_price = ltp` → Day P&L = 0.

**Compounding bug — `exchange_schedule.weekdays` is NULL:**

`seed_and_warm()` has a migration (`SET weekdays = '[0,1,2,3,4]'`) but the value
is JSON notation; PostgreSQL array columns require `ARRAY[0,1,2,3,4]`. Migration silently
fails → `weekdays IS NULL` on both MCX and NON-MCX default rows → `_row_matches_now()`
returns True every day including Saturday → `is_any_segment_open()` = True on weekends
08:00–23:30. Any gate built on `is_any_segment_open()` alone is unreliable on weekends.

**How `daily_book` is written (key facts):**
- Written **once per trading day** per account/symbol at `exchange_schedule.snapshot_time`
  - MCX: 23:45 IST, NSE: 15:45 IST — from DB column, NOT hardcoded
- Consecutive `daily_book` entries for the same symbol are ~24 hours apart (one per trading day)
- No intraday writes; no close+settlement split — one write per day at `snapshot_time`
- On non-trading days: no new writes — latest entry is the prior trading day's snapshot

**Design goals (ALL must hold simultaneously):**
- No hard-coded market hours, close times, or settlement times — fully calendar-driven
- Muhurrat trades (special Saturday session): exchange_schedule date override row handles it
- Weekend trading days: same date override mechanism
- Survive code redeployment at any time on any day

---

## Fix Overview

**Two changes work together:**

1. **Fix the weekdays migration SQL** in `exchange_clock.py:seed_and_warm()` so
   `exchange_schedule.weekdays = ARRAY[0,1,2,3,4]` on default rows after the next deploy.
   This makes `_effective_gate_rows()` return empty on regular weekends — no hard-coded
   weekday guard needed anywhere.

2. **New gate + two-CTE query** in positions.py and holdings.py:
   - Gate: `is_market_active_for_prev_close()` — new function in `exchange_clock.py`
   - Trading day / active: single query (existing path) — latest snapshot < today 08:00
   - Non-trading day / fully closed: two-CTE — `latest_batch → prev_batch`
     (no time-window filter since `daily_book` has one write per trading day, ~24h gap)

---

## Scenario Walkthrough

| Scenario | `_effective_gate_rows` returns | `is_market_active_for_prev_close()` | Path |
|---|---|---|---|
| Sat 10:00 (no override) | empty (weekdays fixed → Mon-Fri filter) | False | Two-CTE → Thu settlement ✓ |
| Muhurrat Sat 18:30 NSE override open | override row, 18:15–19:15, within session | True | Single query → Fri settlement ✓ |
| Muhurrat Sat 22:00 (after snapshot) | override row, after snapshot_time | False | Two-CTE → Thu settlement ✓ |
| Diwali Thu (Tue was last trading day) | empty (holiday override with open_time=None) | False | Two-CTE → Fri settlement ✓ |
| Live Tue 14:00 NSE open | default NON-MCX row, within session | True | Single query → Mon settlement ✓ |
| Tue 23:35 post-MCX-close, before 23:45 snap | default MCX row, in snapshot window | True | Single query → Mon settlement ✓ |
| Tue 23:50 after MCX snapshot | default MCX row, after snapshot_time | False | Two-CTE → Mon settlement ✓ |
| Redeploy Sat 14:00 | empty (after migration fix runs on startup) | False | Two-CTE → Thu settlement ✓ |

---

## Files to Change

### 1. `backend/api/helpers/exchange_clock.py`

**Fix weekdays migration SQL (~line 454) — change JSON notation to PostgreSQL array:**

```python
# BEFORE (broken — JSON notation silently fails for PostgreSQL integer[] column):
await session.execute(_text("""
    UPDATE exchange_schedule
    SET weekdays = '[0,1,2,3,4]'
    WHERE weekdays IS NULL
      AND date IS NULL
      AND source = 'system'
"""))

# AFTER (correct — PostgreSQL array literal notation):
await session.execute(_text("""
    UPDATE exchange_schedule
    SET weekdays = ARRAY[0,1,2,3,4]
    WHERE weekdays IS NULL
      AND date IS NULL
      AND source = 'system'
"""))
```

**Add two new functions after `is_any_segment_open()`:**

```python
def is_market_active_for_prev_close() -> bool:
    """True when the existing single-query prev_close path is correct.

    Returns True if any segment is:
    - Currently in-session (open_time <= now < close_time), OR
    - In the post-close snapshot window (close_time <= now <= snapshot_time)
      where today's settlement snapshot hasn't fired yet.

    Returns False on non-trading days (weekends, holidays, after snapshot_time).
    Fully calendar-driven — no hard-coded hours or weekday numbers.
    exchange_schedule.weekdays must be [0,1,2,3,4] on default rows (fixed by migration).
    """
    if not _CACHE:
        return True  # fail-open: use single query path when cache not loaded
    now = _now_ist()
    for gate in {r.gate for r in _CACHE}:
        for row in _effective_gate_rows(gate):
            if row.open_time is None or row.close_time is None:
                continue  # holiday override — closed
            now_t = now.time().replace(second=0, microsecond=0)
            # In-session
            if row.open_time <= now_t < row.close_time:
                return True
            # In post-close snapshot window
            if row.snapshot_time is not None and row.close_time <= now_t <= row.snapshot_time:
                return True
    return False


def is_trading_day_today() -> bool:
    """True if any segment is scheduled to trade today.

    Uses _effective_gate_rows so: holiday override rows (open_time=None) → False,
    weekday-filtered default rows (weekdays=[0,1,2,3,4]) → False on weekends,
    date-specific Muhurrat/weekend-trading overrides → True.
    Fully calendar-driven.
    """
    if not _CACHE:
        return True  # fail-open
    for gate in {r.gate for r in _CACHE}:
        for row in _effective_gate_rows(gate):
            if row.open_time is not None:
                return True
    return False
```

### 2. `backend/api/routes/positions.py`

**`_fetch_snapshot_close_map(raw, cutoff)` — around line 913 — two-path rewrite:**

```python
async def _fetch_snapshot_close_map(raw: pd.DataFrame, cutoff: datetime) -> dict:
    from backend.api.helpers.exchange_clock import is_market_active_for_prev_close
    from zoneinfo import ZoneInfo

    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    today_08 = now_ist.replace(hour=8, minute=0, second=0, microsecond=0)

    async with get_session() as session:
        if is_market_active_for_prev_close():
            # Trading day / snapshot pending — latest entry before today 08:00 = correct prev_close
            result = await session.execute(_sql_text("""
                SELECT DISTINCT ON (account, symbol)
                       account, symbol, ltp AS ref_close, total_pnl
                FROM daily_book
                WHERE kind = 'positions'
                  AND ltp IS NOT NULL AND ltp > 0
                  AND captured_at < :today_08
                ORDER BY account, symbol, captured_at DESC
            """), {"today_08": today_08})
        else:
            # Non-trading day — latest entry = frozen ltp = NOT prev_close.
            # Need the entry BEFORE the latest. daily_book has one write per trading
            # day (~24h gap between entries), so no time-window filter needed.
            result = await session.execute(_sql_text("""
                WITH latest_batch AS (
                    SELECT account, symbol, MAX(captured_at) AS max_at
                    FROM daily_book
                    WHERE kind = 'positions'
                      AND ltp IS NOT NULL AND ltp > 0
                      AND captured_at < :today_08
                    GROUP BY account, symbol
                ),
                prev_batch AS (
                    SELECT DISTINCT ON (db.account, db.symbol)
                           db.account, db.symbol,
                           db.ltp AS ref_close,
                           db.total_pnl
                    FROM daily_book db
                    JOIN latest_batch lb
                      ON db.account = lb.account AND db.symbol = lb.symbol
                    WHERE db.kind = 'positions'
                      AND db.ltp IS NOT NULL AND db.ltp > 0
                      AND db.captured_at < lb.max_at
                    ORDER BY db.account, db.symbol, db.captured_at DESC
                )
                SELECT account, symbol, ref_close, total_pnl FROM prev_batch
            """), {"today_08": today_08})

        rows = result.mappings().all()
    # ... rest of existing mapping logic unchanged
```

`_override_stale_close_from_snapshot()` — no change; it calls `_fetch_snapshot_close_map`.

### 3. `backend/api/routes/holdings.py`

**`_override_stale_close_for_holdings()` — around line 454 — same two-path pattern:**

```python
async def _override_stale_close_for_holdings(rows: list[dict]) -> list[dict]:
    from backend.api.helpers.exchange_clock import is_market_active_for_prev_close
    from zoneinfo import ZoneInfo

    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    today_08 = now_ist.replace(hour=8, minute=0, second=0, microsecond=0)

    async with get_session() as session:
        if is_market_active_for_prev_close():
            result = await session.execute(_sql_text("""
                SELECT DISTINCT ON (account, symbol)
                       account, symbol, ltp AS ref_close
                FROM daily_book
                WHERE kind = 'holdings'
                  AND ltp IS NOT NULL AND ltp > 0
                  AND captured_at < :today_08
                ORDER BY account, symbol, captured_at DESC
            """), {"today_08": today_08})
        else:
            result = await session.execute(_sql_text("""
                WITH latest_batch AS (
                    SELECT account, symbol, MAX(captured_at) AS max_at
                    FROM daily_book
                    WHERE kind = 'holdings'
                      AND ltp IS NOT NULL AND ltp > 0
                      AND captured_at < :today_08
                    GROUP BY account, symbol
                ),
                prev_batch AS (
                    SELECT DISTINCT ON (db.account, db.symbol)
                           db.account, db.symbol,
                           db.ltp AS ref_close
                    FROM daily_book db
                    JOIN latest_batch lb
                      ON db.account = lb.account AND db.symbol = lb.symbol
                    WHERE db.kind = 'holdings'
                      AND db.ltp IS NOT NULL AND db.ltp > 0
                      AND db.captured_at < lb.max_at
                    ORDER BY db.account, db.symbol, db.captured_at DESC
                )
                SELECT account, symbol, ref_close FROM prev_batch
            """), {"today_08": today_08})
    # ... rest of existing patching logic unchanged
```

### 4. `backend/api/background.py`

**`_task_daily_snapshot()` — add `is_trading_day_today()` guard:**

```python
async def _task_daily_snapshot():
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    if now.weekday() >= 5:  # existing: skip weekends
        return
    from backend.api.helpers.exchange_clock import is_trading_day_today
    if not is_trading_day_today():  # new: skip weekday holidays
        return
    ...  # rest unchanged
```

---

## Redeployment Safety Analysis

| Scenario | What happens on startup | Result |
|---|---|---|
| Sat 14:00 redeploy | `seed_and_warm` runs → migration sets weekdays=ARRAY[0,1,2,3,4] → cache warm | `is_market_active_for_prev_close()` = False → two-CTE → correct ✓ |
| Mon holiday redeploy | weekdays migration runs → holiday override row (open_time=None) in cache | `is_market_active_for_prev_close()` = False; `is_trading_day_today()` = False → no startup snapshot written ✓ |
| Live Tue redeploy | weekdays migration runs → default rows with weekdays=[0,1,2,3,4] → in-session | `is_market_active_for_prev_close()` = True → single query → correct ✓ |
| Muhurrat Sat redeploy (before session) | override row loaded → open_time not yet in window | False → two-CTE → prev prev_close ✓ |
| Muhurrat Sat redeploy (during session) | override row loaded → in-session | True → single query → Fri settlement ✓ |

**ltp, qty accuracy on redeployment:**
- Position qty / holdings qty: fresh from broker REST on first request → always accurate
- ltp: from KiteTicker WebSocket (reconnects immediately) or broker REST fallback. On non-trading day, WebSocket sends last settlement price within seconds of connect → ltp = correct frozen settlement
- prev_close: fixed by this plan → two-CTE gives correct prior settlement ✓

---

## Tests

### `backend/tests/test_holiday_snapshot_cutoff.py` (new file)

Mock `is_market_active_for_prev_close()` + async DB.

**Two-CTE path (market closed):**

| Test | daily_book rows | Expected ref_close |
|---|---|---|
| `test_saturday_mcx` | Fri 23:45 ltp=500, Thu 23:45 ltp=490 | 490 (Thu settlement) |
| `test_diwali_3day_gap` | Tue 23:45 ltp=500, Fri 23:45 ltp=480 (72h gap) | 480 (Fri) |
| `test_nse_non_trading` | Fri 15:45 ltp=1000, Thu 15:45 ltp=980 | 980 (Thu) |
| `test_no_prev_returns_empty` | only one row: Fri 23:45 | empty map |
| `test_holdings_saturday` | Fri 23:45 ltp=1000, Thu 23:45 ltp=980 | 980 (Thu), holdings kind |

**Single-query path (market active):**

| Test | daily_book rows | Expected ref_close |
|---|---|---|
| `test_live_tuesday` | Mon 23:45 ltp=490 | 490 (Mon, single query) |
| `test_post_close_snapshot_window` | Mon 23:45 ltp=490 | 490 (Mon) |

**`is_market_active_for_prev_close()` unit tests** (mock `_effective_gate_rows()`):

| Test | Mocked rows | Expected |
|---|---|---|
| `test_empty_rows_returns_false` | [] | False |
| `test_holiday_override_none_open` | [open_time=None] | False |
| `test_in_session` | [open=08:00, close=23:30, snap=23:45], now=14:00 | True |
| `test_post_close_in_window` | same, now=23:35 | True |
| `test_after_snapshot_false` | same, now=23:50 | False |
| `test_muhurrat_in_session` | [open=18:15, close=19:15, snap=19:30], now=18:30 | True |

**`is_trading_day_today()` unit tests:**

| Test | Mocked rows | Expected |
|---|---|---|
| `test_empty_rows_not_trading` | [] | False |
| `test_holiday_override` | [open_time=None] | False |
| `test_regular_weekday` | [open_time=08:00] | True |

**Weekdays migration fix test:**
- `test_weekdays_migration_sql`: runs the migration SQL in a test transaction, confirms weekdays = [0,1,2,3,4] after update

---

## Agents

- backend: (1) Fix weekdays migration SQL in exchange_clock.py; (2) Add `is_market_active_for_prev_close()` + `is_trading_day_today()` to exchange_clock.py; (3) Rewrite `_fetch_snapshot_close_map` in positions.py; (4) Rewrite `_override_stale_close_for_holdings` in holdings.py; (5) Add `is_trading_day_today()` guard to `_task_daily_snapshot` in background.py
- frontend: skip
- broker: skip
- doc: skip
- backend-test: Write `backend/tests/test_holiday_snapshot_cutoff.py` per test table above
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(backend): calendar-driven prev_close — two-CTE on closed days, fix weekdays migration SQL

## Done when
- Positions Day P&L non-zero on Saturday (~2.03L based on Thu→Fri MCX move)
- Holdings Day P&L stable (~89K, not oscillating)
- Works for single holidays, multi-day runs (Diwali), Muhurrat Saturday trades
- Survives code redeployment at any time on any day
- `exchange_schedule.weekdays = {0,1,2,3,4}` on default rows after deploy
- pytest green, coverage thresholds held
