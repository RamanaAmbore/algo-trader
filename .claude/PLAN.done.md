# Plan: Fix 7 Confirmed Order-Safety / Lot-Size Bugs (C1–C7)

## Context

The operator asked for an exhaustive audit of MCX/NFO lot-vs-contract normalization
across Kite/Dhan/Groww, "make sure there are no gaps." The audit (read-only, opus)
found **7 confirmed code defects**, 5 of them silent oversize/overfill, 3 reachable
on ordinary trading flows — the exact bug class CLAUDE.md already flags as having
caused "multi-lakh P&L distortion + 20× over-orders" historically. I independently
re-verified the 5 highest-severity findings (C2, C5, C6, C7, and the position-
normalization premise behind C7's stale-comment claim) by reading the actual code —
all confirmed exactly as described, including working through the audit's numeric
examples against the real logic. C1, C3, C4 are trusted from the audit report
(narrower, and consistent with patterns already confirmed correct/incorrect
elsewhere in the same files). Operator approved: "go ahead with order-safety fixes."

This plan covers only the **7 CONFIRMED must-fix defects**. The audit's 4 SUSPECT
items (need a live broker check, e.g. Kite's CDS `multiplier` field) and its
"should-fix risks"/"drift" lists are follow-up work, not in this plan, per scope
discipline — flagged at the end.

## The defects

- **C1** — Modifying a resting order can silently resize it on MCX. Order-book rows
  aren't normalized (`orders_helpers.py:334-355` `_row_from_dict` passes broker
  `quantity` through raw — lots for Kite/Dhan MCX). The modify ticket derives
  `_lots` from that raw value, so a price-only change still recomputes and sends
  `quantity = _lots × lot_size` (contracts) into a field the broker reads as lots.
  1-lot CRUDEOILM → broker receives `quantity=10` → modified to 10 lots.

- **C2** — Chase treats each new attempt's fresh `filled_quantity` as if it were
  the running total, not that attempt's own delta. Verified: `_chase_poll_status`
  (`chase.py:916-928`) computes `_already_filled = quantity - remaining_qty` (fill
  from prior orders) then `_new_delta = filled_qty - _already_filled`, but
  `filled_qty` is the CURRENT (freshly re-placed) order's own cumulative fill,
  which starts at 0 — not a chase-wide running total. When the new order's fill
  happens to equal the prior orders' total fill, delta computes to 0 and the real
  fill is silently dropped from `remaining_qty`, so the next re-place re-orders the
  already-filled amount. Worked example (3-lot NIFTY, 225): ends up 300 filled,
  one lot oversize.

- **C3** — Same MCX lots→contracts reverse-translate as C2's fix target
  (`chase.py:902-907`) is applied by *exchange* (`MCX`/`NCO`) regardless of
  *broker*, but Groww already reports MCX fills in contracts, not lots
  (`groww.py:492`, `translate_qty` is a no-op there). Applying the reverse-
  translate to Groww inflates the observed fill by `lot_size×`, which — combined
  with C2 — causes a different double-count on Groww MCX chases.

- **C4** — Service-restart chase recovery (`background.py:6010-6018`) always
  restarts with `quantity=row.quantity` (the *original* full size), never
  subtracting `row.filled_quantity`, and never checks or cancels whatever order
  is still resting at the broker from before the restart (nothing cancels
  resting chase orders on shutdown). `chase_order` starts fresh with
  `current_order_id=None`, so its first attempt cancels nothing. A 2-lot NIFTY
  chase resting during a webhook-triggered deploy (this app's standard deploy
  path — not hypothetical) can end up with up to 300 filled instead of 150.

- **C5** — Verified by hand-tracing the code: template scale-out allocation
  (`template_attach.py:1004-1015`) rounds every non-last scale **up** to a whole
  lot, so a percent-split across N scales can overshoot the position before the
  last scale is computed. The last scale's raw allocation goes negative
  (`parent_qty - used < 0`), and because that negative value is still summed into
  `_total_alloc`, the total coincidentally equals `parent_qty`, so the
  over-allocation warning never fires. The negative-qty scale itself is silently
  dropped (`if q <= 0: continue`), leaving the earlier over-sized GTTs live.
  Traced example: 1-lot NIFTY (75) with scales [40, 40, 20] → two separate 1-lot
  (75) TP GTTs get created against a 75-share position. The first fully exits;
  the second later fires against a flat position and **opens a new opposite
  position**.

- **C6** — Verified: the trailing stop-loss ratchet path
  (`background.py:3243-3267`, called from `_process_trail_entry` at `:3424`)
  builds GTT leg `quantity: parent_qty` (contracts) with **no** `translate_qty`
  call, and `Broker.modify_gtt` (`kite.py:494-518`) does no translation and has
  no ceiling check (unlike `place_gtt`, which requires the caller to have
  translated and additionally calls `_check_kite_gtt_qty_ceiling` as a last-line
  defense at `:474`). A 1-lot CRUDEOIL trail ratchet rewrites the SL leg to
  `quantity=100`, which Kite reads as 100 lots; when it fires, it leaves a
  99-lot reverse position. Also verified the audit's side-note: normally-attached
  GTTs never populate `parent_qty`/`parent_symbol` on the trail entry at all
  (`orders_place.py`'s `_opp_build_attach_entries:515-543` only sets those fields
  inside the OCO-sibling branch), so trailing is currently only reachable via the
  retry-attach path — this is fixed as part of C6 so the ratchet is both safe
  *and* actually functions on the normal fill path.

- **C7** — Verified: the shared lots↔contracts helper
  (`base.py:_exchange_contracts_to_wire:29-61`) silently passes sub-lot MCX
  quantities through **unconverted** with only a log warning ("broker will likely
  reject" — false; the broker accepts the number as lots), and silently
  **floors** non-multiple quantities with no error. Also verified the stale
  rationale behind the MCX skip in the G1 lot-multiple preflight check
  (`actions_preflight.py:92-99`, comment claims "broker returns qty already in
  LOTS"): `broker_apis.py:1992-2003` confirms positions ARE converted to
  contracts for MCX rows before reaching this check, so the skip is wrong and
  lets agent-driven `place_order`/`close_position` quantities through unchecked,
  bypassing every ceiling.

## Fix approach

**C1 (backend + frontend, defense in depth)**
- Backend: normalize MCX order-row quantity to contracts at the same boundary
  where positions are normalized — `orders_helpers.py:_row_from_dict`/
  `_fetch_orders` — reusing the existing `_MCX_LOTS_CONVENTION_BROKERS` allow-list
  and lots→contracts conversion helper already used for positions, so every
  consumer of `OrderRow.quantity` (order book display, modify ticket) sees
  contracts uniformly like everywhere else in the app.
- Frontend: `orderTicketSubmit.js:buildModifyPayload` should omit `quantity` from
  the PUT payload when it equals the order's original quantity (i.e. only the
  price/trigger changed) — a second, independent guard against sending a
  recomputed quantity the operator never touched.

**C2 (backend, chase.py)** — Replace the delta-reconstruction with a real
persisted cumulative-fill counter, maintained across attempts in `chase_order`'s
loop scope (not re-derived from `quantity - remaining_qty` each poll). Each
freshly re-placed order's own `filled_quantity` is added directly to that
counter as soon as it's observed (it's already a from-zero delta for that order,
so no subtraction against prior fills is needed or correct). Additionally,
right before `_ch_cancel_previous` cancels the current resting order, re-query
its status once and fold any last-second fill into the cumulative counter first
— closing the "fill lands between poll and cancel" gap the audit also flagged.
`remaining_qty` for the next `_place_order` call is always
`quantity − cumulative_filled`, derived fresh from the persisted counter.

**C3 (backend, chase.py:902-907)** — Gate the MCX/NCO reverse-translate by
broker-id membership in `_MCX_LOTS_CONVENTION_BROKERS` (the same allow-list
already used correctly elsewhere, e.g. postback conversion), not by exchange
alone.

**C4 (backend, background.py chase-recovery)** — Before restarting a chase row:
query the broker for `row.broker_order_id`'s live status if present; if still
resting, cancel it and fold any fill it shows into the recovery's starting
cumulative-filled value (`max(row.filled_quantity, live_filled)`, mirroring the
existing `_ch_compute_new_filled` MAX-clamp pattern). Add an
`already_filled: int = 0` parameter to `chase_order` so `quantity` keeps meaning
"true original size" (needed downstream for template-attach sizing, G1, etc.)
while `remaining_qty` initializes to `quantity − already_filled`. Pass
`already_filled=` from the recovery path instead of always restarting at full
size. This composes directly with C2's cumulative-counter fix.

**C5 (backend, template_attach.py:989-1023)** — Round every scale's allocation
**down** to a whole lot (not up), including the last scale
(`max(0, parent_qty − used)`, floored), so the running sum can never exceed
`parent_qty` and no allocation can go negative. Any leftover fractional-lot
residual is left unexited exactly as the existing "residual left open" note
already describes — that note's trigger condition stays correct once
over-allocation is structurally impossible.

**C6 (backend + broker)**
- `orders_place.py:_opp_build_attach_entries` — populate `parent_qty`,
  `parent_symbol`, `parent_exchange`, `parent_account`, `parent_product`,
  `current_trigger` on every trailing-eligible entry unconditionally (not only
  inside the OCO-sibling branch), so trailing activates on the normal
  direct-fill attach path, not just retry-attach.
- `background.py:_process_trail_entry`/`_build_trail_modify_kwargs` — resolve
  lot size for the parent symbol/exchange and call `broker.translate_qty`
  before building `orders_payload["quantity"]`, mirroring `apply_plan_live`'s
  pattern for `place_gtt`.
- `kite.py:modify_gtt` (broker agent) — add the same
  `_check_kite_gtt_qty_ceiling(exchange, orders, tradingsymbol)` last-line
  defense `place_gtt` already has, at `:494-518` before calling
  `self.kite.modify_gtt`. Check whether Dhan's `modify_gtt` adapter needs the
  equivalent ceiling and add it if the same gap exists there.

**C7 (broker + backend)**
- `base.py:_exchange_contracts_to_wire` — raise `ValueError` (same pattern as
  the existing `lot_size <= 1` guard) instead of silently passing through when
  `contracts < lot_size`, and instead of silently flooring when `contracts` is
  not a whole multiple of `lot_size`. Both cases mean the caller's quantity is
  wrong, and the broker will silently misinterpret whatever number crosses the
  wire — refuse rather than guess.
- `actions_preflight.py:92-99` — remove the MCX/NCO skip from the G1
  lot-multiple check; the stale "broker returns qty already in LOTS" premise is
  now confirmed false for the paths that reach this preflight (positions are in
  contracts by the time they get here, per `broker_apis.py:1992-2003`). Update
  the comment to state the current (correct) premise so this doesn't regress
  again.

## Agents

- **broker**: `backend/brokers/base.py` (C7 core fix), `backend/brokers/adapters/kite.py`
  (`modify_gtt` ceiling, C6), check + fix `backend/brokers/adapters/dhan.py`
  `modify_gtt` for the same gap if present.
- **backend** (agent 1 — chase fill-accounting, bundled since C2/C3/C4 are the
  same function family in the same file): `backend/api/algo/chase.py`
  (C2 cumulative-counter redesign, C3 broker-gated conversion), `backend/api/background.py`
  chase-recovery block (C4).
- **backend** (agent 2 — remaining backend-side fixes):
  `backend/api/routes/orders_helpers.py` (C1 order-row normalization),
  `backend/api/algo/template_attach.py` (C5 floor-rounding),
  `backend/api/background.py` trail-modify block + `backend/api/routes/orders_place.py`
  `_opp_build_attach_entries` (C6 backend half), `backend/api/algo/actions_preflight.py`
  (C7 G1 skip removal).
- **frontend**: `frontend/src/lib/order/orderTicketSubmit.js` `buildModifyPayload`
  (C1 frontend defense-in-depth) — bundle its own Playwright spec update per the
  standing frontend-change-loop rule.
- **backend-test**: pytest coverage for all 6 backend/broker-touched files —
  especially C2's cumulative-counter chase simulation (multi-attempt partial
  fills reproducing the exact 225→300 scenario) and C5's scale-out allocation
  (the exact [40,40,20] over-lot scenario), both as regression tests that fail
  against the pre-fix logic.
