# Plan: Loss agents — day P&L metric migration

## Context
Loss alert agents (`loss-positions-acct`, `loss-positions-total`) currently check
`metric: "pnl"` (unrealized mark-to-market) and `metric: "pnl_pct"`. Operator
confirmed they should evaluate **day P&L** (`day_change_val`) instead — unrealized
P&L is not a meaningful loss signal (was −₹12.6L today while day P&L was +₹1.1L).

Rate-of-change metrics (`pnl_rate_abs`, `pnl_rate_pct`) also use unrealized P&L
history because `_update_pnl_history` stores `row.get('pnl', 0)` for positions rows.
Changing to `day_change_val` automatically fixes the rate agents.

Three synced changes: history storage → Python defaults → DB conditions.

## Task

1. **`_update_pnl_history`** (agent_engine.py ~line 91): change positions rows to store
   `day_change_val` / `day_change_percentage` instead of `pnl` / `pnl_percentage`.
   Rate metrics (`pnl_rate_abs`, `pnl_rate_pct`) read `field_idx=1` from this history —
   they will automatically measure day P&L velocity with no other changes.

2. **Python defaults** — `_LOSS_AGENTS` in agent_engine.py:
   - `loss-positions-acct`: remove `{"metric": "pnl", ...}`, remove `{"metric": "pnl_pct", ...}`,
     keep `{"metric": "day_val", ..., "value": -30000}`,
     add `{"metric": "day_pct", "scope": "positions.any_acct", "op": "<=", "value": -2.0}`
   - `loss-positions-total`: remove `{"metric": "pnl", ...}`, remove `{"metric": "pnl_pct", ...}`,
     keep `{"metric": "day_val", ..., "value": -50000}`,
     add `{"metric": "day_pct", "scope": "positions.total", "op": "<=", "value": -2.0}`,
     keep `pnl_rate_abs` and `pnl_rate_pct` (unchanged — now track day P&L rate via history fix)
   - `loss-rate-acct`: unchanged (rate conditions are fine; history fix flows through)

3. **DB conditions sync**: `_ae_sync_existing_builtin` (line ~1223) currently preserves
   conditions to allow operator tuning. For this migration we need a targeted override.
   Add a helper `_ae_should_reset_conditions(existing_cond, code_cond) -> bool`:
   - Returns True if `existing_cond` contains any leaf `{"metric": "pnl"}` or
     `{"metric": "pnl_pct"}` (i.e. stale metrics not yet migrated by operator).
   - Call it from `_ae_sync_existing_builtin`, and when True, force-set
     `existing.conditions = code_cond`.
   - This is safe: if an operator already removed `pnl` manually the check returns
     False and conditions are left alone.

## Agents
- backend: implement all three changes + helper + tests
- frontend: skip
- broker: skip
- doc: skip (PULSE_SPEC already covers loss agent logic; no new behaviour surface)
- backend-test: skip (bundled with backend agent)
- playwright: skip

## Files
- `backend/api/algo/agent_engine.py`
  - `_update_pnl_history` ~line 91: `row.get('pnl', 0)` → `row.get('day_change_val', 0)`
    and `row.get('pnl_percentage')` → `row.get('day_change_percentage')`
  - `_LOSS_AGENTS` ~line 794: remove `pnl` / `pnl_pct` leaves, add `day_pct` leaves
  - `_ae_sync_existing_builtin` ~line 1223: call `_ae_should_reset_conditions` and force-reset
  - New helper `_ae_should_reset_conditions(existing, desired) -> bool`
- `backend/tests/test_agent_engine_baseline.py`
  - Update `_update_pnl_history` tests to expect `day_change_val` in bucket
  - Add test: `_ae_should_reset_conditions` returns True when stale `pnl` leaf present
  - Add test: `seed_agents` force-resets conditions for stale loss agent (mock session)

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(alerts): loss agents evaluate day P&L not unrealized — migrate pnl→day_val in _update_pnl_history + _LOSS_AGENTS + DB condition sync

## Done when
- `_update_pnl_history` stores `day_change_val` for positions rows (confirmed by unit test)
- `loss-positions-acct` and `loss-positions-total` Python defaults contain `day_val`/`day_pct` and no `pnl`/`pnl_pct` leaves
- `_ae_should_reset_conditions` returns True for stale DB conditions containing `pnl` leaf
- On `seed_agents()` call, an existing agent with stale conditions gets force-updated
- All pytest green
