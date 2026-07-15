# Broker Layer Specification

Single source of truth for `backend/brokers/` — the vendor-agnostic broker abstraction layer.
Code, tests, and documentation must stay in sync with this file.

**Version**: 1.0 — 2026-07-11  
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
8. [Adapter Implementations](#8-adapter-implementations)
8.1 [Order Placement Guards & Intent Bypass](#81-order-placement-guards--intent-bypass)
9. [Remote Broker & Conn Service](#9-remote-broker--conn-service)
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
| Rate Limit | 10 orders/s | 20 orders/s | 5 orders/s |

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
- **In-process lock** (`threading.Lock`): prevents two threads running login simultaneously
- **Token write**: `tempfile + os.replace()` (POSIX atomic) under flock
- **IPv6**: `_IPv6SourceAdapter` per account; `_IPV6_FAMILY_OVERRIDE` ContextVar for thread safety

### DhanConnection
- Headless TOTP; 2-min cooloff between login attempts
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

Dhan poll priority: `hot` (30s), `warm` (120s), `cold` (600s). Kite/Groww always poll every cycle.

Health surface: `GET /api/admin/broker-health`

---

## 7. KiteTicker & Mmap Pipeline

**Files**: `backend/brokers/kite_ticker.py` · `backend/brokers/tick_buffer.py`

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

**TickerManager failover**: `_consecutive_unhealthy` watchdog; per-account 5-min cooloff prevents ping-ponging. `_swap_history` 128-entry rolling log.

**Universe registration**: startup + segment opens + daily_book past-7d union (backstop survives conn_service restart).

---

## 8. Adapter Implementations

### KiteBroker
- `translate_qty(exchange, raw_qty, lot_size)` — MCX: `contracts = lots × lot_size`; raises `ValueError` on `lot_size≤1` (cache miss guard)
- Every GTT leg AND wing MUST call `translate_qty` before `place_gtt()` — `place_gtt` does NOT auto-translate (incident 2026-07-02)
- `place_order(qty, ...)` has a 50-lot adapter ceiling; bypassed for `intent="close"`
- `_truncate_tag(kwargs)` — defensive 20-char tag truncation before every `place_order`

### DhanBroker
- Instruments CSV from `images.dhan.co` once per IST day; F&O symbol: Dhan format → Kite format
- `historical_data()` returns `[]` by design — excluded from `get_historical_brokers()`
- `place_gtt()` raises `NotImplementedError` for MCX/NCO

### GrowwBroker
- `_retry_groww_auth` wraps every SDK call: `401/403` → re-mint + retry once; `429` → exponential backoff (1→2→4→8s, cap 30s, 3 retries); `504` → refresh session + retry; `400/404` → re-raise immediately
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

---

## 9. Remote Broker & Conn Service

`RemoteBroker` proxies every `Broker` ABC method via `POST /internal/broker/{account}/call/{method}` over UDS. Errors re-raised as `RuntimeError`.

`_ALLOWED_BROKER_METHODS` whitelist (28 methods) — unknown method → 403.

**`api_secret` invariant**: Never leaves conn_service. Main API calls `POST /internal/broker/{account}/verify_postback`; only True/False returned.

Key endpoints: `/health`, `/internal/accounts`, `/internal/broker/{account}/call/{method}`, `/internal/rebuild`, `/internal/broker/{account}/verify_postback`

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

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from Explore audit of broker layer |
| 2026-07-11 | Added §10 Virtual Root Resolution; I8, I9 invariants; broker audit findings pending |
| 2026-07-13 | Added §8.1 Order Placement Guards & Intent Bypass; I10, I11, I12 invariants; close intent now bypasses G2/MCX/Kite ceilings; preflight honours intent; basket adds per-leg guards |
| 2026-07-15 | RemoteBroker.translate_qty overrides base-class no-op to forward to conn_service via _call; fixes MCX/NCO contracts→lots translation |
