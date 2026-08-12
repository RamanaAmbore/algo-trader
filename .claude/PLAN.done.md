# Plan: Dhan _DhanSDKProxy — centralize retry/remint/rate-limit

## Task

Replace `DhanBroker._safe_call(lambda d: d.xxx())` pattern with a transparent
`_DhanSDKProxy` class that intercepts every SDK attribute access via `__getattr__`
and applies rate-limiting, 5xx translation, JSON auth-failure inspection, and
remint+retry in one place — exactly as `@_retry_groww_auth` does for Groww and
`@retry_kite_conn` does for Kite.

Source-IP binding is already correctly centralized in `DhanConnection._build_client()`,
which mounts `_IPv6SourceAdapter` on the SDK's internal `requests.Session` at
construction time. No ContextVar is needed; the adapter persists for all runtime calls
on that handle. No change required for IP binding.

**Before** (every method):
```python
resp = self._safe_call(lambda d: d.get_holdings())
resp = self._safe_call(lambda d: d.place_order(...), endpoint_group="orders")
```

**After** (clean, proxy-intercepted):
```python
resp = self._sdk.get_holdings()
resp = self._sdk_orders.place_order(...)
```

All retry/remint/rate-limit logic lives in `_DhanSDKProxy.__getattr__` only.

## Agents

- broker: Create `_DhanSDKProxy` class in `backend/brokers/adapters/dhan.py`.
  Add 4 proxy properties to `DhanBroker`: `_sdk` (no rate limit), `_sdk_margins`
  (bucket="margins"), `_sdk_history` (bucket="history"), `_sdk_orders` (bucket="orders").
  Refactor all ~30 `_safe_call` call sites to use the appropriate proxy property.
  Refactor `_call_dhan_ledger_raw` to accept the proxy instead of the raw handle
  (transparent since proxy's `__getattr__` returns a callable for any name, lets
  AttributeError from non-existent SDK methods propagate naturally).
  Remove `_safe_call` method entirely.
  Preserve in `_DhanSDKProxy.__getattr__`:
  - Rate throttle via `_DHAN_RATE_LIMITER.throttle(self._bucket)` if bucket set
  - `self._broker._last_req` / `self._broker._last_resp` tracking
  - 5xx SDK exception → BrokerNetworkError (status 502/503/504)
  - DH-904 dict → BrokerRateLimitError
  - `_looks_like_auth_failure(resp)` → log JWT token age + rotation pattern →
    `self._broker._conn.get_dhan_conn(test_conn=True)` → retry once →
    if still auth failure → raise RuntimeError

  Exact `_safe_call` call-site mapping (endpoint_group → proxy property):
  - `endpoint_group="margins"` → `self._sdk_margins` (profile, margins)
  - `endpoint_group="history"` → `self._sdk_history` (ltp/ohlc)
  - `endpoint_group="orders"` → `self._sdk_orders` (place_order)
  - no endpoint_group → `self._sdk` (holdings, positions, orders, trades, cancel,
    modify, gtt operations, market_status, margin_calculator, basket_order_margins,
    get_forever, cancel_forever, modify_forever, funds_ledger)

- frontend: skip
- backend-test: Add tests in `backend/tests/broker/test_dhan_proxy.py`:
  - `test_proxy_passthrough`: mock SDK handle, verify method called and response returned
  - `test_proxy_auth_failure_remint_retry`: first call returns auth-failure dict, verify
    `get_dhan_conn(test_conn=True)` called + method retried + clean response returned
  - `test_proxy_persistent_auth_failure_raises`: both calls return auth-failure, verify
    RuntimeError raised
  - `test_proxy_5xx_raises_network_error`: SDK raises exception with response.status_code=503,
    verify BrokerNetworkError raised
  - `test_proxy_dh904_raises_rate_limit_error`: SDK returns dict with code="DH-904",
    verify BrokerRateLimitError raised
  - `test_proxy_rate_throttle_called`: mock `_DHAN_RATE_LIMITER.throttle`, verify called
    when bucket is set and `_DHAN_RATE_LIMIT_ENABLED` is True
  - `test_proxy_no_throttle_without_bucket`: verify throttle NOT called when bucket=""
  - `test_safe_call_removed`: verify `DhanBroker` has no `_safe_call` attribute

- doc: skip
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

refactor(broker): replace DhanBroker._safe_call with _DhanSDKProxy transparent interceptor

## Done when

- `_safe_call` method no longer exists on `DhanBroker`
- All ~30 SDK call sites use `self._sdk*` proxy properties
- `_DhanSDKProxy` handles rate-limit, 5xx, DH-904, auth-failure-remint-retry in `__getattr__`
- All 8 proxy unit tests pass
- All existing broker tests pass (no regressions)
- Source-IP binding unchanged (no ContextVar changes)
