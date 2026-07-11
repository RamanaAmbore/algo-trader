# GTT Book + Template Attachment Specification

Good-to-till-cancel orders (GTTs) are profit-taking and stop-loss triggers that live on the
broker until a market condition is crossed. Template attachment turns an order template
(take-profit %, stop-loss %, wing hedge) into a concrete GTT plan, then executes it in
sim or live mode. The plan is preview-able before commit, so the operator sees exactly
which orders will be placed.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/api/algo/sim/gtt_book.py` · `backend/api/algo/template_attach.py` ·
`backend/api/routes/templates.py` · `backend/api/models.py`

---

## Contents

1. [SimGttBook Registry](#1-simgttbook-registry)
2. [GTT Lifecycle and Statuses](#2-gtt-lifecycle-and-statuses)
3. [Trigger Crossing and Event Recording](#3-trigger-crossing-and-event-recording)
4. [TemplatePlan and Resolution](#4-templateplan-and-resolution)
5. [Qty Translation and G1 Guard](#5-qty-translation-and-g1-guard)
6. [Apply Path — Sim and Live](#6-apply-path--sim-and-live)
7. [OCO Pair-Watcher and Trail Stops](#7-oco-pair-watcher-and-trail-stops)
8. [Edge Cases](#8-edge-cases)
9. [Test Coverage Map](#9-test-coverage-map)

---

## 1. SimGttBook Registry

`SimGttBook` is an in-memory registry of all outstanding GTTs in simulation mode.

**Core operations**:

- **`place(trigger_type, trigger_values, orders, label="")`** — Register a new GTT.
  Returns the assigned `gtt_id` (string UUID or broker-assigned). Trigger and orders
  are stored together; per-index correspondence is maintained (trigger[i] → order[i]).

- **`cancel(gtt_id)`** — Transition a GTT from ACTIVE to CANCELLED. No-op if already
  TRIGGERED or CANCELLED (idempotent). Records cancellation event via `_on_record` hook.

- **`get(gtt_id)`** — Retrieve a GTT by id, or None if not found.

- **`all_()`** — Return all GTTs (across all statuses).

- **`reset()`** — Clear all GTTs (used at session boundary or mode transition).

- **`check_triggers(prices: dict[str, float])`** — Per-tick crossing check. For each
  ACTIVE GTT, test whether any trigger value has crossed the current LTP from the
  `prices` dict. On crossing, fan out the corresponding orders and transition status
  to TRIGGERED. Records gtt_triggered event per crossing via `_on_record` hook.

---

## 2. GTT Lifecycle and Statuses

Every GTT transitions through a state machine: ACTIVE → (TRIGGERED | CANCELLED).

| Status | Meaning | Next | Trigger |
|---|---|---|---|
| ACTIVE | Waiting for market condition | TRIGGERED, CANCELLED | Just placed |
| TRIGGERED | Market condition crossed; orders fanned out | (terminal) | Crossing detected |
| CANCELLED | Manually cancelled before trigger | (terminal) | Operator or auto-cancel |

**Event recording**: Place, trigger, and cancel events are recorded immediately via
the `_on_record(event_type, gtt_obj)` hook. In production (SimDriver-backed simulator),
the hook is wired to the driver's event recorder so every GTT event lands in the
`algo_order_events` table with full context (gtt_id, trigger_values, orders, status).

---

## 3. Trigger Crossing and Event Recording

`check_triggers(prices)` iterates all ACTIVE GTTs and tests for market crossings.

**Crossing logic**:
- Single-trigger: compare current LTP to trigger value. Cross when LTP crosses threshold.
- Two-leg OCO: test TP (upper) and SL (lower) independently. Cross the first one to hit.

**Order fan-out**:
When a trigger crosses, the corresponding order (or orders for OCO) are immediately
placed via the paper engine (sim) or broker API (live). The GTT status is transitioned
to TRIGGERED. An event record is emitted with the full context:
```json
{
  "event_type": "gtt_triggered",
  "gtt_id": "...",
  "trigger_values": [...],
  "orders": [...],
  "trigger_leg": 0 or 1,
  "crossing_price": 123.45
}
```

**Replay and idempotency**: If the same GTT is checked twice in the same second,
the second check finds status=TRIGGERED and skips the fan-out (idempotent).

---

## 4. TemplatePlan and Resolution

`TemplatePlan` is a pure-data struct that holds the resolved template before execution.

**Fields**:
- `template_id`, `template_name`, `template_slug` — metadata from the template row
- `parent_account`, `parent_symbol`, `parent_side`, `parent_qty`, `parent_exchange` — order context
- `parent_fill_price` — entry price (used for TP/SL offset calculation)
- `parent_lot_size` — (INT, never 0) MCX/NFO contract multiplier, baked in at resolve-time
- `gtts` — list of GttSpec (TP-only, SL-only, or OCO)
- `wing` — optional WingSpec (protective hedge for SELL options)
- `notes` — list of resolution notes (for preview UI)

**Two-step contract**:

1. **`resolve_template_plan(template, overrides, parent_order_ctx)`** — Pure data transform.
   No broker calls, no DB writes. TP/SL trigger values are computed from `parent_fill_price`
   and template percentages. Lot_size is fetched synchronously (or passed in overrides).
   Returns the plan so the UI can preview it via `/api/orders/ticket/preview` before commit.

2. **`apply_plan_sim(plan, driver, parent_order_id)`** and **`apply_plan_live(plan, broker, parent_order_id)`** —
   Side-effecting. Sim path wires the GTT to SimGttBook + SimDriver's paper engine for wings.
   Live path calls `broker.place_gtt()` for GTTs + parallel basket for wings. Both return
   `AttachResult` with placed IDs and any validation errors. On success, attached_gtts_json
   is persisted to the parent order.

---

## 5. Qty Translation and G1 Guard

MCX and NFO contracts are quoted in LOTS in the OrderTicket (e.g. "Lots: 2" = 200 qty
for CRUDEOIL with lot_size=100). The `parent_lot_size` is baked into the TemplatePlan
at resolve-time (never 0).

**`broker.translate_qty(exchange, raw_qty, lot_size)`** — Called for EVERY GTT leg
in `apply_plan_live` before the broker call. Converts lots → contracts if lot_size > 1.

**G1 Guard (LOT_MULTIPLE)** — A synchronous check at the top of `apply_plan_live`
verifies that every GTT leg qty + wing qty is a valid multiple of `parent_lot_size`:
```python
if parent_lot_size > 1:
    for gtt in gtts:
        for order in gtt.orders:
            if order['quantity'] % parent_lot_size != 0:
                return AttachResult.errors("qty not a lot multiple")