- **doc**: sync `CLAUDE.md`'s "Critical math guards" section — these fixes
  supersede/extend the existing GTT-translate-qty and G1-guard notes.

## Tests

- pytest: yes — new/updated tests for chase.py (C2/C3/C4), template_attach.py
  (C5), background.py trail (C6), base.py (C7), actions_preflight.py (C7),
  orders_helpers.py (C1).
- svelte-check: yes.
- playwright: yes — modify-ticket flow (C1), targeted at the price-only-change
  scenario from the audit's own worked example.

## Commit message

fix(orders): eliminate 7 confirmed lot/contract silent-oversize bugs in chase,
template exits, trailing SL, and order modify (C1–C7)

## Done when

All 7 defects have a code fix with a test that reproduces the original failure
mode and passes after the fix; `venv/bin/pytest backend/tests/ -q --tb=line` and
`npx svelte-check`/`npx vitest run` are green; self-audit confirms no other
order-placement/modify/GTT path was left calling the old unguarded
`_exchange_contracts_to_wire`/`modify_gtt`/chase fill-accounting behavior.

## Explicitly out of scope for this plan (flagged, not forgotten)

- **S1–S4 (suspects)**: CDS/currency 1000× multiplier risk, MCX lot-size table
  entries for CPO/GOLDGUINEA/COTTON, Groww MCX unit convention, Dhan trade
  display units — all need a live broker check before a code fix can be
  written with confidence.
- **"Should-fix" risks**: cold-cache lot-size fallback-to-1 entry points,
  template preview quantity display bug, template exits not attaching on
  direct (non-chase) Kite fills, exits sized too small after a chased partial
  fill, postback fallback match failing on MCX, Kite-only conversion used in
  margin checks for all brokers, BFO/CDS missing from expiry auto-close.
- **Drift/cleanup**: dead `placeBasket` function, stale comments not tied to
  an active defect.
