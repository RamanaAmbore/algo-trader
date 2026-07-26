# Broker Layer Specification

Single source of truth for `backend/brokers/` — the vendor-agnostic broker abstraction layer.
Code, tests, and documentation must stay in sync with this file.

**Version**: 1.3 — 2026-07-25  
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
8. [Adapter Implementations](#8-adapter-implementations)
8.1 [Order Placement Guards & Intent Bypass](#81-order-placement-guards--intent-bypass)
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
- Failed `generate_token` cooloff persists to `/tmp/ramboq_dhan_cooloff.json` and survives
  process restarts, preventing tight-retry loops during rate-limit windows.
- IPv6 on both login and runtime sessions

### GrowwConnection
- TOTP token refresh via `GrowwAPI.get_access_token`
- Module-level `requests` monkey-patch for source-bound HTTP

---

## 6. Circuit Breaker & Health

**File**: `backend/brokers/broker_apis.py`

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

Dhan poll priority: `hot` (30s), `warm` (120s), `cold` (600s). Kite/Groww always poll every cycle.

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

**File**: `backend/brokers/broker_apis.py` — `backfill_market_data()` and helpers

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

**Fallback chain**: PriceBroker quote → KiteTicker LTP (PriceBroker outage) → last-known-good 
cache (both sources unavailable). Rows patched via LKG cache marked with `last_price_stale=True`.

---

## 8. Adapter Implementations

### KiteBroker
- `translate_qty(exchange, raw_qty, lot_size)` — MCX: `contracts = lots × lot_size`; raises `ValueError` on `lot_size≤1` (cache miss guard)
- Every GTT leg AND wing MUST call `translate_qty` before `place_gtt()` — `place_gtt` does NOT auto-translate (incident 2026-07-02)
- `place_order(qty, ...)` has a 50-lot adapter ceiling; bypassed for `intent="close"`
- `_truncate_tag(kwargs)` — defensive 20-char tag truncation before every `place_order`

### DhanBroker
- Instruments CSV from `images.dhan.co` once per IST day; F&O symbol: Dhan format → Kite format
- **429 → BrokerRateLimitError** (Jul 2026): `_safe_call()` checks `resp.get("code") == "DH-904"`
  and raises `BrokerRateLimitError` instead of returning the dict as-is. Allows PriceBroker
  failover and registry retry-cooloff to activate correctly.
- **5xx → BrokerNetworkError** (Jul 2026): `_safe_call()` raises `BrokerNetworkError` on HTTP
  502/503/504 responses, enabling transient retry logic upstream.
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

## 8.2. GTT Exchange Validation & MCX Broker Restrictions

**`validate_gtt_exchange(exchange)` method** (commit b8b1214c): New method on the `Broker` base class (`backend/brokers/base.py`, line 222). Default is a no-op (all exchanges allowed). Called at the top of `apply_plan_live` in `template_attach.py` before lot-size resolution, plan resolution, or any broker call.

| Broker | Supported | Unsupported | Raises |
|---|---|---|---|
| Kite | All | — | No (no-op default) |
| Dhan | NSE, BSE, NFO, BFO, CDS | MCX, NCO | `ValueError` |
| Groww | NSE, BSE, NFO, BFO, CDS | MCX, NCO | `ValueError` |

**MCX/Dhan fail-fast in `apply_template_to_order`** (commit b8b1214c): After resolving `caps = capabilities_for(account)`, if `caps.gtt_supports_mcx=False` and `parent_exchange` is MCX or NCO, the function returns an `AttachResult` with errors immediately — before lot-size resolution, plan resolution, or any broker call. Fires `_fire_attach_fail_alert`. `guard_alert_fired=True` suppresses the duplicate alert at the bottom of the function.

**Off-hours GTT note** (commit b8b1214c): When a GTT-only template (no wing) is attached while the exchange is closed, `AttachResult.plan.notes` now includes: "GTT registered off-hours ({exchange} closed) — will activate at next session open". Only applies when no wing leg exists (wing MARKET legs require open hours). See `apply_template_to_order` line 2026–2030.

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

**Canonical examples in the codebase**:
- `_task_ticker_watchdog`: cutover mode exit → `while is_cutover_on(): await asyncio.sleep(300); return`
- `_task_warm_backfill`: already-fired guard → `await asyncio.sleep(86400); return`
- `_task_warm_backfill`: empty symbol universe → `await asyncio.sleep(3600); return`
- `_task_expiry_check`: non-prod branch → `while not is_prod_branch(): await asyncio.sleep(300); return`

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

**I14 — Closed-hours snapshot query combines latest + prior-session CTEs**: `_positions_snapshot()` in `backend/api/routes/positions.py` uses a single SQL query with `latest_batch` (today's most recent capture per account) and `prev_batch` CTEs (prior-session's most recent row per account/symbol). The `prev_batch` window is anchored on `captured_at < max_at AND captured_at >= max_at - INTERVAL '2 days'` to survive UTC/IST date-column edge cases; `prev_close_val` prefers yesterday's `prev_ltp` (from daily_book) FIRST, falling back to snapshot's `previous_close` only when `prev_ltp` is absent/zero. Rationale: after MCX closes at 23:30 IST the broker sets `previous_close = today_settlement`, which would collapse Day P&L to 0. Using yesterday's LTP preserves the correct close price through the closed window.

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
