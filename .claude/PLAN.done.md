# Plan: Order / Chase / Template Audit Fix Sprint

## Context

Comprehensive audit of orders, chase engine, templates, and draft lifecycle surfaced 4 P1
defects, 6 P2 defects, and 5 P3 items. Prod log review added 2 more P1s confirmed firing
on every restart. This plan fixes all P1s and the highest-impact P2s.

---

## Audit findings

### P1 — Must-fix

| ID | File | Finding |
|---|---|---|
| A1 | `backend/api/background.py:4914-4917` | Chase recovery loop drops exchange+product: MCX chases restart with `exchange="NFO"`, `product="NRML"` → raw contracts, Kite rejects, perpetual loop |
| A2 | `backend/api/algo/chase.py:929-944` | `EXPIRED` order status treated as "still open" → new order placed after market close, 3× errors abort, AlgoOrder stuck OPEN forever |
| A3 | `frontend/src/routes/(algo)/orders/+page.svelte:231-247` | `_buildModifyProps` never copies `variety` from order row → AMO/CO cancel/modify rejected by Kite (`variety='regular'` hardcoded) |
| A4 | `backend/api/algo/template_attach.py:1409` | C2 market-hours guard in `apply_plan_live` blocks GTT-only templates off-hours, undoing C1's explicit allowance (C1 passes GTT-only; C2 blocks them) |
| A5 | `backend/api/background.py:4951` | `_task_broker_issue_daily` uses `get_session()` (async generator — HTTP DI only) with `async with` → startup catch-up AND nightly aggregation both silently fail; `broker_issue_daily` table never populated. **Confirmed in prod + dev logs on every deploy.** Fix: swap `get_session` → `async_session`. |
| A6 | `backend/api/background.py:5097` | `recover_live_chases()` called during lifespan startup before SQLAlchemy pool is ready → `DB query failed: Task got Future attached to a different loop`. Chase recovery silently aborts on every cold start. **Confirmed in prod log.** Fix: add `await asyncio.sleep(3)` before the call (allows pool to settle). |

### P2 — Should-fix (included in this plan)

| ID | File | Finding |
|---|---|---|
| B1 | `backend/api/algo/actions_live.py:250-258,408-419,746-757` | Three agent-action callers drop `algo_order_id` before calling `chase_order` → `_emit_chase_terminal` can't find row by stale `broker_order_id`, AlgoOrder stays OPEN, template-attach/auto-TP never fire |
| B2 | `frontend/src/lib/order/OrderTicket.svelte:2673-2684` | If `cancelOrder()` succeeds but `onSubmit()` throws, `onClose()` never called → modal stuck open with "Cancel failed" despite cancel succeeding |
| B3 | `frontend/src/lib/order/OrderTicket.svelte:1968-1978` | `initialDraftId` set but draft already removed from store → form submits as real order with no warning |
| B4 | `frontend/src/routes/(algo)/orders/+page.svelte:129` | `_openOrderCount` only matches `'TRIGGER PENDING'` (space), not `'TRIGGER_PENDING'` (underscore) → badge under-reports |
| B5 | `backend/api/routes/orders_place.py:400` vs `backend/api/routes/orders.py:271` | Key drift: `_opp_build_attach_entries` writes `"low_ltp"`, `_retry_build_gtt_entry` writes `"lowest_ltp"` → trail-stop readers silently miss depending on write path |

### P3 — Nice-to-fix (included)

| ID | File | Finding |
|---|---|---|
| C1 | `backend/api/algo/chase.py:204` | Template-attach failure in `_chase_terminal_fire_fill_hooks` logged at `debug` instead of `warning` |
| C2 | `backend/api/algo/template_attach.py:1718` | `bool(wing_strike_offset)` is False for offset=0 (ATM wing) → C1 won't block ATM wing at close, wing placement runs off-hours |
| C3 | `frontend/src/lib/order/OrderTicket.svelte:1310` | `_loadStrategies` error permanently sets `_strategiesLoaded=true` → empty picker, no retry, must hard-refresh |

---

## Agents

### broker: Fix A1, A2, A5, A6, B1, C1 (chase engine + recovery loop + startup)

File: `backend/api/algo/chase.py`, `backend/api/background.py`, `backend/api/algo/actions_live.py`

