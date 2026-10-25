# Plan: Alias numeric broker client IDs (e.g. Dhan) into the existing mask registry

## Context

The rule the operator just described — "first two characters by default; if two accounts
collide on those, fall back to first-letter + ordinal (D1, D2, ...)" — is **not new work**.
It's already the exact, already-implemented algorithm in `register_accounts()`
(`backend/shared/helpers/utils.py:243`), whose own docstring literally quotes a past
instruction verbatim: *"for dhan the first 2 chars should show as D1 and D2"*. Confirmed live
on prod (read-only, no raw values printed) that the two real Dhan accounts' internal codes
(`DH3747`/`DH6847`) already collide on `DH` and already mask to `D1####`/`D2####` correctly
today.

The actual, narrower gap: the raw NUMERIC Dhan `client_id` (the broker-API-level identifier —
e.g. the value that shows up in postback payloads and some log lines, distinct from the
internal `DH3747`/`DH6847` display codes) is never fed into `register_accounts()` at all. It
falls through to the separate `_scalar_mask()` fallback, which replaces every digit with `#`
— and since the client_id is entirely digits, that produces a useless all-`#` string with no
distinguishing information at all. Also confirmed live: both real Dhan client_ids are the
same length and share the same first 2 characters (differing only from the 3rd character
onward) — so a plain "first 2 chars" rule applied directly to the numeric value alone would
ALSO collide and fail to distinguish them, same failure class as today's bug.

**Resolution, per the operator:** don't derive a fresh mask from the numeric value's own
characters at all. Alias the client_id to the SAME masked string already computed for its
paired account code — so the numeric ID for the account that masks as `D1####` shows as
`D1####` too, not an independently-derived value. This is both simpler and more useful
operationally: any log line showing `D1####` — whether it came from the internal code or the
raw numeric ID — unambiguously means the same real account.

## Approach

`register_accounts(accounts)` currently takes a flat list of account codes. Extend it (or add
a thin companion call) to also accept each account's associated broker `client_id` (sourced
from `BrokerAccount.client_id`, already a real column — `backend/api/models.py:1904`), and
for every account that has one, add an ALIAS entry to the same `_REGISTRY` dict:
`new_map[client_id] = new_map[account_code]` — reusing whatever mask was already computed for
that account by the existing collision-aware algorithm, unchanged.

Both call sites in `backend/brokers/connections.py` (`rebuild_from_db()`, ~line 1840 and
~line 2102) need to pass the client_id mapping alongside the account list — check what's
already available at each call site (the `Connections`/adapter state there should already
have loaded `client_id` from the same `BrokerAccount` rows that produced the account list;
thread it through rather than re-querying).

No change needed to `_scalar_mask()`, `mask_account()`'s dispatch logic, or
`mask_account_in_text()` — once the numeric client_id is a real `_REGISTRY` key, `mask_account()`
finds it on the first lookup (`if s in _REGISTRY: return _REGISTRY[s]`) and the fallback path
is never reached for this value again.

**Re-examine Wave 1's `_pb_redact_payload_for_log()` (`orders.py`)** — it currently DROPS the
`dhanClientId` key entirely from logged payload dumps rather than masking it (a reasonable
choice at the time, since masking it produced a useless all-hash string). Now that aliasing
makes the mask useful, switch it to mask-in-place (`mask_account(value)`) instead of dropping
the key, so the payload dump stays structurally complete while the value is safely masked.

## Files

- `backend/shared/helpers/utils.py` (`register_accounts()` — accept + apply the client_id
  alias mapping; docstring update).
- `backend/brokers/connections.py` (both `register_accounts(...)` call sites — thread
  client_id through).
- `backend/api/routes/orders.py` (`_pb_redact_payload_for_log` — mask instead of drop).

## Tests

- `register_accounts()` with a client_id mapping produces the alias entries; a client_id
  for an account with no collision gets that account's own first-2-char mask; a client_id
  for a colliding account (the real Dhan case) gets that account's ordinal mask — assert
  against `mask_account(account_code)`'s own computed value at runtime, never a hardcoded
  literal (module-global `_REGISTRY` state can shift the exact letter/ordinal depending on
  what else is registered in a given test run).
- Regression: existing alphanumeric masking (unique and colliding cases) is byte-for-byte
  unchanged.
- `_pb_redact_payload_for_log()` now masks `dhanClientId` in place rather than removing the
  key; update/extend Wave 1's own test for this function.
- Broker-layer changes go in `backend/tests/broker/test_*.py`.

## Commit message (draft)

`fix(masking): alias each account's numeric broker client_id to its already-computed mask, so Dhan's raw client ids are distinguishable instead of an all-hash string`

## Done when

- The two real Dhan numeric client_ids mask to the same, already-distinguishable strings as
  their paired account codes (confirmed live, no raw values printed).
- All existing alphanumeric masking behavior is unchanged.
- Full backend suite + CC gate green.