```
Returns `AttachResult.errors` immediately on failure; no broker call is made.

The adapter ceiling in `kite.py:place_gtt()` provides a last-line defense (50-lot cap
on Kite), but the synchronous G1 check fires first and faster.

---

## 6. Apply Path — Sim and Live

### Sim path: `apply_plan_sim(plan, driver, parent_order_id)`

1. Validate parent_lot_size (never 0)
2. For each GttSpec in plan.gtts:
   - Call `driver.record_gtt_placed(gtt)` — registers in SimGttBook
   - Store gtt_id in placed_id field
3. For wing (if present):
   - Submit to driver's paper engine (market-order fill)
   - Store wing_placed_id

### Live path: `apply_plan_live(plan, broker, parent_order_id)`

1. **G1 guard** (top of function) — qty must be a lot multiple
2. For each GttSpec:
   - Call `broker.translate_qty(exchange, order.quantity, parent_lot_size)` for every leg
   - Call `broker.place_gtt(gtt)` → gtt_id
   - Store gtt_id in placed_id field
3. For wing (if present):
   - Parallel `broker.place_order(wing)` via basket
   - Store wing_placed_id

**Error handling**: AttachResult carries errors list. On G1 failure or broker exception,
the function returns early with errors; other GTTs may have already been placed.

---

## 7. OCO Pair-Watcher and Trail Stops

OCO (one-cancels-other) pairs are two GTTs: TP-only (upper trigger) and SL-only (lower).
When one fires, the other should cancel automatically.

**Trail stop** — A SL GTT with a `sl_trail_pct` field. The background poller
`_task_trail_stop` (every 30s) reads the trail distance from attached_gtts_json and
ratchets the SL trigger upward toward the current LTP, never downward.

**30s trail-stop interval** — Runs every 30 seconds during market hours. Reads all
active algo_orders with trail-stop GTTs, computes `new_trigger = max(current_trigger,
ltp - (trail_pct × ltp))`, calls `broker.modify_gtt(gtt_id, new_trigger)`.

Metadata (`sl_trail_pct`) is persisted in attached_gtts_json so the poller can resume
across restarts and recover from missed intervals.

---

## 8. Edge Cases

**Double-trigger guard**: OCO TP and SL fire in the same tick or rapid succession.
The second crossing finds status=TRIGGERED and does nothing (check_triggers skips
already-TRIGGERED GTTs).

**Cancel of already-triggered GTT**: Operator manual cancel on a GTT that just crossed.
The cancel finds status=TRIGGERED and returns a result code (no-op, idempotent).

**Lot_size = 0**: Never reaches the broker. Caught at resolve-time when `get_lot_size()`
returns the instrument's lot_size (always ≥1 for valid instruments). Pure data validation
in `_ticket_validate_input` rejects 0-quantity orders.

**Wing order G2 bypass**: When the parent order carries intent='close', the wing
leg (protective hedge for SELL options) bypasses G2 (FAT_FINGER_5_LOT_CAP) so the
wing doesn't accidentally block because it happened to be exactly 5 lots.

**Broker API transient failure**: If `broker.place_gtt()` raises mid-plan, the function
returns with partial results. The operator can retry or inspect the AlgoOrder's
attached_gtts_json to see which GTTs landed.

---

## 9. Test Coverage Map

### Backend

- `test_gtt_book_lifecycle.py` — place, cancel, trigger, reset operations
- `test_gtt_book_crossing.py` — single and two-leg trigger detection, order fan-out
- `test_apply_plan_sim.py` — simulated GTT attachment, paper-engine wing fills
- `test_apply_plan_live.py` — live GTT placement, translate_qty for every leg, G1 guard
- `test_template_plan_resolve.py` — TP/SL trigger calculation, lot_size propagation
- `test_g1_guard_gtt.py` — lot-multiple validation, error return on failure
- `test_trail_stop_modification.py` — ratcheting SL trigger on fixed 30s interval

### Frontend

- `template_preview.spec.js` — plan preview shape, GTT + wing lines display correctly
- `template_attach_validation.spec.js` — G1 errors surface in UI, preview disabled

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec; G1 guard + qty translation behavior documented |