**A1 — Recovery loop drops exchange+product (`background.py:4914-4917`)**
In `recover_live_chases` (or wherever `_chase_default_cfg()` is called and overridden per row),
after constructing the default config, overwrite `cfg.exchange` and `cfg.product` from the
recovered DB row:
```python
cfg.exchange = row.exchange or cfg.exchange
cfg.product  = row.product  or cfg.product
```
Read the function to find the exact lines and apply the patch.

**A2 — EXPIRED status unhandled (`chase.py:929-944`)**
In `_chase_poll_status`, add `EXPIRED` alongside `CANCELLED` as a terminal non-filled state:
```python
if status in ('CANCELLED', 'EXPIRED'):
    return 'cancelled', 0
```
Adjust return type/value to match existing `CANCELLED` handling exactly — same outcome:
zero filled qty, terminal with no new order placed.

**B1 — Three callers drop algo_order_id (`actions_live.py`)**
Read lines 250-258, 408-419, 746-757 in `actions_live.py`. For each of the three callers of
`chase_order` that currently discard the AlgoOrder row id:
1. Capture the return value of `_write_live_order` / `_place_order_write_intent` into a local `_algo_order_id`
2. Pass it as `algo_order_id=_algo_order_id` to `chase_order()`
Match the signature of the working path in `orders_helpers.py:317` as the reference.

**A5 — `_task_broker_issue_daily` wrong session API (`background.py:4951`)**
`get_session()` in database.py is an async generator (yields, for use as Litestar DI dependency).
It cannot be used with `async with`. Change:
```python
# wrong
from backend.api.database import get_session
async with get_session() as session:
# correct (matches every other background task in this file)
from backend.api.database import async_session
async with async_session() as session:
```
Apply this fix in `_aggregate()` inside `_task_broker_issue_daily`.

**A6 — Chase recovery startup timing (`background.py:5097`)**
`recover_live_chases()` is called in `on_startup` before the SQLAlchemy async engine's
connection pool has fully bound to the running event loop. Insert a short delay:
```python
await asyncio.sleep(3)   # allow pool to settle before DB query
await recover_live_chases()
```
Add this immediately before line 5097.

**C1 — debug→warning in `_chase_terminal_fire_fill_hooks` (`chase.py:204`)**
Change `logger.debug(...)` to `logger.warning(..., exc_info=True)` for template-attach and
TP-arm failure paths inside `_chase_terminal_fire_fill_hooks`.

### backend: Fix A4, B5, C2 (template + key drift)

File: `backend/api/algo/template_attach.py`, `backend/api/routes/orders_place.py`,
`backend/api/routes/orders.py`

**A4 — C2 blocks GTT-only templates off-hours (`template_attach.py:1409`)**
In `apply_plan_live`, the market-hours guard was added as C2. It must be conditioned on
the plan actually having a wing (same logic as C1 in `apply_template_to_order`):
```python
if _template_has_wing(plan) and not _symbol_exchange_open(plan.exchange, now_ctx):
    return AttachResult(errors=["Exchange closed — wing order skipped"])
```
GTT-only plans (no wing) should pass through unchanged regardless of market hours.

**B5 — Key drift `low_ltp` vs `lowest_ltp`**
Read `_opp_build_attach_entries` in `orders_place.py` and `_retry_build_gtt_entry` in
`orders.py`. One writes `"low_ltp"`, the other `"lowest_ltp"`. Standardize to `"lowest_ltp"`
in BOTH places (grep all readers of `attached_gtts_json` to confirm which key is consumed
by trail-stop pollers, use that as the canonical key). Update any reader that uses the
non-canonical key.

**C2 — bool(0) ATM wing false-negative (`template_attach.py:1718`)**
Change `_template_has_wing` to check `wing_strike_offset is not None` instead of
`bool(wing_strike_offset)`:
```python
# wrong: bool(0) is False
has_wing = wing_strike_offset is not None
```
Read the function to see the exact expression and fix it in place.

### frontend: Fix A3, B2, B3, B4, C3 (order ticket + orders page)

File: `frontend/src/lib/order/OrderTicket.svelte`,
`frontend/src/routes/(algo)/orders/+page.svelte`

