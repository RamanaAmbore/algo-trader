# Broker Layer Specification

Single source of truth for `backend/brokers/` — the vendor-agnostic broker abstraction layer.
Code, tests, and documentation must stay in sync with this file.

**Version**: 1.28 — 2026-08-30  
**Owner**: Platform  
**Linked files**: `backend/brokers/base.py` · `backend/brokers/registry.py` · `backend/brokers/connections.py` · `backend/brokers/kite_ticker.py` · `backend/brokers/adapters/` · `backend/brokers/service/` · `backend/brokers/client/`

---

## Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Broker Base Contract](#2-broker-base-contract)
3. [Capabilities Matrix](#3-capabilities-matrix)
4. [Broker Selection SSOT](#4-broker-selection-ssot)
5. [Connections Singleton](#5-connections-singleton)
5.4 [Token Refresh Lifecycle](#54-token-refresh-lifecycle)
6. [Circuit Breaker & Health](#6-circuit-breaker--health)
7. [KiteTicker & Mmap Pipeline](#7-kiteticker--mmap-pipeline)
7.1 [Market-Data Backfill Pipeline](#71-market-data-backfill-pipeline)
7.2 [Instruments & Token-Map Cache](#72-instruments--token-map-cache)
7.3 [Daily Snapshot Orphan Cleanup](#73-daily-snapshot-orphan-cleanup)
9.3 [Instruments & Options Endpoints — MCX Name Normalization](#93-instruments--options-endpoints--mcx-name-normalization-aug-2026)
7.3.1 [Daily Snapshot UPSERT Idempotency & LTP Coalesce Fix](#731-daily-snapshot-upsert-idempotency--ltp-coalesce-fix-aug-2026)
7.3.2 [Admin Snapshot Trigger — Holiday-Aware Market-Open Detection](#732-admin-snapshot-trigger--holiday-aware-market-open-detection-aug-2026)
7.3.3 [Dhan `last_price=0` Fallback in EOD Snapshots](#733-dhan-last_price0-fallback-in-eod-snapshots-aug-2026)
7.3.4 [Weekend Guard for Filtered Holdings & Positions](#734-weekend-guard-for-filtered-holdings--positions-aug-2026)
7.3.5 [Holdings Data Freshness & SSOT Fetch TTL](#735-holdings-data-freshness--ssot-fetch-ttl-aug-2026)
7.3.6 [Firm NAV Computation & Closed-Exchange LTP Overlay](#736-firm-nav-computation--closed-exchange-ltp-overlay)
7.3.7 [Holdings Day P&L Recompute & Backstop Exclusion](#737-holdings-day-pnl-recompute--backstop-exclusion-aug-2026)
7.3.8 [Holdings LTP Override & pnl+cur_val Consistency](#738-holdings-ltp-override--pnlcur_val-consistency-aug-2026)
7.3.9 [Holdings Snapshot Day Change Percentage Formula](#739-holdings-snapshot-day-change-percentage-formula)
8. [Adapter Implementations](#8-adapter-implementations)
8.1 [Order Placement Guards & Intent Bypass](#81-order-placement-guards--intent-bypass)
8.2 [GTT Exchange Validation & MCX Broker Restrictions](#82-gtt-exchange-validation--mcx-broker-restrictions)
8.3 [GTT Template Attachment System Enhancements](#83-gtt-template-attachment-system-enhancements-jul-2026)
8.4 [Broker Postback Fill-Status Mapping](#84-broker-postback-fill-status-mapping)
8.5 [Orders Fetching Resilience & Chase Timeouts](#85-orders-fetching-resilience--chase-timeouts)
8.6 [Order Pairing — Parent-Child Relationship Linking](#86-order-pairing--parent-child-relationship-linking)
9. [Remote Broker & Conn Service](#9-remote-broker--conn-service)
9.1 [Background Task Supervisor](#91-background-task-supervisor)
10. [Virtual Root Resolution](#10-virtual-root-resolution)
11. [Key Invariants](#11-key-invariants)
12. [Test Coverage Map](#12-test-coverage-map)
13. [Known Defects & Risks](#13-known-defects--risks)
14. [Exchange Schedule Table & Clock Module](#14-exchange-schedule-table--clock-module)
15. [Broker Connection Events Audit Log](#15-broker-connection-events-audit-log)
16. [Daily Broker Issue Aggregation & Monitoring](#16-daily-broker-issue-aggregation--monitoring)
16.1 [CONNCHECK TLM Tool](#161-conncheck-tlm-tool)
16.2 [Deploy Notification Receipt Tracking](#162-deploy-notification-receipt-tracking)
16.3 [Alert Routing Restoration](#163-alert-routing-restoration)
17. [Closed-Hours Cache Refresh Pattern](#17-closed-hours-cache-refresh-pattern)

---

## 1. Architecture Overview

```
Path A — In-process (dev, testing)
  main API → get_broker(account) → KiteBroker/DhanBroker/GrowwBroker
  KiteTicker WebSocket runs in main process (Twisted reactor thread)

Path B — Conn service (prod, RAMBOQ_USE_CONN_SERVICE=1)
  conn_service (UDS) owns ALL broker sessions + KiteTicker WebSocket
  main API → get_broker(account) → RemoteBroker → HTTP/UDS → conn_service
  Live ticks → /dev/shm/ramboq_ticks (mmap, 4096 slots, atomic version-word)
  main API reads mmap directly (O(1), no round-trip per tick)
```

**Process separation**: conn_service restarts ONLY when `backend/brokers/` files change (`CONN_TOUCHED` flag). Main API deploys never disrupt broker sessions.

---

## 2. Broker Base Contract

**File**: `backend/brokers/base.py` — `Broker` ABC (28 methods)

All adapters return **Zerodha Kite-normalised shapes**. Callers never branch per vendor.

| Method family | Return shape |
|---|---|
| `holdings()`, `positions()` | `list[dict]` — Kite field names |
| `margins()` | `dict` with `equity`, `commodity` sub-keys |
| `ltp(symbols)` | `dict[broker_symbol, {"last_price": float}]` |
| `quote(symbols)` | `dict[broker_symbol, {open, high, low, close, last_price, volume, oi}]` |
| `instruments(exchange)` | `list[dict]` with `tradingsymbol`, `instrument_token`, `exchange`, `expiry`, `strike`, `lot_size` |
| `historical_data(...)` | `list[dict]` with `date`, `open`, `high`, `low`, `close`, `volume` |
| `holidays(exchange, year)` | `set[str]` ISO date strings |

**Auth invariant**: Adapters handle token refresh transparently. Callers never see `401`. When a fetch raises an auth error, the `@for_all_accounts` decorator's `_per_account` handler (in `backend/shared/helpers/decorators.py`) detects the error via `is_auth_error_str()`, calls `_try_renew(account, connections)` to refresh the token, and retries the function once with fresh handles. If the retry also fails, the exception is re-raised and caught by the circuit breaker.

---

## 3. Capabilities Matrix

**File**: `backend/brokers/capabilities.py` — `BrokerCapabilities` frozen dataclass

| Capability | Kite | Dhan | Groww |
|---|---|---|---|
| GTT Single | ✓ | ✓ (Forever) | ✓ |
| GTT OCO | ✓ | ✓ (Forever) | ✗ (emulated) |
| GTT MCX | ✓ | ✗ | ✗ |
| Bracket Order | ✗ (deprecated) | ✓ | ✗ |
| Atomic Basket | ✗ | ✓ | ✗ |
| Margin Preview | ✓ | ✓ | ✗ |
| GTT Postback | webhook | poll_only | poll_only |
| historical_data | ✓ | ✗ (returns []) | ✗ (returns []) |
| Quote Rate Limit | 1/s | — | — |
| Order Rate Limit | 10 orders/s | 20 orders/s | 5 orders/s |

**`historical_data` invariant**: Kite-only. `get_historical_brokers()` excludes Dhan/Groww. `ohlcv_store` and `intraday_store` MUST use `get_historical_brokers()[0]`, NEVER `get_market_data_broker()`.

---

## 4. Broker Selection SSOT

**File**: `backend/brokers/registry.py`

| Function | Use for |
|---|---|
| `get_broker(account)` | Per-account order entry, holdings, positions |
| `get_market_data_broker()` | Live LTP, quote, instruments (per-request ContextVar cache) |
| `get_historical_brokers()[0]` | OHLCV daily bars, intraday bars — always Kite |
| `all_brokers()` | `@for_all_accounts` fan-out |

`_broker_id_for(account)` resolution: DB cache → conn_service lazy fetch → secrets.yaml → `"zerodha_kite"` default.

`PriceBroker` failover: exception or "too many requests" → mark rate-limited 60s → roll to next broker. Soft-failure predicates: `_quote_has_data`, `_ltp_has_data`, `_instruments_has_kite_shape`.

---

## 5. Connections Singleton

**File**: `backend/brokers/connections.py` — `Connections(SingletonBase)`

Populated by `rebuild_from_db()` — queries `broker_accounts`, decrypts Fernet credentials, builds conn map.

### KiteConnection
- OAuth + TOTP 2FA; token cached at `/opt/ramboq/.log/kite_tokens.json`
- **Cross-process lock** (`fcntl.flock(LOCK_EX)`): serialises concurrent prod+dev logins
- **File lock timeout** (Jul 2026): `fcntl.flock(LOCK_EX | LOCK_NB)` with 30s poll loop
  (100ms retry intervals); raises `BrokerNetworkError` on timeout instead of hanging.
- **In-process lock** (`threading.Lock`): prevents two threads running login simultaneously
- **Token write**: `tempfile + os.replace()` (POSIX atomic) under flock
- **IPv6**: `_IPv6SourceAdapter` per account; `_IPV6_FAMILY_OVERRIDE` ContextVar for thread safety

### DhanConnection
- Headless TOTP; 120s (2-min) cooloff between login attempts
- **Cross-process login lock** (Jul 2026): lock file at `/tmp/ramboq_locks/<account>.lock`
  (system-wide, shared by prod + dev instances). After acquiring lock, token is re-read from
  cache BEFORE calling `generate_token`, preventing stale-token races when two processes
  attempt simultaneous login.
- **File lock timeout** (Jul 2026): `fcntl.flock(LOCK_EX | LOCK_NB)` with 30s poll loop
  (100ms retry intervals); raises `BrokerNetworkError` on timeout instead of hanging.
- **Login cooloff persistence** (Aug 2026): Failed `generate_token` cooloff (`_login_blocked_until`)
  now persists to `/tmp/ramboq_dhan_login_cooloff.json` (per-account) and survives process
  restarts. Prevents immediate rate-limit hammering when ramboq_api/conn_service restarts
  during a failure window. Logs with `[DHAN-COOLOFF]` prefix on load; `[DHAN-LOGIN]` on
  each attempt.
- **Token renewal skip on test_conn** (Jul 2026): `_try_renew()` is now gated on 
  `not test_conn` in `_dhan_conn_under_lock()`. When `test_conn=True` (dead token confirmed), 
  the code skips lightweight renewal and goes straight to `_mint_and_build()` to re-mint 
  via TOTP, ensuring fresh credentials.
- **Login diagnosis logging** (Jul 2026): `_do_login()` now emits a DEBUG log `[DHAN-LOGIN]` 
  with HTTP status code and 200-char response body after every POST, aiding auth failure diagnosis.
- IPv6 on both login and runtime sessions

### GrowwConnection
- TOTP token refresh via `GrowwAPI.get_access_token`
- Module-level `requests` monkey-patch for source-bound HTTP
- **Connection resilience** (Aug 2026): Now includes:
  - `CONN_RESET_HOURS = 23` expiry constant
  - `_is_token_expired()` method — proactive token age check
  - `_check_login_rate_limit()` — 120s cooloff on auth failure (mirrors Dhan)
  - Proactive refresh in `get_groww_conn()` when `_is_token_expired()` returns True
  - Logs with `[GROWW-LOGIN]` prefix on auth failure

### 5.4 Token Refresh Lifecycle

Kite access tokens carry a **23-hour vendor TTL** (`conn_reset_hours: 23` in
`backend_config.yaml` line 5). This is a Zerodha-imposed constant — not
operator-tunable. The platform runs a daily pre-warm cycle to ensure a fresh
token is available before market open.

**Daily cycle:**

| Time (IST) | Action | Code |
|---|---|---|
| 05:30 | `_task_holiday_refresh()` pre-warms tokens for ALL loaded accounts | `background.py:2467–2492` |
| 05:30 + 30 min (retry) | Retry pre-warm if any account failed; repeats until 08:00 | `background.py:2425–2443` |
| 08:00 | `_snapshot_restart_ticker()` passes validated token to KiteTicker restart | `background.py:2101–2112` |

**Per-broker pre-warm calls (05:30):**

| Broker | Call | Effect |
|---|---|---|
| Zerodha Kite | `get_kite_conn(test_conn=True)` | Validates via lightweight `profile()` call; forces re-login if 401 |
| Dhan | `get_dhan_conn(test_conn=True)` | Validates session; forces re-login on failure |
| Groww | `get_groww_conn()` | Validates session |

**Token cache:** `.log/kite_tokens.json` (per-account JSON, `{account: {access_token,
created_at}}`). Cross-process advisory flock (`_cross_process_login_lock`)
serialises concurrent login attempts across `ramboq_api` and `ramboq_conn`
processes.

**Per-call validation:** every `get_kite_conn()` call validates the cached token
via a lightweight `profile()` call before returning the `KiteConnect` object. On
401: cache entry is cleared and a fresh login is forced. The `@retry_kite_conn`
decorator handles transient network failures with automatic retry.

**Token validity window example:**
```
08:00 IST D+0  → KiteTicker restart issues token  (23h TTL → expires 07:00 D+1)
05:30 IST D+1  → pre-warm refreshes token         (new 23h TTL → expires 04:30 D+2)
08:00 IST D+1  → KiteTicker restart uses new token (90 min headroom)
```

The 05:30 pre-warm runs 90 minutes before the ~07:00 expiry, giving ample time
for the retry loop (up to 08:00) to succeed on transient login failures.

---

## 6. Circuit Breaker & Health

**File**: `backend/brokers/broker_apis.py` · `backend/api/routes/health.py`

`_FETCH_HEALTH[account]`: `{last_ok_at, last_fail_at, consecutive_fail_count, circuit_open_until, open_cycle_count}`

State machine (opt-in per account via `circuit_breaker_enabled`):
- 3 consecutive failures → OPEN (skip account, return empty DataFrame + `fetch_failed=True`)
- Cooloff: 5 min → doubles per cycle → 30 min max
- HALF-OPEN: one probe after cooloff

**Circuit breaker persistence** (Aug 2026): CB state (open/half-open/closed transitions)
persists to `broker_accounts.cb_state_json` on each state change via `_record_breaker_state()`.
File-based fallback at `/tmp/ramboq_cb_state.json` loads on startup for non-expired entries.
Prevents redundant probe loops when main API restarts during an active cooloff window; cool-off
windows survive process restarts.

**State machine extraction** (Jul 2026): `_record_breaker_state(account, ok, error, now)` 
extracted from `_record_fetch()` to isolate state transitions under `_BREAKER_LOCK`. Returns 
`(new_breaker_open, was_halfopen, was_recovering)` so conn events fire OUTSIDE the lock, 
preventing deadlock with `enqueue_nowait`. Non-opt-in accounts skip the full state machine 
and update health stamps only (fast path).

**Inactive broker accounts** (Jul 2026): `_hlth_resolve_state()` in `backend/api/routes/health.py` 
now returns `"inactive"` (not `"amber"`) when `last_ok==0 and last_fail==0` — indicating an 
account has never been polled. Frontend `_brokerHealthWorstState` in `frontend/src/lib/stores.js` 
excludes inactive accounts from worst-state calculation when at least one active account 
exists, preventing amber chips on accounts that have not yet been queried.

Dhan poll priority: `hot` (30s), `warm` (120s), `cold` (600s). Kite/Groww always poll every cycle.

**Health heartbeat validity gate** (Aug 2026): `_health_heartbeat` in `service/app.py`
now checks `_is_token_expired()` before stamping `record_session_ok`. Expired tokens
are skipped — chip goes amber naturally instead of showing false-green. Warning fires
once per expiry cycle via `_heartbeat_warned` dedup set. Logs with `[HEARTBEAT]`
prefix.

**False-amber threshold fix** (Aug 2026): `_BROKER_HEALTH_FRESH_WINDOW_S` raised from
300s to 660s in `backend/api/routes/health.py`. Dhan cold-priority poll interval is
600s; the old 300s window caused perpetual amber on cold-priority accounts even when
healthy. New 660s window (1.1×) allows one full cold poll cycle to complete without
the account flipping amber mid-cycle.

**Account loading cache with UDS fallback** (Aug 2026): `_loaded_accounts()` in
`backend/api/routes/brokers.py` now maintains a module-level `_last_known_remote_accounts`
cache (set[str]). When listing accounts for the navbar chip (e.g., "3/5 accounts healthy"),
the function calls `list_remote_accounts()` to fetch the current list from the conn_service.
If conn_service returns empty (brief UDS unavailability at 06:00 IST token-expiry restarts),
the cache serves the last successful list instead. This prevents the navbar from flipping
0/5 → 5/5 during transient 5-second conn_service restart windows. Operator sees stable
chip ("3/5") even when UDS briefly blinks. Cache persists across requests but clears on
process restart.

Health surface: `GET /api/admin/broker-health`

---

## 7. KiteTicker & Mmap Pipeline

**Files**: `backend/brokers/kite_ticker.py` · `backend/brokers/tick_buffer.py` · `backend/brokers/service/routes.py`

```
KiteTicker WebSocket (Twisted reactor, conn_service)
    ↓ on_ticks
TickBufferWriter.upsert(token, last_price, prev_close, avg_price, ts_ns)
    ↓ linear-probe hash write
/dev/shm/ramboq_ticks (64B header + 4096×40B slots)
    ↓ O(1) slot read, no IPC
main API: TickBufferReader.get_ltp(token) (50ms poller)
    ↓
BroadcastBus → SSE → frontend ltpMap
```

**Torn-read protection**: version word checked before/after slot read; retry on mismatch.

**TickerManager failover**: `_consecutive_unhealthy` watchdog; per-account 5-min cooloff prevents
ping-ponging. `_swap_history` 128-entry rolling log.

**Watchdog via asyncio.to_thread** (Aug 2026): `_task_ticker_watchdog()` now runs via
`asyncio.to_thread` (non-blocking I/O, doesn't starve event loop). Watchdog detects stuck
KiteTicker connections and triggers account swap without blocking the main API's asyncio loop.

**Unsubscribe cleanup** (Aug 2026): Unsubscribe path now correctly prunes three data structures:
- `_pending` — set of tokens awaiting subscription ACK
- `_token_to_sym` — token → symbol reverse map
- `_sym_to_token` — symbol → token forward map

Previously, only the mmap writer was updated; lingering stale entries in memory maps caused
duplicate subscription attempts or silent failures on re-register. Cleanup is now complete.

**Re-subscription on reconnect** (Jul 2026): `_on_connect` now re-subscribes all
previously-subscribed tokens (not just tokens added during the disconnect window). Ensures
market data resumes immediately after network transients.

**Universe registration**: startup + segment opens + daily_book past-7d union (backstop survives conn_service restart).

**MMAP missing-symbol suppression** (Aug 2026): `_known_absent_tokens: set[int]` persistent set
(replaces 60s-TTL dict). Warning fires once per token per process lifetime; subsequent lookups 
for that token are silent. Re-registration of already-subscribed tokens now checked against 
this set first to suppress redundant broker subscription attempts.

**MMAP ticker re-registration suppression** (Aug 2026): `TickBufferWriter` now checks if a 
token is already registered in `_token_to_sym` before calling the broker to subscribe. Prevents 
duplicate broker subscription requests when the same token is re-registered (e.g., on 
reconnect or stale data retries).

---

## 7.1 Market-Data Backfill Pipeline

**File**: `backend/brokers/broker_apis.py` — `backfill_market_data()` and helpers

### Token Map Fetch Lock

**File**: `backend/api/routes/quote.py` — `_TOKEN_MAP_FETCH_LOCK`

`_TOKEN_MAP_FETCH_LOCK` is a `threading.Lock()` that serialises cold-cache broker 
`instruments()` downloads. Uses double-checked locking pattern:

```
_get_today_token_map():
  if _TOKEN_MAP_CACHE:
    return _TOKEN_MAP_CACHE  # fast path, no lock
  
  with _TOKEN_MAP_FETCH_LOCK:
    if _TOKEN_MAP_CACHE:      # recheck inside lock
      return _TOKEN_MAP_CACHE  
    _TOKEN_MAP_CACHE = _qt_broker_token_map()  # fetch 6 exchanges sequentially
    return _TOKEN_MAP_CACHE
```

Without this lock: 50+ concurrent `asyncio.to_thread(_get_today_token_map)` calls all 
missed the cold cache simultaneously → each downloaded 6 exchanges sequentially → 
peak 6.4GB RAM → OOM kill on high-volume days (especially expiry when NFO has 300K+ rows).

`_qt_broker_token_map()` downloads all 6 exchanges sequentially (not concurrently) 
under the lock.

### Task Instruments Startup Delay

**File**: `backend/api/background.py` — `_task_instruments`

`_task_instruments` has a 120-second startup delay (`await asyncio.sleep(120)` before 
the first warm run). 

**Reason**: `_task_sparkline_warm` runs at startup and downloads 6 exchanges sequentially 
via `_qt_broker_token_map()`. Without the delay, `_task_instruments` starts its own 
5-exchange download concurrently, causing a double-NFO peak on expiry days (NFO has 
300K+ rows → ~400-500MB per parse). The 120s stagger ensures the sparkline warm finishes 
before instruments begins its cycle.

### Chain Instruments Background Task — NFO/MCX Dedicated Fetch

**File**: `backend/api/background.py` — `_task_chain_instruments` and `backend/api/routes/instruments.py` — `_fetch_chain_instruments()`

A dedicated background task (`bg-chain-instruments`) fetches NFO and MCX contract data exclusively for the option-chain tab. 

**Schedule**: T+30s (first run 30s after startup) → then daily at 08:02 IST

**Scope**: NFO (NSE derivatives) and MCX (commodity futures/options) only; excludes NSE/BSE/CDS to avoid peak memory usage

**Cache key**: `instruments_chain` (separate from the primary `instruments` cache for quota isolation)

**Data source**: `_fetch_chain_instruments()` calls broker.instruments for NFO and MCX exchanges only, storing results in the `instruments_chain` cache

**Use by chain_quotes**: The `chain_quotes` endpoint prefers the `instruments_chain` cache when available, reducing contention with Kite's concurrent NFO lookups that spike during option expiry when 300K+ contract records are parsed simultaneously

**Rationale**: Option chains are expensive to hydrate (large NFO/MCX payloads); dedicated background population ensures the Ticket tab's expiry dropdown and strike picker have pre-warmed data without blocking other routes' instruments lookups

### Options Chain Polling & Timeouts (Aug 2026)

**File**: `backend/api/routes/quote.py` — `_chain_quotes_batch_quote()` and frontend
`ChainCard.svelte`

**Backend timeout reduction** (30s → 12s): `asyncio.wait_for` timeout in 
`_chain_quotes_batch_quote()` reduced from 30 seconds to 12 seconds to reduce thread pool 
hold time during high-volume option expiry days when NFO chain has 300K+ contracts being 
quoted simultaneously.

**Frontend poll interval increase** (5s → 30s): `visibleInterval` for prices polling in 
ChainCard changed from 5 seconds to 30 seconds (default). Reduces API pressure during 
peak expiry windows.

**In-flight guard added**: New `_pricesFetching` guard in `_loadPrices()` prevents 
concurrent broker.quote() calls. When a prices fetch is in-flight, subsequent poll ticks 
are silently dropped until the fetch completes. This prevents request starvation when 
browser polls faster than broker responses can complete.

**Impact**: Options chain tab remains responsive on expiry days (300K+ NFO contracts) 
without blocking other routes; excessive backend load from 5-second polls eliminated; 
simultaneous quote requests prevented by frontend guard.

### Removed Function: `_trigger_instruments_store_populate`

**File**: `backend/api/routes/quote.py` (deleted 2026-07-25)

`_trigger_instruments_store_populate()` was removed from `_get_today_token_map()`.

It called `get_or_fetch_all_today()` immediately after the broker token map was populated, 
launching 6 concurrent `asyncio.gather` downloads while the first set's raw data was still 
in memory — a double OOM storm during peak hours.

**Do NOT re-introduce this function.** The module-level `_TOKEN_MAP_CACHE` is sufficient 
for all callers; lazy population via `get_or_fetch_all_today()` is preferred.

---

## 7.2 Market-Data Backfill Pipeline (continued)

**File**: `backend/brokers/broker_apis.py` · `backend/api/background.py` — `backfill_market_data()` and helpers

When broker APIs return zero or stale `close_price` / `last_price`, the backfill pipeline 
patches missing values from a PriceBroker quote batch. Pipeline stages (Jul 2026 Polish R6):

1. **`_bmd_build_key_index(df)`** — Vectorized extraction (replaced iterrows with pandas 
   Series operations for ~2-3× speedup). Identifies rows with missing close/LTP, builds 
   deduped `EXCHANGE:SYMBOL` key list for batched PriceBroker.quote() call.

2. **`_apply_backfill_to_list(frames)`** — Concatenates non-empty frames and applies 
   `backfill_market_data()` to the combined result. Signature simplified (Jul 2026): 
   `qty_col` parameter removed — the function now uses generic `opening_quantity` / 
   `quantity` lookup internally. Callers pass `list[DataFrame]` only.

3. **`backfill_market_data(df)`** — Canonical consolidation (Jul 2026): removed stale alias 
   `backfill_close_prices`. All callers now use `backfill_market_data(df)` directly.

**Dhan two-tier LKG cache** (Jul 2026): Dhan holdings responses frequently arrive with zero 
`last_price` during market hours. A two-tier last-known-good cache now backs the fallback 
chain: (1) **In-memory Tier-1**: `_LKG_FRAME_BY_ACCT` dict, populated by every successful 
fetch per-account (stores whatever frame is given, **including empty frames**). (2) **DB 
Tier-2**: `_DB_LKG_CACHE` dict, seeded at startup from `daily_book` rows for all holdings 
seen in past 7 days. `_stale_substitute_frame()` checks in-memory LKG first, then DB LKG, 
before returning empty frame. Startup task `_preload_db_lkg_cache()` in `backend/api/background.py` 
populates the DB-backed cache asynchronously.

**Empty frame handling** (Aug 2026): When an account successfully exits all positions (e.g., 
all holdings liquidated), the broker returns an empty DataFrame. The LKG cache now stores 
this empty frame (via `_record_lkg_frame` in `_fetch_holdings_local` and `_fetch_positions_local`), 
overwriting any prior non-empty LKG. This ensures that on subsequent breaker-open 
short-circuits, `_stale_substitute_frame` returns an empty frame (no phantom positions) 
rather than a stale prior-session snapshot. 24-hour TTL gate applies; after 24h offline, 
the account is considered absent.

**Fallback chain**: PriceBroker quote → KiteTicker LTP (PriceBroker outage) → in-memory LKG 
(Tier 1, includes empty) → DB LKG from `daily_book` (Tier 2) → empty frame. Rows patched via 
LKG cache marked with `last_price_stale=True` and `account_stale=True`.

---

## 7.3 Daily Snapshot — MCX Lot-Scale Day P&L Fix

**File**: `backend/api/algo/daily_snapshot.py` — `_snap_compute_day_pnl()`

When computing day P&L for intraday positions, `_snap_compute_day_pnl()` now accepts 
an optional `multiplier` parameter (default=1). When a multiplier is provided (as for MCX 
positions with `lot_size > 1`), the function scales `overnight_quantity`, 
`day_buy_quantity`, and `day_sell_quantity` by the lot_size before computing the decomposed 
intraday P&L formula:

```python
def _snap_compute_day_pnl(row, multiplier=1):
  oq = row.get("overnight_quantity", 0) * multiplier
  dbq = row.get("day_buy_quantity", 0) * multiplier
  dsq = row.get("day_sell_quantity", 0) * multiplier
  return decomposed_intraday_pnl(oq, dbq, dsq, row["ltp"], row.get("avg_price"))
```

**Impact**: For MCX positions with large lot sizes (CRUDEOIL lot=100, etc.), the stored 
`daily_book.day_pnl` is now correct in ₹ terms on first appearance. Previously, when a 
brand-new MCX position had no prior `daily_book` row, the snapshot would compute day P&L 
using the raw contract quantity (e.g. 1 contract) without scaling by lot size, resulting 
in a value off by 100× (₹50 instead of ₹5000). This fix ensures the formula always 
operates in the correct unit (lots, not raw contracts).

**Caller responsibility**: Snapshot writers must read `multiplier` from broker response 
(mapped from `lot_size` field) and pass it via `r.get("multiplier", 1)` to the function.

---

## 7.3a Daily Snapshot Orphan Cleanup

**File**: `backend/api/algo/daily_snapshot.py` — `snapshot_daily_book()` + `_delete_orphan_positions()` + `_delete_prior_orphan_positions()`

After each per-account positions UPSERT in `snapshot_daily_book()`, a new async helper 
`_delete_orphan_positions(target_date, account, current_symbols)` deletes stale `daily_book` 
rows for that `(date, account, kind="positions")` whose `symbol` is NOT in the current broker 
response.

### Problem addressed

Kite removes settled/squared-off positions from `broker.positions()` after settlement (~16:15 IST). 
The prior UPSERT-only pattern left ghost rows indefinitely in the `daily_book` table. Because 
`_positions_snapshot()` has no staleness filter, closed positions remained visible in the UI 
all night after market close.

### Solution

**Broker response sentinel** (Jul 2026): `_fetch_account_data()` now initialises 
`out["positions"] = None` (was `[]`); a successful broker call sets it to a list (possibly 
empty); failure leaves it `None`. This distinguishes two cases:

- **`positions = []`** — Broker call succeeded; all positions are closed/settled (SSOT)
- **`positions = None`** — Broker call failed; skip cleanup (fail-open)

**Orphan deletion logic**: After successful positions UPSERT, `_delete_orphan_positions()` is 
called with the set of symbols from the broker response. It executes:

```sql
DELETE FROM daily_book 
WHERE date = target_date 
  AND account = account 
  AND kind = 'positions' 
  AND symbol NOT IN (current_symbols)
```

If the broker call failed (`raw["positions"] is None`), the cleanup is skipped entirely 
(fail-open behaviour — no deletion risk if the broker is temporarily unreachable).

### Prior-day orphan cleanup (commit 21d1656a)

After the same-day orphan deletion, a second pass `_delete_prior_orphan_positions(account, 
current_symbols)` removes stale positions from the **most-recent prior-day snapshot batch**. 
This addresses settled options (e.g., IDFCFIRST after expiry) that are no longer held but 
persisted in yesterday's snapshot row.

**Scope**: Past 7 days of daily_book snapshots; scoped to positions `kind='positions'` only.

### Cleanup timing

- **Execution**: After each `snapshot_daily_book()` run for each account
- **Scope**: Only `kind='positions'` rows; `kind='holdings'` is unaffected
- **Idempotency**: Safe to re-run; DELETE is idempotent
- **Prior-day scope**: Past 7 days (prevents unbounded table scans)

### Impact

Closed positions no longer appear in the UI after settlement, including settled options from 
prior-day snapshots. The `_positions_snapshot()` route query returns only live positions + 
their prior-session state, providing an accurate real-time view without stale entries.

---

## 7.3.0 Quantity/Lots/Lot-Size Normalization

**File**: `backend/brokers/broker_apis.py` — `_annotate_lot_size()`

All broker responses normalize to a **uniform quantity unit: CONTRACTS**.

### Why

MCX positions ship from Kite as LOTS (e.g., lot_size=100, Kite sends qty=5 meaning 500 contracts). 
NFO positions ship as CONTRACTS. Equity as-is. Without normalization, code multiplies by 
`lot_size` inconsistently → wrong P&L, wrong order qty ceilings.

### Solution: `_annotate_lot_size()` SSOT

After every broker fetch (holdings, positions), `_annotate_lot_size(df, broker_id, exchange)` 
applies one rule uniformly:

1. **Lookup** instrument's `lot_size` from adapter's `_LOT_INDEX` (pre-loaded at startup)
2. **For MCX/NCO rows** — multiply `quantity`, `overnight_quantity`, `day_buy_quantity`, 
   `day_sell_quantity` by `lot_size` (convert lots → contracts)
3. **For NFO/CDS/BFO rows** — already in contracts; derive `lots = quantity / lot_size`
4. **For equity** — no-op; `lots = quantity`, `lot_size = 1`
5. **Add informational fields** — `lots: int` and `lot_size: int` to every row

**Invariant after return**: `daily_book.quantity` is ALWAYS in CONTRACTS, regardless of 
exchange or lot structure.

### Daily snapshot persistence

`daily_snapshot.py::snapshot_daily_book()` UPSERT now includes `lots` and `lot_size` columns 
in the `daily_book` table (schema via SQL migration). Both are informational (stored for audit + 
UI display); all P&L and day P&L math operates on `quantity` (contracts).

### New UPSERT guards (Aug 2026)

**Guard 1: `day_pnl` stale-preservation fix**

```sql
ON CONFLICT (...) DO UPDATE SET
  day_pnl = CASE WHEN EXCLUDED.ltp IS NOT NULL THEN ...formula... END
```

**Problem**: Mid-session passes with NULL LTP would preserve a prior session's `day_pnl` even 
when the formula correctly returned 0 (flat close). 

**Fix**: Only write `day_pnl` when EXCLUDED.ltp is NOT NULL. If NULL, the column is unchanged 
(and on next pass when LTP arrives, the formula re-computes).

**Guard 2: `previous_close` advancement gate**

```sql
previous_close = CASE 
  WHEN EXCLUDED.ltp IS NOT NULL AND EXCLUDED.ltp != daily_book.ltp 
  THEN EXCLUDED.ltp 
  ELSE daily_book.previous_close 
END
```

**Problem**: Weekend snapshots with frozen LTPs would advance `previous_close` on every poll, 
even though the price hadn't changed. The next session's formula `(ltp - previous_close)` 
would compute wrong day P&L.

**Fix**: Only advance `previous_close` when ltp actually changes (not on frozen weekend snapshots).

### `previous_close` immutability (Aug 2026)

`previous_close` is frozen after the first INSERT for each (account, symbol, 
captured_at date). The UPSERT `ON CONFLICT DO UPDATE` clause deliberately omits 
`previous_close = EXCLUDED.previous_close` — intraday writes never overwrite it. 
Only `fix_daily_book_prev_close()` may update `previous_close`, once per session 
at open (see section 7.3.12).

---

## 7.3.1 MCX 23:45 ltp=0 Corruption — Three-Layer Defense (Aug 2026)

**File**: `backend/api/algo/daily_snapshot.py` — `snapshot_daily_book()` UPSERT  
**File**: `backend/api/routes/positions.py` · `backend/api/routes/holdings.py` — CTEs  
**File**: `backend/brokers/broker_apis.py` — `_enrich_positions()`

MCX occasionally returns `ltp=0` at settlement (~23:45 IST). Without guards, a zero 
LTP overwrites a valid prior-session settlement price, causing grids to show blank 
cells and NAV to collapse.

### Layer 1: UPSERT NULLIF coalesce (write-side guard)

```sql
ON CONFLICT (date, account, kind, symbol) DO UPDATE SET
  ltp = COALESCE(NULLIF(EXCLUDED.ltp, 0), daily_book.ltp),
  payload_json = CASE 
    WHEN EXCLUDED.ltp IS NOT NULL AND EXCLUDED.ltp > 0 THEN EXCLUDED.payload_json 
    ELSE daily_book.payload_json 
  END,
  ...
```

When `EXCLUDED.ltp = 0`, `NULLIF(0, 0)` returns NULL, so `COALESCE` falls back to 
`daily_book.ltp` (the existing valid price). Row is UPSERT'd with NULL LTP to preserve 
join integrity; the existing LTP survives.

### Layer 2: Writer NULL-set on zero-outside-session (mid-write guard)

When `ltp=0` and the timestamp falls **outside** the MCX regular session window 
(09:00–17:00 IST) or evening session (17:00–23:30 IST), the writer sets `ltp_val = None` 
(not a skip). Row stays in the batch and is committed; UPSERT NULLIF then preserves 
the existing good LTP.

### Layer 3: Reader filters zero rows (read-side guard)

Both positions and holdings routes now filter `WHERE ltp IS NOT NULL AND ltp > 0` in 
their `latest_batch` CTEs. Final WHERE adds `(db.ltp IS NULL OR db.ltp > 0)` to 
exclude zero rows from snapshot delivery.

**Impact**: Off-hours grids (positions, holdings, sparklines) now display frozen prices 
correctly throughout closed windows without blank cells from MCX zero-LTP corruption.

---

## 7.3.2 Admin Snapshot Trigger — Holiday-Aware Market-Open Detection (Aug 2026)

**File**: `backend/api/routes/admin.py` — `POST /api/admin/pnl/snapshot`

The admin snapshot trigger endpoint allows operators to manually capture daily book snapshots (holdings, positions, trades) for a given date. The endpoint now correctly detects market-open status using holiday-aware logic and allows explicit overrides.

### Request schema

```json
{
  "date": "YYYY-MM-DD or 'today'",
  "market_open": boolean | null
}
```

**`date`** — ISO format date string or `"today"` (resolved to IST). Trades are only available for today's IST date; historical dates capture holdings + positions only.

**`market_open`** — Optional override. When provided (true/false), forces EOD mode (`false`) or live mode (`true`) regardless of actual exchange status. When `null` or omitted, auto-detects via `is_any_segment_open()`.

### Market-open detection (Aug 2026 fix)

Prior behaviour: `_is_exchange_open_at()` checked time-of-day only, ignoring holidays. Snapshot triggered on a weekend would capture holdings at stale broker LTPs (not settlement prices).

**New behaviour**: `is_any_segment_open(_ts_indian())` checks both:
- Time of day (each exchange's session hours)
- Holiday calendar (NSE/BSE/MCX/NCDEX/CDSL/USDINR closed dates)

Result: Triggering snapshot on a weekend or holiday correctly detects closed state and writes EOD prices to `daily_book`.

### Explicit override pattern

Operator can force EOD mode on any date:

```json
POST /api/admin/pnl/snapshot
{"date": "2026-08-15", "market_open": false}
```

Regardless of the time-of-day or holiday status, the snapshot captures with settlement/close prices instead of live LTPs.

### Response schema

```json
{
  "accounts": ["ZG0790", "DH3747"],
  "holdings_rows": 42,
  "positions_rows": 18,
  "trades_rows": 0,
  "errors": []
}
```

**`accounts`** — List of broker accounts that contributed data.

**`holdings_rows`**, **`positions_rows`**, **`trades_rows`** — Rows written to `daily_book` per kind. Trades only present for today's IST date.

**`errors`** — List of per-account capture failures (account name + error message). Snapshot completes partially (other accounts' data written) even if some fail.

---

## 7.3.3 Dhan `last_price=0` Fallback in EOD Snapshots (Aug 2026)

**File**: `backend/api/algo/daily_snapshot.py` — `_snap_holding_eod_vals()`

When capturing holdings snapshots on non-trading days (weekends, holidays), Dhan's market-data cache is often cold and returns `last_price=0` for all holdings. This causes holdings rows to disappear from the Pulse grid during closed windows.

### Fallback chain in `_snap_holding_eod_vals()`

When `last_price=0` AND `mid_session=False` (i.e., snapshot is capturing EOD prices):

1. Try `close_price` (broker's prior-session close, if available)
2. Fall back to `previous_close` (from daily_book, if available)
3. Only use `last_price=0` if both above are missing/zero

**Impact**: Dhan holdings appear in Pulse on non-trading days even when Dhan's quote API is cold. Holdings show prior-session close prices (visually stale but correct) instead of disappearing.

**Example**: On Sunday morning, operator checks Pulse → Dhan holdings visible with Saturday's close prices (not live LTPs). After market open Monday, holdings refresh to live LTPs.

---

## 7.3.4 Weekend Guard for Filtered Holdings & Positions (Aug 2026)

**File**: `backend/api/algo/daily_snapshot.py` — `_snap_all_filtered()`

When a broker returns all-zero quantities for holdings or positions (e.g., weekend when no positions open), the snapshot upsert skips to protect stale data. The guard now operates independently for holdings and positions.

### Prior behaviour

If ANY rows were filtered (holdings=0 or positions=0), but the OTHER category had data, the function would still skip upsert. Example: Weekend snapshot with zero positions but active holdings → holdings were silently dropped.

### New behaviour (Aug 2026)

The guard fires per-category:

- **Holdings filtered entirely** → skip holdings UPSERT, but still UPSERT positions if present
- **Positions filtered entirely** → skip positions UPSERT, but still UPSERT holdings if present

**Impact**: On weekends, Dhan holdings (which remain even when positions flatten) are now captured. Weekend snapshots no longer lose active holdings when open positions drop to zero.

---

## 7.3.5 Holdings Gate Now NSE-Specific (Aug 2026)

**File**: `backend/api/helpers/snapshot_gate.py` — `closed_hours_or_broker()`

Holdings routes now use NSE-only closed-hours gate instead of waiting for all
market segments to close:

```python
data, source = await closed_hours_or_broker(
    exchange='NSE',
    snapshot_fn=_holdings_snapshot,
    broker_fn=_fetch_holdings_live,
    segment_exchanges=["NSE"],  # NEW: restrict to NSE only
    route_key='holdings',
)
```

**Impact**: Holdings enter snapshot mode at NSE close (~15:35 IST) instead of
at MCX close (~23:30 IST). The gate now uses `segment_exchanges=["NSE"]`
parameter to restrict market-open check to NSE segments only. This means:

- Holdings show frozen prices once NSE settles
- MCX-only positions (if any) still receive live broker fetch
- Pre-market and post-MCX-settlement holdings display correct day P&L from
  prior-session snapshot without waiting 8 hours for MCX to close

---

## 7.3.6 Holdings Data Freshness & SSOT Fetch TTL (Aug 2026)

**File**: `backend/brokers/broker_apis.py` — `fetch_holdings()` + `_HOLDINGS_SSOT_TTL`

`fetch_holdings()` now enforces a **30-second TTL** on broker responses, mirroring 
`fetch_positions()` (commit 39c21cca). Added module-level:

```python
_HOLDINGS_SSOT_TTL = 30.0  # seconds
_holdings_ssot_refresh_at: dict[str, float] = {}
```

Logic: `fetch_holdings()` checks elapsed time since last successful fetch for each 
account. If `now() < _holdings_ssot_refresh_at[account]`, the broker call is bypassed 
and the cached holdings DataFrame is returned. On stale (TTL expired), `fetch_holdings()` 
calls `_fetch_holdings_cached(force_refresh=True)`, guaranteeing fresh data within 30s.

**Impact**: Holdings grid no longer shows stale positions within a 30s window after 
rapid account switches or when polling overlaps with position changes. Aligns holdings 
cache freshness with the stricter positions cache.

---

## 7.3.7 Snapshot Path Now Calls `_override_stale_close_for_holdings` (Aug 2026)

**File**: `backend/api/routes/holdings.py` — `_holdings_snapshot()` + 
`_build_holding_row_from_snapshot()`

Both the **broker path** (live fetch) AND the **snapshot path** (closed-hours
read) now apply `_override_stale_close_for_holdings()` to patch stale or missing
`close_price` values with the correct prior-session settlement LTP from
`daily_book.ltp` (captured at settlement, `captured_at < today_08:00 IST`).

**Broker path flow**:
1. `_fetch_holdings_live()` calls `fetch_holdings()`
2. `_override_stale_close_for_holdings()` patches `close_price` from DB snapshot
3. Day P&L recomputed using patched `close_price`

**Snapshot path flow** (during closed hours or on broker failure):
1. `_holdings_snapshot()` queries `daily_book` for latest batch per account
2. `_build_holding_row_from_snapshot()` reconstructs row from snapshot columns
3. `_override_stale_close_for_holdings()` patches `close_price` from prior-session DB row
4. Day P&L recomputed using patched `close_price`

**Impact**: Both paths now use the same prior-session settlement LTP as the
day P&L reference price, eliminating divergence between live and snapshot
displays. Previously the snapshot path used the drifted `db.previous_close`
from the rolling-shift UPSERT, which could drift overnight.

---

## 7.3.8 Firm NAV Computation & Closed-Exchange LTP Overlay

**File**: `backend/api/algo/nav.py` — `compute_firm_nav()` + `_fetch_holdings_phase()`  
**File**: `backend/api/helpers/snapshot_gate.py` — `latest_snapshot_ltp_map()`  
**File**: `backend/api/routes/positions.py` — `_overlay_snapshot_for_closed_exchanges()` + `_process_overlay_row()`  
**File**: `backend/api/routes/holdings.py` — `_hold_tag_closed_row()`

Firm NAV calculation (v4 formula: `cash_total + positions_mtm + holdings_mtm`) ensures
that when an exchange closes, both the `/api/holdings` route and the daily NAV snapshot
use **the same LTP** — a DB snapshot LTP captured at last market close, not a stale
broker response.

### Holdings phase with snapshot overlay

`_fetch_holdings_phase()` (called by `compute_firm_nav()`):

1. Fetches holdings from all broker accounts via `fetch_holdings()`
2. **NEW (Aug 2026)**: Calls `latest_snapshot_ltp_map("holdings")` to fetch the DB-backed
   LTP map for all holdings symbols (captures snapshots across all exchanges from the
   most recent `daily_book` row per account/symbol)
3. **NEW (Aug 2026)**: Applies `_overlay_closed_exchange_ltp(df, snap_map)` to each
   holdings DataFrame before summing `cur_val`

### Overlay logic — `latest_snapshot_ltp_map` return type

`latest_snapshot_ltp_map(kind)` (commit adc5e1f0) now returns:

```python
dict[tuple[str, str], tuple[float, float | None]]
# Key: (account, symbol)
# Value: (ltp, day_pnl)
#   - ltp: settlement LTP from daily_book
#   - day_pnl: broker-computed EOD day P&L (nullable — None or 0 = no data)
```

The return type changed from `dict[tuple[str, str], float]` (LTP only) to include
a second tuple element carrying the stored `day_pnl`. This fixes the weekend
zero-delta bug where both `snap_price` and `close_px` came from the same Friday
settlement snapshot → `(Fri − Fri) × qty = 0`.

### Closed-exchange day P&L overlay using stored values

**File**: `backend/api/routes/positions.py` — `_process_overlay_row()`

For positions rows on closed exchanges (lines 474–491):

1. Unpack snapshot tuple: `snap_ltp, snap_day_pnl = snap_map.get(key, (None, None))`
2. **If `snap_day_pnl` is non-zero** → use it directly as the authoritative `day_change_val`:
   ```python
   if snap_day_pnl is not None and snap_day_pnl != 0.0:
       dcv = snap_day_pnl
   ```
3. **Otherwise** → fall back to price-recompute path using prior-session close:
   ```python
   elif ref_close > 0:
       dcv = (snap_ltp - ref_close) × qty
   ```

**File**: `backend/api/routes/holdings.py` — `_hold_tag_closed_row()`

For holdings rows on closed exchanges, the same logic applies: when
`snap_day_pnl` is authoritative (non-None and non-zero), it replaces the
broker-computed value with the stored settlement day P&L.

**Impact**: The weekend zero-delta bug is fixed. When NSE and MCX both settle
on Friday, the snapshot captures Friday settlement `ltp` and `day_pnl`. On
Saturday-Sunday when the grid reads that snapshot for closed-exchange rows,
the overlay uses the stored `day_pnl` directly instead of recomputing
`(Fri_close − Fri_close) × qty = 0`.

### Pre-session divergence eliminated (Aug 2026)

Before market open, the firm NAV calculation and the holdings grid now show
the same values, eliminating the ~6L (60-lakh) pre-session NAV divergence that
occurred when firm NAV used live broker LTPs (which had not yet updated to the
new session's opening levels) while the holdings route used frozen DB snapshots.

### Function: `latest_snapshot_ltp_map(kind)` (Aug 2026 — commit adc5e1f0)

**File**: `backend/api/helpers/snapshot_gate.py`

```python
async def latest_snapshot_ltp_map(
    kind: str  # 'positions' or 'holdings'
) -> dict[tuple[str, str], tuple[float, float | None]]:
```

**Purpose**: Returns a unified lookup map of (ltp, day_pnl) tuples keyed by
(account, symbol) for use in closed-exchange overlay logic across holdings,
positions, and NAV routes.

**Query strategy**: Queries `daily_book` for the most-recent batch per account
(using `MAX(captured_at)` within each account group). Returns only rows where
`ltp IS NOT NULL AND ltp > 0`.

**Return format**:
- **Key**: `(account: str, symbol: str)`
- **Value**: `(ltp: float, day_pnl: float | None)`
  - `ltp` — settlement LTP from the snapshot (always positive when row is present)
  - `day_pnl` — broker-computed end-of-day P&L, or None if unavailable

**Guarantee**: Uses the **identical `latest_batch` CTE** as the per-route snapshot
readers (`_positions_snapshot`, `_holdings_snapshot`), ensuring the two paths
(live broker fetch + closed-exchange overlay, vs. snapshot read path) never
diverge on which daily_book batch is authoritative.

**Fail-open**: Returns `{}` on any database error; callers gracefully omit the
snapshot-map lookup and fall through to the price-recompute fallback.

---

## 7.3.9 `close_price` Always Synced to `ref_close` (Aug 2026)

**File**: `backend/api/routes/holdings.py` — `_override_stale_close_for_holdings()`

The epsilon guard (`abs(ref_close - current_close) > 0.005`) that gated whether
`close_price` gets synced has been **removed**. `close_price` is now **always** set
to `ref_close` (the prior-session settlement LTP from daily_book).

**Prior behaviour**: Epsilon guard prevented sync when prices were already "close
enough", saving DB writes but leaving `close_price` stale.

**New behaviour**: `close_price` is unconditionally synced to `ref_close` to keep
`_recompute_day_change_pct`'s denominator consistent with `day_change_val`. Both
metrics now derive from the same prior-session price source.

**Impact**: Holdings day P&L percentage metric `day_change_percentage = day_pnl /
(close_price × qty) × 100` and the numerator `day_pnl = (ltp - close_price) × qty`
now use identical denominators, preventing floating-point divergence that could
cause NavStrip to show inconsistent day P&L vs. percentage figures.

---

## 7.3.10 Holdings Day P&L Recompute & Backstop Exclusion (Aug 2026)

**File**: `backend/api/routes/holdings.py` — `_override_stale_close_for_holdings()`

Holdings day P&L now recomputes for **ALL rows where a daily_book snapshot with 
`previous_close > 0` exists** (commit 39c21cca), not just rows where `close_price` 
was patched from zero. Formula applied universally:

```python
day_pnl = (ltp - previous_close) * qty
```

Prior behaviour: Only rows where Dhan's `close_price=0` was patched from Kite quote 
received day P&L recompute. Holdings with pre-existing stale `close_price` values 
retained their (incorrect) cached day P&L.

**New behaviour**: When `_override_stale_close_for_holdings()` runs during daily book 
snapshot retrieval, it recomputes day P&L for **all** holdings rows where the previous 
session's `daily_book` row exists and contains a valid `previous_close`. This ensures 
holdings day P&L is never stale due to broker cache delays.

### Exclusion: `apply_day_change_backstop` NOT applied to holdings (Aug 2026)

`apply_day_change_backstop()` is called in positions flow but **explicitly removed** 
from holdings (commit 39c21cca). Reason: backstop depends on `overnight_quantity` 
column (Case 1: `oq=0`; Case 2: `oq>0, dcv==0`; Case 3: `oq=0` intraday). Holdings 
rows have no `overnight_quantity` field (equity holdings do not have intraday decomposition). 
Applying backstop to holdings with missing `overnight_quantity` would fire Case 1 
incorrectly (missing field → 0 → matched condition → recompute on every fetch).

**Scope**: Backstop reserved for positions P&L only. Holdings use direct 
`(ltp - previous_close) * qty` formula without edge-case recovery.

---

## 7.3.11 Holdings LTP Override & pnl+cur_val Consistency (Aug 2026)

**File**: `backend/api/routes/holdings.py` — `_override_stale_ltp_from_ticker()`
**File**: `backend/brokers/broker_apis.py` — `_build_holdings_pnl_expr()`

### LTP patch consistency recompute

`_override_stale_ltp_from_ticker()` patches `last_price` from the KiteTicker for any
holdings row with a stale/zero LTP after backfill. Previously, after patching `last_price`,
the function updated `day_change_val` and `day_change` but left `pnl` and `cur_val` stale
(computed against the old zero `last_price`). This caused the NavStrip H slot 2 to display
the invested amount (`inv_val`) instead of current market value when LTP was first patched.

**Fix (Aug 2026, commit bad82021)**: After patching `last_price`, the function now also
recomputes `pnl`, `pnl_per_share`, and `cur_val` on the same rows:

```python
if 'average_price' in raw.columns and 'pnl' in raw.columns:
    _avg_p = pd.to_numeric(raw.loc[_sel, 'average_price'], errors='coerce').fillna(0)
    _pnl_p = (_ltp_p - _avg_p) * _qty_p
    raw.loc[_sel, 'pnl'] = _pnl_p.where(_ltp_p > 0, raw.loc[_sel, 'pnl'])
    if 'pnl_per_share' in raw.columns:
        raw.loc[_sel, 'pnl_per_share'] = (
            _pnl_p / _qty_p.replace(0, float('nan'))
        ).fillna(0).where(_ltp_p > 0, raw.loc[_sel, 'pnl_per_share'])
    if 'inv_val' in raw.columns and 'cur_val' in raw.columns:
        _inv_p2 = pd.to_numeric(raw.loc[_sel, 'inv_val'], errors='coerce').fillna(0)
        raw.loc[_sel, 'cur_val'] = (_inv_p2 + raw.loc[_sel, 'pnl']).where(
            _ltp_p > 0, raw.loc[_sel, 'cur_val']
        )
```

This ensures the API response is internally consistent: `last_price`, `pnl`, `pnl_per_share`, 
and `cur_val` all reflect the same fresh LTP immediately after the patch.

### Broker pnl zero-trust policy

`_build_holdings_pnl_expr()` in `broker_apis.py` now requires broker `pnl` to be both
non-null AND non-zero before trusting it:

```python
# Before:
pl.when(_broker_pnl.is_not_null())

# After:
pl.when(_broker_pnl.is_not_null() & (_broker_pnl != 0.0))
.then(_broker_pnl)
.when((_ltp > 0) & (_avg > 0))
.then(_pnl_calc)
.otherwise(pl.lit(0.0))
```

**Rationale**: Kite sends `pnl=0.0` (not null) during the pre-market window when
`last_price=0`. The old code trusted that zero, setting `cur_val = inv_val`, making the
holdings card display invested amount instead of current value. The new code treats
`pnl=0.0` as "no data" and falls back to the computed formula `(ltp - avg) × qty`. At
true breakeven (`ltp == avg`) both formulas give 0, so there is no regression for
genuinely flat positions.

**Impact**: Holdings P&L and `cur_val` now remain consistent with the live LTP throughout
the pre-market and post-settlement windows, eliminating the 30-second gap where the
NavStrip H slot would show stale values until the next refresh.

---

## 7.3.11 Universal Day P&L Formula in Snapshot (Aug 2026)

**File**: `backend/api/routes/positions_helpers.py` — `build_row_from_snapshot_raw()`

When reconstructing a position from a closed-hours snapshot, the day P&L now uses 
a universal formula that handles all five position states:

```python
day_pnl = total_pnl - (prev_close - avg) * overnight_quantity
```

**Five position states covered**:

1. **Overnight open** (`oq > 0, qty > 0`): 
   `day_pnl = total_pnl - (prev_close - avg) × oq` → zeroes the per-unit cost gap, leaving intraday move
2. **New today** (`oq = 0, qty > 0`): 
   `day_pnl = total_pnl` (term is zero; formula degenerates correctly)
3. **Partial close** (`oq > 0, qty > 0, qty < oq`): 
   Formula handles mixed legs (open + closed portions)
4. **Fully closed intraday** (`oq = 0, qty = 0`): 
   `day_pnl = realised` (total_pnl only contains realised P&L)
5. **Fully closed overnight** (`oq > 0, qty = 0`): 
   `day_pnl = total_pnl − (prev_close − avg) × oq` (exit price − close) × qty

**Fallback**: When `prev_close` is unavailable, the formula uses the stored 
`day_pnl` directly from the `daily_book` row (no re-calculation).

**Impact**: All closed-hours position snapshots now use consistent day P&L 
regardless of position state, eliminating edge-case gaps between live and snapshot 
displays.

---

## 7.3.11a Positions P&L Unification — `pnl + realised` (Aug 2026)

**File**: `backend/brokers/broker_apis.py` — `_enrich_positions()`

When computing `pnl` in positions rows, `_enrich_positions()` now unifies realised
and unrealised P&L by summing them:

```python
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

**Broker-specific handling**:

- **Kite**: Separates `pnl` (unrealised) and `realised` for closed/partially-closed legs
- **Dhan**: May split P&L across both fields
- **Groww**: May split P&L across both fields
- **Other brokers**: `realised = 0` by default

Adding `pnl + realised` ensures the snapshot's `daily_book.total_pnl` captures 
the full economic P&L (realised gains/losses on closed legs + unrealised MTM on 
open legs). This total is then used by `build_row_from_snapshot_raw()` during 
closed-hours position reconstruction via the universal day P&L formula.

**Impact**: The `total_pnl` stored in `daily_book` snapshots now accurately reflects
the position's total P&L across all brokers, enabling consistent day P&L calculations
in the snapshot path (section 7.3.11).

---

## 7.3.12 Holdings Snapshot Day Change Percentage Formula

**File**: `backend/api/routes/holdings.py` — `_build_holding_row_from_snapshot()`

### Snapshot retrieval and `as_of` field

When a daily holdings snapshot is retrieved from the `daily_book` table, `_holdings_snapshot()` 
returns either a populated `HoldingsResponse` with `as_of=<timestamp>` or an empty response 
with `as_of=None` (Aug 2026 fix). On first deploy when no DB snapshot exists, `as_of=None` 
signals to `closed_hours_or_broker()` that no snapshot is available, so the broker fallback 
fires correctly during closed hours instead of short-circuiting. This prevents empty grids 
during the first market-close window after deployment.

When a daily holdings snapshot is retrieved, the `day_change_percentage` metric is 
computed from a snapshot row fetched from the `daily_book` table:

```python
day_pnl = (ltp - previous_close) * quantity
day_change_percentage = (day_pnl / (previous_close * quantity)) * 100
```

The denominator uses **`previous_close`** (the prior session's close price from 
`daily_book`), NOT the current LTP.

### Fallback when `previous_close` is zero or missing

When a holding was purchased same-day (no prior session close exists) or when the 
broker returns zero for `previous_close` (cold-boot), the formula falls back to 
`avg_cost` (average purchase price):

```python
day_change_percentage = (day_pnl / (avg_cost * quantity)) * 100
```

### Rationale

Using `previous_close` as the denominator gives an accurate intraday P&L percentage 
relative to the session's opening state. Using LTP would distort the metric:
- **Down-moves**: `(negative_pnl / negative_ltp) × 100` inflates the percentage 
  magnitude.
- **Up-moves**: `(positive_pnl / positive_ltp) × 100` understates the percentage gain.

The `avg_cost` fallback ensures same-day purchases show a realistic cost-basis 
percentage rather than crashing on zero denominator.

### Source data

SQL query in `_build_holding_row_from_snapshot()` now selects `db.previous_close` 
from the `daily_book` table (commit 13ec7c18), ensuring the correct price is 
available for every holding row.

### `previous_close` SSOT: Prior-Day Socket-Derived LTP (Aug 2026)

**File**: `backend/api/algo/daily_snapshot.py` — `snapshot_daily_book()` + 
`_holdings_rows()` / `_positions_rows()`

The `previous_close` field in daily_book snapshot rows now sources from prior-day 
`daily_book.ltp` (socket-derived LTP frozen by the UPSERT), NOT the broker's 
REST `close_price` field. This ensures Day P&L is computed against the actual 
last-traded price from the previous session, not the broker's potentially stale 
or recalculated close price.

**Lookup chain** (per call site):

1. **Primary (Aug 2026)**: Query prior-day `daily_book` rows where 
   `date < :today AND ltp IS NOT NULL AND ltp > 0`, grouped by 
   `(account, symbol, kind)` descending by date. Build `prev_ltp_map` dict 
   keyed `(account, symbol, kind) → float(ltp)`.

2. **Fallback**: If no prior-day row exists or LTP lookup fails, use broker's 
   REST `close_price` field (applies only to new positions/holdings with no 
   prior snapshot).

```python
"previous_close": (
    (prev_ltp_map or {}).get((account, symbol, "holdings"))
    or (float(r["close_price"]) if r.get("close_price") else None)
)
```

**Impact**:

- Day P&L formula `(socket_ltp − previous_close) × qty` now uses uniform 
  socket-frozen LTPs across all sessions (intraday, weekend, holidays, 
  mid-session edges).
- Eliminates overnight, weekend, and holiday distortion from broker's 
  recalculated `close_price` that may reflect settlement adjustments or 
  corporate actions.
- Ensures Day P&L consistency when exchanges close mid-session (MCX during 
  NSE close, NCDEX holidays, etc.).

### `previous_close_backup` Column (Aug 2026)

**File**: `backend/api/algo/daily_snapshot.py` — `fix_daily_book_prev_close()`

A new `previous_close_backup` column in `daily_book` stores a copy of 
`previous_close` saved by `fix_daily_book_prev_close()` before it overwrites 
`previous_close` at market open. This provides a guard against legacy-corrupted 
rows where `previous_close ≈ ltp` (rolling-shift corruption from overnight hours).

**Schema**:

- **`previous_close_backup`** — `DOUBLE NULLABLE` — copy of `previous_close` 
  saved by `fix_daily_book_prev_close()` before it overwrites `previous_close`. 
  COALESCE guard ensures only the first daily backup survives (subsequent updates 
  within the same day preserve the original value). Never updated by intraday 
  UPSERT.

**Purpose**: Reader fallback chain in `_resolve_previous_close()` (holdings and 
positions) checks: (1) if `previous_close ≈ ltp` (corruption), try 
`previous_close_backup`; (2) if backup also absent/corrupted, fall back to 
`prev_ltp` from prior snapshot batch. This ensures day P&L uses the correct 
prior-session price even when legacy data has corrupted `previous_close` values.

### Reader Fallback Chain for `previous_close` (Aug 2026)

**File**: `backend/api/routes/holdings.py` — `_build_holding_row_from_snapshot()`  
**File**: `backend/api/routes/positions_helpers.py` — `build_row_from_snapshot_raw()`

Both holdings and positions snapshot readers use `_resolve_previous_close(pc_f, 
ltp_f, backup_f, prev_ltp_f)` to guard against legacy-corrupted rows:

```python
def _resolve_previous_close(pc_f, ltp_f, backup_f, prev_ltp_f):
    # pc_f: current previous_close value from row
    # ltp_f: current ltp value from row
    # backup_f: previous_close_backup (saved copy, nullable)
    # prev_ltp_f: ltp from prior snapshot batch (fallback)
    
    if pc_f is not None and abs(pc_f - ltp_f) < 0.005:
        # Rolling-shift corruption: pc ≈ ltp
        if backup_f is not None and abs(backup_f - ltp_f) >= 0.005:
            return backup_f  # Layer 1: use backup if uncorrupted
        elif prev_ltp_f is not None:
            return prev_ltp_f  # Layer 2: fall back to prior batch LTP
    
    return pc_f  # Normal case: use current previous_close
```

**Three-layer fallback**:

1. **If `previous_close ≈ ltp`** (epsilon < 0.005 = rolling-shift corruption):
   - Try `previous_close_backup` (saved copy)
   - If backup is also absent or ≈ ltp: fall back to `prev_ltp` from the prior 
     snapshot batch
   - Otherwise return `previous_close` unchanged

2. **Layer 2** (if backup unavailable): Use `prev_ltp` (prior batch's settlement 
   LTP, queried from 7-day lookback)

3. **Layer 3** (normal case): Return `previous_close` as-is

**Impact**: Day P&L calculations use the correct prior-session close price even 
when `daily_book` rows contain legacy-corrupted `previous_close` values. Operator 
sees accurate overnight moves immediately after market open, not just after 
`fix_daily_book_prev_close()` runs at 08:00 IST.

### Dhan `close_price=0` handling and backfill recompute (Aug 2026)

**File**: `backend/api/algo/daily_snapshot.py` — `_backfill_recompute_derived()` 
(commit a737b1e2)

Dhan's `holdings()` API does not return `previousClosePrice`. The adapter therefore 
sets `close_price=0` and `day_change=ltp-0=ltp` (always incorrect). The snapshot 
backfill (`_backfill_market_data_dicts`) calls Kite's quote API to patch the missing 
`close_price` with the actual prior-session close. 

When `close_price` is patched from zero, `_backfill_recompute_derived()` now accepts 
a `close_was_missing: bool` flag. When True, it recomputes `day_change = ltp - 
real_close` ONLY when `day_change` is falsy or zero (Aug 2026 fix). This preserves 
pre-existing non-zero `day_change_val` from brokers like Dhan and Groww that supply 
decomposed intraday P&L values. Previously, the guard `not r.get("day_change")` prevented 
recompute because Dhan had already set a (wrong) value; this left `daily_book.day_pnl = ltp` 
(e.g. 3952) instead of the actual day move (e.g. -28). Now, when Dhan provides a decomposed 
`day_change` (rare, but correct), it is preserved; naive recompute (ltp - close) × qty 
only fires on zero/missing.

**Effective timing**: This fix works when the snapshot runs BEFORE Kite updates 
`ohlc.close` to today's close (window: ~3:30–18:00 IST, depending on market hours). 
The canonical EOD snapshot at 15:35 IST sees `ohlc.close` = prior session close and 
captures correct `day_pnl`.

**Invariant I25 — Dhan `close_price=0` detection and backfill**: Adapter sets 
`close_price=0` and `day_change=ltp` for Dhan holdings (no `previousClosePrice` in 
API). Backfill pipeline detects this via `close_was_missing=True` flag after patching 
from Kite quote; recomputes `day_change` unconditionally to override the stale broker 
value. Ensures holdings snapshot stores correct day P&L.

### Daily Book `day_pnl` stores total P&L, not per-share change (Aug 2026)

**File**: `backend/api/algo/daily_snapshot.py` — `_snap_holding_eod_vals()` 
(commit a737b1e2)

The value stored in `daily_book.day_pnl` for `kind='holdings'` rows is the **total 
day P&L** (`day_change_per_share × qty`), NOT the per-share `day_change`. This is 
consistent with positions rows, which use the naive formula `(ltp - close) × qty`.

When `_build_holding_row_from_snapshot()` computes `day_change_percentage`, it 
divides this total by `close_notional = previous_close × qty`:

```python
day_change_percentage = (daily_book.day_pnl / (previous_close * qty)) * 100
```

This convention ensures the stored value is directly interpretable as the P&L you 
would realize if you squared off the position at LTP on that session.

### Three-Layer prev_close Defect Fix (Aug 2026 — commit f1ecf3c8)

**Files**: 
- `backend/api/algo/daily_snapshot.py` — `snapshot_daily_book()`, `fix_daily_book_prev_close()`
- `backend/api/routes/positions.py` — `_override_stale_close_from_snapshot()`
- `backend/api/routes/holdings.py` — `_override_stale_close_for_holdings()`
- `backend/api/background.py` — `_task_daily_snapshot()`

**Defect**: When both NSE and MCX close between 15:35 IST (NSE settlement) and 08:00 
IST next morning (session open), the `daily_book.previous_close` column was set to 
`daily_book.ltp` from that settlement (wrong). At session open, `ltp == previous_close` 
→ `day_change = (ltp - prev_close) × qty = 0` for all symbols, hiding the overnight 
move since the prior-prior session. Frontend displayed zero day P&L for all holdings 
during the closed-hours window.

**Root cause**: Two issues stacked:

1. **prev_ltp_map query branch (overnight hours)**: Before 08:00 IST, `snapshot_daily_book()` 
   queried `daily_book.ltp` directly when building the `prev_ltp_map`. NSE settlement 
   (~15:35 IST) and MCX settlement (~00:15 IST) both capture `ltp` and write it to 
   `daily_book` on today's date. Using that LTP as `previous_close` created the 
   wrong-on-first-insert tuple.

2. **UPSERT rolling-shift bug**: The UPSERT `ON CONFLICT ... DO UPDATE` clause 
   attempted to advance `previous_close` only when LTP changed. But the query used 
   `COALESCE(EXCLUDED.ltp, daily_book.ltp)` to guard against NULL overwrites during 
   mid-session passes. This meant the first-insert of a settlement snapshot set 
   `previous_close = NULL`, then the UPSERT guard preserved it unchanged on 
   subsequent mid-session passes → stuck at NULL until the next date rolled over.

**Fix (Layer 1 — query logic in snapshot_daily_book)**:

Before 08:00 IST (overnight mode): Query `daily_book.previous_close` from rows with 
`date < today` instead of `ltp`. The UPSERT rolling-shift already stores the 
prior-prior-session settlement in the `previous_close` column (e.g., Aug 23 settlement 
lives in Aug 24's `previous_close` after NSE settlement fires and shifts the date). 
Using yesterday's `previous_close` gives the correct overnight display.

At/after 08:00 IST (new-session mode): Query `daily_book.ltp` from rows with 
`date < today`. This is the prior-session settlement (= correct new-session baseline). 
At this point, `ltp == prev_close` is valid (no intraday movement yet).

```python
# Before 08:00 IST: read prior-prior-session settlement
if now_ist < today_8am:
    _prev_sql = """
        SELECT DISTINCT ON (account, symbol, kind)
               account, symbol, kind, previous_close AS ltp
        FROM daily_book
        WHERE date < :today
          AND previous_close IS NOT NULL AND previous_close > 0
          AND kind IN ('holdings', 'positions')
        ORDER BY account, symbol, kind, date DESC
    """
# At/after 08:00 IST: read prior-session settlement
else:
    _prev_sql = """
        SELECT DISTINCT ON (account, symbol, kind)
               account, symbol, kind, ltp
        FROM daily_book
        WHERE date < :today
          AND ltp IS NOT NULL AND ltp > 0
          AND kind IN ('holdings', 'positions')
        ORDER BY account, symbol, kind, date DESC
    """
```

**Fix (Layer 2 — close-override paths in positions + holdings routes)**:

Changed the `today_ist_cutoff` formula used by `_override_stale_close_from_snapshot()` 
(positions) and `_override_stale_close_for_holdings()` (holdings) from 
`today_ist_midnight` to a session-boundary-aware cutoff:

```python
# Before 08:00 IST: cutoff = yesterday's 08:00 IST
# (excludes today's EOD snapshots from the close-override query)
# At/after 08:00 IST: cutoff = today's 08:00 IST
today_ist_cutoff = today_ist_8am if now_ist >= today_ist_8am else today_ist_8am - timedelta(days=1)
```

This ensures that before 08:00 IST, the close-override query reads from yesterday's 
08:00 cutoff backward, excluding tonight's MCX settlement snapshot from being used as 
the day's reference price.

**Fix (Layer 3 — data repair on startup and 08:00 IST transition)**:

New function `fix_daily_book_prev_close(now_ist)` repairs today's `daily_book` rows:

- **Overnight mode (before 08:00 IST)**: Identifies rows where 
  `|previous_close - ltp| < 0.005` (= the buggy first-insert state). Updates 
  `previous_close` from yesterday's `daily_book.previous_close` (the correct 
  prior-prior-session value).

- **New-session mode (at/after 08:00 IST)**: Unconditionally updates today's rows to 
  yesterday's `daily_book.ltp` (the prior-session settlement = new-session baseline).

Called twice:
1. **Startup**: `_task_daily_snapshot()` fires this once on boot to repair any 
   overnight-mode dirty data left from the previous session.
2. **Daily 08:00 IST transition**: Called again in `_task_daily_snapshot()` at 08:00 
   IST to transition the baseline for that day's new session.

```python
async def fix_daily_book_prev_close(now_ist=None) -> int:
    if now_ist < today_8am:
        ref_col = "previous_close"  # read prior-prior-session
        mode = "overnight"
    else:
        ref_col = "ltp"              # read prior-session
        mode = "new-session"
    # UPDATE daily_book SET previous_close = ref_col
    # WHERE date = :today AND |previous_close - ltp| < 0.005 (overnight)
    #    OR date = :today (new-session, unconditional)
```

### Market-open time loading (Aug 2026)

The fix fires at the **NSE session open time**, loaded dynamically from 
`exchange_schedule` via `exchange_clock.get_nse_open_time()`. On holidays 
(`open_time = None`), the fix does not fire.

**Lifecycle**:

- **Backend startup** (`seed_and_warm()`): NSE open time loaded from `exchange_schedule` 
  table and cached in `_nse_open_time_cached` module variable
- **Daily 05:30 IST** (`_task_holiday_refresh`): Combined morning task runs once per day:
  1. `load_today_open_time()` reads NSE open time from `exchange_schedule` table
  2. Holiday calendar refresh via NSE API (retries until 08:00 IST if NSE API is slow)
  3. Best-effort proactive token refresh for all broker types (Kite TOTP auto-login, 
     Dhan RenewToken API, Groww session refresh). Failures logged as warnings only — 
     `@retry_kite_conn` decorator auto-recovers on next API call.
  4. Note: token refresh skipped under `RAMBOQ_USE_CONN_SERVICE=1` (broker layer owns it)
- **08:00 IST transition**: `fix_daily_book_prev_close()` fires at the cached NSE 
  open time; if no row found or schedule not loaded, gracefully skips (fail-open)
- **06:00 IST — Token hard-expiry** (external Kite/Dhan/Groww server-side event, no code). 
  First API call after expiry triggers `@retry_kite_conn` decorator → auto re-auth 
  within 2–30s for all brokers

**Rationale**: Hardcoding "08:00 IST" would miss special market sessions (e.g., 
extended hours on festival eves, extended holidays). Loading from the schedule 
table ensures the fix respects the real market calendar.

**Correct behavior after fix**:

- **Overnight window** (MCX settlement 00:15 → 08:00 IST next day): 
  `prev_close = prior-prior-session settlement ≠ ltp` 
  → `day_change = (settlement - prior-prior) × qty` 
  → frontend shows yesterday's session performance

- **After 08:00 IST** (new session open): 
  `prev_close = prior-session settlement == ltp` (no intraday movement yet) 
  → valid state; live LTP ticks above/below this baseline throughout the day

---

## 7.3.13 Positions Per-Exchange Day P&L Overlay (Aug 2026)

**File**: `backend/api/routes/positions.py` — `_overlay_snapshot_for_closed_exchanges()`

Positions now patch `day_change_val`, `day_change_percentage`, and `close_price`
for closed-exchange rows immediately upon their exchange closure, without waiting
for all markets to close.

**Mechanism**: After fetching live positions (via broker), `_overlay_snapshot_for_closed_exchanges()`
queries `_fetch_ref_close_map()` to fetch prior-session close prices from daily_book
(cutoff: `captured_at < today_08:00 IST`). For each row:

- **If exchange is open now**: Use broker values as-is (live LTP, live day P&L)
- **If exchange is closed now** (e.g., NFO closed at 15:30): Recalculate
  `day_change_val = (broker_ltp - ref_close) × qty`
  `day_change_percentage = (day_change_val / (ref_close × qty)) × 100`
  `close_price = ref_close`

**Impact**: NFO/BSE positions show correct day P&L immediately after their close
(~15:30 IST for equity derivatives), without waiting for MCX to close at 23:30 IST.
MCX rows remain unaffected and continue receiving live broker calculations until
MCX session closes.

**Example (Aug 2026)**: At 15:35 IST (2 minutes after NSE/NFO close):
- Live positions fetch shows NFO rows with stale day P&L (broker hasn't updated settlement prices)
- Overlay patches those rows from prior-session daily_book snapshot
- NavStrip displays correct overnight move immediately, not 8 hours later

---

## 7.4 Snapshot Quantity Read Path — No Multiplier (Aug 2026)

**File**: `backend/api/routes/positions_helpers.py` — `build_row_from_snapshot_raw()`

When reading a position from a `daily_book` snapshot (closed-hours read path), 
`daily_book.qty` contains CONTRACTS (not lots), written by `_positions_qty_fields` at 
snapshot time. The read seam must NOT apply any multiplier.

**Prior bug**: MCX positions read from closed-hours snapshots incorrectly applied the 
lot_size multiplier a second time, resulting in `qty = contracts × lot_size²`. Example: 
3 lots CRUDEOIL stored as 300 contracts → read as 300 × 100 = 30,000. This corrupted 
NavStrip slots 1 and 3 during closed hours (when the snapshot path is used instead of 
live broker fetch).

**Fix (Aug 2026, commit cef00739)**: `build_row_from_snapshot_raw()` now applies 
no multiplier and uses the qty directly: `effective_qty = qty or 0`. The quantity is 
already in contracts; no further scaling is needed.

**Related guard**: `extract_snapshot_multiplier()` is now deprecated and always returns 1 
(kept for import compatibility). This documents the invariant and prevents accidental 
re-introduction of the multiplier.

---

## 8. Adapter Implementations

### Base Broker
- **`translate_qty(exchange, raw_qty, lot_size)` SSOT** (Aug 2026): Canonical qty conversion
  moved to `backend/brokers/base.py`. Decorated with `@exchange_qty_convention`, which applies
  MCX/NCO contracts→lots rule before adapter body runs. All adapters (Kite, Dhan, Groww)
  inherit this rule; no duplication. Guard: raises `ValueError` on `lot_size≤1` (instruments
  cache miss). Adapters may override for broker-specific adjustments, but the MCX rule is
  unified at base layer. Base implementation (no-op after decorator) suitable for any broker
  that needs no further adjustment.

### KiteBroker
- `translate_qty` inherited from base; no adapter-specific overrides
- Every GTT leg AND wing MUST call `translate_qty` before `place_gtt()` — `place_gtt` does NOT auto-translate (incident 2026-07-02)
- `place_order(qty, ...)` has a 50-lot adapter ceiling; bypassed for `intent="close"`
- `_truncate_tag(kwargs)` — defensive 20-char tag truncation before every `place_order`

### DhanBroker
- `translate_qty` inherited from base; MCX/NCO contracts→lots conversion now unified (Aug 2026),
  previously individual adapter implementations. Groww also participates.
- Instruments CSV from `images.dhan.co` once per IST day; F&O symbol: Dhan format → Kite format
- **429 → BrokerRateLimitError** (Jul 2026): `_DhanSDKProxy` checks `resp.get("code") == "DH-904"`
  and raises `BrokerRateLimitError` instead of returning the dict as-is. Allows PriceBroker
  failover and registry retry-cooloff to activate correctly.
- **5xx → BrokerNetworkError** (Jul 2026): `_DhanSDKProxy` raises `BrokerNetworkError` on HTTP
  502/503/504 responses, enabling transient retry logic upstream.
- **GTT operations rate limiting** (Aug 2026): GTT mutations (`place_forever`, `modify_forever`,
  `cancel_forever`, `get_forever`) now use the `"orders"` rate-limit bucket (`_sdk_orders`)
  instead of the default no-bucket category (`_sdk`). This aligns GTT operations with
  regular order placement under the same rate-limit ceiling (20 orders/s).
- **Request stagger to avoid 429s** (Aug 2026): `_DhanSDKProxy` stagger delay (50ms per request)
  built into SDK call retry logic to avoid triggering Dhan's 429 rate-limit during burst periods.
- **Order type UNKNOWN_CAPS fallback** (Aug 2026): `_dhan_normalise_one_order()` now has a
  fallback for unrecognised `order_type` field values: when the `orderType` field from Dhan's
  response is unrecognised, the adapter returns `"UNKNOWN_CAPS"` string instead of raising.
  Allows order-book grids to display unknown order types gracefully rather than crashing.
- `historical_data()` returns `[]` by design — excluded from `get_historical_brokers()`
- `place_gtt()` raises `NotImplementedError` for MCX/NCO

### GrowwBroker
- `translate_qty` inherited from base; MCX/NCO contracts→lots conversion now applied (Aug 2026).
  Previously Groww was a noop for MCX, sending raw contract qty and causing potential oversize orders.
- `_retry_groww_auth` wraps every SDK call: `401/403` → re-mint + retry once; `429` → exponential backoff (1→2→4→8s, cap 30s, 3 retries); `504` → refresh session + retry; `400/404` → re-raise immediately
- `instruments()` uses per-account `@ssot_fetch` key (`groww_instruments_{account}`) to prevent cache collision when multiple Groww accounts are active simultaneously
- Entitlement counter in `GET /api/admin/broker-health extra` field

---

## 8.1. Order Placement Guards & Intent Bypass

**Close intent semantics**: When `intent="close"` is passed through the order flow:
- **G2 fat-finger cap** (5-lot max per trade) — bypassed for close
- **MCX 20-lot cap** — bypassed for close
- **Kite adapter 50-lot ceiling** — bypassed for close

Close orders may exceed all lot caps without triggering validation errors. Non-close orders remain subject to all guards.

**Preflight endpoint**: `POST /api/orders/preflight` now parses and forwards `intent` to guard evaluation. Previously ignored intent, causing G2 to fire on close orders > 5 lots. Preflight now correctly models close semantics and returns margin/segment checks with proper guard bypass.

**Basket LIVE safety checks**: Basket order dispatch now runs per-leg guards before placement:
- **Market-hours gate**: Leg skipped if exchange closed, unless `variety=amo` (after-market order exemption)
- **MCX 20-lot cap**: Per-leg check, bypassed for `intent="close"`
- **Preflight**: Margin and segment validation per leg

Previously, basket placement lacked these guards and sent all legs unconditionally.

## 8.3. GTT Template Attachment System Enhancements (Jul 2026)

**Template attach blocked on close/offset orders** (commit 0a456e9f): Template `template_id` is now cleared at three layers when an order would reduce/close an existing position:

1. **Ticket submit layer** — `_is_offsetting_position()` in `orders_place.py` (async helper):
   - Checks broker position book (30s TTL cache) for the symbol/exchange/account
   - BUY against net quantity < 0 → closing a SHORT → True
   - SELL against net quantity > 0 → closing a LONG → True
   - Fails open (returns False) on any exception — does not block the order on position-fetch failure
   - When close intent is detected: `template_id` is cleared via `msgspec.structs.replace(data, template_id=None)` before AlgoOrder persistence
   - Also guards explicit `intent="close"` flow

2. **Reconcile layer** — `_opl_reconcile_attach_eligible()` in `orders_place.py`:
   - Returns False when `row.intent == "close"` (stored on AlgoOrder)
   - Also checks `row.is_close_intent` flag for legacy compatibility
   - Prevents reconcile sweep from firing template attach on close orders

3. **Postback layer** — two-part guard in `orders_postback.py`:
   - `_pb_wants_template_attach()` returns False when `row.intent == "close"` or `row.is_close_intent == True`
   - `_pb_check_and_fire_template_attach()` runs async position-offset check (`_is_offsetting_position`) before dispatching the attach
   - Ensures postback fan-out respects both explicit close intent and detected close via position state

4. **Frontend layer** — `OrderTicket.svelte`:
   - When `action === 'close'`: TemplateBar is hidden and `templateId` is cleared in the form state
   - Prevents operators from accidentally attaching templates to close orders

**Rationale**: When an order is closing/reducing an existing position, attaching exit GTTs (TP/SL) to that order is semantically incorrect — the order itself IS the exit. Attaching exits to a close order causes the broker to place nested stops on a hedge leg or second-leg position that may not exist.

**Partial fill guard** (#2): Template attach fires only when a parent order is FULLY filled (`filled_qty >= qty`). Partial fills are logged but do not trigger GTT placement, ensuring the remaining open qty is left without premature stops.

**Sub-lot scale rounding** (#3): Scale-out GTT quantities are rounded up to the nearest lot multiple so no sub-lot GTT leg reaches the broker. The last entry is trimmed if total exceeds parent qty. Qty lost to rounding is noted in `plan.notes`.

**GTT LIMIT TP slippage offset** (#1): When `tp_order_type=LIMIT`, a tick offset is applied to the LIMIT price to improve fill probability:
- **NFO/BFO/CDS options**: `template.tp_limit_tick_offset_nfo` (default 0.05) — 5 paise per contract
- **Futures / other exchanges**: `template.tp_limit_tick_offset_default` (default 0.5) — 50 paise per contract

For BUY parents (exit is SELL): LIMIT set below trigger.  
For SELL parents (exit is BUY): LIMIT set above trigger.  
SL legs always remain LIMIT at trigger with no offset.

**Wing feasibility flag** (#10): Preview endpoint returns `wing_feasible=False` in `TicketPreviewResponse` when a wing template is required but no liquid strike was found (chain empty, all OI below threshold, quote failure). Operator sees this before submit so they can adjust settings or skip the wing.

**Wing failure alerting**: When wing scan fails (hard-reject, chain miss, quote error), `wing_skipped_reason` is set on `AttachResult` and an ntfy alert fires immediately with the skip reason. Operator receives Telegram ping ≤30s so they can decide whether to arm exits manually.

**Postback idempotency lock** (TOCTOU fix): Template-attach idempotency now re-fetches the row INSIDE the critical section (per-parent-order async lock) instead of once before acquiring the lock. Prevents a race where the postback handler and reconcile path both pass the `attached_gtts_json is None` check simultaneously and double-place GTTs.

**GTT trigger validation** (#9, #29): Preview endpoint validates trigger direction vs parent side and circuit band:
- BUY parent TP trigger must be above fill price; SL must be below.
- SELL parent TP trigger must be below fill price; SL must be above.
- Trigger must be > 0.
- LTP sanity: trigger must not deviate >50% from LTP when known (catches gross misconfiguration).

Returns `gtt_trigger_errors` in `TicketPreviewResponse` (422 on submit if present).

**Kite MARKET GTT rejection** (#6): Orders requesting `tp_order_type=MARKET` for GTT now raise `BrokerCapabilityError` (added to error hierarchy) instead of silently coercing to LIMIT. Operator sees the error at preview time.

**Full fill detection** (#2): AttachResult carries `wing_skipped_reason` field (set when wing scan returns no candidate). Consumed by API response + alert channel so operator knows WHY the wing wasn't attached (hard-reject, no candidates, OI too low, etc.).

## 8.3.1 GTT Pre-flight Lot-Size Validation (Aug 2026)

**File**: `backend/api/routes/template_attach.py` — `apply_plan_live()`

**G1 lot-size check in `apply_plan_live`**: A synchronous G1 guard now fires at the TOP of 
`apply_plan_live` (before `broker.translate_qty`, plan resolution, or any broker call) to 
catch misconfigured template plans immediately:

- **Check scope**: Every GTT leg qty + wing leg qty verified against `plan.parent_lot_size`
- **Condition**: G1 fires when raw qty is not a multiple of lot_size (e.g., qty=15 for MCX 
  where lot_size=100)
- **Return on failure**: `AttachResult.errors` populated immediately; function returns without 
  calling the broker

This gate prevents misconfigured GTT templates from reaching the exchange. The per-lot cap 
(50-lot adapter ceiling in `kite.py:place_order`) provides last-line defence; this pre-flight 
check stops obviously broken plans early with a diagnostic message.

## 8.2. GTT Exchange Validation & MCX Broker Restrictions

**`validate_gtt_exchange(exchange)` method** (commit b8b1214c): New method on the `Broker` base class (`backend/brokers/base.py`, line 222). Default is a no-op (all exchanges allowed). Called at the top of `apply_plan_live` in `template_attach.py` before lot-size resolution, plan resolution, or any broker call.

| Broker | Supported | Unsupported | Raises |
|---|---|---|---|
| Kite | All | — | No (no-op default) |
| Dhan | NSE, BSE, NFO, BFO, CDS | MCX, NCO | `ValueError` |
| Groww | NSE, BSE, NFO, BFO, CDS, NCO | MCX | `ValueError` |

**Groww NCO support** (Aug 2026): Groww adapter maps `"NCO"` (National Commodity Options) in 
`_EXCHANGE_TO_GROWW` → `"MCX"` and `_SEGMENT_TO_GROWW` → `"COMMODITY"`, placing it in the 
same tier as MCX. However, Groww GTT does not support NCO — `_GROWW_GTT_UNSUPPORTED` includes 
both MCX and NCO.

**Dhan GTT NFO/BFO qty ceiling** (Aug 2026): `dhan.py:place_gtt` enforces a 50,000-contract 
qty ceiling for NFO and BFO legs (constant `_DHAN_GTT_MAX_QTY = 50_000`). Raises `ValueError` 
on breach to catch untranslated lots-vs-contracts bugs before they reach the Dhan API.

**MCX/Dhan fail-fast in `apply_template_to_order`** (commit b8b1214c): After resolving `caps = capabilities_for(account)`, if `caps.gtt_supports_mcx=False` and `parent_exchange` is MCX or NCO, the function returns an `AttachResult` with errors immediately — before lot-size resolution, plan resolution, or any broker call. Fires `_fire_attach_fail_alert`. `guard_alert_fired=True` suppresses the duplicate alert at the bottom of the function.

**Off-hours GTT note** (commit b8b1214c): When a GTT-only template (no wing) is attached while the exchange is closed, `AttachResult.plan.notes` now includes: "GTT registered off-hours ({exchange} closed) — will activate at next session open". Only applies when no wing leg exists (wing MARKET legs require open hours). See `apply_template_to_order` line 2026–2030.

## 8.4. Broker Postback Fill-Status Mapping

**File**: `backend/api/routes/orders_postback.py` — `_BROKER_FILLED_STATUSES`

Per-broker mapping of fill-completion status tokens used to gate template-attach fan-out (#13):

| Broker | Fill token | Notes |
|---|---|---|
| Zerodha Kite | `COMPLETE` | Canonical fill status via postback webhook |
| Dhan | `TRADED` | Returned by postback webhook; mapped to FILLED |
| Groww | `COMPLETE` | Via postback webhook |
| (default) | `COMPLETE` | Fallback for unknown brokers |

Template attach only fires when `_broker_is_fill_status(broker_id, status)` returns True. Non-fill terminal statuses (CANCELLED, REJECTED, EXPIRED) are blocked even if routed through `_BROKER_STATUS_MAP` to FILLED. This prevents attaching GTT exits on non-fill events.

---

## 8.5 Orders Fetching Resilience & Chase Timeouts

**File**: `backend/api/routes/orders_helpers.py` · `backend/api/routes/orders.py`

### Orders list with per-broker timeout

`_fetch_orders()` in `orders_helpers.py` fetches `broker.orders()` from all active 
broker accounts in parallel using `ThreadPoolExecutor`. Each broker call is wrapped 
in an explicit future with a **8-second timeout** (`_BROKER_ORDERS_TIMEOUT = 8`):

```python
_BROKER_ORDERS_TIMEOUT = 8

for fut, account in futs:
    try:
        results.append(fut.result(timeout=_BROKER_ORDERS_TIMEOUT))
    except concurrent.futures.TimeoutError:
        logger.warning(f"orders list timed out for {account} after {_BROKER_ORDERS_TIMEOUT}s")
        results.append([])
```

A timed-out broker:
- Logs a warning (account ID + timeout threshold)
- Contributes an empty list `[]` to the response
- Does NOT block other brokers' results
- Does NOT raise or return a 500 error

The pool is shut down with `cancel_futures=True` to prevent stale futures from 
re-blocking after the timeout expires.

**Impact**: GET `/api/orders/` (used by orders panel + chase reconcile) never blocks 
on a single slow/hung broker account. If Dhan hangs, Kite/Groww orders appear 
immediately; Dhan row is omitted from the list.

### Chase active endpoint with 10-second snapshot timeout

`/api/chases/active` (list in-flight chase orders) calls `_chase_snapshot_broker_status_by_id()` 
to snapshot the broker order book. This function wraps the orders cache fetch in 
`asyncio.wait_for(timeout=10.0)`:

```python
async def _chase_snapshot_broker_status_by_id() -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        _ord_resp = await asyncio.wait_for(
            get_or_fetch("orders", _fetch_orders, ttl_seconds=_ORDERS_TTL),
            timeout=10.0,
        )
        for _o in (_ord_resp.rows or []):
            out[_bid] = {"status": <UPPER>, "average_price": <float>}
    except asyncio.TimeoutError:
        logger.warning("chases/active broker snapshot timed out after 10s")
    except Exception as _oe:
        logger.debug(f"chases/active broker snapshot failed: {_oe}")
    return out
```

On timeout:
- Returns an empty `{}` immediately (no hanging)
- Logs a warning (no exception raised)
- Chase reconcile treats missing order IDs as "keep row as OPEN"
- Prevents `/chases/active` panel from lock-starving when broker order fetch hangs

**Impact**: Chase panel never blocks the operator even if the broker order list fetch 
exceeds 10 seconds. Operator sees in-flight orders (cached state) rather than a blank 
grid or spinner lock. The next poll (3s default) attempts fresh data.

### Frontend polling guard

**File**: `frontend/src/lib/order/ChaseCard.svelte`

Chase card now includes an in-flight polling guard:

```javascript
let _fetching = false;

async function _poll() {
  if (_fetching) return;  // Silently drop concurrent polls
  _fetching = true;
  try {
    // fetch active chases
  } finally {
    _fetching = false;
  }
}
```

The recurring `visibleInterval` callback (default 3s) now calls `_poll()` instead of 
calling `_load()` directly. When a poll is in-flight, concurrent `visibleInterval` 
ticks are silently dropped. This prevents request starvation when the browser is 
polling faster than the API responds (e.g., `fetch timeout > visibleInterval`).

## 8.6 Order Pairing — Parent-Child Relationship Linking

**File**: `backend/api/routes/orders.py` — `POST /api/orders/pair`

Establishes a parent-child relationship between two AlgoOrder rows, enabling operators 
to track which orders are related (e.g., entry + exit legs, multi-leg strategies).

**Endpoint**: `POST /api/orders/pair`

**Request body**:
```json
{
  "parent_id": "<UUID>",
  "child_id": "<UUID>"
}
```

**Validation**:
- Both `parent_id` and `child_id` must reference existing AlgoOrders (404 if not found)
- `child_id` must not have an existing parent (`parent_order_id` must be None; 400 if occupied)
- `parent_id` ≠ `child_id` (400 if same order used twice)

**Effect**:
- Updates `AlgoOrder.parent_order_id` on child row to `parent.id`
- Child order is now considered "linked" to the parent strategy

**Response**: 200 OK with updated child AlgoOrder record, or 4xx on validation failure

**PositionRow schema impact** (Aug 2026):
- New field `pair_group_key: str | None` set to `parent.id` when this position is linked to an 
  open AlgoOrder (status=OPEN) with `parent_order_id=parent.id`. Null when no matching order exists
- New field `is_orphan: bool` is True when position (account, tradingsymbol) has no matching 
  open AlgoOrder

**UI access** (`OrderPairModal.svelte`):
- Accessible from MarketPulse positions grid header and Derivatives legs header
- Fetches `/api/orders/recent` to populate parent + unlinked-child pickers
- On submit, calls `POST /api/orders/pair` to establish relationship

**Frontend side effects**:
- MarketPulse position rows show coral "O" badge when `is_orphan=true`
- `postSortRows` callback keeps positions with matching `pair_group_key` adjacent in grid (parent row 
  immediately followed by child rows)
- ChaseCard shows "O" chip for dangling child orders (parent not in active chase list)

---

## 9. Remote Broker & Conn Service

`RemoteBroker` proxies every `Broker` ABC method via `POST /internal/broker/{account}/call/{method}` over UDS.

**Typed error mapping** (Jul 2026): `RemoteBroker._call()` no longer raises raw `RuntimeError`.
Instead, it:
- Maps `error_type` string in response payload to domain exceptions:
  - `"auth_error"` → `BrokerAuthError`
  - `"network_error"` → `BrokerNetworkError`
  - `"rate_limit_error"` → `BrokerRateLimitError`
  - `"error"` (default) → `BrokerError`
- Transport failures (socket errors, timeouts) → `BrokerNetworkError`

This enables circuit breaker and PriceBroker failover logic to correctly handle remote errors.

`_ALLOWED_BROKER_METHODS` whitelist (28 methods) — unknown method → 403.

**`api_secret` invariant**: Never leaves conn_service. Main API calls `POST /internal/broker/{account}/verify_postback`; only True/False returned.

**NavBreakdown account display** (Jul 2026): `frontend/src/lib/NavBreakdown.svelte` now 
includes `connStatus.accounts` (all registered broker accounts) in the account union when 
building the holdings popup. Previously only accounts with active positions/holdings 
appeared; now all accounts are visible even if idle or flat.

Key endpoints: `/health`, `/internal/accounts`, `/internal/broker/{account}/call/{method}`, `/internal/rebuild`, `/internal/broker/{account}/verify_postback`

---

## 9.1 Background Task Supervisor

**File**: `backend/brokers/service/routes.py` — `_supervised(coro_fn, *, name, restart_delay=60)`

All long-running broker tasks (`_task_fetch_positions`, `_task_fetch_holdings`, etc.) wrap
in `_supervised` to enable resilient restart-on-crash semantics:

- **Crash handling**: If coroutine raises an exception (not `CancelledError`), logs the error
  and reschedules the task after `restart_delay` seconds (default: 60s).
- **Graceful cancellation**: `CancelledError` propagates immediately without retry (app
  shutdown, task cancel).
- **Task naming**: Each wrapped coroutine is tracked by name in logs for operator diagnostics.

This prevents a single failed broker fetch from silencing all subsequent polls, improving
resilience when broker APIs experience transient outages or the conn_service encounters
temporary socket issues.

### Background Task Early-Return Contract

**Critical requirement for all `_task_*` coroutines wrapped in `_supervised()`**:

`_supervised()` restarts any returning coroutine **immediately with zero delay** — it only 
sleeps (`restart_delay=60s`) on exceptions, not on clean returns.

**Consequence of violation**: If a `_task_*` function returns early (before its main `while True:` 
loop) without sleeping, the event loop runs the coroutine at 100% CPU on a tight no-op loop. 
This starves the asyncio event loop, preventing `on_startup()` from completing, and uvicorn 
never binds the port.

**Rule**: Any `_task_*` function that exits before its main `while True:` loop MUST include an 
`await asyncio.sleep(N)` before the `return` to yield control and prevent tight looping.

### Token Refresh Delegation to Conn-Service

**File**: `backend/api/background.py` — `_task_holiday_refresh()` (morning 05:30 task)

When `RAMBOQ_USE_CONN_SERVICE=1` is set, the morning `_task_holiday_refresh()` in the main API 
skips token refresh and logs a WARNING to make the delegation visible to operators:

```
WARNING: _task_holiday_refresh token refresh skipped — RAMBOQ_USE_CONN_SERVICE=1 (conn-service owns token management)
```

**Rationale**: In conn-service mode, all broker session management (including proactive token 
refresh) is owned by the conn-service background tasks. The main API's `_task_holiday_refresh()` 
still runs to load holiday calendars and open times, but token refresh is delegated. With 
conn-service disabled, the combined 05:30 task refreshes Kite/Dhan/Groww tokens proactively 
on the main process.

**Canonical examples in the codebase**:
- `_task_ticker_watchdog`: cutover mode exit → `while is_cutover_on(): await asyncio.sleep(300); return`
- `_task_warm_backfill`: already-fired guard → `await asyncio.sleep(86400); return`
- `_task_warm_backfill`: empty symbol universe → `await asyncio.sleep(3600); return`
- `_task_expiry_check`: non-prod branch → `while not is_prod_branch(): await asyncio.sleep(300); return`

### Expiry Task Restart-Blindness Fix (commit cbbe0f23)

**Module-level sentinel**: `backend/api/background.py:_expiry_last_run_date` tracks whether 
expiry engine has run today. On service restart, `_task_expiry_check` now fires immediately 
if current time > 09:20 IST and sentinel is None (hasn't run today), then sets sentinel after 
successful engine completion. Prevents expiry-close loop from being skipped on restart after 
market open.

**Expiry engine re-scan loop** (commit cbbe0f23): `ExpiryEngine.run()` in `backend/api/algo/expiry.py` 
now loops after initial `_run_nfo_close()` — sleeps `_rescan_min` (default 30) minutes between scans, 
runs until 15:25 IST. Picks up newly-ITM NFO positions intraday (when position wasn't ITM at close 
but ticks ITM during day).

**NSE NIFTY quote key fix**: `_fetch_underlying_ltps()` now uses `NSE:NIFTY 50` (not `NSE:NIFTY`) 
for Kite quote API calls to match actual tradable symbol.

### Market-Data Spot-Price Auto-Subscription (Aug 2026)

**File**: `backend/api/background.py` — `_perf_subscribe_book_symbols()`

The background payoff performance subscription task now auto-subscribes to anchor contract 
LTPs to ensure real-time spot pricing for derivatives positions:

- **MCX options**: Auto-subscribes MCX front-month futures (via `resolve_symbol('CRUDEOIL', 'MCX')`) 
  for all MCX option positions. Ensures payoff chart spot price gets live KiteTicker ticks instead 
  of 30-second REST polls.
  
- **NFO/BFO equity underlyings**: Auto-subscribes NSE/BSE spot contracts (NIFTY, BANKNIFTY, 
  stock names, etc.) for all NSE option positions. Ensures option payoff derives spot from 
  live SSE feed.

**Rationale**: Options payoff curves depend critically on spot price. Without live subscription, 
the chart recomputes spot from broker quote REST polls (30s cadence), causing stale payoff 
curves when spot moves rapidly. Auto-subscription ensures spot is always current, improving 
payoff accuracy during volatile intraday windows.

---

## 9.2 Morning Token Refresh — Consolidated 05:30 Task

**File**: `backend/api/background.py` — `_task_holiday_refresh()`

All morning broker token management is now consolidated into a single 05:30 IST task
that handles holiday calendar loading, open-time resolution, and best-effort token
refresh for all brokers in sequence.

### Token refresh during 05:30 task

| Broker | Refresh Method | Behavior |
|---|---|---|
| Kite | TOTP auto-login | `get_kite_conn(test_conn=False)` during 05:30 window before 06:00 hard expiry |
| Dhan | RenewToken API | Lightweight token renewal attempt; falls back to full TOTP if renewal fails |
| Groww | Session refresh | Proactive `get_groww_conn()` refresh if `_is_token_expired()` returns True |

All three brokers attempt refresh during the single 05:30 IST combined task. Token refresh
failures are logged as warnings only — they do NOT block subsequent API calls. The
`@retry_kite_conn` decorator provides automatic recovery on the first failed fetch after
token expiry (within 2–30s depending on broker).

**Rationale**: Merging separate 04:00 holiday + 05:45 Kite token tasks into a single
05:30 call reduces task overhead and ensures both operations complete before the 06:00
hard expiry and market open. Best-effort design avoids blocking market-open latency
on failed preemptive refresh.

**Skipped under conn-service mode**: When `RAMBOQ_USE_CONN_SERVICE=1`, token management
is delegated entirely to the conn-service, and the main API task logs a WARNING and exits
early.

### On-demand token renewal on auth failure (safety net)

**File**: `backend/shared/helpers/decorators.py` — `@for_all_accounts._per_account._try_renew`

**Auth error signal detection**: `backend/shared/helpers/auth_error.py` — `is_auth_error_str(err)`

Zero-dependency helper module that detects 401/403/token-expiry signals in error strings
without importing the broker layer. Used by both the decorator and broker_apis layer.

When any broker fetch raises an exception, `_per_account` (inside `@for_all_accounts`) checks
if the error string matches auth-failure signals via `is_auth_error_str()`. If True:

1. `_try_renew(account, connections)` dispatches to the connection's re-auth method (duck-typed):
   - `KiteConnection` → `get_kite_conn(test_conn=True)`
   - `DhanConnection` → `get_dhan_conn(test_conn=True)` (then rebuild broker via `get_broker`)
   - `GrowwConnection` → `conn.refresh()` (then rebuild broker via `get_broker`)
2. If renewal succeeds, the original function is retried once with fresh handles
3. If the retry also fails, the exception is re-raised and propagates to the caller

Logs `[TOKEN-RENEW] {account}: auth error — renewing token` at INFO (per-broker branch).
Renewal failures log warnings but do not block the original exception from propagating.

This is a safety net independent of the proactive prewarm schedule — if a token expires
between prewarm cycles for any reason, the first failed fetch self-heals without operator
intervention.

---

## 9.3 Instruments & Options Endpoints — MCX Name Normalization (Aug 2026)

**Files**: `backend/api/routes/instruments.py` · `backend/api/routes/options.py`

Kite's MCX commodity instruments carry tradingsymbols with intraday markers
(e.g., "CRUDEOIL", "CRUDEOILM", "NATURALGAS") and legacy spaced names in the
`name` field (e.g., "CRUDE OIL", "CRUDE OIL M"). Two endpoints now normalize
MCX underlyings by tradingsymbol-prefix extraction, matching the frontend's
virtual root derivation.

### Instruments expiry index (`_build_expiries_index`)

**File**: `backend/api/routes/instruments.py` — line 201

When building the expiry index for options chain-quotes lookups, MCX underlying
keys are derived by stripping trailing digits from tradingsymbol via:

```python
raw_key = re.sub(r'\d.*', '', inst.s).upper()  # "CRUDEOILM" → "CRUDEOIL"
_exp_index[raw_key].add(inst.x)
```

A diagnostic log line `[expiries-index] normalized MCX spaced names: ...` is
emitted per instruments reload. This ensures the cache key matches the
prefix form the frontend sends in
`GET /api/options/chain-quotes?und=CRUDEOIL&exp=...` requests.

### Chain-quotes symbol map (`_chain_quotes_build_sym_map`)

**File**: `backend/api/routes/options.py` — line 2172

When scanning the instrument response for matching CE/PE contracts, MCX
underlyings use the same prefix-extraction before comparison:

```python
raw_key = re.sub(r'\d.*', '', inst.s).upper()  # "CRUDEOILM" → "CRUDEOIL"
if raw_key != und:
    continue
```

This ensures consistent key derivation across both index and chain-quotes paths.

### Fast-path guard in `chain_quotes`

**File**: `backend/api/routes/options.py` — `chain_quotes()` route

Before short-circuiting on the fast-path (cached index), the endpoint now
validates `und in _exp_index`:

```python
if _exp_index is not None and und in _exp_index:
    # fast-path: return cached rows
else:
    # slow-path: scan instrument response
```

Key-miss (underlying not found in index) now falls through to slow-path scan
instead of returning empty silently.

**Impact**: Option chain expiry dropdown and strike-by-strike quotes now work
correctly for MCX commodities (CRUDEOIL, CRUDEOILM, NATURALGAS, GOLD, GOLDM,
etc.) — immune to intraday marker variations — without manual workaround.

---

## 10. Virtual Root Resolution

**File**: `backend/api/algo/symbol_resolver.py`

Virtual symbols (`CRUDEOIL`, `CRUDEOIL_NEXT`, `USDINR`, etc.) are never sent raw to broker adapters. They must be resolved to an actual exchange-traded contract BEFORE any broker call.

### Resolution rules:

| Virtual | Resolves to |
|---|---|
| `CRUDEOIL` | Front-month MCX futures (nearest expiry, expiry > today IST) |
| `CRUDEOIL_NEXT` | Back-month MCX futures (second nearest expiry, expiry > today IST) |
| `USDINR` | Front-month CDS futures |
| `USDINR_NEXT` | Back-month CDS futures |

**Rollover rule**: Contracts where `expiry == today` are EXCLUDED (`expiry > today`, strictly greater). On expiry day, the next contract becomes front-month automatically.

**`_NEXT` edge case**: If only one active contract exists (very near month-end), `_NEXT` falls back gracefully — returns None or the only available contract with a warning. Must not crash.

**Frontend `rootOf` map**: `seedRootMapFromInstruments(instruments)` builds reverse map `contract → bare_root`. Used by `_resolve_sparkline_db_key` for Tier 4 daily_book lookup. Must match backend resolved names exactly.

**Broker layer rule**: Virtual symbols must be resolved by `symbol_resolver.resolve_symbol()` BEFORE passing to `broker.ltp()`, `broker.historical_data()`, or `broker.quote()`. The adapter layer does not know about virtual symbols.

### Resolution SSOT:

| Layer | Function |
|---|---|
| Backend resolve | `symbol_resolver.resolve_symbol(sym, exch)` |
| Backend root | `symbol_resolver.root_of(contract)` |
| Frontend resolve | `resolveVirtual(sym, exch)` in `rootOf.js` |
| Frontend root | `rootOf(contract)` in `rootOf.js` |
| Sparkline key bridge | `_resolve_sparkline_db_key(sym, exch)` in `quote.py` |

---

## 11. Key Invariants

**I1 — Kite-only for historical data**: `ohlcv_store._broker_fetch_sync` and `intraday_store._broker_fetch_sync` MUST use `get_historical_brokers()[0]`. Violation: silent empty bars (incident 2026-07-11).

**I2 — `translate_qty` before every GTT leg (unified base class)**: `apply_plan_live` MUST call `broker.translate_qty(exchange, raw_qty, lot_size)` for every GTT leg AND wing before `broker.place_gtt()` (incident 2026-07-02: 1-lot MCX = 100 lots sent). As of Aug 2026, `translate_qty` is implemented in the base class with `@exchange_qty_convention` decorator handling MCX/NCO contracts→lots conversion; all adapters (Kite, Dhan, Groww) inherit this unified rule instead of duplicating it. RemoteBroker must still override to delegate via `_call()` to the conn-service.

**I3 — Token cache atomicity**: `tempfile + os.replace()` under `fcntl.flock(LOCK_EX)`. No direct JSON writes.

**I4 — `api_secret` containment**: Never leaves conn_service. HMAC computed inside; only bool returned.

**I5 — Circuit breaker opt-in**: `circuit_breaker_enabled=True` required per account. Never enable globally.

**I6 — Torn-read retry**: TickBufferReader checks version word before/after; retry on mismatch. Do not remove.

**I7 — Per-request market-data broker coherence**: All quote/ltp/instruments in one asyncio Task use same `PriceBroker` instance (ContextVar). Reset at request boundary via `reset_market_data_broker_ctx()`.

**I8 — Virtual symbols resolved before broker call**: `resolve_symbol()` called before any `broker.ltp()` / `broker.historical_data()` / `broker.quote()`. Adapters do not handle virtual symbols.

**I9 — DB-first for sparklines**: `daily_book kind='sparkline'` (Tier 4) checked BEFORE broker fallback. Yesterday's snapshot is valid sparkline data.

**I10 — Close intent bypasses ALL lot caps**: Single-leg and basket orders with `intent="close"` bypass G2 (5-lot FAT_FINGER), MCX 20-lot cap, and Kite 50-lot adapter ceiling. Non-close orders remain subject to all guards.

**I11 — Preflight honours intent**: `POST /api/orders/preflight` parses `intent` parameter and applies guard bypass consistently with order placement. Previously ignored intent.

**I12 — Basket per-leg guards**: Basket LIVE dispatch validates each leg independently: market-hours gate (skip if closed unless `variety=amo`), MCX 20-lot cap (bypass for close), preflight (margin/segment checks). No leg placement without passing its guards.

**I13 — RemoteBroker translate_qty delegation**: Any RemoteBroker proxy must override `translate_qty` to delegate via `_call`; the base-class no-op is unsafe for MCX/NCO contracts and sends raw contract qty to the adapter.

**I14 — Closed-hours snapshot query combines latest + prior-session CTEs and filters qty by date**: `_positions_snapshot()` in `backend/api/routes/positions.py` uses a single SQL query with `latest_batch` (today's most recent capture per account) and `prev_batch` CTEs (prior-session's most recent row per account/symbol). The `prev_batch` window is anchored on `captured_at < max_at AND captured_at >= max_at - INTERVAL '2 days'` to survive UTC/IST date-column edge cases; `prev_close_val` prefers yesterday's `prev_ltp` (from daily_book) FIRST, falling back to snapshot's `previous_close` only when `prev_ltp` is absent/zero. Rationale: after MCX closes at 23:30 IST the broker sets `previous_close = today_settlement`, which would collapse Day P&L to 0. Using yesterday's LTP preserves the correct close price through the closed window. Off-hours query uses refined filter `AND (db.qty != 0 OR db.date = :today_ist)` (commits cef00739, 5ac11f56): positions closed intraday (qty=0, captured today IST) appear in the snapshot with 'closed' chip + opacity decoration in derivatives legs grid; next morning before market opens, yesterday's closed legs are absent (db.date != today_ist), only carried-overnight open positions show, matching broker book when gate opens.

**I15 — Template attach only on full fill**: Template attach fires when `filled_qty >= qty` (parent order fully filled). Partial fills are logged but do NOT trigger GTT placement. The remaining open qty must be left without premature stops.

**I16 — GTT trigger direction vs parent side**: GTT trigger validation confirms BUY TP > fill, SELL TP < fill, BUY SL < fill, SELL SL > fill. Triggers must be strictly positive. Circuit band check: trigger must not deviate >50% from LTP when known. Validation fires at preview (422) and logs CRITICAL at apply-at-fill if violated.

**I17 — Per-broker fill-status token resolution**: Template attach gate checks broker-specific fill tokens via `_broker_is_fill_status(broker_id, status)`. Kite: COMPLETE, Dhan: TRADED, Groww: COMPLETE. Non-fill terminal statuses (CANCELLED, REJECTED, EXPIRED) block attach even if routed through `_BROKER_STATUS_MAP` to FILLED.

**I18 — Postback attach TOCTOU protection**: Idempotency check re-fetches `attached_gtts_json` INSIDE the per-parent-order async lock (`_get_template_attach_lock`). Prevents postback handler + reconcile path from both passing the `is None` check simultaneously and double-placing GTTs.

**I19 — LIMIT TP slippage offset per exchange**: LIMIT TP legs apply exchange-specific tick offsets to improve fill probability. NFO/BFO/CDS: 0.05 (default). Futures/others: 0.5 (default). Config keys: `template.tp_limit_tick_offset_nfo`, `template.tp_limit_tick_offset_default`. SL legs always remain at trigger with no offset.

**I20 — Scale-out rounding to lot multiple**: Scale-out GTT qtys rounded UP to nearest lot multiple; last entry trimmed to cap total at parent_qty. Qty lost to rounding is noted in `plan.notes`. Ensures no sub-lot GTT leg reaches the broker.

**I21 — Wing feasibility in preview**: `TicketPreviewResponse` includes `wing_feasible=False` when wing template required but no liquid strike found. Operator can adjust settings or skip wing before submit. Prevents silent wing-skip surprises at fill time.

**I22 — Template attach blocked on close/offset orders**: When an order would close or reduce an existing position, `template_id` is cleared at three layers: (1) ticket submit via `_is_offsetting_position()` position-book check + explicit `intent="close"` guard, (2) reconcile via `_opl_reconcile_attach_eligible()` intent/flag check, (3) postback via `_pb_wants_template_attach()` + `_pb_check_and_fire_template_attach()` async position check. Frontend also hides TemplateBar and clears `templateId` when `action === 'close'`. Ensures exit GTTs are never attached to close orders — a close order IS the exit; attaching stops to it creates nested/phantom positions.

**I23 — Snapshot orphan deletion on broker response**: After each per-account positions UPSERT in `snapshot_daily_book()`, `_delete_orphan_positions()` removes stale `daily_book` rows (kind='positions') whose symbol is absent from the current broker response. Broker response is marked with sentinel: `positions = None` (call failed, skip cleanup) vs `positions = []` or `[...]` (call succeeded, cleanup proceeds). Kite removes settled/squared-off positions from `broker.positions()` after settlement; orphan deletion prevents closed positions from persisting in the UI all night. Cleanup is idempotent and safe to re-run.

**I24 — Holdings day_change_percentage uses previous_close denominator**: Holdings snapshot metric `day_change_percentage` is computed as `(day_pnl / (previous_close × qty)) × 100`, where `previous_close` is fetched from `daily_book`. Fallback to `avg_cost` when `previous_close` is zero/missing (same-day buys, cold-boot). Using LTP as denominator inflates/understates percentage on down/up moves respectively. SSOT: `_build_holding_row_from_snapshot()` in `backend/api/routes/holdings.py`.

**I25 — Dhan `close_price=0` detection and backfill**: Dhan's `holdings()` API lacks `previousClosePrice`; adapter sets `close_price=0` and `day_change=ltp`. Backfill pipeline patches `close_price` from Kite quote, then `_backfill_recompute_derived()` detects `close_was_missing=True` flag and recomputes `day_change = ltp - real_close` unconditionally, overriding the stale broker value. Ensures `daily_book.day_pnl` stores correct day P&L (not `ltp`). Effective when snapshot runs before Kite updates `ohlc.close` to today's final value (~3:30–18:00 IST); canonical EOD snapshot at 15:35 IST guaranteed correct.

**I26 — Daily book `day_pnl` stores total P&L**: The value stored in `daily_book.day_pnl` for `kind='holdings'` rows is the total day P&L (`day_change_per_share × qty`), consistent with positions rows. When computing `day_change_percentage`, this total is divided by `close_notional = previous_close × qty`. Ensures stored value is directly interpretable as P&L if position were squared at LTP.

**I27 — Orders fetch per-broker 8-second timeout**: `_fetch_orders()` wraps each broker's `orders()` call with an explicit 8-second timeout (`_BROKER_ORDERS_TIMEOUT = 8`). A timed-out broker logs a warning and contributes an empty list; other brokers' results are unaffected. GET `/api/orders/` never blocks on a single hung account. Pool is shut down with `cancel_futures=True` to prevent stale-future re-blocking.

**I28 — Chase active 10-second snapshot timeout**: `_chase_snapshot_broker_status_by_id()` wraps the orders cache fetch in `asyncio.wait_for(timeout=10.0)`. On timeout, returns empty dict `{}`; chase reconcile treats missing order IDs as "keep OPEN". Prevents `/chases/active` panel from lock-starving when broker fetch hangs. Next poll (3s default) attempts fresh snapshot.

**I29 — Frontend chase polling guard**: `ChaseCard.svelte` includes in-flight `_fetching` flag. `visibleInterval` callback calls `_poll()` which silently drops concurrent polls while one is in-flight. Prevents request starvation when browser polls faster than API responds (e.g., when fetch timeout > polling interval).

**I30 — Snapshot quantity read path does not apply multiplier** (Aug 2026): `daily_book.qty` is written in CONTRACTS by the snapshot write path (`_positions_qty_fields`); the read seam (`build_row_from_snapshot_raw()`) must NOT apply any multiplier. Prior bug: MCX read applied lot_size multiplier twice, resulting in `qty = contracts × lot_size²` (e.g. 300 contracts × 100 = 30,000). Fix: `effective_qty = qty or 0` (no scaling). This affects closed-hours position grids, NavStrip P slot 1 (quantity), and NavStrip P slot 3 (NAV).

---

## 12. Test Coverage Map

| File | Coverage |
|---|---|
| `test_broker_registry.py` | Registry resolution, adapter dispatch |
| `test_broker_capabilities.py` | Capability matrix, UNKNOWN_CAPS fallback |
| `test_broker_connection_layer.py` | KiteConnection lifecycle, token cache, locks |
| `test_broker_health_under_cutover.py` | Circuit breaker transitions |
| `test_broker_priority.py` | PriceBroker failover, rate-limit cooloff |
| `test_remote_broker.py` | UDS dispatch, error mapping |
| `test_tick_buffer.py` | mmap writer/reader, version bumps, torn-read |
| `test_ticker_failover.py` | Account swap, cooloff logic |
| `test_market_data_broker.py` | Kite-only historical selection |
| `test_virtual_root_endpoints.py` | resolve_symbol, root_of, _NEXT edge cases |

### Gaps:
- `translate_qty` raises on MCX `lot_size=0` (instruments cache miss path)
- PriceBroker soft-failure predicates unit tests
- Token cache cross-process lock under concurrent write (integration)
- conn_service method whitelist 403 enforcement
- `_retry_groww_auth` all four branches
- Mmap torn-read under concurrent write stress
- Virtual root `_NEXT` with only one active contract (edge case)

---

## 13. Known Defects & Risks

### B-D1 — Historical broker excludes rate-limited accounts but still returns them if ALL are limited
**Status**: Acceptable — Tier 4 + self-heal handle empty bars

### B-R1 — DhanBroker instruments CSV not retried on 429
**Status**: Risk / low frequency

### B-R2 — Cross-process flock advisory only (NFS caveat)
**Status**: Non-issue on current VM-local infra

---

## 13.1 Connection Resilience Improvements (Jul 2026 Polish)

**Kite 502/503 retry routing fix**
- `backend/brokers/adapters/kite.py`: `DataException` (HTTP 502/503 gateway errors) now maps 
  to `BrokerNetworkError` instead of `BrokerInputError`. This allows the retry decorator 
  (`@retry_kite_conn`) to correctly re-attempt transient gateway failures.

**Dhan login stagger**
- `backend/brokers/connections.py`: Multiple Dhan accounts now stagger login 2 seconds apart 
  at startup to avoid concurrent auth storms that trigger broker rate-limiting or session conflicts.

**Token TOCTOU fix**
- `backend/brokers/connections.py`: `get_kite_conn()` and `get_dhan_conn()` fast-path returns 
  are now inside `self._login_lock` to prevent stale handle races when two threads request 
  the same broker connection simultaneously during token refresh.

**Dhan cooloff persistence**
- `backend/brokers/broker_apis.py`: `_dhan_next_poll` cooloff entries now persist to 
  `/tmp/ramboq_dhan_cooloff.json` and survive process restarts. Prevents tight-retry loops 
  when ramboq_api restarts during a Dhan rate-limit window.

**Operator impact**: These fixes reduce spurious login failures, retry noise, and intermittent 
connection dropouts in the logs. Circuit breaker should open/close more predictably.

---

## 15. Broker Connection Events Audit Log

**Table**: `broker_connection_events` (shared ramboq DB)

Chronological log of broker connection lifecycle events. Every auth attempt, token
rotation, fetch failure, and circuit-breaker transition is recorded with precise
timestamps for operator forensics and alert tuning.

### Event types

| Type | When | Source |
|---|---|---|
| `auth_fail` | OAuth/TOTP failure or connection rejected | `connections.py` (KiteBroker, DhanBroker) |
| `fetch_fail` | Broker API call raised exception (429, 401, 500, etc) | `broker_apis.py` (for positions/holdings/margins) or adapter |
| `token_ok` | Token refresh succeeded | `connections.py` |
| `rotation_detected` | New token differs from prior token | `connections.py` |
| `fetch_ok_recovery` | First successful fetch after consecutive failures | `broker_apis.py` |
| `circuit_open` | Health state machine → OPEN (skip account) | `broker_apis.py` |
| `circuit_close` | Health state machine → HALF-OPEN (probe) | `broker_apis.py` |
| `ticker_close` | KiteTicker WebSocket closed | `kite_ticker.py` |
| `ticker_error` | KiteTicker on_error callback fired | `kite_ticker.py` |
| `ticker_reconnect` | TickerManager.swap() executed | `kite_ticker.py` |

### Schema

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `account` | VARCHAR(32) | Broker account (e.g. ZG0790) |
| `event_type` | VARCHAR(32) | One of the 10 types above |
| `event_ts` | TIMESTAMP TZ | Event timestamp (UTC, indexed) |
| `detail` | JSONB | Error message, token digest, or context |

Index: `(account, event_ts DESC)` for operator drill-downs.

### API endpoint

**GET /api/admin/health/broker-connection-events** (admin-guarded)

| Param | Default | Purpose |
|---|---|---|
| `account` | (optional) | Filter by account; blank = all |
| `event_type` | (optional) | Filter by event type; blank = all |
| `since` | (optional, ISO 8601) | Start of time range; blank = last 24 hours |
| `limit` | 100 | Max rows returned |

Response: `{events: [{id, account, event_type, event_ts, detail}, ...], total_count}`

### Retention

No automatic purge. Operator may delete rows manually via SQL or via a
retention-tuning setting if added in the future. Current default: indefinite.

### Diagnostic example

```
GET /api/admin/health/broker-connection-events?
  account=ZG0790&
  event_type=auth_fail&
  since=2026-07-15T00:00:00Z&
  limit=50
```

Returns the last 50 auth failures for account ZG0790 since midnight UTC — useful
for debugging recurring 2FA timeouts or credential rotation issues.

---

## 14. Exchange Schedule Table & Clock Module

**Files**: `backend/api/models.py` · `backend/api/helpers/exchange_clock.py` ·
`backend/api/routes/exchange_schedule.py` · `backend/config/backend_config.yaml`

Single source of truth for market segment timing (open/close windows, snapshot
triggers, settlement cutoffs). Replaces three scattered definitions:
`_EXCHANGE_TO_GATE` dict, `market_segments` YAML, and hardcoded times in
`background.py`. Fully editable from `/admin/settings` UI.

### Table: `exchange_schedule`

Persistent configuration rows, one row per (gate, date, session) combination.
Operator-editable defaults (date=NULL) and date-specific overrides (holidays,
special sessions).

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `gate` | VARCHAR(32) NOT NULL | Grouping label: "NSE", "MCX" |
| `exchanges` | TEXT[] NOT NULL | Exchanges in this gate: {"NSE","BSE","NFO","BFO","CDS"} or {"MCX"}. Used for matching; a Muhurat row with exchanges=["NSE","BSE"] closes NFO/BFO |
| `date` | DATE NULL | NULL = recurring default; specific date = override |
| `weekdays` | INT[] NULL | ISO weekdays [1..7]; 1=Mon, 7=Sun; NULL on date-override rows |
| `session_name` | VARCHAR(32) NOT NULL | "regular", "morning", "evening", "muhurat", "settlement", "closed" |
| `is_open` | BOOLEAN NOT NULL | FALSE = market closed for this gate/date/session |
| `open_time` | TIME NULL | IST open time (NULL when is_open=FALSE) |
| `close_time` | TIME NULL | IST close time (NULL when is_open=FALSE) |
| `snapshot_time` | TIME NULL | IST: when daily_book LTP snapshot is captured (set only on LAST session of day) |
| `snapshot_reset_time` | TIME NULL | IST: when prev_close rolls over (cutoff for settlement queries; usually 08:00; NULL on settlement rows) |
| `reason` | VARCHAR(256) NULL | "Independence Day", "Diwali Muhurat 2026" (operator-entered) |
| `source` | VARCHAR(32) NOT NULL DEFAULT 'operator' | "legacy_seed" or "operator" (audit trail) |

Unique constraint: `(gate, date, session_name)`  
Index: `(gate, date)` for fast date-override lookups

### Default seed records (date=NULL, inserted at startup)

| Gate | Exchanges | Weekdays | Session | Is Open | Open | Close | Snapshot | Reset |
|---|---|---|---|---|---|---|---|---|
| NSE | {NSE,BSE,NFO,BFO,CDS} | {1,2,3,4,5} | regular | true | 09:15 | 15:30 | 15:31 | 08:00 |
| NSE | {NSE,BSE,NFO,BFO,CDS} | {1,2,3,4,5} | settlement | false | — | — | 16:15 | — |
| MCX | {MCX} | {1,2,3,4,5} | morning | true | 09:00 | 17:00 | — | — |
| MCX | {MCX} | {1,2,3,4,5} | evening | true | 17:00 | 23:30 | 23:31 | 08:00 |
| MCX | {MCX} | {1,2,3,4,5} | settlement | false | — | — | 00:15 | — |

**Settlement row semantics**: `is_open=false` means trading is closed. These rows
exist solely to carry `snapshot_time` so `background.py` knows when to fire the
final settlement capture (NSE BHAV at 16:15; MCX final at 00:15).
`background.py` distinguishes close-snapshot vs settlement-capture by checking
`session_name == "settlement"`.

### Date-override examples

**Holiday close (one row suppresses ALL sessions for that gate):**

| Gate | Date | Session | Is Open | Reason |
|---|---|---|---|---|
| NSE | 2026-08-15 | closed | false | Independence Day |
| MCX | 2026-08-15 | closed | false | Independence Day |

**Diwali Muhurat (equity open, F&O closed):**

| Gate | Exchanges | Date | Session | Is Open | Open | Close | Snapshot | Reset | Reason |
|---|---|---|---|---|---|---|---|---|---|
| NSE | {NSE,BSE} | 2026-11-01 | muhurat | true | 17:45 | 18:45 | 18:46 | 08:00 | Diwali Muhurat |

Only NSE and BSE are in the `exchanges` array, so NFO/BFO/CDS do not match this
row → `resolve_sessions_for("NFO", 2026-11-01)` returns [] → NFO is correctly
closed for the day without needing separate closed-override rows.

### Runtime lookup algorithm (Aug 2026 — date-override-first)

**Per-exchange session resolution** (`resolve_sessions_for(exchange, on_date)`):
1. Look for date-specific override rows where `exchange = ANY(row.exchanges)` and
   `row.date = on_date` — **these suppress the default row entirely** if found
2. If any override row has `is_open=false` (or `open_time=NULL`), return [] (closed)
3. If any override row has `open_time=HH:MM`, return it (custom session hours)
4. If no date-override rows, fall back to defaults where `exchange = ANY(row.exchanges)`,
   `row.date IS NULL`, and `on_date.isoweekday() IN row.weekdays`
5. If no defaults or weekday not in list, return [] (closed)

**Exchange membership per row** — `is_exchange_open(exchange)` now checks 
per-row `exchanges` list membership. Example: Muhurat override with 
`exchanges=["NSE","BSE"]` correctly leaves NFO/BFO closed for that day without 
needing separate closed-override rows.

**Gate-level session resolution** (`resolve_sessions_for_gate(gate, on_date)`):
1. Look for date-specific override rows where `row.gate = gate` and `row.date = on_date`
   — **override rows suppress defaults** if found
2. If found, return all matching rows (open AND settlement rows)
3. Otherwise, fall back to defaults where `row.gate = gate`, `row.date IS NULL`,
   and weekday check
4. Used by `background.py` only (snapshot/settlement triggers fire per gate)

**Settlement cutoff per gate** — `settlement_cutoff_for(gate)` reads the default 
(non-override) row's `open_time` field (typically 08:00 IST) as the reset boundary 
for prior-close lookups, instead of the hardcoded `snapshot_reset_time`.

**Exchange-to-gate mapping** (internal constant):
```
NSE, BSE, NFO, BFO, CDS  →  "NSE"
MCX                       →  "MCX"
```

### Public API: `backend/api/helpers/exchange_clock.py`

Module-level async-loaded cache (1-hour TTL). Sync methods safe after warm.

| Function | Returns | Purpose |
|---|---|---|
| `is_exchange_open(exchange, *, at=None)` | bool | True if `exchange` is inside an active session at `at` (default: now IST). Uses per-exchange lookup. |
| `is_exchange_closed(exchange, *, at=None)` | bool | `not is_exchange_open(...)` |
| `snapshot_time_for(exchange, *, on=None)` | time\|None | IST close-snapshot time for exchange on date (open sessions only) |
| `snapshot_reset_time_for(exchange, *, on=None)` | time | IST prev_close reset time; defaults to 08:00 if NULL in DB |
| `sessions_with_snapshot_time_now(*, at=None)` | list[ExchangeSchedule] | All sessions (open OR settlement, any gate) whose snapshot_time matches current IST minute (minute-precision). Used by `background.py` triggers. |
| `async settlement_cutoff_for(exchange)` | datetime | Prior-session settlement boundary. Formula: `today_ist + reset_time` if `now_ist >= reset_time`, else `yesterday_ist + reset_time`. |
| `async settlement_ref_close_map(exchange, kind, pairs)` | dict[(account,symbol), float] | `daily_book.ltp` WHERE `captured_at < settlement_cutoff_for(exchange)` for given (account, symbol) pairs. |
| `async refresh_cache()` | None | Reload `exchange_schedule` from DB (called hourly + on any admin write) |
| `async seed_and_warm(session)` | None | Insert 5 default rows + market_holidays + market_special_sessions; call `refresh_cache()` |

### Background integration

`_snapshot_probe_nse_mcx()` in `background.py` replaces hardcoded trigger times:

```python
# Every minute:
sessions_now = exchange_clock.sessions_with_snapshot_time_now()
for session in sessions_now:
    if session.session_name == "settlement":
        await trigger_settlement_capture(session.gate)  # final BHAV/settlement
    else:
        await trigger_close_snapshot(session.gate)      # daily_book.ltp capture
```

Triggers (5 per trading day Mon–Fri):
- **NSE 15:31** — close snapshot (regular session)
- **NSE 16:15** — settlement capture (settlement session, is_open=false)
- **MCX 23:31** — close snapshot (evening session)
- **MCX 00:15** — settlement capture (settlement session, is_open=false)

### Admin API: `backend/api/routes/exchange_schedule.py`

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/admin/exchange-schedule` | List all rows (defaults first, then by date ascending) |
| PUT | `/api/admin/exchange-schedule` | Upsert row: ON CONFLICT (gate, date, session_name) DO UPDATE |
| DELETE | `/api/admin/exchange-schedule/{id}` | Delete date-override row (default rows protected; 400 if date IS NULL) |

**PUT payload** (all fields optional except `gate` + `session_name`):

```json
{
  "gate": "NSE",
  "exchanges": ["NSE", "BSE"],
  "date": "2026-11-01",
  "session_name": "muhurat",
  "is_open": true,
  "open_time": "17:45",
  "close_time": "18:45",
  "snapshot_time": "18:46",
  "snapshot_reset_time": null,
  "reason": "Diwali Muhurat 2026"
}
```

When `exchanges` is null/omitted, server fills it from the default row for that
gate. After any write, `exchange_clock.refresh_cache()` is called
server-side.

### Frontend settings: Exchange Schedule section

**File**: `frontend/src/routes/(algo)/admin/settings/+page.svelte`

Two tables: defaults (date=NULL) and overrides (date-specific)

**Defaults table**:
- Columns: Gate | Session | Open | Close | Snapshot | Reset | Actions
- Per-gate rows (NSE/regular + NSE/settlement + MCX/morning + MCX/evening +
  MCX/settlement)
- Edit button (✏); delete blocked (400 response)
- Shows "—" for open/close when is_open=false

**Overrides table**:
- Columns: Gate | Date | Session | Open? | Open | Close | Reason | Actions
- Sorted by date ascending
- Edit (✏) and delete (🗑) buttons
- [+ Add Override] button

**Unified add/edit form**:
- Gate dropdown (NSE | MCX)
- Date input (blank = edit default; filled = new override)
- Session name text input ("regular", "morning", "closed", "muhurat", etc.)
- Exchanges multi-select (checkboxes; defaults to all in gate; operator
  deselects to exclude F&O for Muhurat)
- Is open toggle (Yes | No)
- Conditional fields (shown only when Is open=Yes):
  - Open time
  - Close time
  - Snapshot time (blank = no snapshot)
  - Reset time (blank = no reset)
- Reason text input (optional)
- Save | Cancel buttons

Behaviour:
- Date blank + existing session name → upserts default record
- Date filled → upserts date-override
- UNIQUE (gate, date, session_name) ensures safe re-edit
- After save, cache refreshes immediately on server

### Adding a new exchange

**Option A**: Edit existing gate's `exchanges` array from UI
```
Default row: gate="NSE", exchanges=["NSE", "BSE", "NFO", "BFO", "CDS"]
→ Add "GIFT": exchanges=["NSE", "BSE", "NFO", "BFO", "CDS", "GIFT"]
→ Save
→ GIFT now inherits NSE schedule (09:15–15:30, settlement 16:15)
```

**Option B**: Create new default row with own gate
```
[+ Add Session] → Gate="GIFT", Session="regular", Exchanges=["GIFT"],
Open=09:15, Close=15:30, Snapshot=15:31, Reset=08:00
→ Save
→ GIFT has its own independent schedule
```

### Decorator: `@apply_settlement_overlay(kind)`

Applied to async route handlers returning `list[Row]` (positions, holdings).
Patches day P&L and close_price for closed-exchange rows after market close:

```python
for row in rows:
    if is_exchange_closed(row.exchange):
        # Fetch prior-session settlement snapshot
        ref_close = await exchange_clock.settlement_ref_close_map(
            row.exchange, kind, [(row.account, row.symbol)]
        )[(row.account, row.symbol)]
        
        if snap_ltp is not None and ref_close > 0:
            # Patch using prior-session LTP
            row.day_change_val = (snap_ltp - ref_close) * row.qty
            row.day_change_percentage = (row.day_change_val / (ref_close * row.qty)) * 100
            row.close_price = ref_close
```

Ensures frozen snapshot P&L is consistent with broker settlement settlement
prices even after both NSE and MCX have closed.

---

## 16. Daily Broker Issue Aggregation & Monitoring

**File**: `backend/api/models.py` · `backend/api/background.py` · `backend/brokers/service/routes.py`

### Table: `broker_issue_daily`

Per-account daily roll-up of broker connection health issues. Written by
`_task_broker_issue_daily` background cron (23:45 IST + once at startup).

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `broker_id` | VARCHAR(32) | Broker vendor (zerodha_kite, dhan, groww) |
| `account` | VARCHAR(32) | Account code (e.g. ZG0790), indexed |
| `issue_date` | DATE | Date (IST) of aggregated events, indexed |
| `issue_count` | INT | Total issue-count for the day |
| `breakdown` | JSONB | `{auth_fail, fetch_fail, circuit_open, rotation_detected}` counts |
| `updated_at` | TIMESTAMP | Last-write timestamp (UTC) |

Unique constraint: `(broker_id, account, issue_date)`. UPSERT
replaces when a re-run happens or the date rolls.

### Background Task: `_task_broker_issue_daily`

**Registration**: `_supervised(coro_fn="_task_broker_issue_daily", name="bg-broker-daily")`

- **Execution**: 23:45 IST daily + once at startup (catches yesterday if the
  service was down)
- **Logic**: Queries `broker_connection_events` for `issue_date`, groups by
  event_type, pivots into JSONB breakdown dict (`{auth_fail: N, fetch_fail: N, ...}`),
  UPSERTs into `broker_issue_daily` with the aggregate count
- **Resilience**: Wrapped in `_supervised()` so a single failed aggregation
  doesn't silence the task forever; crashes trigger a 60s retry

---

## 16.1 CONNCHECK TLM Tool

**File**: `tools/tlm/conncheck.py` → delegates to `scripts/check_broker_conn_issues.py`

CLI tool integrated into the daily TLM audit pipeline (`/tlm` endpoint).
Scans `broker_issue_daily` for the last N days and emits a severity score.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | No issues (green) |
| 1 | P1 — critical issues detected |
| 2 | P2 — warnings detected |
| 3 | DB unreachable / query failed |

### Thresholds

**Config key**: `backend_config.yaml` section `broker_issue_thresholds`

| Threshold | Default | Meaning |
|---|---|---|
| `auth_fail_p1` | 10 | If any account has ≥10 auth failures, P1 |
| `circuit_open_p1` | 5 | If any account has ≥5 circuit opens, P1 |
| `total_p1` | 50 | If total issues across all accounts ≥50, P1 |
| `total_p2` | 20 | If total issues ≥20 but <50, P2 |
| `lookback_days` | 7 | Scan the last N days of `broker_issue_daily` |

Operator can tune these via `/admin/settings` (or backend_config.yaml).

---

## 16.2 Deploy Notification Receipt Tracking

**File**: `scripts/monitor_ntfy_deploy.py`

After a successful prod deploy (main branch only), an async monitor polls the
ntfy.sh API to verify that the notification was delivered to subscribed
devices. Called from `webhook/deploy.sh` after a successful `git push origin main`.

### Behaviour

- **Trigger**: On successful prod deploy (main branch push completes)
- **Poll interval**: Every 2s, max 30s total
- **Failure handling**: Logs warning but does NOT fail the deploy (best-effort only)
- **Integration**: Wired into deploy.sh → called after `systemctl restart ramboq_api`

Helps operators catch silent ntfy.sh delivery failures early so they know
whether the deployment alert actually reached the team's devices.

---

## 16.3 Alert Routing Restoration

**File**: `backend/config/backend_config.yaml`

Order failure and agent-alert email routing restored to `true`:

```yaml
alerts:
  order_failure:
    email: true               # was: false; restored
  agent_alert:
    email: true               # was: false; restored
```

Alerts now route to designated + admin recipients via SMTP as originally
designed.

---

## 17. Closed-Hours Cache Refresh Pattern

**File**: `backend/api/background.py` — `_task_closed_hours_refresh()` (deprecated pattern)

Prior implementation: `_task_closed_hours_refresh()` ran every 30 minutes during closed hours,
calling `snapshot_daily_book()` to write fresh snapshots. This pattern has been **consolidated
into `_task_daily_snapshot()`**.

### New pattern (Aug 2026 consolidation)

`_task_daily_snapshot()` now exclusively owns all daily book writes:

1. **Startup snapshot** — fires once when both NSE and MCX are closed (skipped if markets open)
2. **NSE settlement pass** — fires at 16:15 IST daily (after OCP settlement)
3. **MCX settlement pass** — fires at 00:15 IST daily (after MCX settlement, ~7h45m after
   MCX closes at 23:30 IST previous day)

**Rationale**: Daily book writes are meaningful only when broker settlement prices are
available (post-settlement). Mid-session writes during closed hours dilute the data with
stale broker LTPs that have not yet updated to the new session's open levels.

### Broker cache management during closed hours

During closed-hours windows, broker raw-data cache (`_RAW_CACHE`) is actively busted by
background tasks to ensure fresh broker data on next market open. This is separate from
daily book snapshot writes (which only happen at settlement). Cache busting allows the
broker to prefetch during the quiet window without polluting snapshots.

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from Explore audit of broker layer |
| 2026-07-11 | Added §10 Virtual Root Resolution; I8, I9 invariants; broker audit findings pending |
| 2026-07-13 | Added §8.1 Order Placement Guards & Intent Bypass; I10, I11, I12 invariants; close intent now bypasses G2/MCX/Kite ceilings; preflight honours intent; basket adds per-leg guards |
| 2026-07-15 | RemoteBroker.translate_qty overrides base-class no-op to forward to conn_service via _call; fixes MCX/NCO contracts→lots translation |
| 2026-07-15 | Added I14 invariant — closed-hours snapshot query uses combined latest+prev_batch CTE; prev_close_val prefers prev_ltp over previous_close to prevent post-MCX-close Day P&L collapse |
| 2026-07-19 | Polish R6: Extracted `_record_breaker_state()` from `_record_fetch()` to isolate state machine under `_BREAKER_LOCK`; added §7.1 Market-Data Backfill Pipeline documenting vectorized `_bmd_build_key_index`, `_apply_backfill_to_list` param removal, `backfill_market_data` consolidation |
| 2026-07-24 | v1.1 Resilience improvements (commit 8352fc9f): Dhan cross-process login lock (`/tmp/ramboq_locks/<account>.lock`); file lock timeout (30s poll, `LOCK_EX|LOCK_NB`); MMAP `_known_absent_tokens` persistent set; Dhan 429→BrokerRateLimitError and 5xx→BrokerNetworkError; KiteTicker re-subscription on reconnect; Kite quote rate limiting (1/s); RemoteBroker typed error mapping; circuit breaker persistence (`/tmp/ramboq_cb_state.json`); background task supervisor `_supervised()` with crash restart |
| 2026-07-24 | v1.2 Daily broker-issue aggregation (commit 629397ac): `broker_issue_daily` table + `_task_broker_issue_daily` cron; CONNCHECK TLM tool for issue severity scoring; ntfy deploy-receipt monitor; alert routing restored (order_failure.email + agent_alert.email = true) |
| 2026-07-25 | v1.3 Production outage fixes: Added §7.2 Instruments & Token-Map Cache covering `_TOKEN_MAP_FETCH_LOCK` double-checked locking (cold-cache serialisation, prevents 6.4GB OOM peak), `_task_instruments` 120s startup delay (stagger with `_task_sparkline_warm`), and removal of `_trigger_instruments_store_populate()` (was causing double OOM storm). Added §9.1 Background Task Early-Return Contract documenting `_supervised()` restart behaviour and requirement for all `_task_*` early-exit paths to include `await asyncio.sleep()` to prevent tight event-loop starvation and port-binding failure. |
| 2026-07-25 | Groww instruments cache: per-account `@ssot_fetch` key (`groww_instruments_{account}`) prevents cross-account cache collision when multiple Groww accounts are active |
| 2026-07-26 | v1.4 GTT exchange validation (commit b8b1214c): Added §8.2 documenting `validate_gtt_exchange(exchange)` method on Broker base class; Dhan/Groww override to raise ValueError for MCX/NCO; called at top of apply_plan_live before broker calls; MCX/Dhan fail-fast in apply_template_to_order before lot-size resolution; off-hours GTT note appended to plan.notes when GTT-only template attached while exchange closed |
| 2026-07-26 | Dhan token renewal fix (commit 63262b94): `_dhan_conn_under_lock()` now gates `_try_renew()` on `not test_conn` to skip lightweight renewal when token is confirmed dead (DH-906); goes straight to full PIN+TOTP re-mint via `_mint_and_build()`. `_do_login()` emits DEBUG log `[DHAN-LOGIN]` with HTTP status + 200-char body for auth failure diagnosis. |
| 2026-07-27 | v1.5 Broker health and account display: Inactive broker accounts (last_ok=0, last_fail=0) now return "inactive" state in health endpoint instead of "amber"; frontend excludes inactive from worst-state calc when active accounts exist. Dhan two-tier LKG cache (in-memory Tier-2 seeded from daily_book at startup + DB Tier-3 fallback) reduces stale holdings during outages via `_DB_LKG_CACHE` and `_preload_db_lkg_cache()`. NavBreakdown holdings popup now includes all registered broker accounts (not just those with active holdings) via `connStatus.accounts` union. |
| 2026-07-27 | v1.6 Template system enhancements: Added §8.3 GTT Template Attachment documenting partial-fill guard (#2), sub-lot scale rounding (#3), LIMIT TP tick offset (#1), wing feasibility flag (#10), wing failure alerting, postback TOCTOU idempotency lock, GTT trigger validation (#9, #29), Kite MARKET GTT rejection via `BrokerCapabilityError`, and full-fill detection. Added §8.4 Broker Postback Fill-Status Mapping documenting `_BROKER_FILLED_STATUSES` dict for per-broker fill-token resolution (Kite: COMPLETE, Dhan: TRADED, Groww: COMPLETE). |
| 2026-07-27 | v1.7 Template attach blocking on close orders (commit 0a456e9f): Updated §8.3 to document three-layer blocking mechanism for close/offset orders: (1) ticket submit clears `template_id` via `_is_offsetting_position()` position-book check + `intent="close"` guard, (2) reconcile path returns False when close intent detected via `_opl_reconcile_attach_eligible()`, (3) postback path checks both intent flags and runs async position offset via `_pb_check_and_fire_template_attach()`. Frontend also clears TemplateBar and `templateId` when `action === 'close'`. Added I22 invariant: template attach blocked when order closes/reduces existing position at all three attachment layers (ticket/reconcile/postback) + frontend. Prevents nested exit GTTs on close orders. |
| 2026-07-27 | v1.8 Snapshot orphan deletion (commit 47c49e20): Added §7.3 Daily Snapshot Orphan Cleanup documenting `_delete_orphan_positions()` async helper called after each per-account positions UPSERT in `snapshot_daily_book()`. Removes stale `daily_book` (kind='positions') rows for symbols absent from broker response. Broker response uses sentinel pattern: `positions=None` (call failed, skip cleanup) vs `positions=[]` or `[...]` (call succeeded, proceed). Prevents closed positions from persisting in UI after settlement. Added I23 invariant: orphan deletion enforces SSOT for settled positions; cleanup is fail-open and idempotent. |
| 2026-07-27 | v1.9 Expiry restart fix + prior-day orphan cleanup (commits cbbe0f23, 21d1656a): Updated §9.1 Background Task Supervisor with expiry task restart-blindness fix (module-level `_expiry_last_run_date` sentinel ensures immediate fire on service restart > 09:20 IST; not run today yet); expiry engine re-scan loop (every 30 min until 15:25 IST, catches newly-ITM positions); NSE NIFTY quote key fix (NSE:NIFTY 50 not NSE:NIFTY). Updated §7.3 to add `_delete_prior_orphan_positions()` pass removing settled options from prior-day snapshots (7-day scope). Updated I14 invariant to note off-hours `_positions_snapshot()` query includes `AND qty != 0` guard (commit 21d1656a) to exclude flat/expired positions. Expiry-day auto-close agents changed from inactive → active status. |
| 2026-07-28 | v1.10 Snapshot intraday-closed position inclusion (commits cef00739, 5ac11f56): Updated §7.3 Daily Snapshot Orphan Cleanup and I14 invariant — off-hours `_positions_snapshot()` query refined from `AND qty != 0` (cef00739) to `AND (qty != 0 OR date = :today_ist)` (5ac11f56). Positions closed intraday (qty=0, captured today IST) now appear in off-hours snapshot with 'closed' chip + opacity:0.45 decoration in derivatives legs grid. Prior-session closed positions (qty=0, date≠today) excluded. Next morning before market opens, yesterday's closed legs absent, only carried-overnight open positions visible, matching broker book when gate opens. Frontend `buildCleanLegs()` filters qty===0 rows from payoff POST (strategy analytics unchanged). Legs grid shows closed legs for intraday history. |
| 2026-08-08 | v1.11 Closed-hours snapshot consolidation (commit TBD): Added §16 Closed-Hours Cache Refresh Pattern documenting consolidation of per-30m closed-hours writes into `_task_daily_snapshot()` settlement passes only. `_task_closed_hours_refresh()` no longer calls `snapshot_daily_book()` — daily book writes happen exclusively at NSE (16:15 IST) and MCX (00:15 IST) settlement passes, plus startup snapshot when both markets closed. Broker cache busting still active during closed hours to ensure fresh data on next open, separate from snapshot persistence. Rationale: settlement writes are meaningful only when broker settlement prices available; mid-session writes dilute snapshots with stale LTPs. |
| 2026-08-06 | v1.11 Holdings snapshot day_change_percentage formula fix (commit 13ec7c18): Added §7.4 Holdings Snapshot Day Change Percentage Formula documenting the corrected formula: `day_pnl / (previous_close × qty) × 100` (uses `previous_close` denominator, not LTP). Fallback to `avg_cost` when `previous_close` is zero/missing. Added I24 invariant. SQL now selects `db.previous_close` from daily_book; fixes distortion where using LTP inflated negatives and understated positives. |
| 2026-08-06 | v1.12 Dhan holdings day_pnl fix (commit a737b1e2): Added subsection to §7.4 documenting Dhan `close_price=0` handling and backfill recompute. `_backfill_recompute_derived()` now accepts `close_was_missing: bool` flag; when True, recomputes `day_change = ltp - real_close` unconditionally to override stale Dhan adapter value. Added subsection on daily_book `day_pnl` storing total P&L (not per-share change). Added I25 and I26 invariants capturing `close_was_missing` detection and total-P&L storage convention. Fixes Dhan holdings snapshot storing incorrect day_pnl (e.g. 3952 instead of -28) when `close_price` was patched from zero. |
| 2026-08-09 | v1.10 MCX lot-scale day P&L fix (commit TBD): Added §7.3 Daily Snapshot — MCX Lot-Scale Day P&L Fix documenting `_snap_compute_day_pnl()` `multiplier` parameter (default=1). When provided, scales `overnight_quantity`, `day_buy_quantity`, `day_sell_quantity` by lot_size before computing decomposed intraday P&L formula. Fixes brand-new MCX positions (CRUDEOIL lot=100) showing day_pnl off by 100× (₹50 instead of ₹5000) on first snapshot appearance. Caller responsibility: snapshot writers pass `r.get("multiplier", 1)` via lot_size field. Holdings snapshot day_change now computed from price diff × qty via `prev_batch` CTE (same as positions). |
| 2026-08-11 | v1.13 Orders fetching resilience and chase timeouts (commit 5ec9afec): Added §8.5 Orders Fetching Resilience & Chase Timeouts documenting per-broker 8-second timeout in `_fetch_orders()` with early-exit on timeout + empty-list fallback (pool shutdown with `cancel_futures=True`), 10-second `asyncio.wait_for` timeout in `_chase_snapshot_broker_status_by_id()` to prevent `/chases/active` lock starvation, and frontend `ChaseCard.svelte` polling guard (`_fetching` flag) to drop concurrent polls. Added I27, I28, I29 invariants capturing timeout semantics and fallback behaviour for orders list + chase snapshot + frontend polling. |
| 2026-08-11 | v1.14 Broker resilience fixes (commit fd4e9ae6): Added §9.2 Token Pre-Warm Task & Expiry Prevention documenting hourly `_task_prewarm_tokens()` in conn-service pre-warming Kite (05:45–05:59 IST), Dhan (token_age > 22h), and Groww (expired check). Updated §5 DhanConnection with login cooloff persistence to `/tmp/ramboq_dhan_login_cooloff.json` surviving restarts + `[DHAN-COOLOFF]` / `[DHAN-LOGIN]` logs. Updated §5 GrowwConnection with hardening: `CONN_RESET_HOURS = 23`, `_is_token_expired()` method, `_check_login_rate_limit()` 120s cooloff, proactive refresh in `get_groww_conn()`, `[GROWW-LOGIN]` logs. Updated §6 Circuit Breaker & Health with health heartbeat validity gate (skip expired tokens, once-per-cycle warning via `_heartbeat_warned` dedup), false-amber threshold fix (`_BROKER_HEALTH_FRESH_WINDOW_S` 300s→660s, accommodates 600s Dhan cold poll). Added subsection to §9.1 documenting `_task_token_refresh()` no-op warning when `RAMBOQ_USE_CONN_SERVICE=1`. |
| 2026-08-13 | v1.15 KiteTicker + Dhan resilience + GTT pre-flight enhancements: Updated §7 KiteTicker & Mmap Pipeline with watchdog `asyncio.to_thread` non-blocking path, unsubscribe cleanup (prunes `_pending`, `_token_to_sym`, `_sym_to_token` maps), and MMAP re-registration suppression to check `_token_to_sym` before broker subscribe. Added §8.3.1 GTT Pre-flight Lot-Size Validation documenting synchronous G1 check at top of `apply_plan_live` before `broker.translate_qty` or any broker call; fails fast on misconfigured templates. Updated §8 DhanBroker with request stagger (50ms per request to avoid 429s) and order_type UNKNOWN_CAPS fallback (returns neutral string instead of raising on unrecognised `orderType` field). Updated §6 Circuit Breaker & Health to clarify persistence to `broker_accounts.cb_state_json` on each transition (open/half-open/closed) with fallback to `/tmp/ramboq_cb_state.json` on startup. |
| 2026-08-14 | v1.16 Daily snapshot UPSERT idempotency fix + background task auto-subscription (commit 43771b98): Added §7.3.1 Daily Snapshot UPSERT Idempotency & LTP Coalesce Fix documenting `COALESCE(EXCLUDED.ltp, daily_book.ltp)` + `payload_json` CASE guard preventing mid-session NSE passes from overwriting MCX settlement LTPs with NULL (fixes blank grid cells + NAV collapse). Renumbered subsequent sections (7.3.2 Firm NAV, 7.3.3 Holdings formula). Added subsection to §9.1 Background Task Supervisor documenting MCX options auto-subscription of front-month futures (via `resolve_symbol('CRUDEOIL', 'MCX')`) and NFO/BFO equity options auto-subscription of NSE/BSE spot underlyings (NIFTY, BANKNIFTY, stock names) in `_perf_subscribe_book_symbols()` to ensure real-time spot pricing for payoff charts via live KiteTicker ticks instead of 30s REST polls. |
| 2026-08-14 | v1.17 Order-pair feature (commit 6f374a1a): Added §8.6 Order Pairing — Parent-Child Relationship Linking documenting `POST /api/orders/pair` endpoint (validation: parent + child must exist and be distinct, child cannot have existing parent); updates `AlgoOrder.parent_order_id` on child row. PositionRow schema additions: `is_orphan: bool` (True when no open AlgoOrder matches position's account/tradingsymbol), `pair_group_key: str\|None` (shared root AlgoOrder ID for linked positions). Frontend: `OrderPairModal.svelte` for establishing pairs; MarketPulse shows coral "O" badge on orphan positions; `postSortRows` keeps paired positions adjacent in grid; ChaseCard shows "O" chip for dangling children. |
| 2026-08-15 | v1.18 Admin snapshot trigger + Dhan EOD fallback + weekend guard (commit TBD): Added §7.3.2 Admin Snapshot Trigger documenting `POST /api/admin/pnl/snapshot` endpoint with `market_open` override. Changed holiday-aware detection from time-only `_is_exchange_open_at()` to full `is_any_segment_open()` (checks both hours + holiday calendar). Added §7.3.3 Dhan `last_price=0` Fallback in EOD Snapshots documenting `_snap_holding_eod_vals()` fallback chain: `close_price` → `previous_close` → `last_price=0` when mid_session=False. Ensures Dhan holdings appear in Pulse on non-trading days. Added §7.3.4 Weekend Guard for Filtered Holdings & Positions documenting per-category upsert skip: holdings filtered → skip holdings only, positions filtered → skip positions only (prior: filtered either → skip both). Fixes weekend Dhan holdings disappearing when open positions flatten. Renumbered subsequent sections (7.3.5 Firm NAV). |
| 2026-08-16 | v1.19 Day P&L backstop Case 2 + Holdings Dhan/Groww fallback (commit 7b8d432c): Updated `backend/api/algo/pnl_math.py:apply_day_change_backstop()` to handle Case 2: overnight positions where LTP gate zeroed `day_change_val` but broker `pnl` is valid (`oq>0, dcv==0, pnl≠0, close>0, avg>0`). Recovery formula mirrors frontend SSOT: `pnl − (close − avg) × oq`. Added `_apply_holdings_dcv_fallback()` post-processing in `broker_apis.py:_enrich_holdings()` for Dhan/Groww holdings where backfill symbol resolution fails, leaving `day_change_val==0`. Fallback: when `day_change` (scalar ltp−close) is present and `close_price > 0`, sets `day_change_val = day_change × opening_quantity`. Ensures holdings Day P&L displays correctly on cold-cache loads instead of showing 0 for symbols outside symbol-resolver cache. |
| 2026-08-20 | v1.20 Auth-error retry refactor + account loading cache (commit cecc9842): Moved auth-error detection and token renewal from scattered inline blocks in `broker_apis.py` into unified `@for_all_accounts._per_account._try_renew` handler in `backend/shared/helpers/decorators.py`. New zero-dependency `backend/shared/helpers/auth_error.py` module exports `is_auth_error_str(err)` to detect 401/403/token-expiry signals without importing broker layer. Updated §2 Broker Base Contract auth invariant and §9.2 On-demand token renewal subsection. Removed `_maybe_renew_on_auth_error`, `_rebuild_holdings_after_renewal`, `_rebuild_positions_after_renewal`, `_rebuild_margins_after_renewal` from `broker_apis.py` — logic now centralized in decorator `_try_renew`. Added module-level `_last_known_remote_accounts` cache in `backend/api/routes/brokers.py:_loaded_accounts()` to serve fallback account list when conn-service briefly unavailable (06:00 IST token-expiry restart window). Prevents navbar chip from flipping 0/5 → 5/5 during transient UDS blips. Added subsection in §6 Circuit Breaker & Health documenting account loading cache. |
| 2026-08-24 | v1.21 Holdings data freshness and day P&L fixes (commit 39c21cca): Added §7.3.5 Holdings Data Freshness & SSOT Fetch TTL documenting 30-second TTL on `fetch_holdings()` mirroring `fetch_positions()` pattern. Added `_HOLDINGS_SSOT_TTL = 30.0` and `_holdings_ssot_refresh_at` dict tracking last fetch per account; cache bypass on TTL miss forces fresh broker call. Added §7.3.7 Holdings Day P&L Recompute & Backstop Exclusion documenting two fixes: (1) `_override_stale_close_for_holdings()` now recomputes day P&L for ALL holdings rows where `previous_close > 0` exists (not just Dhan patched rows), formula `(ltp - previous_close) × qty` applied universally, (2) `apply_day_change_backstop()` explicitly removed from holdings flow (retained for positions only) since holdings lack `overnight_quantity` field required for backstop Case 1/2/3 edge cases. Prevents spurious backstop matches on missing field when overnight_qty defaults to 0. Renumbered subsequent sections (7.3.6→7.3.8). |
| 2026-08-26 | v1.22 Holdings H slot consistency fix (commit bad82021): Added §7.3.8 Holdings LTP Override & pnl+cur_val Consistency documenting two fixes: (1) `_override_stale_ltp_from_ticker()` now recomputes `pnl` and `cur_val` on patched rows after LTP patch (was leaving them stale, causing NavStrip H slot 2 to show `inv_val` instead of `ltp × qty`); (2) `_build_holdings_pnl_expr()` changes broker pnl trust policy from trusting non-null to trusting non-null AND non-zero (Kite sends `pnl=0.0` pre-market when `last_price=0`, old code trusted that zero → `cur_val = inv_val`, now falls back to computed formula `(ltp-avg)×qty`). At breakeven (`ltp==avg`) both formulas give 0, so no regression. Renumbered subsequent section (7.3.8→7.3.9). |
| 2026-08-27 | v1.23 Holdings gate NSE-specific + per-exchange P&L overlay + chain polling tuning (commits bb778062, 13f59ac0, d4e75014): Added §7.3.5 Holdings Gate Now NSE-Specific documenting `closed_hours_or_broker(segment_exchanges=["NSE"])` parameter; holdings now enter snapshot mode at NSE close (15:35 IST) instead of MCX close (23:30 IST). Added §7.3.7 Snapshot Path Now Calls `_override_stale_close_for_holdings` documenting both broker AND snapshot paths now call `_override_stale_close_for_holdings()` to patch from prior-session daily_book.ltp (cutoff: `captured_at < today_08:00 IST`), eliminating divergence between live/snapshot displays. Added §7.3.9 `close_price` Always Synced to `ref_close` documenting removal of epsilon guard; `close_price` now unconditionally synced to keep denominator consistent with `day_change_val` numerator. Added §7.3.13 Positions Per-Exchange Day P&L Overlay documenting `_overlay_snapshot_for_closed_exchanges()` now patches day P&L for closed-exchange rows (NFO/BSE) immediately after their close (~15:30 IST) using prior-session daily_book snapshot, without waiting for MCX to close. Added Options Chain Polling & Timeouts subsection documenting backend timeout reduction (30s→12s) in `_chain_quotes_batch_quote()` and frontend interval increase (5s→30s) in ChainCard.svelte; added `_pricesFetching` in-flight guard to prevent concurrent quote() calls. Renumbered §7.3.6+ holdings sections (6→6, 7→7, 8→8, etc.). |
| 2026-08-28 | v1.24 Exchange schedule table & clock module (from PLAN): Added §14 Exchange Schedule Table & Clock Module documenting new `exchange_schedule` DB table (date-aware, operator-editable defaults + overrides via `/admin/settings`), module-level cache, public API (`is_exchange_open`, `snapshot_time_for`, `snapshot_reset_time_for`, `sessions_with_snapshot_time_now`, `settlement_cutoff_for`, `settlement_ref_close_map`, `refresh_cache`, `seed_and_warm`), admin routes (GET/PUT/DELETE `/api/admin/exchange-schedule`), and `@apply_settlement_overlay(kind)` decorator for patch-on-close P&L + close_price. Replaces hardcoded `_EXCHANGE_TO_GATE` dict in snapshot_gate.py, `market_segments` YAML block, and 6 hardcoded trigger times in background.py. Single gate/exchanges distinction: a Muhurat row with exchanges=[NSE,BSE] correctly closes NFO/BFO/CDS via per-exchange `_resolve_for_exchange()` lookup. Backend snapshot triggers fully DB-driven: 5 per-day (NSE 15:31 + 16:15, MCX 23:31 + 00:15, MCX morning no-snapshot). Renumbered §15+ sections (14→15, 15→16, 16→17). |
| 2026-08-30 | v1.25 Closed-exchange overlay stored day P&L fix (commit adc5e1f0): Updated §7.3.8 Firm NAV Computation & Closed-Exchange LTP Overlay documenting `latest_snapshot_ltp_map(kind)` return type change from `dict[tuple[str,str], float]` (LTP only) to `dict[tuple[str,str], tuple[float, float\|None]]` (LTP + day_pnl tuple). Added subsection on Overlay Logic documenting new return format and weekend zero-delta bug fix: when both snap_ltp and snap_close come from same Friday settlement snapshot, old code computed `(Fri−Fri)×qty=0` on weekends; now stores EOD `day_pnl` in tuple and uses it directly when non-zero, falling back to price-recompute only when `day_pnl` is None/zero. Added Function subsection documenting `latest_snapshot_ltp_map(kind)` signature, query strategy (MAX(captured_at) per account), return format, guarantee (identical CTE as route snapshot readers), and fail-open behaviour. Updated `_process_overlay_row()` code path (positions.py lines 474–491) and `_hold_tag_closed_row()` (holdings.py) to unpack tuples and prioritize stored `day_pnl` over recomputed values. Impact: weekend grids now show correct overnight P&L from Friday settlement instead of zero-delta phantom move. |
| 2026-08-30 | v1.26 Consolidated morning task schedule (backend implementation): Merged three separate morning events (04:00 holiday refresh, 05:45 Kite token pre-warm, 06:00 hard expiry) into a single 05:30 IST `_task_holiday_refresh()` combined task covering: (1) open-time loading from `exchange_schedule`, (2) NSE API holiday calendar refresh (retries until 08:00 if slow), (3) best-effort proactive token refresh for all brokers (Kite TOTP auto-login, Dhan RenewToken API, Groww session refresh). Token refresh failures logged as warnings only; `@retry_kite_conn` decorator provides automatic recovery on next API call. Skipped under `RAMBOQ_USE_CONN_SERVICE=1`. Updated §7.3.10 Market-open time loading section with new 05:30 lifecycle, updated §9.2 Token Pre-Warm Task title to "Morning Token Refresh — Consolidated 05:30 Task" and restructured content to reflect unified schedule. Updated Token Refresh Delegation to Conn-Service subsection in §9.1 Background Task Supervisor. |
| 2026-08-30 | v1.27 MCX name normalization in instruments & options endpoints (commit TBD): Added §9.3 Instruments & Options Endpoints — MCX Name Normalization documenting two fixes: (1) `_build_expiries_index()` in instruments.py line 201 normalizes MCX underlying names by stripping spaces (`key = inst.u.upper().replace(" ", "")`) so the expiry cache key matches spaceless form frontend sends (e.g., "CRUDE OIL" → "CRUDEOIL"), (2) `_chain_quotes_build_sym_map()` in options.py line 2172 applies same normalization in comparison (`if (inst.u or "").upper().replace(" ", "") != und`). Diagnostic log `[expiries-index] normalized MCX spaced names: ...` emitted per reload. Impact: option chain expiry dropdown and strike-by-strike quotes now work correctly for MCX commodities (CRUDEOIL, NATURALGAS, GOLD, SILVER, etc.) without manual workaround. |
| 2026-08-30 | v1.28 Token Refresh Lifecycle (documentation): Added §5.4 Token Refresh Lifecycle documenting Kite 23-hour vendor TTL, daily 05:30 pre-warm cycle via `_task_holiday_refresh()` with 30-minute retry loop (until 08:00), per-broker validation calls (Kite profile(), Dhan session check, Groww session refresh), token cache at `.log/kite_tokens.json`, cross-process login flock serialisation, per-call validation via lightweight profile() call, and example 90-minute headroom window before expiry (07:00 D+1). Clarifies timing invariants and retry semantics for token lifecycle management. |
