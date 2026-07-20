# Plan: Comprehensive Audit — MCX/F&O Lots vs Contracts, Guards (Close/Add/Buy/Sell), Templates, Chase

## Task

Thorough D1/D5 audit across the full orders pipeline for MCX and equity F&O,
covering all intents (buy, sell, close, add), guards (G1, G2, adapter ceiling,
GTT-QTY-GUARD), template attach qty translation, and chase state machine/timing.

Key questions to answer per intent × exchange:
- Does the ticket handler correctly convert LOTS → contracts at the boundary?
- Does G2 (5-lot cap / MCX 20-lot cap) check against lots (not contracts)?
- Is the 50-lot adapter ceiling correctly bypassed for close intent only?
- Does `translate_qty` (MCX: contracts→lots; NFO: pass-through contracts) fire on every
  path that reaches the broker (ticket, basket, GTT leg, wing, chase close order)?
- Does the GTT layer call `translate_qty` for EVERY leg including wing?
- Does chase generate a close order with the right qty for MCX vs NFO?

Also audit: chase timing in default vs live mode; NavStrip 5-min cold-start delay
(known — but check if there's a low-cost fix to warm NavStrip faster post-deploy).

## Agents

- backend: skip
- frontend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Audit agents (parallel — all read-only)

**Audit 1 — MCX + NFO ticket/basket qty pipeline**:
Read `backend/api/routes/orders_place.py` fully (all of `_ticket_validate_input`,
`_resolve_fno_qty`, `_ticket_enforce_lot_and_fat_finger`, `ticket_order_handler`).
Read `backend/api/routes/orders_basket.py` (full lot-resolution path).
Read `backend/brokers/adapters/kite.py` `to_kite_qty`, `translate_qty`, `place_order`
adapter ceiling block, close-intent bypass.
Answer for each intent (buy/sell/close/add) on each exchange (MCX, NFO, equity):
- Does lots→contracts happen exactly once at boundary?
- Does G2 check against LOTS, not contracts?
- Does adapter ceiling bypass fire only on close?
- Is there any path where raw contracts reach `place_order` on MCX?

**Audit 2 — GTT template attach qty translation + wing**:
Read `backend/api/algo/template_attach.py` lines 1300–1520 (apply_plan_live, G1 guard,
`_ta_live_place_one_gtt`, wing placement, translate_qty calls).
Check: does every GTT leg call `translate_qty` before `broker.place_gtt`?
Does the wing call `translate_qty`?
Does G1 guard fire before translate_qty, or after? (order matters for MCX)
What happens when `translate_qty` raises ValueError (lot_size=0 on MCX)? Is the error
surfaced to the operator or silently dropped?
Check the `_resolve_lot_size_for_order` caller pattern (lines 1966–1980) — does the
error result actually get returned if lot_size resolution fails? (Explore agent flagged this
but caller at 1966–1980 appeared to check — verify with actual code read.)

**Audit 3 — Chase: timing, mode-gating, close qty**:
Read `backend/api/algo/chase.py` fully — find:
- What modes trigger chase to actually execute live broker calls vs sim/paper?
- How is `next_attempt_at` set, and does the frontend "default mode" suppress chase execution?
- When chase generates a close order, what qty does it send? Does it call `translate_qty`
  for MCX? Or does it send raw contracts?
- Does chase call `broker.cancel_order` + `broker.place_order` for each attempt? Is
  `place_order` called with correct intent="close" to bypass G2 and adapter ceiling?
- What is the chase execution mode gate — is "default mode" the same as sim/paper, or
  is chase disabled in some frontend-only mode?
Read `backend/api/background.py` for any background chase task — confirm whether chase
starts immediately on order placement or via a background poller.

**Audit 4 — Close-intent guard matrix** (cross-cutting):
Across all paths (ticket, basket, GTT, chase, `_arm_take_profit`), verify the invariant:
- G2 (fat-finger cap): bypassed for intent="close"
- Adapter ceiling (50-lot): bypassed for intent="close"
- G1 (lot-multiple): NOT bypassed for close (still enforced)
- GTT layer: no close-intent bypass (GTT has no intent concept) — correct
- Chase: intent="close" → G2/ceiling bypassed → but does chase still enforce G1?
Look for any path where a close order could be blocked by G2 or ceiling incorrectly,
OR where a non-close order bypasses G2/ceiling incorrectly.

## Tests

- pytest: no (audit only — fixes in follow-up plan)
- svelte-check: no
- playwright: no

## Commit message

(none — audit produces punch list, not code changes)

## Done when

Punch list produced with file:line citations, severity (P1/P2/P3), grouped by:
- MCX qty pipeline
- NFO qty pipeline
- Template attach / GTT layer
- Chase close-order qty
- Guard matrix (G1/G2/ceiling/intent)
- Chase timing / mode-gating
Ready to implement fixes in a follow-up `/depl`.