**A3 — `_buildModifyProps` missing variety (`orders/+page.svelte:231-247`)**
Read `_buildModifyProps`. Add `variety: o.variety || 'regular'` to the returned props object.
Then in `OrderTicket.svelte`, verify `_variety` is derived from the prefill props and passed
to both `modifyOrder()` and `cancelOrder()` calls. Both API calls already accept variety as
a parameter — just ensure it flows from the order row through to the API call.

**B2 — cancelOrder success + onSubmit throws → modal stuck (`OrderTicket.svelte:2673-2684`)**
Wrap the cancel button's click handler so `onClose()` is called even if `onSubmit` throws:
```js
try {
  await cancelOrder(orderId, _account, _variety);
  try { await onSubmit({ action: 'cancel', orderId }); } catch (_) {}
  onClose();
} catch (e) {
  submitErr = e?.message || 'Cancel failed';
} finally {
  submitting = false;
}
```

**B3 — initialDraftId set but draft removed → silent real order (`OrderTicket.svelte:1968-1978`)**
After mounting with `initialDraftId`, check whether the draft still exists in `payoffDrafts`:
```js
$effect(() => {
  if (initialDraftId && !payoffDrafts.value.has(initialDraftId)) {
    // Draft was removed externally — disengage draft mode silently and
    // clear the id so submit follows the normal path
    _draftMode = false;
    // do NOT submit — just let the form stay open for operator to decide
  }
});
```
The operator keeps the pre-filled values but no longer submits as a draft if the backing
entry is gone. Add a visible notice "Draft no longer exists — will place as live order if you submit."

**B4 — TRIGGER_PENDING badge mismatch (`orders/+page.svelte:129`)**
Change `_openOrderCount` derivation to match both spellings:
```js
const _openOrderCount = $derived(
  orders.filter(o =>
    o.status === 'OPEN' ||
    o.status === 'TRIGGER PENDING' ||
    o.status === 'TRIGGER_PENDING'
  ).length
);
```

**C3 — _loadStrategies error suppresses retry (`OrderTicket.svelte:1310`)**
In the catch block, do NOT set `_strategiesLoaded = true` on failure. Instead:
- Set an `_strategiesErr` flag and show a small "Reload" link in the strategy picker
- Keep `_strategiesLoaded = false` so a retry (button click) re-enters `_loadStrategies`

### backend-test: Tests for A1, A2, A4, B1, B5

File: `backend/tests/broker/test_chase_*.py`, `backend/tests/test_template_attach.py`

Write pytest tests (using existing fixtures/mocks in the test files):

1. **A1**: Simulate `recover_live_chases` with an MCX row (`exchange='MCX'`, `product='MIS'`). Assert the recovered `ChaseConfig` has `exchange='MCX'`, not `'NFO'`.

2. **A2**: Mock `_chase_poll_status` to return `status='EXPIRED'`. Assert the chase loop terminates cleanly (not `_emit_chase_terminal` with error), filled_qty=0, no subsequent order placement.

3. **A4**: Create a GTT-only plan (no wing). Mock `_symbol_exchange_open` to return False. Call `apply_plan_live`. Assert it succeeds (not blocked). Repeat with a wing plan — assert it IS blocked.

4. **B1**: For one of the three fixed callers, mock `_write_live_order` to return a known id. Assert `chase_order` is called with that `algo_order_id`.

5. **B5**: Assert `_opp_build_attach_entries` and `_retry_build_gtt_entry` both write the same key name (`lowest_ltp`). Read both outputs and compare key names.

---

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message

fix(orders): audit sprint — EXPIRED chase, MCX recovery exchange/startup, broker-daily session fix, GTT-only off-hours, variety pass-through, algo_order_id propagation

## Done when

- Chase loop terminates cleanly on EXPIRED status (no new order placed)
- MCX chase recovery uses correct exchange+product from DB row
- AMO/CO modify/cancel sends correct variety string
- GTT-only template attach succeeds off-hours (C2 no longer blocks it)
- Three agent-action callers thread algo_order_id through to chase_order
- cancelOrder success always closes modal even if onSubmit callback throws
- initialDraftId with missing draft shows notice instead of silent live order
- TRIGGER_PENDING badge count matches pending list length
- key drift (low_ltp → lowest_ltp) unified
- `_task_broker_issue_daily` no longer errors on startup (uses `async_session`)
- Chase recovery no longer fails with "different loop" on cold start
- svelte-check 0 errors, pytest green
