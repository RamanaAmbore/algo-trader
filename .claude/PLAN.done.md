# Plan: Broker Layer Robustness — Full Audit Fix Batch

## Context
Full re-audit of broker layer using 7-day prod logs + code review + official Kite/Dhan/Groww API docs. 
Previous session fixed: DataException→NetworkError, Dhan stagger, TOCTOU lock, 6AM pre-warm, cooloff persist.
This batch addresses active log errors (P0) and the remaining structural gaps found in this audit.

**API rate limits confirmed from docs:**
- Kite: quote 1/s · history 3/s · orders 10/s · all others 10/s
- Dhan: orders 10/s (250/min) · data 5/s (100K/day) · quotes 1/s · non-trading 20/s
- Groww: orders 10/s (250/min) · live-data 10/s (300/min) · non-trading 20/s (500/min)
- All three brokers: tokens expire at 6 AM IST daily (no refresh endpoint)

---

## P0 — Active prod errors (fix these first)

### P0-1: Dhan token rotation storm — "once every 2 minutes" (143 log hits)
**Files:** `backend/brokers/adapters/dhan.py`, `backend/brokers/connections.py`

Log evidence: `Dhan generate_token returned no accessToken: 'Token can be generated once every 2 minutes'` — accounts DH6847 and DH3747. Root cause: prod (`/opt/ramboq`) and dev (`/opt/ramboq_dev`) detect the same Dhan account token as stale simultaneously. Each process has its own `_cross_process_login_lock` path (under their respective /opt dirs), so both call `generate_token` within the same 2-minute window.

Fix:
1. Make the lock file path for `_cross_process_login_lock` system-wide (under `/tmp/ramboq_locks/`) rather than relative to the deployment dir, so prod and dev share the same lock for the same account.
2. After acquiring the cross-process lock in `_dhan_conn_under_lock`, re-read the persisted token from cache before calling `generate_token` — another process may have already refreshed it.
3. After a failed `generate_token` (2-min error), set `_login_blocked_until` to now+120s instead of the current 130s, matching Dhan's exact constraint.

### P0-2: MMAP-MISSING-SYM (103,077 warnings/week, continuous)
**Files:** `backend/brokers/` — wherever mmap token registration lives

Four specific instrument tokens never registered: 492033, 738561, 2939649, 3001089. These are queried every 60-90s but never found. Find these token IDs, identify why they're missing (expired instruments, wrong exchange segment, or stale subscription list), and either register them or suppress the warning with a known-absent cache so it fires once per token, not on every poll.

### P0-3: `get_int` NameError still firing (117 hits on Jul 24)
**Files:** `backend/api/background.py` or `backend/api/algo/agent_engine.py`

The previous fix added `from backend.shared.helpers.settings import get_int` inside `_cycle_outside_fire_at` in agent_engine.py. But logs show the error still appears on Jul 24 from `backend.api.background`. Grep `background.py` for any bare `get_int` usage without a local import and add the import there too.

### P0-4: AttributeError 'function' object has no attribute 'register' (3,407/week)
**Files:** `backend/api/audit.py` or ASGI middleware layer

3,407 occurrences wrapped in ExceptionGroup. The explore previously said "no issue in current code" at line 380 — but prod logs prove it's real. Likely triggered only in the SSE/streaming code path that wasn't exercised by the local check. Read the full streaming/SSE setup in audit.py; find where a function is used where a callable object with `.register()` is expected, and fix the type mismatch.

---

## P1 — Broker correctness gaps

### P1-1: Dhan 429 silent degradation (empty response instead of retry)
**File:** `backend/brokers/adapters/dhan.py:858-859`

When Dhan returns `{"status": "error", "code": "DH-904", "remarks": "Rate limit exceeded"}`, `_safe_call()` returns the dict as-is. The normaliser (`_normalise_holdings` etc.) returns `[]`, so broker_apis sees `ok=False` but no exception — no retry or backoff fires. Fix: In `_safe_call()`, check for `DH-904` in the response body and raise `BrokerRateLimitError` with the response content. This triggers the existing retry_kite_conn decorator (rename to `retry_broker_conn` or apply generically) with exponential backoff.

### P1-2: Dhan HTTP 5xx not mapped to BrokerNetworkError
**File:** `backend/brokers/adapters/dhan.py:54-59`

Only 4 Dhan error codes mapped. Any HTTP 502/503/504 from Dhan falls through to generic `BrokerError`. Add HTTP status-code check in `_safe_call()`: if response.status_code in (502, 503, 504), raise `BrokerNetworkError`. Mirrors the fix already done for Kite.

### P1-3: KiteTicker no re-subscription after reconnect
**File:** `backend/brokers/ticker/kite_ticker.py:917-962`

On WebSocket reconnect, `_on_connect` only re-subscribes tokens in `_pending` (added during the disconnect window). Tokens that were already subscribed before the disconnect are in `_subscribed` but not in `_pending`, so they're silently dropped. Fix: In `_on_connect`, after clearing pending, also re-subscribe all tokens in `_subscribed`. Chunk them at `KITE_TICKER_CHUNK_SIZE`.

