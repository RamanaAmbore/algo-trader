# Plan: On-demand token renewal on auth failure (all brokers)

## Task

Two complementary fixes to ensure broker tokens are always valid:

**Fix 1 — On-demand renewal**: When any broker fetch call (`_fetch_margins_local`,
`_fetch_holdings_local`, `_fetch_positions_local`) catches an exception and
`_is_auth_error_str(str(exc))` is True, immediately call the connection's re-auth
method and retry the call once. Applies to all accounts: Kite, Dhan, Groww.

Add `_maybe_renew_on_auth_error(account)` in `broker_apis.py`:
- Gets `conn = Connections().conn.get(account)`
- If `KiteConnection`: calls `conn.get_kite_conn(test_conn=True)` 
- If `DhanConnection`: calls `conn.get_dhan_conn(test_conn=True)`
- If `GrowwConnection`: calls `conn.refresh()` (existing method)
- Logs `[TOKEN-RENEW]` at INFO on attempt
- Returns True if renewal was attempted, False otherwise

The three `_fetch_*_local()` functions each wrap their main broker call in a
try/except that already exists (line ~2515 in broker_apis.py). Add renewal + single
retry in each:
```python
except Exception as e:
    if _is_auth_error_str(str(e)) and _maybe_renew_on_auth_error(account):
        try:                        # one retry after renewal
            df_margins = pd.DataFrame([broker.margins(segment="equity")])
            ...  # same flatten + account assignment as happy path
            _record_fetch(account, ok=True)
            return df_margins
        except Exception as e2:
            logger.error(f"[{account}] margins retry after renewal also failed: {e2}")
    logger.error(...)
    _record_fetch(account, ok=False, ...)
```

**Fix 2 — Proactive pre-warm polling cadence**: `service/app.py:_task_prewarm_tokens`
sleeps 3600s between checks. The Kite window is 05:45–05:59 IST (14 min). An hourly
cycle statistically misses this window. Change sleep to 60s — matching
`background.py:_task_token_refresh` — so the window is reliably hit.

**Fix 3 — Connection badge diagnostic logging**: `_loaded_accounts()` in
`backend/api/routes/brokers.py` swallows all exceptions with bare
`except Exception: return set()`. Add `logger.warning` so UDS failures leave a trace.

## Agents

- broker: Three changes in `backend/brokers/broker_apis.py` and
  `backend/brokers/service/app.py` and `backend/api/routes/brokers.py`:

  **broker_apis.py**:
  - Add `_maybe_renew_on_auth_error(account: str) -> bool` function after
    `_is_auth_error_str()` (~line 68). Uses `Connections().conn.get(account)` and
    dispatches to `get_kite_conn(test_conn=True)` / `get_dhan_conn(test_conn=True)` /
    `conn.refresh()` based on connection type. Logs `[TOKEN-RENEW] {account}: renewal
    attempted` at INFO. Catches and logs exceptions from the renewal itself.
  - In `_fetch_margins_local()` exception handler (~line 2515): if auth error detected,
    call `_maybe_renew_on_auth_error(account)` and retry the full broker call + flatten
    once before falling through to the error path.
  - Same pattern in `_fetch_holdings_local()` and `_fetch_positions_local()`.

  **service/app.py** line 714: change `await _asyncio.sleep(3600)` to
  `await _asyncio.sleep(60)`. Add comment: "# 60s — matches background.py
  _task_token_refresh cadence; hourly polling statistically misses the 05:45–05:59
  Kite window".

  **backend/api/routes/brokers.py** `_loaded_accounts()` (lines 277–290):
  - Replace `except Exception: return set()` with:
    `except Exception as e: logger.warning("_loaded_accounts failed: %s", e); return set()`
  - After `list_remote_accounts()` returns empty: log
    `logger.warning("_loaded_accounts: conn_service returned no accounts")`.

- frontend: skip
- backend: skip
- doc: skip
- backend-test: New file `backend/tests/broker/test_token_renewal.py`:
  - Test 1: `_maybe_renew_on_auth_error` calls `get_kite_conn(test_conn=True)` on
    KiteConnection when called (mock Connections().conn, assert call made)
  - Test 2: same for DhanConnection → `get_dhan_conn(test_conn=True)`
  - Test 3: same for GrowwConnection → `conn.refresh()`
  - Test 4: `_fetch_margins_local` retries after auth error and returns data on
    second attempt (first call raises 401-string exception, second succeeds)
  - Test 5: `_task_prewarm_tokens` sleep is 60s — grep `service/app.py` body for
    `sleep(3600)` inside the function, assert NOT found; assert `sleep(60)` IS found
  - Test 6: `_loaded_accounts()` emits WARNING log when `list_remote_accounts()`
    raises
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(brokers): on-demand token renewal on auth failure for all brokers + prewarm 60s cadence

## Done when

- Auth failure on any broker call (Kite/Dhan/Groww) triggers immediate re-auth + one
  retry before marking the account failed
- `_task_prewarm_tokens` uses 60s sleep (reliable 05:45 window coverage)
- `_loaded_accounts()` logs warnings on conn_service failures (0/5 diagnosable)
- All 6 new tests pass; existing broker tests unaffected
