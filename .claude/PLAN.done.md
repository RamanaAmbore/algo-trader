# Plan: Wire real chase into the Chain-tab (basket) live order path

## Context

Operator asked: "is chase active for order ticket too? it should be active. the
common chase area should show if the order is placed in chain or order ticket."

Investigated via a dedicated research agent that read every relevant file in
full. Confirmed facts (not guesses):

- **Ticket tab already has real chase.** `orders_place.py`'s live path calls
  `_start_live_chase()` (`orders_helpers.py:276-456`) whenever
  `data.chase AND order_type == "LIMIT" AND price > 0`
  (`_opl_chase_eligible`, `orders_place.py:1653-1663`). This function is a
  **replacement for** `broker.place_order` — it places the order itself as
  the first step of spawning `chase_order()` as a background `asyncio.create_task`
  (`orders_helpers.py:402-407`), then keeps re-quoting the limit per the
  operator's L/M/H aggressiveness until fill or attempt-cap.

- **Chain-tab (basket) live orders have NO chase at all.** The live branch in
  `orders_basket.py` (~lines 513-601) places every leg via a single direct
  `broker.place_order` call, persists an `AlgoOrder` row with
  `status="OPEN"`, and stops — nothing ever re-quotes an unfilled LIMIT leg.
  This is silent: the frontend's `_chaseEnabled` (`SymbolPanel.svelte:1679-1683`)
  shows the CHASE indicator as "always on" for Chain (reasonably, since all
  Chain orders are LIMIT), but that's cosmetic only — the backend never
  actually starts a chase loop for these orders today.

- **No schema change needed.** `BasketLeg` already carries `chase: bool = True`
  and `chase_aggressiveness: str = "low"` per leg (`backend/api/schemas.py:510-512`)
  — confirmed unused (grep: zero references) in both the live AND paper
  basket branches. The paper branch even hardcodes `"chase_agg": "low"`
  (`orders_basket.py:683`) instead of reading the operator's actual per-leg
  choice — a smaller, adjacent bug fixed in the same pass.

