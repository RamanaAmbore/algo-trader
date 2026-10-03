# Data Persistence Pipeline Specification

Every market-data route in RamboQuant follows a three-tier caching hierarchy: in-memory
LRU (Tier 1) → PostgreSQL (Tier 2) → broker API (Tier 3). This spec defines the fetch
ladder, completeness checks, retention policies, and the canonical closed-hours gate
that prevents unnecessary broker calls when markets are shut.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/api/helpers/snapshot_gate.py` · `backend/api/background.py` ·
`backend/api/routes/quote.py` · `backend/api/routes/holdings.py` · `backend/api/routes/positions.py`

---

## Contents

1. [Three-Tier Hierarchy](#1-three-tier-hierarchy)
2. [Four Stores and Their TTLs](#2-four-stores-and-their-ttls)
3. [Fetch Ladder for Each Store](#3-fetch-ladder-for-each-store)
4. [Completeness Checks](#4-completeness-checks)
5. [Persistence Modes](#5-persistence-modes)
6. [Closed-Hours Route Gate](#6-closed-hours-route-gate)
7. [Raw Broker Cache (30s TTL)](#7-raw-broker-cache-30s-ttl)
8. [Event Queues](#8-event-queues)
9. [Holiday Calendar Pipeline](#9-holiday-calendar-pipeline)
10. [Retention Policies](#10-retention-policies)
11. [Edge Cases and Self-Healing](#11-edge-cases-and-self-healing)
12. [Test Coverage Map](#12-test-coverage-map)

---

## 1. Three-Tier Hierarchy

Every market-data fetch follows the same pattern:

```
Tier 1: In-memory LRU (asyncio.Lock per key, <1s to hours TTL)
  ↓ (if miss)
Tier 2: PostgreSQL (ohlcv_daily, instruments, holidays, intraday, daily_book)
  ↓ (if miss or hard mode)
Tier 3: Broker API (Kite, Dhan, Groww; rate-limited, slow)
  ↓ (on success)
Write-back to Tier 2 for future hits
```

Per-key `asyncio.Lock` deduplicates concurrent fetches (only one request proceeds, others
await the result).

---

## 2. Four Stores and Their TTLs

| Store | Data | TTL | Refresh |
|---|---|---|---|
| ohlcv_store | Daily OHLCV bars | 5 years (DB) | daily 08:00 IST warm |
| instruments_store | Symbol → token map | 24 hours | daily 08:00 IST warm |
| holidays_store | Holiday dates per exchange | 1 year (immutable post-year) | yearly 01/01, retry 04:00-08:00 IST |
| intraday_store | 5/15/30/60-min bars | 90 days (DB), 5-min today (Tier 1) | every 5 min during market |

**Tier 1 in-memory**: LRU with separate locks per key. LRU is bounded (typical: 1000 entries).
When the LRU evicts an old entry, Tier 2 (DB) becomes the next hit.

---

## 3. Fetch Ladder for Each Store

### OHLCV Daily Bars

```
1. ohlcv_daily (DB cache, completeness gate)        ← always try first
     If boundary + ≤4d gaps → use it
     If empty ↓
2. ohlcv_store broker fallback (Kite only)          ← Kite historical_data REST
     Always use get_historical_brokers()[0] (Kite, not Dhan)
     If non-empty → write-back to ohlcv_daily
     If empty ↓
3. _self_heal_empty_bars (bypass_cache, not in cooloff) ← last resort
```

### Instruments (Symbol → Token)

```
1. instruments_store Tier 1/2 (24h cache)           ← daily TTL, warmed at startup + 08:00 IST
     If non-empty → use it
     If empty ↓
2. broker.instruments(exchange=...)                  ← Kite-only walk (Dhan schema incompatible)
     Write-back to instruments_store
```

### Holidays

```
1. In-process LRU (yearly TTL)                       ← immutable after year boundary
     If non-empty and within year → use it
     If miss ↓
2. PostgreSQL market_holidays (daily refresh via task) ← runs 04:00 IST, retries until 08:00
     If non-empty → use it
     If miss ↓
3. NSE API (cold boot)                               ← fallback for fresh database
```

### Intraday (5/15/30/60-min)

```
1. Tier 1 LRU (5-min TTL today, persistent for hist)  ← fast, frequent refreshes
     If today and non-empty → use it
     If history request and in DB → use DB
     If empty ↓
2. DB intraday_daily (90d retention)                 ← historical bars, immutable EOD
     If non-empty → use it
     If empty ↓
3. broker.historical_data(..., interval=...)        ← Kite historical_data REST
     Write-back to intraday_daily
