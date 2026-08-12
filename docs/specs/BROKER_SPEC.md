# Broker Layer Specification

Single source of truth for `backend/brokers/` — the vendor-agnostic broker abstraction layer.
Code, tests, and documentation must stay in sync with this file.

**Version**: 1.14 — 2026-08-11  
**Owner**: Platform  
**Linked files**: `backend/brokers/base.py` · `backend/brokers/registry.py` · `backend/brokers/connections.py` · `backend/brokers/kite_ticker.py` · `backend/brokers/adapters/` · `backend/brokers/service/` · `backend/brokers/client/`

---

## Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Broker Base Contract](#2-broker-base-contract)
3. [Capabilities Matrix](#3-capabilities-matrix)
4. [Broker Selection SSOT](#4-broker-selection-ssot)
5. [Connections Singleton](#5-connections-singleton)
6. [Circuit Breaker & Health](#6-circuit-breaker--health)
7. [KiteTicker & Mmap Pipeline](#7-kiteticker--mmap-pipeline)
7.1 [Market-Data Backfill Pipeline](#71-market-data-backfill-pipeline)
7.2 [Instruments & Token-Map Cache](#72-instruments--token-map-cache)
7.3 [Daily Snapshot Orphan Cleanup](#73-daily-snapshot-orphan-cleanup)
7.3.1 [Firm NAV Computation & Closed-Exchange LTP Overlay](#731-firm-nav-computation--closed-exchange-ltp-overlay)
8. [Adapter Implementations](#8-adapter-implementations)
8.1 [Order Placement Guards & Intent Bypass](#81-order-placement-guards--intent-bypass)
8.2 [GTT Exchange Validation & MCX Broker Restrictions](#82-gtt-exchange-validation--mcx-broker-restrictions)
8.3 [GTT Template Attachment System Enhancements](#83-gtt-template-attachment-system-enhancements-jul-2026)
8.4 [Broker Postback Fill-Status Mapping](#84-broker-postback-fill-status-mapping)
9. [Remote Broker & Conn Service](#9-remote-broker--conn-service)
9.1 [Background Task Supervisor](#91-background-task-supervisor)
10. [Virtual Root Resolution](#10-virtual-root-resolution)
11. [Key Invariants](#11-key-invariants)
12. [Test Coverage Map](#12-test-coverage-map)
13. [Known Defects & Risks](#13-known-defects--risks)

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

**Auth invariant**: Adapters handle token refresh transparently. Callers never see `401`. Re-auth failure raises a domain exception caught by the circuit breaker.

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

---

## 6. Circuit Breaker & Health

**File**: `backend/brokers/broker_apis.py` · `backend/api/routes/health.py`

`_FETCH_HEALTH[account]`: `{last_ok_at, last_fail_at, consecutive_fail_count, circuit_open_until, open_cycle_count}`

State machine (opt-in per account via `circuit_breaker_enabled`):
- 3 consecutive failures → OPEN (skip account, return empty DataFrame + `fetch_failed=True`)
- Cooloff: 5 min → doubles per cycle → 30 min max
- HALF-OPEN: one probe after cooloff

**Circuit breaker persistence** (Jul 2026): `circuit_open_until` state persists to
`/tmp/ramboq_cb_state.json`; non-expired entries are loaded on startup. Prevents
redundant probe loops when main API restarts during an active cooloff window.

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

**Re-subscription on reconnect** (Jul 2026): `_on_connect` now re-subscribes all
previously-subscribed tokens (not just tokens added during the disconnect window). Ensures
market data resumes immediately after network transients.

**Universe registration**: startup + segment opens + daily_book past-7d union (backstop survives conn_service restart).

**MMAP missing-symbol suppression** (Jul 2026): `_known_absent_tokens: set[int]` replaces the
60s-TTL dict. Warning fires once per token per process lifetime; subsequent lookups for that
token are silent. Reduces log spam when Dhan lacks certain F&O contracts.

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

## 7.3.1 Firm NAV Computation & Closed-Exchange LTP Overlay

**File**: `backend/api/algo/nav.py` — `compute_firm_nav()` + `_fetch_holdings_phase()`

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

### Overlay logic

`_overlay_closed_exchange_ltp()` iterates each row in the holdings DataFrame:
- If the exchange is **open now**, use broker `cur_val` (live LTP × qty)
- If the exchange is **closed now**, look up `(account, symbol)` in the snapshot map
  - If a snapshot LTP exists and is positive, **recalculate** `cur_val = snapshot_ltp × qty`
  - Otherwise, fall back to broker `cur_val` (may be stale, but fail-open)

This mirrors the same overlay applied in `_overlay_snapshot_for_closed_exchanges()` in
`backend/api/routes/holdings.py` (the `/api/holdings` route). Both now use the
identical snapshot LTP when an exchange is closed.

### Impact

**Pre-session divergence eliminated (Aug 2026)**: Before market open, the firm NAV
calculation and the holdings grid now show the same values, eliminating the ~6L
(60-lakh) pre-session NAV divergence that occurred when firm NAV used live broker LTPs
(which had not yet updated to the new session's opening levels) while the holdings route
used frozen DB snapshots.

---

## 7.4 Holdings Snapshot Day Change Percentage Formula

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

---

## 8. Adapter Implementations

### KiteBroker
- `translate_qty(exchange, raw_qty, lot_size)` — MCX: `contracts = lots × lot_size`; raises `ValueError` on `lot_size≤1` (cache miss guard)
- Every GTT leg AND wing MUST call `translate_qty` before `place_gtt()` — `place_gtt` does NOT auto-translate (incident 2026-07-02)
- `place_order(qty, ...)` has a 50-lot adapter ceiling; bypassed for `intent="close"`
- `_truncate_tag(kwargs)` — defensive 20-char tag truncation before every `place_order`

### DhanBroker
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
- `historical_data()` returns `[]` by design — excluded from `get_historical_brokers()`
- `place_gtt()` raises `NotImplementedError` for MCX/NCO

### GrowwBroker
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

## 8.2. GTT Exchange Validation & MCX Broker Restrictions

**`validate_gtt_exchange(exchange)` method** (commit b8b1214c): New method on the `Broker` base class (`backend/brokers/base.py`, line 222). Default is a no-op (all exchanges allowed). Called at the top of `apply_plan_live` in `template_attach.py` before lot-size resolution, plan resolution, or any broker call.

| Broker | Supported | Unsupported | Raises |
|---|---|---|---|
| Kite | All | — | No (no-op default) |
| Dhan | NSE, BSE, NFO, BFO, CDS | MCX, NCO | `ValueError` |
| Groww | NSE, BSE, NFO, BFO, CDS | MCX, NCO | `ValueError` |

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

### Token Refresh No-Op Warning

**File**: `backend/api/background.py` — `_task_token_refresh()`

When `RAMBOQ_USE_CONN_SERVICE=1` is set, `_task_token_refresh()` in the main API is a 
no-op — all token management is delegated to the conn-service. This task now logs a 
WARNING at startup to make the no-op visible to operators, preventing confusion if they 
expect this background task to do token work on the main process:

```
WARNING: _task_token_refresh called but RAMBOQ_USE_CONN_SERVICE=1 — skipping (conn-service owns token management)
```

**Rationale**: Token refresh happens in conn-service background tasks (`_task_prewarm_tokens`, 
`_task_health_heartbeat`, etc.). The main API background task is kept as a guard rail; 
when active (non-conn-service mode), it refreshes Kite/Dhan/Groww tokens synchronously. 
With conn-service mode enabled, it correctly exits early with a diagnostic log.

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

---

## 9.2 Token Pre-Warm Task & Expiry Prevention

**File**: `backend/brokers/service/app.py` — `_task_prewarm_tokens()`

Conn-service runs an hourly background task to pre-warm broker tokens before they
expire, preventing mid-session auth failures when operators rely on long-lived
credentials.

### Pre-warm schedule by broker

| Broker | Window | Condition | Logs |
|---|---|---|---|
| Kite | 05:45–05:59 IST | Daily before 06:00 expiry | `[PREWARM]` |
| Dhan | On-demand hourly | `token_age > 22h` (expiry=23h) | `[PREWARM]` |
| Groww | On-demand hourly | `_is_token_expired()` returns True | `[PREWARM]` |

**Dhan co-expiry prevention**: Multiple Dhan accounts minted at similar times could
expire together, causing a cluster of auth failures. The 22-hour threshold ensures
at least one fresh token is available before expiry; staggered account startups mean
pre-warms fire at different times, spreading the refresh load.

**Groww proactive refresh**: `GrowwConnection.get_groww_conn()` now calls
`_is_token_expired()` synchronously on every session request and proactively
refreshes if True, preventing expired-credential exceptions on the critical path.

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

**I2 — `translate_qty` before every GTT leg**: `apply_plan_live` MUST call `broker.translate_qty(exchange, raw_qty, lot_size)` for every GTT leg AND wing before `broker.place_gtt()`. Incident: 2026-07-02, 1-lot MCX = 100 lots sent.

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

## 14. Broker Connection Events Audit Log

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

## 15. Daily Broker Issue Aggregation & Monitoring

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

## 15.1 CONNCHECK TLM Tool

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

## 15.2 Deploy Notification Receipt Tracking

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

## 15.3 Alert Routing Restoration

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

## 16. Closed-Hours Cache Refresh Pattern

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
