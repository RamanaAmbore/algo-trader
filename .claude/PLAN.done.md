# Plan: Broker layer hardening — inspired by fenix patterns

## Context

Researched TheHardeep/fenix to evaluate whether it could replace our broker layer (it
cannot — GPLv3, no WebSocket, no multi-account coordination, different response shape).
However the deep-dive into fenix's internals surfaced three patterns that are genuinely
better than our current approach and are worth adopting: typed exception hierarchy,
per-endpoint rate-limit token buckets, and structured error-code mapping tables.

**Connection/recovery verdict:** fenix is actually LESS robust than us — no retry, no
circuit breaker, no stale-data substitution, no cross-process coordination. Our layer
wins on every resilience dimension. But fenix's error classification and rate-limiting
are more precise.

---

---

## Resilience comparison (direct)

| Dimension | fenix | RamboQuant | Winner |
|---|---|---|---|
| Automatic retry on 429/503 | None — raises immediately | `@retry_kite_conn`, `@_retry_groww_auth`, exponential backoff | **Ours** |
| Circuit breaker | None | CLOSED→OPEN→HALF-OPEN, 5m→30m backoff, jitter | **Ours** |
| Stale-data on outage | None | LKG frame returned with `stale=True` | **Ours** |
| WebSocket auto-failover | None | KiteTicker watchdog, account swap, reactor-dead exit | **Ours** |
| Cross-process token cache | None (single-process) | fcntl-locked `kite_tokens.json`, prod+dev share safely | **Ours** |
| IPv6 source binding | None | ContextVar per-account adapter mount | **Ours** |
| Multi-account coordination | Instantiate separately | Deferred-account stabilizer, priority column, poll gating | **Ours** |
| Per-endpoint rate limiting | Token bucket, 1.1× padding, multi-bucket per call | Poll-priority (hot/warm/cold) interval gate | **fenix** (more precise) |
| Typed exception hierarchy | Full tree: Auth/RateLimit/Network/Order/Input | Ad-hoc strings, SDK exceptions bubble through | **fenix** |
| Error code mapping table | `_DIRECT_ERROR_CLASSES` dict per adapter | String matching (`"DH-901" in str(e)`) | **fenix** |
| Request diagnostics capture | `last_request_url/body/response` per instance | `_FETCH_HEALTH` stamps, no per-request metadata | **fenix** |
| Empty-result detection | `_is_empty_response_error()` distinct from real errors | `_quote_has_data` predicate (Dhan-specific) | Tied |

---

## What fenix provides (for reference)

**15 Indian brokers** — Kite, Dhan, Groww, AngelOne, Upstox, Fyers, and 9 others — behind
one `place_order / cancel_order / positions / holdings / margins / orders` API.

**Auth flow (relevant brokers):**
- Kite: POST login → POST TOTP → handshake → request_token → access_token exchange.
  Saves `_headers` dict; `use_headers()` restores saved state.
- Dhan: Pre-minted `client_id + access_token` from portal (no headless refresh).
- Groww: SHA-256 HMAC on `api_key + api_secret + timestamp` → Bearer token OR TOTP path.

**Instrument masters:** `load_fno_tokens()`, `load_mcx_tokens()` etc. cache per-instance;
lot_size embedded in token dict. Caller must multiply manually: `qty = lots × lot_size`.

**GTT:** Dhan `place_forever_order()` + OCO. Groww: NOT abstracted (not in public API).
Zerodha bracket orders deprecated/removed.

**License:** GPLv3. Viral copyleft — any derivative work must also be GPL.

---

## Hard blockers against adoption

### 1. GPLv3 license (hard stop)
RamboQuant is a proprietary, closed codebase. Linking against a GPLv3 library in a
distributed or SaaS product requires the whole product to become GPL. This alone rules
out fenix as a dependency.

### 2. No WebSocket / real-time ticks
fenix has zero KiteTicker support. Real-time ticks are behind a paid "Fenix-Pro" add-on
with no open-source release. Our entire trading engine — tick distribution to agents,
fill detection in the chase loop, SSE streaming to the UI — depends on the KiteTicker →
mmap → BroadcastBus pipeline. We would need to build this ourselves regardless.

### 3. No IPv6 per-account source binding
Kite whitelists specific IPs; Dhan enforces "one active session per partner-app per
source IP". Our ContextVar-based binding (separate IPv4/IPv6 per account on the same
VPS) is the core workaround. fenix has no concept of source IP binding.

### 4. No multi-account coordination or cross-process token cache
fenix = one instance per account, single process. Our conn_service keeps broker
sessions alive across backend code pushes. Shared fcntl-locked `kite_tokens.json`
ensures prod + dev never race on re-auth. None of this exists in fenix.