```

---

## 4. Completeness Checks

Every store implements a completeness predicate before returning cached data:

**OHLCV**: Boundary present (first and last days exist) AND no gaps > 4 calendar days
(handles weekends but catches missing trading days).

**Instruments**: Non-empty list with valid instrument_type field (`t` in response).

**Holidays**: Non-empty set (must have at least one date).

**Intraday**: For today — accept any non-empty result (market may have just opened).
For history — accept if the date span closes (last bar ≥ requested end date).

---

## 5. Persistence Modes

Three modes control cache behavior: `off` (default), `soft` (Tier 1+2 bypass), `hard`
(soft + ticker recycle). Flip via `POST /api/admin/persistence/mode/{off|soft|hard}`.

| Mode | Tier 1 | Tier 2 | Broker | Ticker | Use |
|---|---|---|---|---|---|
| off | ✓ | ✓ | ✓ | Live | Default: normal caching |
| soft | — | — | ✓ | Live | Bypass DB; cold-boot only |
| hard | — | — | ✓ | Restart | Test: full reset |

**Soft mode**: No in-process cache reads/writes (Tier 1), no DB reads/writes (Tier 2).
Broker is always called. Ticker SSE remains live (no restart). Used for testing after
broker data changes; reverts to `off` on restart.

**Hard mode**: Soft mode + ticker restart. KiteTicker connection is recycled so mmap
gets fresh data. Used for complete reset. Reverts to `off` on restart.

---

## 6. Closed-Hours Route Gate

**`closed_hours_or_broker(exchange, snapshot_fn, broker_fn, route_key="")`**
is the canonical gate for every operator-visible data route.

**Invariant**: `broker_fn` is NEVER called when the market is closed. This is enforced
by checking `is_any_segment_open()` synchronously (reads config + cached holidays) and
returning snapshot if closed.

**Source tags**:
- `'live'` — broker_fn succeeded during market hours
- `'snapshot'` — market closed; snapshot_fn returned data
- `'snapshot-fallback'` — market open but broker_fn raised; snapshot_fn used
- `'stale-live'` — market open but broker_fn raised; last-known live payload (<120s old) returned instead

**Anti-flicker cache** — Each route nominates a `route_key` string (e.g. 'positions',
'holdings'). On successful broker calls, the response is stashed with timestamp.
On broker failure, if a stale-live entry younger than 120s exists, return it instead
of the DB snapshot. Prevents live/snapshot alternation flicker every 30s.

**Usage**:
```python
data, source = await closed_hours_or_broker(
    exchange='NSE',
    snapshot_fn=_positions_snapshot,
    broker_fn=_positions_live,
    route_key='positions',
)
```

Every new data route MUST use this gate. Tests patch `_any_segment_open()` to simulate
closed hours.

---

## 7. Raw Broker Cache (30s TTL)

`_RAW_CACHE` (dict keyed by `"{account}:{endpoint}"`) holds the raw broker DataFrame
for 30 seconds. Shared by routes, nav, investor slice.

**Paths that write**: `fetch_holdings()`, `fetch_positions()`, `fetch_margins()`.
Each successful call stashes the DataFrame with a 30s TTL.

**Paths that read**: NAV computation, investor slice, position and holding routes.

**Invalidation**: `?fresh=1` query param or postback completion calls `_raw_cache_invalidate(key)`.

---

## 8. Event Queues

Five active event queues (generic `EventQueue` class, bulk `executemany` INSERT):

| Queue | Table | Use | Retention |
|---|---|---|---|
| algo_events | algo_events | Agent conditions checked + fired | 30 days |
| agent_events | agent_events | Alert delivery + action results | (see Audit log) |
| agent_event_queue | agent_events | Coalesced agent fires (500-batch, 1s flush) | (see Audit log) |
| algo_order_events | algo_order_events | GTT placements + fill events | 30 days |
| mcp_audit | mcp_audit | MCP tool executions | 90 days |

**`agent_event_queue`** — `EventQueue(AgentEvent, name="agent_event", batch_size=500,
flush_interval_s=1.0, max_queue=10_000, on_full="drop")`. Coalesces N agent fires per
cycle into one bulk INSERT, eliminating N individual DB round-trips during bursts.
Defined in `backend/api/algo/events.py`.

Workers are started at API boot and drained on shutdown. Failed writes are logged
but don't stop the API (graceful degradation).

---

## 9. Holiday Calendar Pipeline

Four-tier read (with fallback):

```
1. In-process LRU (yearly, immutable post-year boundary)
   ↓ (if miss)
2. Module-level dict TTL (per-exchange, 24h window)
   ↓ (if miss)
3. PostgreSQL market_holidays (daily 04:00 IST refresh, retry until 08:00)
   ↓ (if miss on cold boot)
