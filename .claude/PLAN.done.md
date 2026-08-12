# Plan: Remove dead imports + stale comment

## Context

Full stale/dead code audit ran across broker layer, API layer, frontend, and tests.
Overall the codebase is clean. Two actionable findings:

1. **Dead imports in two API route files** — symbols imported but never referenced
   in the file body. They accumulated when basket-order logic was split into
   `orders_basket.py` (the schema imports) and when HMAC postback signing was moved
   to `orders_postback.py` (hashlib/hmac). `is_authenticated_request` was imported
   as a guard candidate but the route uses `auth_or_demo_guard` instead. In
   `health.py`, `delete` was pulled in speculatively, `mask_account` was an
   unrealised refactor stub — neither is referenced.

2. **Stale comment in `base.py:58`** — references `_safe_call` which was removed in
   the `_DhanSDKProxy` refactor (commit ae006393). Should point to `_DhanSDKProxy`,
   `@_retry_groww_auth`, and `@retry_kite_conn` as the current dispatch paths.

One flagged item was a false positive: `rate_limiter.py:85-91` looks like
`sleep_time` could be unbound, but the `else: return` at line 88 exits the entire
method — line 91 is only ever reached via the `if refill_rate > 0` branch which
sets `sleep_time` first. Code is correct.

## Agents

- backend: Remove dead imports in `backend/api/routes/orders.py` and
  `backend/api/routes/health.py`. Exact changes:
  - `orders.py:20-21` — remove `import hashlib` and `import hmac`
  - `orders.py:33` — remove `is_authenticated_request` from the `auth_guard` import
    (keep `jwt_guard`, `auth_or_demo_guard`, `admin_guard`, `is_admin_request`)
  - `orders.py:39-42` — remove the 4-name basket-schema import block
    (`BasketGroup`, `BasketGroupResult`, `BasketLegResult`, `BasketMarginGroupResult`)
  - `health.py:17` — remove `delete` from the `from litestar import ...` line
    (keep `Controller`, `get`, `post`)
  - `health.py:34` — remove `mask_account` from the `from ... utils import ...` line
    (keep `config`)
  No new tests needed — import removal is validated by the existing test suite
  passing unchanged.
- broker: Update stale comment in `backend/brokers/base.py:58`.
  Change: `_last_req / _last_resp before / after each HTTP call so operator debugging
  doesn't require log-diving:` ... `Adapters update these dicts in their HTTP dispatch
  path (_safe_call, etc.)` → replace `(_safe_call, etc.)` with
  `(_DhanSDKProxy, @_retry_groww_auth, @retry_kite_conn)`.
- frontend: skip
- doc: skip
- backend-test: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
chore(cleanup): remove dead imports in orders.py + health.py; fix stale _safe_call comment in base.py

## Done when

- `from litestar import Controller, get, post` in health.py (no `delete`)
- `from backend.shared.helpers.utils import config` in health.py (no `mask_account`)
- No `hashlib`, `hmac`, `is_authenticated_request`, `BasketGroup*` in orders.py
- `base.py:58` references `_DhanSDKProxy` not `_safe_call`
- `pytest backend/tests/ -q` green

## Masking audit (demo→signin) — no action needed

Separate deep audit of demo mode → signed-in account masking transition:
- All data endpoints (`/api/orders/`, `/api/accounts/`, `/api/positions/`, `/api/holdings/`, `/api/funds/`)
  mask account codes via `is_admin_request()` for demo users ✓
- Cache poisoning bug ("demo→signin lag bug") was already fixed — orders.py uses
  `msgspec.structs.replace()` (copy-not-mutate) so the shared TTL cache keeps unmasked
  data while demo requests receive masked copies ✓
- Frontend `authStore.login()` wipes all `rbq.cache.*` localStorage keys on signin to
  prevent masked→unmasked visual flash ✓
- `is_authenticated_request()` accepts stale JWTs for masking decisions only; full
  `token_version` gate fires on next request → P3 theoretical only, no action needed ✓

**Conclusion**: Masking system is correct. No items added to this plan.

## Critical files

| File | Change |
|---|---|
| `backend/api/routes/orders.py:20-21,33,39-42` | Remove 7 dead imports |
| `backend/api/routes/health.py:17,34` | Remove `delete` and `mask_account` |
| `backend/brokers/base.py:58` | Update `_safe_call` → `_DhanSDKProxy`/`@_retry_groww_auth`/`@retry_kite_conn` |
