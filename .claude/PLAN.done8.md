# Plan: Daily session lifecycle — SessionGuard + event-driven ticker + prev_close correctness

## Context

Root cause: `exchange_clock._CACHE` empty after restart → `is_any_segment_open()` fail-open
returns `True` on weekends → live broker path runs → `prev_settlement_pnl` cancellation
causes chg% = 0. Fix requires a correct market-day derivation, a clean session lifecycle,
and a recovery process that catches up missed timed events on restart without DB persistence.

---

## Timed events (market open day)

Six one-time events fire in order on every market day:

| Event | Time | Responsibility |
|---|---|---|
| `MarketCalendar` | 04:00 | Derive market day from `exchange_schedule` + overrides |
| `CloseReset` | 08:00 | Fetch BHAV via Kite REST → update `previous_close` in `daily_book` — idempotent |
| `NonMcxClose` | `exchange_schedule.close_time` (NON-MCX) | Gate flips — NON-MCX closed; unsubscribe NON-MCX symbols at close + 1 min (WebSocket stays open for MCX) |
| `NonMcxSnapshot` | `_effective_snapshot_time()` NON-MCX | EOD DB write — `daily_book` rows |
| `McxClose` | `exchange_schedule.close_time` (MCX) | Gate flips — MCX closed; stop ticker at close + 1 min (WebSocket teardown) |
| `McxSnapshot` | `_effective_snapshot_time()` MCX | EOD DB write — `daily_book` rows |

**Snapshot time derivation** (`_effective_snapshot_time(row)`):
- `snapshot_time` NULL in `exchange_schedule` → `close_time + 15 min`
- `snapshot_time` set explicitly → use that value

Snapshot is not independently scheduled — always derived from close unless overridden.

`is_exchange_closed_now()` re-derives from `exchange_schedule` + current time on every call
(used by `closed_hours_or_broker()` gate). No sentinel needed.

**Why REST for snapshot, not WebSocket**: KiteTicker stops streaming at market close (15:30
NON-MCX, 23:30 MCX). Call auction settlement (NON-MCX) and MCX final price are computed
after the WebSocket stops. REST `last_price` at snapshot time captures the settled price.

---

## SessionGuard

Runs at every server startup. No DB persistence — all events are idempotent via API:
- `CloseReset`: Kite REST always returns correct BHAV after 08:00 regardless of how many times called.
- `NonMcxSnapshot` / `McxSnapshot`: Kite REST `last_price` always returns correct price —
  live if market open, frozen settlement if closed.

**Startup sequence:**

```
1. Recovery (unconditional — no market day check):
   a. time ≥ 08:00               → run CloseReset
   b. time ≥ NonMcxClose time    → run NonMcxClose (gate flip + unsubscribe at close+1min)
   c. time ≥ NonMcxSnapshot time → run NonMcxSnapshot
   d. time ≥ McxClose time       → run McxClose (gate flip + stop_ticker at close+1min)
   e. time ≥ McxSnapshot time    → run McxSnapshot

2. Derive market day from exchange_schedule (_is_market_day_today())
   Not a market day → done

3. Market day + market currently open → schedule future timed events at their times
```

Recovery works without symbol fetch — REST fetch needs no instrument tokens.
Ticker callbacks (unsubscribe/stop) are no-ops when ticker is already stopped.

---

## KiteTicker lifecycle — event-driven, no timers

| Current | New |
|---|---|
| `unsubscribe_non_mcx()` at 16:15 hardcoded sentinel | `unsubscribe_non_mcx()` at `NonMcxClose time + 1 min` (15:31) |
| `stop_ticker()` at 00:30 hardcoded sentinel | `stop_ticker()` at `McxClose time + 1 min` (23:31) |
| `start_ticker()` at 08:00 | unchanged — token rotation still requires 08:00 restart |

Unsubscribe/stop fire 1 minute after close — independent of snapshot. Snapshot uses REST
and does not require the ticker to be running.

**Full sequence:**
```
NonMcxClose (15:30) → unsubscribe NON-MCX symbols (15:31, WebSocket stays open for MCX)
NonMcxSnapshot (15:45) → fetch via REST, write daily_book

McxClose (23:30) → stop ticker entirely (23:31, WebSocket teardown)
McxSnapshot (23:45) → fetch via REST, write daily_book
```

1-minute buffer ensures last ticks from the closing minute are captured before disconnecting.
KiteTicker stops streaming MCX ticks at close — stop at close + 1 min aligns with this.