4. NSE API (historical fetch via Kite)
```

**Daily task** (`_task_market_holidays`): Runs at 04:00 IST, queries NSE API,
upserts `market_holidays` table. Retries every 30 min until 08:00 IST if the API
is unreachable.

**Buster**: Date rollover (new day) busts Tiers 1+2. Tier 3 (DB) persists across
restarts.

---

## 10. Retention Policies

Configurable in `/admin/settings`:

| Data | Default | Tunable |
|---|---|---|
| ohlcv_daily | 5 years | ✓ |
| instruments | 7 days | ✓ |
| intraday | 90 days | ✓ |
| holidays | forever | — |
| nav_daily | forever | ✓ |
| daily_book | forever | ✓ |
| investor_events | forever | ✓ |
| monthly_statements | forever | ✓ |
| algo_events | 30 days | ✓ |
| agent_events | 30 days | ✓ |
| mcp_audit | 90 days | ✓ |
| audit_log | 365 days | ✓ |

**Cleanup**: Background task runs daily at 03:00 IST, deletes records older than
the retention window.

**daily_book `kind='positions'` orphan sweep — expiry-day-final freeze (2026-09)**:
Two orphan-sweep functions in `backend/api/algo/daily_snapshot.py`
(`_delete_orphan_positions` same-day, `_delete_prior_orphan_positions` 7-day
lookback) delete a position row once its symbol stops appearing in the
broker's daily fetch. A row is exempted from this sweep — frozen exactly as
captured — when BOTH are true: (1) the symbol parses as an F&O contract
(`backend.api.algo.derivatives.parse_tradingsymbol`) whose own real expiry
date equals the trading SESSION (08:00 IST boundary, not the `date` column)
the row was last captured in, and (2) the current instant hasn't yet crossed
the next genuine trading day's 08:00 IST open after that expiry
(`backend.api.algo.expiry_freeze.next_market_open_ist` — holiday/weekend/
special-session aware, via `holidays_store` + `market_special_sessions`).
A position CLOSED by normal trading while its contract still had time left
is NOT exempted — it is swept immediately by the existing, unmodified logic.
`positions.py:_positions_snapshot` additionally (a) unions in a frozen row
that the account's own single-batch `latest_batch` join would otherwise mask
behind a fresher batch for a different, still-live symbol in the same
account, and (b) excludes outright any row whose freeze window has already
ended as a defensive backstop (primary refresh is `closed_hours_or_broker`
switching back to the live broker fetch once any segment reopens). A
`kind='positions_empty'` sentinel row (written only when the broker
CONFIRMS zero positions, never on an ambiguous/failed fetch) gives a fully
flat account a real prune anchor going forward — without it, an account
that never writes another 'positions' row has no anchor and its stale rows
are never swept at all. Full rationale: `backend/api/algo/expiry_freeze.py`
module docstring.

---

## 11. Edge Cases and Self-Healing

| Scenario | Recovery | Expected Behavior |
|---|---|---|
| Cold start (DB empty) | Tier 3 broker, write-back | First fetch is slow; subsequent hits DB |
| Broker rate-limited (cooloff) | Serve from DB or daily_book; retry after cooloff | User sees snapshot; request logged as 'snapshot-fallback' |
| daily_book snapshot missing | _self_heal_empty_bars + ltp_only_flat_pad | Sparkline falls back to flat [ltp, ltp] (visual artifact) |
| Close snapshot write failed | Retry on next close event (idempotent UPSERT) | 15 min later: close_settled overwrites; no data loss |
| Backfill script without write queue | Silent DROP of writes | **MUST call `write_queue.start()` before fetch** |
| KiteTicker stale (30s watchdog) | Auto-failover to next Kite account, 5-min cooloff | Tick delay up to 30s; LTP/sparklines briefly stale |
| Conn_service restart | Main API reads mmap directly; ticker auto-reconnects | No data loss; ticker recovery in <30s |

**Warm task schedule** (self-healing checkpoints):

- `_task_sparkline_warm` — Startup, 00:30 IST, segment opens
- `warm_sparkline_cache` — Startup, 08:30 IST
- `_task_market_holidays` — Daily 04:00 IST (retry until 08:00)
- `backfill_ohlcv_daily` — Via `POST /api/admin/persistence/backfill` (not standalone)

---

## 12. Test Coverage Map

### Backend — covered

- `test_persistence_mode_cli.py` — persistence mode (off/soft/hard) flipping and TTL behavior
- `test_holiday_refresh.py` — holiday calendar pipeline, daily refresh, retry logic
- `test_fetch_holidays_tier_restructure.py` — four-tier holiday fallback chain
- `test_ohlcv_equity_persistence.py` — OHLCV daily bars, Tier 1 miss → DB → broker
- `test_intraday_store_db_only.py` — intraday bars: 5-min live TTL, DB persistent history
- `test_retention_tables.py` — retention policy cleanup, table-specific windows
- `test_retention_new_tables.py` — retention for new tables (nav_daily, daily_book, etc.)
- `test_last_good_quote.py` — last-known-good quote cache, off-market staleness handling

### Backend — gaps

- Closed-hours route gate (broker_fn never called when closed)
- Anti-flicker stale-live caching (120s window on broker failures)
- Raw broker cache (30s TTL, per-account deduplication)
- Completeness gates (OHLCV boundary + ≤4d gaps, instruments non-empty)
- Event queue bulk INSERT with worker lifecycle

### Frontend — covered

(No Playwright tests currently exist for closed-hours snapshot display)

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec; four stores, fetch ladder, closed-hours gate, raw cache, retention documented |
