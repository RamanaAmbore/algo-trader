# Plan: Revert cold-boot Dhan exclusion in connections.py

## Context

The previous commit changed `_rebuild_from_yaml` to exclude Dhan/Groww accounts from
`self.conn` during cold-boot, assuming `rebuild_from_db()` would always repopulate them.
That assumption is wrong: if `rebuild_from_db()` fails (silently caught exception in
`_rebuild_broker_connections`) or falls back to the YAML view (empty DB table), Dhan/Groww
are permanently absent from `self.conn` and `@for_all_accounts` never iterates them.

Before the commit: Dhan was in `self.conn` as a KiteConnection stub. When `rebuild_from_db()`
overwrote it with a real DhanConnection, holdings showed correctly. When `rebuild_from_db()`
fell back to YAML, the stub was still in the iteration (failing quietly but present).
After the commit: if `rebuild_from_db()` falls back, Dhan is gone entirely — permanent
zero holdings on the public performance page.

## Task

Revert the cold-boot filter at `connections.py:1542-1547`. Restore all accounts
(including Dhan/Groww) to `self.conn` as `KiteConnection` stubs during cold-boot.
`rebuild_from_db()` remains the authoritative path that overwrites stubs with real
DhanConnection/GrowwConnection objects — that logic is unchanged and correct.
Update the docstring to remove the now-incorrect claim.

## Agents

- backend: skip
- frontend: skip
- broker: Revert `_rebuild_from_yaml` in `backend/brokers/connections.py`.

  **Exact change** — replace lines 1542-1547:
  ```python
  # BEFORE (broken — excludes Dhan/Groww from cold-boot YAML view)
  _KITE_BROKER_IDS = {"zerodha_kite", "kite", ""}
  self.conn = {
      account: KiteConnection(account, secrets)
      for account, blob in accts.items()
      if str(blob.get("broker") or "").lower() in _KITE_BROKER_IDS
  }
  ```
  ```python
  # AFTER (correct — all accounts in YAML view; rebuild_from_db() overwrites with real adapters)
  self.conn = {
      account: KiteConnection(account, secrets)
      for account in accts.keys()
  }
  ```
  Also update the docstring (lines 1526-1540) to remove the claim that Dhan/Groww are
  intentionally excluded. The correct docstring: used as initial sync seed and fallback;
  `rebuild_from_db()` overwrites with the correct adapter per broker type.

  Update `backend/tests/broker/test_cold_boot_broker_type.py` to match:
  - `test_dhan_account_absent_from_conn` → flip assertion: Dhan account IS in `conn` at
    cold-boot (as a KiteConnection stub)
  - `test_groww_account_absent_from_conn` → same flip
  - `test_only_kite_accounts_in_conn` → remove or flip: cold-boot conn contains all accounts

  Keep the `_broker_id_map`, `_priority_map`, `_hist_enabled_map` tests — those are still
  correct. Keep `TestHoldingsErrorString` — that fix is still valid.

- doc: skip
- backend-test: skip (broker agent updates the affected tests)
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(broker): revert cold-boot Dhan exclusion — restore KiteConnection stubs for all accounts in YAML view

## Done when
- `_rebuild_from_yaml` includes all accounts in `self.conn` regardless of broker type
- Existing `test_cold_boot_broker_type.py` assertions updated to match
- pytest passing
