# Plan: fix(orders): close 5 of 7 ORDER_LIFECYCLE gaps — watchdog, cancellation alert, placement events, agent trigger

## Context

Seven gaps were identified in the ORDER_LIFECYCLE.md audit. Exploration confirms 5 are fixable
in a single backend pass; 2 are deferred:

| Gap | Severity | Action |
|---|---|---|
| GAP-1: No auto-reconcile for non-chased OPEN orders | CRITICAL | **Fix — new background watchdog** |
| GAP-2: GTT detection lag 30s | HIGH | **Fix — OCO default interval 15s→5s (one-liner)** |
| GAP-3: No alert on order cancellation | Medium | **Fix — add alert in postback pipeline** |
| GAP-4: Frontend 15s polling lag | Medium | **Deferred** — `order_update` WS already triggers `_debouncedLoadOrders()` which hits fresh data (cache invalidated before broadcast); real-time per-row patch is low priority |
| GAP-5: No "placed" event for live orders | Medium | **Fix — add `write_event("placed")` in live success path** |
| GAP-6: Agent order not visible in event log | Medium | **Fix — add `write_event("agent_trigger")` in `_write_live_order`** |
| GAP-7: Failed GTT cancel — no retry | Low | **Deferred** — cancel_gtt only appears in `_oco_cancel_survivor` (OCO sibling path); no call site found in orders_place.py or template_attach.py; needs separate investigation |

## Task

Five backend changes:

**A — GAP-1: `_task_open_order_watchdog` in `background.py`**  
New supervised background task. Every 5 minutes (configurable via `get_int("orders.open_order_watchdog_seconds", 300)`):
1. Query all `AlgoOrder` rows where `status="OPEN"` and `mode="live"` and `created_at < now() - 5min`.
2. Group by account.
3. For each account, call existing `_rco_reconcile_account(acct, rows, attach_queue)` (`orders.py:939`).
4. After loop, fire template-attach for any fills in `attach_queue`.
5. Register in `on_startup()` same pattern as `_task_oco_pair_watcher` (lines 5501-5533):
   `asyncio.create_task(_supervised(_task_open_order_watchdog, name="bg-open-order-watchdog"))`

**B — GAP-2: OCO interval 15s→5s**  
In `background.py` around line 2963, the interval is:
`max(5, get_int("templates.oco_pair_poll_seconds", 15))`  
Change default from `15` to `5` in the call.

**C — GAP-3: Cancellation alert**  
In `orders_postback.py` — in the caller of `_sync_apply_row_status` (the function that processes
each row after status sync), add: when `new_status == "CANCELLED"`, fire a brief ntfy alert
with account, symbol, side, qty, and "Order cancelled by broker". Use `send_ntfy_alert()`
from `alert_utils`. The alert should NOT fire for operator-initiated cancels (already covered by
the cancel route) — check `source` or use a flag param.

Alternatively: in `_postback_broadcast_fanout` (`orders.py:529`), after the `order_update` WS
broadcast, add: `if str(status).upper() == "CANCELLED": await _opp_send_cancel_alert(...)`.
This is cleaner — `_postback_broadcast_fanout` already has account/symbol/status.

**D — GAP-5: Live "placed" event**  
In `_opp_live_handle_success` (`orders_place.py:1674`) — currently paper path calls
`_opp_paper_write_placed_event()` but live path does not call `write_event`. Add a
fire-and-forget call to `write_event(algo_order_id, "placed", f"live {mode} {side} {qty} {symbol}")`.
The `_live_algo_id` is available in the caller scope.

**E — GAP-6: Agent trigger event in order log**  
In `_write_live_order` (`actions.py:484`) — after writing the `AlgoOrder` row (line 526),
add a fire-and-forget call:
`asyncio.create_task(write_event(row_id, "agent_trigger", f"{agent.slug}: {action_type}"))`
Also add `"agent_trigger"` to `VALID_KINDS` in `order_events.py:40`.

**Also update `docs/audits/ORDER_LIFECYCLE.md`** — mark GAP-1 through GAP-3, GAP-5, GAP-6 as
"Fixed in commit X"; add note on GAP-4 deferral; add note on GAP-7 pending investigation.

