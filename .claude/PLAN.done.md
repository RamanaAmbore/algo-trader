# Plan: Broker connection robustness — instruments cache, retry backoff, get_int crash, Groww SSL

## Context

Live server audit identified 4 distinct error patterns. In priority order:

1. **P0 — Agent engine crashes every ~5 min** (`name 'get_int' is not defined`)  
   `agent_engine.py:_cycle_outside_fire_at` uses `get_int` at line 1438 but never imports it. All lazy imports of `get_int` in that file are scoped to OTHER functions (`_v2_underlying_breakdown`, `_v2_cfg`). The agent engine scheduler calls `_cycle_outside_fire_at` on every cycle → NameError → entire agent loop dies and restarts every ~5 min. 31 occurrences in today's log.

2. **P1 — Kite `instruments` 429 storm on every restart/deploy**  
   `kite.py:instruments()` calls `self.kite.instruments(exchange)` with zero caching. Each preflight in `actions_preflight.py:_preflight_fetch_instruments` calls `broker.instruments(exchange)` independently. At service startup / after conn-service restart, all concurrent preflights fire in parallel → 5-10+ parallel downloads of the 7MB Kite instruments CSV → `NetworkException: Too many requests`. Seen in bursts at 07:25, 07:30, 07:34, 07:36, 07:42 UTC.

3. **P2 — `retry_kite_conn` retries immediately with no backoff**  
   `decorators.py:retry_kite_conn` loops with no `sleep` between attempts. On a 429, it fires the next retry instantly — making the rate-limit problem worse.

4. **P3 — Groww SSL EOF not retried**  
   `SSLEOFError: EOF occurred in violation of protocol` for `api.groww.in/v1/positions/user` at 07:15. urllib3 exhausted its own retries with no application-level recovery.

---

## Agents

- **backend**: Fix `agent_engine.py:_cycle_outside_fire_at` — add `from backend.shared.helpers.settings import get_int` as a lazy import inside the function (line 1434).

- **broker**: Three fixes in broker layer:
  1. **Instruments in-process cache** — `backend/brokers/adapters/kite.py`: Add per-`(account, exchange)` TTL cache (4h) + threading.Lock coalescing so concurrent callers await the first fetch rather than all firing in parallel. Use a module-level dict `_INSTR_CACHE: dict[str, tuple[float, list]]` keyed by `f"{account}:{exchange or ''}"`; `_INSTR_LOCK: threading.Lock()`. On cache miss, only the first thread fetches; others wait for the lock and then get the result.
  2. **Exponential backoff in `retry_kite_conn`** — `backend/shared/helpers/decorators.py`: After a failed attempt (not the last), add `time.sleep(min(2 ** attempt, 30))`. Special-case 429 ("Too many requests" in `str(e)`): sleep at least 30s before retry. Import `time` at the top of the file.
  3. **Groww SSL EOF retry** — `backend/brokers/adapters/groww.py`: In the `@_retry_groww_auth` decorator (or in `broker_apis.py` Groww fetch wrapper), add a catch for `requests.exceptions.SSLError` (or its urllib3 parent `urllib3.exceptions.SSLError`) with 1 retry after `time.sleep(2)` and a log line `[GROWW-SSL] SSL EOF on {method}; retrying after 2s`.

- **backend-test**: Write 3 tests in `backend/tests/broker/`:
  - `test_kite_instruments_cache`: mock `KiteBroker.kite.instruments`, verify second concurrent call returns cached result (not second HTTP call).
  - `test_retry_kite_conn_backoff`: mock `time.sleep`, verify it's called with exponential delays on retry.
  - `test_retry_kite_conn_429`: verify that when exception message contains "Too many requests", sleep ≥ 30s.

- **doc**: skip
- **playwright**: skip

---

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

---

## Commit message

fix(broker): instruments TTL cache + retry backoff + get_int crash + Groww SSL retry

---

## Done when

- Agent engine no longer logs `name 'get_int' is not defined` (P0 fix in agent_engine.py)
- `kite.py instruments()` returns cached result within 4h TTL; concurrent callers coalesce on the first fetch
- `retry_kite_conn` sleeps exponentially between attempts; 429 triggers ≥30s sleep
- Groww SSLError retried once after 2s with log
- All 3 new tests pass
