# Plan: Fix proxy retry translation + chain-cache coalescing + bid-ask depth indicator

## Context

Two audit passes surfaced fixes across the broker proxy layer and the options chain route:

1. **`_DhanSDKProxy` P1** — the auth-remint retry call at line 936 uses the raw SDK handle directly. A DH-904 or 5xx on the retry path bypasses error translation: DH-904 silently returns `[]` (data loss); 5xx surfaces as an untyped exception the circuit breaker misses.

2. **`_DhanSDKProxy` P2/P3** — `_last_resp["status_hint"]` stays `"auth_fail"` after a successful transparent recovery; GTT mutations (place/modify/cancel/get forever) use the no-bucket `_sdk` instead of `_sdk_orders`; retry path lacks tests for 5xx/DH-904.

3. **`_CHAIN_SYM_CACHE` P2×2** — no coalescing lock (concurrent cache misses both walk 156K instrument rows) and no LRU cap (unbounded growth on expiry day).

4. **Bid/ask depth P2** — `_chain_quotes_bid_ask_from_q` silently fills missing depth with `last_price`; order ticket prefills BUY/SELL limit at `last_price` with no indicator that real depth was absent.

5. **P3 cleanup** — OOM guard test doesn't cover `options_helpers.py`; stale JSDoc in `api.js`.

---

## Agents

### broker: Fix `_DhanSDKProxy` in `backend/brokers/adapters/dhan.py`

**Fix 1 — P1: extract `_raw_call` inner function, apply to both first call and retry**

Inside `_DhanSDKProxy.__getattr__`'s `_invoke` closure, extract a local helper:

```python
def _raw_call(handle):
    try:
        result = getattr(handle, name)(*args, **kwargs)
    except Exception as _exc:
        _raw = getattr(_exc, "response", None)
        _status = getattr(_raw, "status_code", None)
        if _status in (502, 503, 504):
            raise BrokerNetworkError(
                f"Dhan HTTP {_status} for {broker.account!r}: {_exc}"
            ) from _exc
        raise
    if isinstance(result, dict) and result.get("code") == "DH-904":
        raise BrokerRateLimitError(
            f"Dhan rate limit (DH-904) for {broker.account!r}: {result}"
        )
    return result
```

Replace:
- First call: `resp = getattr(d, name)(*args, **kwargs)` + surrounding try/except + DH-904 check → `resp = _raw_call(d)`
- Retry: `resp = getattr(fresh, name)(*args, **kwargs)` → `resp = _raw_call(fresh)`

The 5xx try/except and DH-904 check blocks at lines ~907-926 are removed (absorbed into `_raw_call`).

**Fix 2 — P2: update `_last_resp` to `"ok"` after successful retry**

After `resp = _raw_call(fresh)` (before the persistent-auth-failure check):
```python
if not _looks_like_auth_failure(resp):
    broker._last_resp = {"shape": type(resp).__name__, "status_hint": "ok"}
```

**Fix 3 — P3: GTT mutations → `_sdk_orders` bucket**

In `DhanBroker`, change `self._sdk` → `self._sdk_orders` for:
- `place_gtt` → `self._sdk_orders.place_forever(...)`
- `_modify_gtt_target_leg` → `self._sdk_orders.modify_forever(...)`
- `modify_gtt` → `self._sdk_orders.modify_forever(...)`
- `cancel_gtt` → `self._sdk_orders.cancel_forever(...)`
- `get_gtts` → `self._sdk_orders.get_forever()`

**Fix 4 — P3: docstring for `_DhanSDKProxy`**

Extend the class docstring to explain why `__slots__` + `object.__setattr__`/`object.__getattribute__` are needed: normal `self.x = x` would invoke any `__setattr__` override; `__getattr__` is only called when normal lookup fails, so we must use `object.__getattribute__` to read slots inside `__getattr__` without recursion.

For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.

---

### backend: Fix `_CHAIN_SYM_CACHE` in `backend/api/routes/options.py`

**Fix 5 — P2: add coalescing asyncio.Lock for `_CHAIN_SYM_CACHE`**

Add module-level:
```python
_CHAIN_SYM_CACHE_LOCK: asyncio.Lock | None = None

def _get_chain_sym_cache_lock() -> asyncio.Lock:
    global _CHAIN_SYM_CACHE_LOCK
    if _CHAIN_SYM_CACHE_LOCK is None:
        _CHAIN_SYM_CACHE_LOCK = asyncio.Lock()
    return _CHAIN_SYM_CACHE_LOCK
```

