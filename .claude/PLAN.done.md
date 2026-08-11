# Plan: fix(frontend+backend): withGuard() double-valve — in-flight guard at both layers

## Context

ChaseCard introduced a `_fetching` boolean that prevents a second poll from firing while the
first is still in-flight. The same problem exists across 6+ other frontend components AND their
backend counterparts: when network is slow and `visibleInterval` fires faster than the fetch
resolves, concurrent requests pile up → event-loop saturation → unresponsive page.

**Double-valve architecture:**
- **Valve 1 (frontend):** `withGuard(fn)` HOF — prevents redundant HTTP requests from being
  sent at all. Best-effort UX optimization.
- **Valve 2 (backend):** `@with_guard` Python decorator — ensures even if duplicate requests
  arrive (concurrent tabs, retry logic, frontend bug), the handler does not execute concurrently.
  This is the reliability guarantee.

**Why `@with_guard` is distinct from `@ssot_fetch`:**
- `@ssot_fetch(mode='coalesce')` — multiple callers wait for one in-flight result (queue)
- `@ssot_fetch(mode='serialize')` — runs calls one at a time (queue)
- `@with_guard` — DROP/skip if already in-flight (no queueing)

For **background tasks**, drop is correct: queueing a stale run wastes resources. For
**route handlers**, coalesce (`@ssot_fetch`) is better UX; `@with_guard` on routes returns
a 429 to protect against burst — apply only where backend fetch is genuinely expensive and
has no cache layer.

---

## Agents

### frontend agent

Add `withGuard(fn)` to `frontend/src/lib/stores.js` and apply to HIGH-risk polling surfaces.

**(1) Add `withGuard(fn)` to `stores.js`** (after `marketAwareInterval` export, ~line 550):
```javascript
export function withGuard(fn) {
  let _running = false;
  return async function _guarded() {
    if (_running) return;
    _running = true;
    try { return await fn(); } finally { _running = false; }
  };
}
```
Export alongside `visibleInterval` and `marketAwareInterval`. Each `withGuard(fn)` call
creates its own independent `_running` flag.

**(2) Apply to HIGH-risk components:**

| File | Function | Interval | Change |
|---|---|---|---|
| `frontend/src/lib/MarketPulse.svelte` | `_runTick` | 5s `marketAwareInterval` | `marketAwareInterval(withGuard(_runTick), _tickMs, _HIDDEN_TICK_MS)` |
| `frontend/src/lib/charts/ChartWorkspace.svelte` | `_loadIntraday` | 3s `visibleInterval` | `visibleInterval(withGuard(_loadIntraday), 3000)` |
| `frontend/src/lib/charts/ChartWorkspace.svelte` | `_pollStatus` | 5s `visibleInterval` | `visibleInterval(withGuard(_pollStatus), 5000)` |
| `frontend/src/lib/LogPanel.svelte` | `_loadOrders`, `_loadAgents`, `_loadSystem`, `_loadConn`, `_loadSim` | 3s via `_every()` | Wrap each fn: `_every(withGuard(_loadOrders), ms)` — or modify `_every(fn, ms)` to apply `withGuard` internally |
| `frontend/src/lib/UnifiedLog.svelte` | `_fetch` | 3s `visibleInterval` | `visibleInterval(withGuard(_fetch), pollMs)` |
| `frontend/src/lib/charts/PriceChart.svelte` | `load` | 3s `visibleInterval` | `visibleInterval(withGuard(load), pollMs)` |
| `frontend/src/lib/OptionChainTab.svelte` | `_refreshChainQuotes` | 5s `visibleInterval` | `visibleInterval(withGuard(_refreshChainQuotes), 5000)` |

Import `withGuard` from `$lib/stores.js` in each changed file.

**Do NOT touch** components that call `createDataStore.load()` — already deduped internally.
**Do NOT touch** `derivatives/+page.svelte` `_refreshChainQuotes` — already has `if (_refreshing) return` guard.

For every file you change, you MUST write or update at least one test. This is mandatory.

---

### backend agent

Add `with_guard` decorator to the backend and apply to background tasks.

**(1) Add `with_guard` to `backend/shared/helpers/utils.py`** (or create
`backend/shared/helpers/guards.py` if `utils.py` is too large):

```python
import asyncio
import functools
import logging

logger = logging.getLogger(__name__)

def with_guard(fn):
    """Decorator: skip/drop concurrent invocations of async fn.
    
    If fn is already running, the second call returns None immediately.
    Correct for background tasks where a stale run should not queue.
    For API routes prefer @ssot_fetch(mode='coalesce') instead.
    """
    _running = False

    @functools.wraps(fn)
    async def _guarded(*args, **kwargs):
        nonlocal _running
        if _running:
            logger.debug("%s: skipped — already in-flight", fn.__qualname__)
            return None
        _running = True
        try:
            return await fn(*args, **kwargs)
        finally:
            _running = False

    return _guarded
```

**(2) Apply to background task functions in `backend/api/background.py`:**
Find every `async def _task_*` or scheduled background function that:
- Calls broker APIs (holdings, positions, orders, instruments)
- Runs on a short interval (< 60s)
- Lacks an existing `@ssot_fetch` or per-function `asyncio.Lock`

Apply `@with_guard` to each. Example targets (verify line numbers before editing):
- `_fetch_positions_direct` / `_fetch_holdings_direct` if called from a tight loop
- Any task that is supervised and could be re-fired while still running
- `_task_instruments_store_populate` if it lacks a guard (recent commit added 120s delay
  but not a concurrent-execution guard)

**Do NOT apply to:**
- Functions already decorated with `@ssot_fetch` — they have their own concurrency control
- Route handlers — use `get_or_fetch` / `@ssot_fetch(mode='coalesce')` there instead
- Functions that hold `asyncio.Lock` internally

**(3) Apply `@with_guard` to `orders_helpers._fetch_orders`** as a second-valve complement
to the 8s per-broker timeout already added. The timeout stops a slow broker; `@with_guard`
stops a concurrent invocation if the previous fetch hasn't returned yet.

For every file you change, you MUST write or update at least one test covering the guard
behaviour (mock the guarded function, call it twice concurrently, assert second call returned
None and the underlying function was called only once).

---

### frontend-test agent

Write Vitest tests for `withGuard()` in `frontend/src/lib/__tests__/withGuard.test.js`:
- Second call while first is in-flight: skips (returns undefined, fn called once)
- After first resolves: second call executes (fn called again)
- Error path: fn throws → `_running` resets, next call executes normally
- Independent state: two `withGuard(fn)` wrappers have separate `_running` flags
- Async correctness: fn is awaited, not fire-and-forget

---

### backend-test agent: skip
### broker agent: skip
### doc agent: skip
### playwright agent: skip

---

## Tests
- pytest: yes
- svelte-check: yes
- vitest: yes
- playwright: no

## Commit message
fix(guards): withGuard() double-valve — frontend HOF + backend @with_guard for 7 polling surfaces

## Done when
- `withGuard(fn)` exported from `stores.js` with Vitest tests green
- `with_guard` Python decorator in `backend/shared/helpers/`
- MarketPulse, ChartWorkspace (×2), LogPanel, UnifiedLog, PriceChart, OptionChainTab all
  wrapped on frontend
- Background tasks with tight intervals wrapped with `@with_guard` on backend
- `_fetch_orders` has `@with_guard` as second valve alongside the 8s timeout
- svelte-check 0 errors, vitest 0 failures, pytest 0 failures