## Agents

- backend: Make all five code changes (A–E):
    (A) Add `_task_open_order_watchdog` in `backend/api/background.py`. Import or call existing
    `_rco_reconcile_account` from `backend/api/routes/orders.py`. The watchdog queries OPEN+live
    rows older than 5min via raw SQL or ORM, groups by account, calls `_rco_reconcile_account`
    per account, fires template-attach for fills. Register in `on_startup()` with `_supervised`.
    (B) In `background.py` at the OCO pair-watcher interval line (~2963): change default from 15
    to 5 → `max(5, get_int("templates.oco_pair_poll_seconds", 5))`.
    (C) In `backend/api/routes/orders.py` inside `_postback_broadcast_fanout` (~line 570): after
    `order_update` broadcast, add: when `str(status).upper() == "CANCELLED"`, call
    `asyncio.create_task(_opp_send_cancel_alert(account, symbol, side, qty, status_message))`.
    Define `_opp_send_cancel_alert` as a small async helper that calls `send_ntfy_alert` from
    `alert_utils`. This avoids touching `_sync_apply_row_status`.
    (D) In `backend/api/routes/orders_place.py` inside `_opp_live_handle_success` (~line 1674):
    add fire-and-forget `write_event(algo_order_id, "placed", f"live {mode} ...")`. Read the
    function signature to find where `algo_order_id` is available (it's a param or local var).
    (E) In `backend/api/algo/actions.py` inside `_write_live_order` (~line 526, after row insert):
    add `asyncio.create_task(write_event(row_id, "agent_trigger", f"{getattr(agent,'slug','?')}: {action_type}"))`.
    In `backend/api/algo/order_events.py` VALID_KINDS (~line 40): add `"agent_trigger"`.

- doc: Update `docs/audits/ORDER_LIFECYCLE.md` — in §12 Gap Analysis, mark each gap status:
    - GAP-1: ✅ Fixed — `_task_open_order_watchdog` added, runs every 5min
    - GAP-2: ✅ Fixed — OCO watcher default interval changed to 5s
    - GAP-3: ✅ Fixed — `_postback_broadcast_fanout` sends cancel alert on CANCELLED postback
    - GAP-4: ⏸ Deferred — `order_update` WS already triggers cache-busted reload (250ms lag); per-row patch not yet implemented
    - GAP-5: ✅ Fixed — `write_event("placed")` added to live order success path
    - GAP-6: ✅ Fixed — `write_event("agent_trigger")` added in `_write_live_order`; "agent_trigger" added to VALID_KINDS
    - GAP-7: ⏸ Deferred — cancel_gtt call site not found in order placement path; under investigation

- backend-test: Write tests:
    (1) `test_open_order_watchdog_reconciles_stale_open` — mock DB returning one OPEN+live row >5min
    old; verify `_rco_reconcile_account` is called.
    (2) `test_postback_cancel_alert_fires` — mock `_postback_broadcast_fanout` with status=CANCELLED;
    verify `_opp_send_cancel_alert` task is created.
    (3) `test_live_placed_event_written` — mock `_opp_live_handle_success`; verify
    `write_event("placed", ...)` is called fire-and-forget.
    (4) `test_agent_trigger_event_written` — mock `_write_live_order`; verify
    `write_event("agent_trigger", ...)` is called after row insert.
    (5) `test_agent_trigger_in_valid_kinds` — import VALID_KINDS from order_events; assert
    "agent_trigger" in VALID_KINDS.

- broker: skip
- frontend: skip
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(orders): GAP-1 watchdog, GAP-2 OCO 5s, GAP-3 cancel alert, GAP-5 placed event, GAP-6 agent_trigger

## Done when

- `_task_open_order_watchdog` registered in `on_startup`, calls `_rco_reconcile_account` for stale OPEN rows
- OCO watcher default interval is 5s
- `_postback_broadcast_fanout` fires cancel alert when status=CANCELLED
- `write_event("placed")` called in live order success path
- `write_event("agent_trigger")` called in `_write_live_order`; "agent_trigger" in VALID_KINDS
- All 5 pytest tests pass
- ORDER_LIFECYCLE.md gap statuses updated