In the chain-quotes route around line 2554, wrap the check-build-cache sequence:
```python
sym_map = _CHAIN_SYM_CACHE.get(sym_cache_key)
if sym_map is None:
    async with _get_chain_sym_cache_lock():
        sym_map = _CHAIN_SYM_CACHE.get(sym_cache_key)  # double-check
        if sym_map is None:
            sym_map = await asyncio.to_thread(_chain_quotes_build_sym_map, ...)
            # LRU eviction (Fix 6) applied here before storing
            _CHAIN_SYM_CACHE[sym_cache_key] = sym_map
```

**Fix 6 — P2: LRU cap on `_CHAIN_SYM_CACHE`**

Add:
```python
_CHAIN_SYM_CACHE_MAX_SIZE = 64
```

Inside the `if sym_map is None:` branch (after acquiring the lock), before storing the new entry:
```python
if len(_CHAIN_SYM_CACHE) >= _CHAIN_SYM_CACHE_MAX_SIZE:
    _CHAIN_SYM_CACHE.pop(next(iter(_CHAIN_SYM_CACHE)))
```

**Fix 7 — P2: add `depth_available` field to chain quote response**

In `_chain_quotes_bid_ask_from_q` (around line 2173), track when fallback is used:
- Return a tuple `(bid, ask, depth_available: bool)` where `depth_available=False` when either bid or ask fell back to `last_price`.

In `ChainQuoteRow` (the TypedDict / dataclass for a row), add field `depth_available: bool`.

In the chain-quotes serialisation, include `depth_available` per strike in the JSON response so the frontend can render it.

For every file you change or create, you MUST write or update at least one test that covers the changed behaviour.

---

### frontend: Bid-ask depth indicator in `frontend/src/`

**Fix 8 — P2: visual indicator when `depth_available=false`**

In `OptionChainTab.svelte` (around lines 594, 608 where limit price is prefilled):
- When `depth_available=false` for a strike, add a visual indicator on the bid/ask cells: dim the value and show a tooltip or `(L)` suffix indicating "last price used — no depth available".
- The prefill logic itself is correct (`BUY → ask, SELL → bid`); only the display needs to signal the fallback.

Read `api.js:971-974` and correct the JSDoc comment to reflect the actual response shape: `{k, ce_bid, ce_ask, pe_bid, pe_ask, ce_depth_available, pe_depth_available}`.

For every file you change or create, you MUST write or update at least one test that covers the changed behaviour.

---

### backend-test: Fill proxy + OOM guard test gaps

**Fix 9 — P3: extend `test_dhan_proxy.py` with retry-path tests**

Add to `backend/tests/broker/test_dhan_proxy.py`:
- `test_proxy_5xx_on_retry_raises_network_error` — first call returns auth-failure, remint succeeds, retry raises 5xx exception → `BrokerNetworkError`
- `test_proxy_dh904_on_retry_raises_rate_limit_error` — first call returns auth-failure, remint succeeds, retry returns DH-904 dict → `BrokerRateLimitError`
- `test_proxy_last_resp_ok_after_transparent_recovery` — first call auth-fails, retry succeeds → `broker._last_resp["status_hint"] == "ok"`
- `test_proxy_gtt_uses_orders_bucket` — verify `get_gtts()` call uses `_sdk_orders` (bucket="orders"); assert throttle called with "orders"

**Fix 10 — P3: extend OOM guard in `test_cache_timeout.py`**

In `test_options_chain_instruments_no_timeout`, extend the regex check to also scan `backend/api/routes/options_helpers.py` for any `get_or_fetch("instruments", ...)` + `timeout_seconds` combination.

For every file you change or create, you MUST write or update at least one test that covers the changed behaviour.

---

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message

fix(broker+chain): proxy retry translation, chain-cache coalescing, bid-ask depth indicator

## Done when

- `_DhanSDKProxy` retry path applies full 5xx/DH-904 translation (`_raw_call` inner fn)
- `_last_resp["status_hint"]` is `"ok"` after transparent recovery
- GTT forever calls use `_sdk_orders` bucket
- `_CHAIN_SYM_CACHE` has asyncio lock (double-checked) + 64-entry LRU cap
- Chain quote response includes `depth_available` per strike
- Bid/ask cells in OptionChainTab show visual indicator when `depth_available=false`
- All existing tests pass; 4 new proxy retry tests + extended OOM guard test added
- Broker cov ≥ 80%, API cov ≥ 45%, svelte-check 0 errors
