# Plan: Broker layer — @ssot_fetch consolidation + remaining TOCTOU/resilience gaps

## Context

This week's broker fixes landed in 7 separate commits, each patching one symptom at a
time. Three structural issues remain:

1. **Hand-rolled TOCTOU coalesce still exists at two sites** — `kite.py` and
   `broker_apis.py` both re-implement the exact pattern that `@ssot_fetch(coalesce)`
   was built to replace. The `_RAW_CACHE` / `_RAW_INFLIGHT` block in `broker_apis.py`
   is ~60 lines of bespoke lock+Event logic; Kite's `_INSTR_LOCK` / `_INSTR_CACHE`
   double-check is a smaller version of the same thing.

2. **Groww login lock ordering bug (same as Dhan's that was fixed in f84dcb2b)** —
   `GrowwConnection.refresh()` still uses the composite
   `with self._login_lock, _cross_process_login_lock(...)` form that holds the
   cross-process file lock for the full inner check. Dhan was fixed to nested form;
   Groww was not touched.

3. **Dhan error map gaps** — only 4 DH-codes are mapped. Other codes that appear in
   prod logs (DH-903 order rejected, DH-905 service unavailable) fall through to bare
   `BrokerError`, bypassing the auth/rate-limit recovery paths in the CB and retry logic.

## Agents

- backend: skip
- frontend: skip
- broker: Three changes in `backend/brokers/`:

  **Change 1 — Remove `_INSTR_LOCK` / `_INSTR_CACHE` from `kite.py` and apply
  `@ssot_fetch(coalesce)` to `KiteBroker.instruments()`.**

  `backend/brokers/adapters/kite.py`:
  - Delete module-level `_INSTR_CACHE`, `_INSTR_LOCK`, `_INSTR_TTL` (lines 48–54).
  - Import `ssot_fetch` from `backend.shared.helpers.ssot_fetch`.
  - Replace the `instruments()` method body with just the raw SDK call —
    `@ssot_fetch(coalesce, key=lambda self, exchange=None: f"{self.account}:{exchange or ''}")`
    on the method handles deduplication and caching (non-None results cached by the
    decorator; TTL is handled via the decorator's result cache — no expiry needed since
    Kite instruments are a daily dump; use `force_refresh=True` for the rare manual flush).
  - Remove `import time as _time_mod` if only used by `_INSTR_TTL`.

  **Change 2 — Replace `_RAW_CACHE` / `_RAW_INFLIGHT` block in `broker_apis.py` with
  `@ssot_fetch(coalesce)` on `fetch_holdings`, `fetch_positions`, `fetch_margins`.**

  `backend/brokers/broker_apis.py`:
  - Delete `_RAW_CACHE_LOCK`, `_RAW_CACHE`, `_RAW_TTL_S`, `_RAW_INFLIGHT` and the four
    helper functions: `_raw_cache_get`, `_raw_cache_reserve`, `_raw_cache_put`,
    `_raw_cache_release` (~85 lines total).
  - Keep `_raw_cache_invalidate` but re-implement it as `force_refresh=True` calls on
    each of the three decorated functions, or as a simple dict clear if the decorator's
    internal `_result_cache` is accessible. Cleanest: add a thin `_raw_cache_invalidate`
    wrapper that calls each function with `force_refresh=True` on the next call (set a
    `_raw_invalidated` flag that the decorated function checks). Actually simplest: keep
    a module-level `_raw_invalidated: set[str]` flag that the wrapped function body
    checks — if flagged, pass `force_refresh=True` to its own re-entry. Better: expose
    the decorator's `_result_cache` dict reference and clear it directly.
    **Simplest approach**: rewrite `_raw_cache_invalidate` to call each decorated
    function with `force_refresh=True` and discard the result (fires a background
    refetch that populates cache) OR simply expose `_result_cache` on the decorator
    wrapper (add `wrapper._result_cache = _result_cache` inside `ssot_fetch`) and call
    `.pop(key)` from `_raw_cache_invalidate`.
  - Decorate `fetch_holdings`, `fetch_positions`, `fetch_margins` with
    `@ssot_fetch(mode="coalesce", key=<fixed string "holdings"/"positions"/"margins">)`.
  - The three functions already guard the zero-arg path with `if not args and not kwargs:`.
    Keep that guard — `@ssot_fetch` should only wrap the zero-arg fast path. Use a thin
    inner function for the decorated path, or restructure so the decorator sits on the
    zero-arg function body only.
    **Cleaner**: extract the zero-arg paths to `_fetch_holdings_cached()`,
    `_fetch_positions_cached()`, `_fetch_margins_cached()` decorated with
    `@ssot_fetch(coalesce, key="holdings"/"positions"/"margins")`. The public outer
    functions remain unchanged and call the decorated inner on zero-arg path.

  **Change 3 — Fix Groww login lock ordering in `connections.py`.**

  `backend/brokers/connections.py` `GrowwConnection.refresh()` (line 1411):
  - Change composite `with self._login_lock, _cross_process_login_lock(cache_key):` to
    nested form matching DhanConnection:
    ```python
    with self._login_lock:
        with _cross_process_login_lock(cache_key):
            # inner check + mint
    ```
  - No logic change — just lock ordering so the cross-process lock is NOT held during
    the in-process guard check.

  **Change 4 — Extend Dhan error map with missing codes.**

  `backend/brokers/adapters/dhan.py` `_DHAN_ERROR_MAP` (line 56):
  - Add:
    ```python
    "DH-903": BrokerOrderError,   # Order rejected by exchange
    "DH-905": BrokerNetworkError, # Service temporarily unavailable
    "DH-907": BrokerAuthError,    # Account suspended
    "DH-908": BrokerInputError,   # Invalid request parameters
    ```
  - Also extend the `_ROTATION_SIGNAL_PATTERNS` list if any DH-903/905/907 text appears
    in prod logs as a rotation signal (check existing log pattern list first).

- doc: skip
- backend-test: Add/update tests in `backend/tests/broker/`:
  - `test_ssot_fetch.py`: add a test that `force_refresh=True` evicts the result cache
    (verify via the exposed `_result_cache` dict if exposed, or by calling twice and
    confirming the underlying function ran twice).
  - `test_broker_resilience.py`: add tests for:
    - Kite `instruments()` coalesce: concurrent calls share one SDK invoke.
    - `fetch_holdings/positions/margins` coalesce: concurrent calls share one fetch.
    - `_raw_cache_invalidate` invalidates all three caches.
    - Groww login: acquiring `_login_lock` first before `_cross_process_login_lock`
      (verify by checking lock acquisition order in the code path — mock the
      cross-process lock and assert `_login_lock.locked()` when it's entered).
    - Dhan DH-903 maps to `BrokerOrderError`; DH-905 to `BrokerNetworkError`.
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(brokers): @ssot_fetch for Kite instruments + RAW_CACHE; Groww lock order; Dhan error map

## Done when

- `kite.py` has no `_INSTR_LOCK` / `_INSTR_CACHE` — instruments() is decorated with @ssot_fetch
- `broker_apis.py` has no `_RAW_INFLIGHT` / `_raw_cache_reserve` — holdings/positions/margins use @ssot_fetch
- `_raw_cache_invalidate` still works (clears the decorator's result cache for all three)
- `GrowwConnection.refresh()` uses nested `with` blocks, not composite form
- Dhan `_DHAN_ERROR_MAP` covers DH-901/902/903/904/905/906/907/908
- `venv/bin/pytest backend/tests/broker/ -q --tb=line` passes (no new failures)