### 5. Different response shape
fenix uses its own ExchangeCode enums + unified position/order dict keys. Our entire
downstream — `nav.js`, `positions.py`, `pnl_math.py`, derivatives analytics, the
NavStrip — expects Kite-shape dicts. Migrating would touch hundreds of callsites.

### 6. Gaps in what we actually need
- No basket_order_margins (critical for pre-trade margin validation in our chase loop)
- No historical OHLCV candles
- No market_status per exchange (our closed-hours gate depends on this)
- No translate_qty for MCX lots→contracts (callers would have to handle lot math)
- No circuit breaker / stale-data substitution / auto-downgrade
- No Groww OCO emulation (pair-watcher background task)
- No conn_service (broker sessions die on every backend restart)

---

## What fenix does better / could inspire

| Concept | fenix approach | Our status | Worth adopting? |
|---|---|---|---|
| Typed exception hierarchy | `AuthenticationError`, `RateLimitExceededError`, etc. | Ad-hoc broker-specific exceptions | Nice-to-have; low priority |
| Capability matrix per broker | `has` dict per class | `capabilities.py` matrix | Already done |
| `use_headers()` session restore | Serialise auth headers to dict | Token JSON cache | Functionally equivalent |
| 15-broker support | One class per broker | 3 brokers (Kite/Dhan/Groww) | Relevant only if we add brokers |
| Rate-limit token buckets | Per-endpoint buckets | Circuit breaker + poll gating | Covers our cases |

If we ever want to add Angel One / Upstox / Fyers: wrapping a fenix adapter behind our
`Broker` abstract class is possible — but only if we mirror the code (not import the
GPLv3 lib) or obtain a commercial license from the author.

---

---

## Three patterns worth adopting from fenix

### Pattern 1 — Typed broker exception hierarchy

**Why:** Our adapters currently let SDK exceptions (`kiteconnect.exceptions.TokenException`,
`dhanhq.DhanApiException`, raw `httpx.HTTPStatusError`) bubble up. Upstream callers
(`broker_apis.py`) match on string fragments. A typed tree lets you catch
`BrokerAuthError` specifically in the retry decorator without string-scanning.

**What fenix does:**
```python
BrokerError
├── NetworkError → RequestTimeoutError, RateLimitExceededError
├── AuthenticationError
├── PermissionDeniedError
├── InsufficientFundsError
├── InvalidOrderError → OrderNotFoundError
└── InputError
```
Each carries: `message, broker, error_code, status_code, payload, url, method`.

**What to build:**  
New file `backend/brokers/errors.py`:
```python
class BrokerError(Exception):
    def __init__(self, msg, *, broker=None, code=None, status=None): ...

class BrokerAuthError(BrokerError): ...      # 401, token expired, DH-901
class BrokerRateLimitError(BrokerError): ... # 429, DH-904
class BrokerNetworkError(BrokerError): ...   # timeout, connection reset
class BrokerOrderError(BrokerError): ...     # invalid order, order not found
class BrokerInputError(BrokerError): ...     # bad qty, bad symbol
```

Per-adapter mapping dicts in each adapter file:
```python
# adapters/dhan.py
_DHAN_ERROR_MAP = {
    "DH-901": BrokerAuthError,
    "DH-904": BrokerRateLimitError,
    "DH-906": BrokerOrderError,
}
```

Retry decorators (`@retry_kite_conn`, `@_retry_groww_auth`) catch `BrokerAuthError`
instead of string-matching `"TokenException"`.

**Files:** `backend/brokers/errors.py` (new), `backend/brokers/adapters/kite.py`,
`backend/brokers/adapters/dhan.py`, `backend/brokers/adapters/groww.py`,
`backend/brokers/broker_apis.py`

---

### Pattern 2 — Per-endpoint rate-limit token bucket for Dhan

**Why:** Dhan has known per-endpoint rate limits that differ from each other:
- Auth/generate-token: 1 per 2 min (we already track `_login_blocked_until`)
- Order placement: ~10/s
- Historical data: 3/s (same as Kite)
- Margins: unknown but throttled

Our current poll-priority (hot/warm/cold) gates the *poll interval* per account but
doesn't prevent a burst of N API calls in one cycle from hitting Dhan's per-endpoint
ceiling and triggering a 429 that opens the circuit breaker unnecessarily.

**What fenix does:**
```python
_token_buckets[endpoint_group] = {
    'tokens': capacity, 'refill_rate': capacity/period,
    'capacity': capacity, 'last_refill_time': monotonic()
}
# throttle() holds per-bucket locks DURING sleep; 1.1× padding
```