All times derived from `exchange_schedule.close_time` — not hardcoded. Date-specific overrides
flow through automatically. 15:30 / 23:30 are defaults only.

Removes: `_unsub_nonmcx_done` sentinel (16:15), `_ticker_stop_done` sentinel (00:30).
Adds: scheduled `unsubscribe_non_mcx()` at `NonMcxClose + 1 min`; `stop_ticker()` at `McxClose + 1 min`.

---

## MarketCalendar fix: weekend gap in `load_today_open_time()`

**Problem**: On weekends, `_effective_gate_rows("NON-MCX")` returns `[]` → falls to
`not rows` branch → `_TODAY_MARKET_OPEN = time(8, 0)` (wrongly named `_TODAY_NSE_OPEN` in current code) → CloseReset fires on weekends with stale BHAV.

**Fix**: Replace `_TODAY_NSE_OPEN` with `_is_market_day_today() -> bool`. Reads purely
from `exchange_schedule` via `_effective_gate_rows()` (DB-backed cache) — no hardcoded times:

```python
def _is_market_day_today() -> bool:
    non_mcx = _effective_gate_rows("NON-MCX")
    mcx     = _effective_gate_rows("MCX")
    non_mcx_open = bool(non_mcx) and non_mcx[0].open_time is not None
    mcx_open     = bool(mcx)     and mcx[0].open_time     is not None
    return non_mcx_open or mcx_open
```

- `_effective_gate_rows()` applies weekday filter + date-specific overrides from `exchange_schedule`
- `open_time IS NOT NULL` → market open; holidays set `open_time = NULL` → closed
- `[]` rows → weekend (weekday filter excludes Sat/Sun) → `False`
- All schedule decisions (open/close/snapshot times, holidays, special sessions) come from
  `exchange_schedule` — no hardcoding anywhere in this path

SessionGuard uses `_is_market_day_today()` directly. No downstream process calls
`get_nse_open_time()` — all gate on `_is_market_day_today()`.

**Fix fail-open bug**: `is_any_segment_open()` line 282: `if not _CACHE: return True` →
change to `return False` (fail-closed — empty cache means schedule not loaded, not market open).

---

## exchange_schedule: nullable snapshot_time

`snapshot_time` column made nullable. Default = `close_time + 15 min` when NULL.
Seed rows use NULL (derived). Date-specific overrides can still set it explicitly.

**File**: `backend/api/helpers/exchange_clock.py`

```python
def _effective_snapshot_time(row) -> time | None:
    if row.snapshot_time:
        return row.snapshot_time
    if row.close_time:
        dt = datetime.combine(date.today(), row.close_time) + timedelta(minutes=15)
        return dt.time()
    return None
```

`sessions_with_snapshot_time_now()` calls `_effective_snapshot_time(row)` instead of
reading `row.snapshot_time` directly.

Migration in `seed_and_warm()`: `UPDATE exchange_schedule SET snapshot_time = NULL WHERE source = 'system'`

---

## Files to change

| File | Change |
|---|---|
| `backend/api/helpers/exchange_clock.py` | Add `_is_market_day_today()`, `_effective_snapshot_time()`; fix fail-closed in `is_any_segment_open()` |
| `backend/api/background.py` | **Recovery**: new `_session_guard()` runs at startup — time-gated unconditional catchup (CloseReset, NonMcxClose+unsubscribe, NonMcxSnapshot, McxClose+stop_ticker, McxSnapshot); then market day check; then schedule future timed events if open. Remove 16:15 + 00:30 hardcoded sentinels. |
| `backend/api/algo/daily_snapshot.py` | Gate CloseReset on `_is_market_day_today()`; derive snapshot times via `_effective_snapshot_time()` |

---

## Done when
- `SessionGuard` runs at startup, recovers missed timed events via idempotent API calls — no DB persistence
- `_is_market_day_today()` correctly returns False on weekends/holidays
- `is_any_segment_open()` fails closed (not open) when cache is empty
- `CloseReset` never runs on non-market days
- `unsubscribe_non_mcx()` at `NonMcxClose + 1 min` (no 16:15 hardcoded timer)
- `stop_ticker()` at `McxClose + 1 min` (no 00:30 hardcoded timer)
- `snapshot_time` derived from `close_time + 15` when NULL in `exchange_schedule`
- chg% and day_pnl correct on weekends/off-market hours
- All new tests pass
