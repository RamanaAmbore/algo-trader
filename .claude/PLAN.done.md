# Plan: Auth retry in @for_all_accounts decorator + 0/5 chip false-zero fix

## Task
Three related fixes:

1. **Decorator-level auth retry** — move token renewal out of the three `_fetch_*_local`
   exception handlers into `@for_all_accounts._per_account`. One Kite/Dhan/Groww account
   failing auth triggers renewal + one retry for ONLY that account. Other accounts are
   unaffected (ThreadPoolExecutor isolation).

2. **0/5 chip false-zero** — with 2 Kite + 3 Dhan/Groww accounts, chip shows 0/5 (not 2/5)
   at 06:00 IST token expiry because `list_remote_accounts()` returns `[]` when UDS to
   conn_service is briefly unavailable (Kite renewal load spikes). `_loaded_accounts()`
   falls back to `set()` → all accounts show `loaded=False`. Fix: cache the last known
   account list in a module-level variable; serve from cache when `list_remote_accounts()`
   returns empty, so Dhan/Groww continue showing healthy.

3. **Empty-frame false-fail (existing rebuild helpers)** — `_rebuild_positions_after_renewal`
   / `_rebuild_holdings_after_renewal` call `_enrich_*` on empty DataFrames when there are
   no open positions/holdings → throws → returns None → `_record_fetch(ok=False)` falsely.
   Removed when decorator takes over; guarded in the interim.

4. **Position P&L correctness through retry path** — the current `_rebuild_positions_after_renewal`
   helper manually re-calls enrichment steps and can miss or mis-sequence them (e.g. calling
   `_enrich_positions` without `apply_day_change_backstop`). The decorator retry calls the
   FULL `_fetch_positions_local` function end-to-end (including all enrichment + backstop),
   guaranteeing P&L fields (`unrealised`, `day_change_val`, `pnl`) are always computed
   via the canonical path. Broker agent must ensure the retry in `_per_account` passes
   fresh `kite` + `broker` kwargs to the retried call (not the stale handles from the
   failed attempt). Test: after decorator retry, returned DataFrame contains correct
   non-null `unrealised` and `day_change_val` fields.

## Agents
- broker: (1) Create `backend/shared/helpers/auth_error.py` — extract `_is_auth_error_str`
  patterns into `is_auth_error_str(err: str) -> bool` with no broker imports.
  (2) Update `backend/shared/helpers/decorators.py` `for_all_accounts._per_account`:
  on auth error call `_try_renew(acc, connections)` (duck-typed: get_kite_conn /
  get_dhan_conn / refresh) then retry the function call once with fresh kite+broker
  handles. Raise on second failure so the per-function except block still handles it.
  (3) In `backend/brokers/broker_apis.py`: remove `_maybe_renew_on_auth_error` +
  `_rebuild_holdings_after_renewal` + `_rebuild_positions_after_renewal` +
  `_rebuild_margins_after_renewal`; remove the auth-retry blocks from the three
  `_fetch_*_local` exception handlers (three one-liners each become just
  `logger.error / _record_fetch(ok=False) / return df`). Import `is_auth_error_str`
  from the new shared module (still used by `_record_fetch` event-type logic).
  (4) In `backend/api/routes/brokers.py` `_loaded_accounts()`: add a module-level
  `_last_known_accounts: set[str] = set()` cache; after a successful `list_remote_accounts()`
  call update the cache; when `list_remote_accounts()` returns `[]`, serve from cache
  instead of returning `set()`.
- backend-test: Update `backend/tests/broker/test_token_renewal.py` — repoint tests
  at the decorator-level retry (mock `@for_all_accounts._per_account` raising an auth
  exception → verify `get_kite_conn(test_conn=True)` / `get_dhan_conn(test_conn=True)` /
  `.refresh()` is called and the function is retried). Remove tests for the removed
  per-function helpers. Add test: empty positions/holdings after renewal → no
  `_record_fetch(ok=False)` (verifies the old empty-frame bug is gone).
  Add test: `list_remote_accounts()` returns `[]` → `_loaded_accounts()` returns cached
  set, not empty set.
  Add test: decorator retry on auth error returns DataFrame with correct `unrealised` +
  `day_change_val` fields (not None, not empty) — verifies P&L enrichment runs on retry.
- doc: skip
- frontend: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
refactor(brokers): move auth-error retry into @for_all_accounts decorator

Extract is_auth_error_str to shared module; add _try_renew + one-shot
retry to for_all_accounts._per_account so token renewal covers every
broker fetch without per-function boilerplate. Removes _maybe_renew_on_auth_error
and three _rebuild_*_after_renewal helpers from broker_apis.py.
Fixes 0/5 chip false-zero: cache last known account list in _loaded_accounts()
so Dhan/Groww show healthy when UDS is briefly unavailable.
Fixes empty-frame false-fail: positions/holdings with 0 rows after
renewal no longer trigger _record_fetch(ok=False).

## Done when
- `venv/bin/python -m radon cc backend/ -s -n D` → no output (no D/E/F grades)
- pytest passes (broker coverage ≥ 80%)
- `_maybe_renew_on_auth_error` and `_rebuild_*_after_renewal` no longer exist in broker_apis.py
- `is_auth_error_str` exists in `backend/shared/helpers/auth_error.py`
- `@for_all_accounts` retries on auth error for Kite, Dhan, and Groww
- `_loaded_accounts()` serves from cache when `list_remote_accounts()` returns `[]`
- decorator retry produces DataFrame with non-null `unrealised` + `day_change_val` (P&L enrichment verified by test)