**What to build:**  
`backend/brokers/rate_limiter.py` — a `TokenBucketLimiter` class:
```python
class TokenBucketLimiter:
    def __init__(self, limits: dict[str, tuple[float, float]]):
        # limits = {"orders": (10, 1.0), "history": (3, 1.0), "auth": (0.5, 120.0)}
    def throttle(self, endpoint_group: str) -> None: ...  # blocks until token available
```

Wire into `DhanConnection._safe_call()` before each Dhan HTTP call, keyed on endpoint
group. Zero change to Kite/Groww adapters.

**Files:** `backend/brokers/rate_limiter.py` (new), `backend/brokers/adapters/dhan.py`

---

### Pattern 3 — Per-call request diagnostics on broker instances

**Why:** When a Dhan 429 or a Groww 403 fires, we log it but don't store the request
metadata (URL, method, body) on the broker instance. Debugging requires grepping logs.
fenix stores `last_request_url`, `last_request_method`, `last_request_body`,
`last_response_headers`, `last_json_response` per instance.

**What to build:**  
Add to `backend/brokers/base.py` `Broker` abstract class:
```python
self._last_req: dict = {}   # url, method, body (truncated 2k)
self._last_resp: dict = {}  # status, headers, body (truncated 2k)
```

Update in `_safe_call()` / `_fetch()` wrappers in each adapter before the HTTP call
and after the response. Expose via a `last_request_debug()` method.
Wire the debug dict into `_FETCH_HEALTH[account]['last_request']` so
`/admin/broker-health` can surface it without log-diving.

**Files:** `backend/brokers/base.py`, `backend/brokers/adapters/dhan.py`,
`backend/brokers/adapters/groww.py`, `backend/brokers/routes/health.py`

---

## Scoping decision

All three patterns are **independent** and can ship as one broker agent pass. Patterns 1
and 3 are low-risk refactors (additive). Pattern 2 (token bucket) is the only behaviour
change and should have a feature-flag (`DHAN_RATE_LIMIT_ENABLED` in
`backend_config.yaml`) so it can be toggled without a code push during the soak period.

## Recommendation

**Do not adopt fenix.** Our broker layer is more sophisticated in every dimension that
matters for production live trading:

- KiteTicker (real-time ticks) — fenix has nothing
- Multi-account IP coordination — fenix has nothing
- conn_service (session persistence across restarts) — fenix has nothing
- Circuit breaker + stale data — fenix has nothing
- Kite-shape normalisation (downstream compatibility) — fenix uses its own shape
- GPLv3 licence — incompatible with proprietary codebase

fenix is a good starting point for a *new* project that doesn't need real-time ticks
and runs a single account. For RamboQuant, adopting it would mean:
- Rewriting or wrapping our entire conn_service
- Migrating hundreds of Kite-shape callsites
- Building WebSocket support ourselves anyway
- Potentially contaminating the codebase with GPL obligations

The only realistic partial use case — re-exporting auth logic for a new broker like
Angel One or Upstox — would need the author's explicit commercial licence or a clean
re-implementation inspired by (not copied from) fenix.

## Agents

- broker: Implement Pattern 1 (typed exception hierarchy in `backend/brokers/errors.py`
  + per-adapter error maps + update retry decorators to catch `BrokerAuthError`),
  Pattern 2 (`TokenBucketLimiter` in `backend/brokers/rate_limiter.py` + wire into
  DhanConnection with `DHAN_RATE_LIMIT_ENABLED` flag),
  Pattern 3 (add `_last_req/_last_resp` to `Broker` base + expose in health route)
- backend: skip
- frontend: skip
- doc: skip
- backend-test: Add `backend/tests/test_broker_rate_limiter.py` — token bucket
  behaviour tests (throttle blocks, refill works, multi-bucket ordering)
- playwright: skip

## Tests

- pytest: yes (test_broker_rate_limiter.py + ensure no existing broker tests regress)
- svelte-check: no
- playwright: no

## Commit message

feat(broker): typed exceptions + per-endpoint Dhan rate-limiter + request diagnostics (fenix-inspired)

## Done when

- `backend/brokers/errors.py` exists with 5-class typed hierarchy
- Each adapter has `_<BROKER>_ERROR_MAP` dict and raises typed exceptions
- `backend/brokers/rate_limiter.py` with `TokenBucketLimiter` passes unit tests
- `DHAN_RATE_LIMIT_ENABLED` flag in `backend_config.yaml` controls token bucket
- `/admin/broker-health` response includes `last_request` debug dict
- pytest green, no existing broker test regressions
