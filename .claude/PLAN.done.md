# Plan: Chase restart recovery + create_task reference + mode guard + G1 defensive guard

## Task

Fix the remaining audit findings:

**P1 — Live chase tasks lost on server restart**: When the server restarts mid-chase,
the asyncio task is gone but the `AlgoOrder` row is stuck at `status=OPEN` with `engine=live`
and `next_attempt_at IS NOT NULL`. No recovery path exists (paper engine has
`recover_from_db()` at `background.py:4883`; live chase has nothing). Position stays
open with no re-quoting until manually intervened.

**P2 — `asyncio.create_task` fire-and-forget**: `orders_helpers.py:304` calls
`asyncio.create_task(chase_order(...))` with no reference stored. On shutdown the task
is silently GC'd or cancelled without cleanup.

**P3 — `chase_order` has no internal mode guard**: The live/prod gate lives only at the
ticket entry path (`_opp_live_check_mode_gates`). Any future caller invoking `chase_order`
directly (recovery, retry loop, background job) bypasses the gate.

**P3 — `_apply_live_g1_guard` undefended against None quantity**: In
`template_attach.py:1312–1339`, `int(leg.get("quantity"))` raises `TypeError` if the
leg dict has a None qty (malformed plan). No try/except — propagates as unhandled.

## Agents

- backend: Implement all four fixes below.

  **Fix 1 — `orders_helpers.py:304`: store task reference**
  Change `asyncio.create_task(chase_order(...))` to assign the task to a variable,
  add it to a module-level `_LIVE_CHASE_TASKS: set[asyncio.Task] = set()`, and use
  `task.add_done_callback(_LIVE_CHASE_TASKS.discard)` so the set auto-cleans on completion.

  **Fix 2 — `background.py`: add `recover_live_chases()` at startup**
  Write a new async function `recover_live_chases(session_factory)` that:
  1. Queries `AlgoOrder` for rows where `status='OPEN'` AND `engine='live'` AND
     `next_attempt_at IS NOT NULL` AND `updated_at < now() - 120s` (2-min grace period
     avoids recovering orders that just started).
  2. For each row, builds a `ChaseConfig` from `_chase_default_cfg()` plus `intent`
     from `row.intent` (if the field exists — use `getattr(row, "intent", None)`).
  3. Calls `asyncio.create_task(chase_order(algo_order_id=row.id, account=row.account,
     symbol=row.symbol, transaction_type=row.transaction_type, quantity=row.quantity,
     cfg=cfg))` — adding the task to `_LIVE_CHASE_TASKS` in `orders_helpers.py`.
  4. Logs `[CHASE-RECOVERY] restarting chase for order #{row.id} {row.symbol}`.
  Call `recover_live_chases()` in `on_startup()` in `background.py` at the same
  location as paper recovery (after line ~4877, before appending to `bg_tasks`).
  Import `recover_live_chases` from wherever it's defined (can live in `orders_helpers.py`
  or a new `backend/api/algo/chase_recovery.py`).

  **Fix 3 — `chase.py:chase_order`: add mode guard at top**
  At the very start of `chase_order` (before any broker calls), add:
  ```python
  from backend.api.algo.expiry import is_prod_branch
  if not is_prod_branch():
      logger.warning("[CHASE] chase_order called on non-prod branch — aborting")
      return ChaseResult(status=ChaseStatus.CANCELLED, reason="non-prod branch")
  ```
  This mirrors the guard at `background.py:996` for the expiry engine. Only applies
  when `RAMBOQ_BRANCH != prod` — dev server is safe; prod is unaffected.

  **Fix 4 — `template_attach.py:_apply_live_g1_guard`: defensive None guard**
  Wrap the `int(leg.get("quantity"))` call (around line 1325) in a `try/except TypeError`
  and skip the leg with a warning log rather than propagating an unhandled TypeError.

  After all fixes, run:
  `cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/ -q --tb=short -x 2>&1 | tail -20`

  Write one test in `backend/tests/test_audit_remediation.py`:
  `test_apply_live_g1_guard_skips_none_qty_leg` — call `_apply_live_g1_guard` with a
  plan whose leg has `quantity=None`; assert it returns None (no error raised) rather
  than raising TypeError.

- frontend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(chase): restart recovery + task reference + mode guard + G1 defensive None guard

Remaining P1/P2/P3 items from orders audit:
- chase recovery: recover_live_chases() at startup re-queues OPEN live chase orders
  after server restart (2-min grace period, default config)
- task reference: _LIVE_CHASE_TASKS set in orders_helpers; auto-discards on completion
- mode guard: chase_order refuses to run on non-prod branch (mirrors expiry engine guard)
- G1 defensive: _apply_live_g1_guard skips legs with None quantity (TypeError → warning)

## Done when

- `recover_live_chases()` called in `background.py:on_startup` and restarts OPEN live chases
- `asyncio.create_task` result stored in `_LIVE_CHASE_TASKS` set; cleaned up via callback
- `chase_order` guards against non-prod branch at function entry
- `_apply_live_g1_guard` does not raise on None qty leg
- `test_apply_live_g1_guard_skips_none_qty_leg` passes
- All pytest green