- **Ordering constraint that drives the redesign:** `_start_live_chase` needs
  an `AlgoOrder.id` handle (`algo_order_id` param) to keep `broker_order_id`
  in sync across replace attempts and so its terminal handler can find the
  row later (`chase.py` docstring: "Required for any chased order that has a
  template attached"). The ticket path achieves this by **pre-persisting**
  the `AlgoOrder` row (`broker_order_id=None`) *before* calling
  `_start_live_chase`, then seeding `broker_order_id` back onto that same
  row afterward via `_ticket_seed_broker_order_id` (`orders_place.py:1719-1737`,
  a small generic helper, reusable as-is). Basket's current flow is the
  opposite order (place first, insert row after, already populated) —
  this has to flip for chase-eligible legs specifically.

- **Fail-closed invariant that must be preserved** (documented in this
  project's CLAUDE.md, "Live ticket placement fail-closed on AlgoOrder
  pre-persist", commit 8fca413b): a DB insert failure during pre-persist
  must refuse the order immediately, never place a live order with no DB
  row to track it. `orders_place.py`'s pre-persist helper
  (~lines 1622-1650) returns `None` on insert failure and the caller
  aborts before ever calling the broker. The basket version must follow
  the identical pattern.

## Fix

### `backend/api/routes/orders_basket.py` — live-mode branch (~lines 513-601)

1. Compute `_leg_chase_eligible` mirroring `_opl_chase_eligible`'s exact
   logic, using the already-existing per-leg fields:
   `leg.chase and _leg_order_type == "LIMIT" and _leg_price > 0`.

2. **When chase-eligible:**
   - Pre-persist the `AlgoOrder` row FIRST, `broker_order_id=None`,
     `status="OPEN"`, same fields currently set at `orders_basket.py:547-559`
     (account/symbol/exchange/transaction_type/quantity/product/template_id/
     template_overrides_json/basket_tag/strategy_id/target_pct), inside its
     own `try/except` that returns/skips this leg with a `status="error"`
     `BasketLegResult` on any DB failure — mirroring `orders_place.py`'s
     fail-closed pattern exactly, never calling the broker if the row can't
     be persisted first.
   - Call `_start_live_chase(account=..., symbol=sym, exchange=exch,
     transaction_type=side, quantity=_kq, aggressiveness=(leg.chase_aggressiveness
     or "low"), algo_order_id=<the pre-persisted row's id>, intent=_leg_intent,
     product=(leg.product or "NRML"), variety=(leg.variety or "regular"),
     validity="DAY")` — import it the same way `orders_place.py` does
     (`from backend.api.routes.orders_helpers import _start_live_chase`).
     This call *replaces* the existing direct `broker.place_order` call for
     this leg — do not call both.
   - Seed `broker_order_id` back onto the pre-persisted row via
     `_ticket_seed_broker_order_id(live_algo_id, order_id)` (imported from
     `orders_place.py` — it's already generic, no ticket-specific coupling,
     reuse as-is rather than duplicating it into `orders_basket.py`).
   - Continue into the existing `_attach_basket_leg_template(...)` call and
     `leg_results.append(BasketLegResult(..., status="OPEN"))`, using the
     pre-persisted row's id.

3. **When NOT chase-eligible** (MARKET/SL-M order type, or `leg.chase is
   False`, or price ≤ 0): leave the existing code path completely
   unchanged — direct `broker.place_order`, then insert the `AlgoOrder` row
   already populated with `broker_order_id`, exactly as today.

4. Both branches keep flowing into the SAME existing `except Exception`
   block's margin-error classification (`orders_basket.py:575-601`) for
   whichever step is still inside the `try` — read the surrounding function
   in full before editing to place the new pre-persist/chase-start calls
   correctly relative to the existing try/except boundaries, since the
   fail-closed pre-persist step needs its OWN narrower try/except (per
   point 2) distinct from the broker-call error handling.

### `backend/api/routes/orders_basket.py` — paper-mode branch (~line 669-691)

Small, low-risk adjacent fix: change the hardcoded `"chase_agg": "low"`
(line 683) to `"chase_agg": (leg.chase_aggressiveness or "low")` — reads the
operator's actual per-leg aggressiveness choice instead of ignoring it.
Leave everything else in the paper branch unchanged.

## Explicitly out of scope

- Do not change `_start_live_chase`, `chase_order`, or any chase-engine
  internals (`chase.py`) — confirmed correct and already handles its own
  crash-isolation (the `_on_task_done`/`on_event` machinery) and dispatch
  mechanism (plain `asyncio.create_task`, not `_supervised` — confirmed
  deliberate for a bounded, terminating coroutine).
- Do not touch the Ticket-tab path (`orders_place.py`) — confirmed already
  correct, this plan only closes the Chain/basket gap.
- Do not change `_chaseEnabled`'s frontend gating logic (`SymbolPanel.svelte`)
  — it already correctly shows CHASE as available for Chain (LIMIT-only) and
  for Ticket when order type is LIMIT/SL. Separately noted (not fixed here,
  flagging for awareness only): the frontend's `_chaseEnabled` includes SL
  for Ticket, but the backend's `_opl_chase_eligible` only ever chases
  `order_type == "LIMIT"` — SL tickets show the CHASE UI but never actually
  chase. That mismatch predates this investigation and is a separate,
  narrower bug from what was asked; flag it to the operator after this
  fix ships rather than bundling an unrelated backend change into this pass.
- Do not touch `_attach_basket_leg_template` or the template-attach-on-fill
  architecture — unaffected by this change.

## Tests (mandatory, same commit)

Extend `backend/tests/` (likely `test_orders_basket.py` or a broker-layer
basket test file — check existing basket test file naming first) with:
- A chase-eligible LIMIT leg (`chase=True`, valid price) on the live path
  results in `_start_live_chase` being called (mock it) instead of
  `broker.place_order` being called directly for that leg — assert
  `broker.place_order` is NOT called when chase fires.
- A non-eligible leg (MARKET order type, OR `chase=False`, OR price=0) still
  calls `broker.place_order` directly, unchanged from today — regression
  guard.
- The pre-persisted `AlgoOrder` row's `algo_order_id` is correctly passed
  into the mocked `_start_live_chase` call.
- A DB pre-persist failure (mock the insert to raise) results in the leg
  reported as `status="error"` and `broker.place_order`/`_start_live_chase`
  is NEVER called — the fail-closed regression guard, mirroring the existing
  ticket-path test coverage for commit 8fca413b (find and reference that
  existing test as the pattern to copy).
- Paper-mode branch: `chase_agg` passed to `register_open_order` reflects
  `leg.chase_aggressiveness`, not a hardcoded `"low"`, when the leg specifies
  a different value.

## Verification

1. `venv/bin/pytest backend/tests/ -q --tb=line` — full suite green.
2. Manually confirm (via dev DB or a dry paper/live basket submit) that a
   chase-eligible Chain basket leg produces a running chase task the same
   way a Ticket-tab LIMIT order does — check `ChaseCard`/`/api/orders/algo/recent`
   shows `attempts` incrementing for the basket-originated row.
3. Confirm a non-chase-eligible leg (e.g. MARKET) still places instantly with
   no behavior change.

## Commit message (draft)

`fix(orders): wire real chase into the Chain-tab basket live order path (was silently placing once and never re-quoting)`

## Done when

- A chase-eligible LIMIT leg submitted from the Chain tab starts a real
  background chase loop, identical in mechanism to a Ticket-tab LIMIT order.
- Non-eligible legs (MARKET/SL-M, `chase=False`, zero price) are completely
  unaffected — same direct-placement behavior as before.
- Fail-closed pre-persist invariant holds for the new basket chase path (no
  untracked live order can ever be placed).
- Paper-mode basket legs respect the operator's per-leg chase-aggressiveness
  choice instead of a hardcoded value.
- New/updated tests green; full suite green.