### P1-4: Kite quote rate limit missing (1 req/s per Kite docs)
**File:** `backend/brokers/adapters/kite.py:30-33`

Kite enforces 1 req/s on the quote endpoint. The rate limiter has `"history": (3, 1.0)` and `"orders": (10, 1.0)` but no `"quote"` bucket. Add `"quote": (1, 1.0)` and call `_KITE_RATE_LIMITER.throttle("quote")` in `quote()`, `ltp()`, and `ohlc()` methods.

### P1-5: RemoteBroker wraps all errors in RuntimeError
**File:** `backend/brokers/client/remote_broker.py:75-110`

When conn_service returns a broker error, `_call()` raises `RuntimeError` regardless of the original error type. This bypasses BrokerError-specific handling (auth retry, circuit breaker). Fix: The conn_service response should include the original error class name. Parse the error payload — if it contains `"error_type": "BrokerAuthError"`, re-raise the appropriate BrokerError subclass. If not parseable, fall back to BrokerNetworkError (not RuntimeError) for connectivity issues.

---

## P2 — Resilience gaps

### P2-1: Circuit breaker state lost on restart
**File:** `backend/brokers/broker_apis.py:244-330`

`_CB` dict is in-memory. After a restart, breaker resets to CLOSED even if it was OPEN. Persist `circuit_open_until` to `/tmp/ramboq_cb_state.json` (same pattern as Dhan cooloff). On module load, merge non-expired entries. Use `_dhan_next_poll_lock` or a new `_cb_lock`.

### P2-2: File lock timeout — can hang forever
**File:** `backend/brokers/connections.py:94-120`

`fcntl.flock(fp.fileno(), fcntl.LOCK_EX)` has no timeout. A crashed process holding the lock blocks all subsequent logins indefinitely. Fix: Use `fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)` with a retry loop (poll every 0.5s, give up after 30s), then raise `BrokerNetworkError("login lock timeout — another process may be stuck")`. This is safe: if the holding process is dead, the OS will release the advisory lock anyway; the LOCK_NB approach just makes the timeout explicit.

### P2-3: Background task crash recovery
**File:** `backend/api/background.py` — all `_task_*` functions

Tasks can crash silently (bare `except Exception: pass` or unhandled exception exits the coroutine). Fix: Wrap each `_task_*` in a supervisor:
```python
async def _supervised(coro_fn, *, name, restart_delay=60):
    while True:
        try:
            await coro_fn()
        except Exception:
            logger.exception(f"[BG-CRASH] {name} crashed — restarting in {restart_delay}s")
            await asyncio.sleep(restart_delay)
```
Wire all `asyncio.create_task(_task_*())` through `_supervised`.

---

## P3 — Low priority / informational

- **asyncpg protocol state 3** (6,718/week): Python 3.13 compat issue. `pool_pre_ping=True` is present but doesn't fix this. Real fix: pin asyncpg to a compatible version or move to Python 3.12 on server. Not a code change — infra ticket.
- **Groww backoff reset after 429→401→retry**: `sleep_s` resets to 1.0 on re-mint (groww.py:154-173). Minor edge case — add `sleep_s` carry-over across the re-mint path.
- **Dhan WebSocket**: not implemented by Dhan (purely poll-based). No action needed.

---

## Agents

- backend: Fix P0-1 (Dhan rotation — shared lock path + double-check after lock), P0-3 (get_int in background.py), P1-1 (Dhan 429 raise BrokerRateLimitError), P1-2 (Dhan 5xx → BrokerNetworkError), P1-3 (KiteTicker re-subscribe on reconnect), P1-4 (Kite quote rate limit 1/s), P1-5 (RemoteBroker re-raise typed errors), P2-1 (CB state persist), P2-2 (file lock timeout), P2-3 (task supervisor wrapper)
- broker: Fix P0-2 (MMAP-MISSING-SYM — read mmap ticker code, identify the 4 tokens, add known-absent cache to suppress per-token once-only logging)
- frontend: skip
- backend-test: Tests for P0-1 (Dhan rotation lock shared path), P1-1 (Dhan 429 raises BrokerRateLimitError), P1-2 (Dhan 5xx → BrokerNetworkError), P1-4 (Kite quote throttle enforced), P2-1 (CB state survives reload), P2-3 (supervisor restarts crashed task)
- doc: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(brokers): Dhan rotation lock, MMAP suppress, Dhan 429/5xx errors, Kite quote throttle, CB persist, task supervisor

## Done when
- Dhan "once every 2 minutes" errors stop — shared lock prevents concurrent rotation
- MMAP-MISSING-SYM warnings fire once per token, not every 60s
- `get_int` NameError gone from background.py logs
- `AttributeError: 'function' has no attribute 'register'` root cause identified and fixed
- Dhan 429 response raises BrokerRateLimitError (verified by test)
- Dhan 502/503 raises BrokerNetworkError (verified by test)
- KiteTicker re-subscribes all prior tokens on reconnect
- Kite `quote()`/`ltp()` throttled at 1/s
- Circuit breaker OPEN state survives process restart
- File lock times out after 30s instead of hanging forever
- Background tasks restart automatically after crash
- All new and existing pytest tests green
