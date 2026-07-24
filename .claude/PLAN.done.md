# Plan: Broker Layer Resilience — Token Lifecycle + Connection Robustness

## Context
Full-week log audit identified connection errors still generating noise (~6k+ asyncpg events/week, concurrent login storms, 502/503 mis-routing, reactive-only token refresh). Several items from the audit were already fixed in earlier sessions; this plan covers the remaining open issues.

**Already confirmed fixed (skip):**
- asyncpg `pool_pre_ping=True` — present in `database.py:43,91`
- historical_data 429 rate limiter — present in `kite.py:30-33`
- `audit.py` AttributeError `register` — no issue in current code

---

## Open Issues (prioritized)

### P1-A — Kite 502/503 misclassified as BrokerInputError (1 line)
**File:** `backend/brokers/adapters/kite.py:42`
```python
# Current (wrong):
"DataException": BrokerInputError,
# Fix:
"DataException": BrokerNetworkError,
```
Kite SDK raises `DataException` for HTTP 502/503. Mapping it to `BrokerInputError` suppresses retries. Groww already does this correctly. Change to `BrokerNetworkError` so `retry_kite_conn` retries on gateway errors.

### P1-B — Dhan concurrent login storm (no stagger)
**File:** `backend/shared/helpers/decorators.py:244`
The `@for_all_accounts` ThreadPoolExecutor fans out all Dhan account logins simultaneously. Dhan allows only 1 auth call per 300s per account (`broker_apis.py:78`). On restart with N Dhan accounts, N concurrent auth POSTs fire at once.

Fix: In `_per_account()` within `@for_all_accounts`, when broker type is `"dhan"`, sleep `2 * position_in_list` seconds before calling `get_broker(acc)`. This staggers Dhan logins 2s apart while keeping parallel fan-out for other brokers.

Alternative (simpler): Use a `threading.Semaphore(1)` around Dhan-specific `get_broker()` calls so they serialize automatically without a sleep.

### P1-C — Kite token TOCTOU: check outside lock, use after stale
**File:** `backend/brokers/connections.py:511-512, 1090-1091`
Two fast-path checks read `self.kite` / `self._dhan` without holding `self._login_lock`, then return the reference. If another thread refreshes the token inside the lock between the check and the return, callers get a stale handle that will fail with TokenException on the next API call.

Fix (both Kite and Dhan paths): Remove the fast-path return outside the lock. Use double-checked locking: acquire `self._login_lock`, re-check expiry, return the refreshed object. Since lock contention only happens ~once per day (at 6AM token refresh), the overhead is negligible.

### P2-A — Kite 6AM proactive token refresh (no pre-warm task)
**File:** `backend/api/background.py`
No background task refreshes Kite tokens before 6AM expiry. Token renewal happens only reactively (first failed request triggers TokenException → retry). This means the first Kite call after 6AM fails until the retry loop completes.

Fix: Add a new scheduled task `_task_token_refresh` that fires daily at 05:45 IST. It calls `get_kite_conn(test_conn=True)` for every Kite account. This triggers the token refresh before expiry so 06:00 AM requests succeed on the first try.

Scheduling pattern: copy `_task_market`'s daily-at-hhmm pattern (`background.py:340-354`). Hardcode `"05:45"` (no config knob needed — token expiry is a Kite API contract, not operator-tunable).

### P2-B — Dhan rate-limit cool-off lost on restart
**File:** `backend/brokers/broker_apis.py:286,407`
`_dhan_next_poll` is a module-level dict reset to `{}` on every restart. If Dhan rate-limited an account right before a crash/restart, the cool-off is forgotten and the restarted process immediately hammers again, hitting the same limit.

Fix: On every write to `_dhan_next_poll` (line 407), also write the `{account: expiry_epoch}` entry to a small JSON file in `/tmp/ramboq_dhan_cooloff.json`. On module load (line 286), read and merge the file entries that haven't expired yet. The file write is cheap (small dict, infrequent writes).

### P3 — Unprotected module-level dict writes
**File:** `backend/brokers/broker_apis.py:286,295,306`
`_dhan_next_poll`, `_dhan_poll_priority_cache`, `_breaker_optin_cache` are module-level dicts written from multiple threads. Python's GIL makes single-key dict assignments atomic, so this is not a corruption risk for simple reads/writes. However, the check-then-set patterns around them can see stale values between check and write under concurrent threads.

Fix: Wrap `_dhan_next_poll` reads/writes in a `threading.Lock()` (already one pattern needed for P2-B write path). For `_dhan_poll_priority_cache` and `_breaker_optin_cache`, the read-after-write race is harmless (worst case: stale priority for one poll cycle). No lock needed there — P3 is informational only.

---

## Agents

- backend: Implement P1-A (kite.py:42 one-line), P1-B (dhan stagger in decorators.py), P1-C (double-checked locking in connections.py:511-512 and 1090-1091), P2-A (5:45 AM token refresh task in background.py), P2-B (cooloff JSON persist in broker_apis.py:286,407). Skip P3 (GIL-safe, informational).
- frontend: skip
- broker: skip (all changes in backend/brokers/ + backend/api/ are within backend agent scope)
- doc: skip
- backend-test: Write pytest tests for:
  1. `kite.py` DataException → BrokerNetworkError mapping
  2. Dhan stagger: mock `@for_all_accounts` with 3 Dhan accounts, assert sequential delay ≥ 2s per account
  3. `_dhan_next_poll` cooloff persistence: write entry, simulate restart (reimport), assert entry loaded
  4. Kite token double-check: simulate concurrent `get_kite_conn()` calls, assert no stale handle returned
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(brokers): DataException→NetworkError, Dhan stagger, TOCTOU lock, 6AM pre-warm, cooloff persist

## Done when
- `DataException` maps to `BrokerNetworkError` in kite.py error map
- Dhan accounts stagger 2s apart on `@for_all_accounts` fan-out
- `get_kite_conn` and `get_dhan_conn` fast-path checks are inside `self._login_lock`
- `_task_token_refresh` fires daily at 05:45 IST calling `test_conn=True` for all Kite accounts
- `_dhan_next_poll` entries persist to `/tmp/ramboq_dhan_cooloff.json` and survive restart
- All new pytest tests green
