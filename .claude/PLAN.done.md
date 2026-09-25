# Plan: Fix Order-Ticket / Chase / Close-Button Audit Findings (D1–D6, R1, R3, R6, R7)

## Context

Operator reported: "in orders, some times close buy and close sell buttons
don't work. margins also I am not sure if it is correct. When order is placed,
it fails chase behavior etc need to be audited. entire order ticket, order
chain, price chart needs to be audited." A read-only audit (this session) found
**6 confirmed defects (D1–D6)** in the order-ticket → chase pipeline, several of
which compound into exactly the symptoms reported ("close didn't work", "order
fails", margin-looks-right-then-fails). Two of the audit's flagged risks (R2 —
chase restart-recovery, R3 — chase placing a stale-remainder order after a
failed cancel) turned out to already be fixed as a side effect of the C1–C7
order-safety commit (`f5db7765`) that just shipped — verified directly against
the current code below, not assumed. This plan covers the still-open items:
D1–D6 (must-fix), R1 (real waste, not just theoretical), R6 (quick), and R7
(the specific "close button does nothing" symptom the operator described —
confirmed as a UX defect, not a placement bug: the CLOSE/BUY buttons are side
selectors, not submit buttons).

**Operator decision on R7**: keep the existing two-step flow (select side, then
Submit) — do not make CLOSE a one-click submit. Fix by making the labels
unambiguous: the Submit button reflects the actual action about to fire, and
the side-selector buttons get a visual/label cue that they only select side.

**Already fixed, no action needed (verified against current code, not the stale
audit text)**:
- **R2** (chase restart-recovery duplicating/overfilling) — this is exactly C4
  from the just-shipped order-safety fix. `background.py:_recover_chase_already_filled`
  now reconciles true fill state and cancels orphaned resting orders before
  restarting.
- **R3** (chase places a new order for a stale remainder when the previous
  order's cancel "fails" because it already completed) — this is now resolved
  as a side effect of C2's `_ch_capture_late_fill`: it re-queries the
  just-cancelled order's FINAL status AFTER the cancel call (regardless of
  whether the cancel itself succeeded, since a completed order's cancel is a
  no-op/failure at the broker but the post-cancel status read still returns the
  true COMPLETE state), folding any late fill into `cumulative_filled` before
  the next order is sized. Will still add one regression test confirming this
  (not previously covered), since it was fixed incidentally, not intentionally
  tested for this exact scenario.

## The defects and fixes

**D1 — Chased ticket orders always go out as NRML, regardless of the operator's
selected product.** `orders_place.py:_ticket_place_or_chase_live` calls
`_start_live_chase(...)` without `product`/`variety`/`validity`;
`orders_helpers.py:_live_chase_config` never sets them either, so
`ChaseConfig.product` keeps its dataclass default `"NRML"` (verified:
`chase.py:362`). Chase is on by default for every LIMIT/SL ticket. Closing an
MIS F&O position via chase sends NRML to the broker — opens a separate opposite
NRML leg instead of flattening the MIS one (matches "close didn't work"), or
gets rejected for margin ("order fails"). Closing/buying CNC equity with chase
gets rejected outright. The non-chase direct-place branch three lines below
already reads `data.variety` correctly (`orders_place.py:1559`) — only the
chase path is missing this.
**Fix**: thread `data.product`, `data.variety`, `data.validity` from the ticket
request into `_start_live_chase` → `_live_chase_config` → `ChaseConfig`, mirroring
how the direct-place branch already reads them.

**D2 — Close tickets opened while the instrument cache is still loading send a
many-times-oversized order.** `MarketPulse.svelte` and
`admin/derivatives/+page.svelte` fall back to `lot = Number(inst?.ls || 1)` = 1
when `getInstrument` hasn't resolved yet. `OrderTicket.svelte` only repairs a
lot size that starts at 0 (never one that starts at 1 — a "1" looks like a
valid equity lot size, not a placeholder), and the repair effect only re-runs
when `_resolvedSymbol` changes, which never happens for a close ticket (same
symbol throughout). With `_lotSize=1`, `buildPlacePayload` sends raw contracts
as if non-F&O; the backend's `_resolve_fno_qty` always treats F&O quantity as
lots and multiplies by the real lot size again (e.g. 1-lot NIFTY close →
`quantity=75` sent → backend computes `75 × 75 = 5625` contracts). Close intent
skips the 5-lot/MCX-20-lot/50-lot ceilings, so nothing catches this. The margin
preview uses a different, correct code path, so it shows a small, correct
number right up until submit. `PerformancePage.svelte` already awaits
`_instrumentsReady` before reading lot size — the correct pattern the other two
hosts should follow.
**Fix**: `MarketPulse.svelte` and `admin/derivatives/+page.svelte` await
instrument readiness (mirror `PerformancePage.svelte:183`'s pattern) before
opening a close ticket, instead of falling back to `1`. As defense in depth,
also make `OrderTicket.svelte`'s lot-size repair effect re-check when
`getInstrument` transitions from unresolved→resolved even when
`_resolvedSymbol` hasn't changed (a close ticket's symbol is static).

**D3 — A 15s client timeout renders a failed/slow order as a false success.**
`frontend/src/lib/api.js:_request` returns `null` instead of throwing when its
internal 15s timeout fires. `placeTicketOrder` resolves `null` →
`OrderTicket.svelte`'s `submitOk` renders "LIVE BUY 75 X @₹… · #?" and the
modal closes — a fake success with an unknown order id. The ticket can
legitimately exceed 15s because of preflight's full instruments download (R1)
plus the chase's own internal 15s `wait_for`.
**Fix**: distinguish a genuine timeout from a real response in `api.js` — throw
(or return a distinct sentinel) on timeout instead of `null`, and have the
ticket's error handler render an explicit "still processing, check the order
book" state rather than a false success when it can't confirm one way or the
other.

**D4 — A second click while a submit is in flight queues and fires a duplicate
order.** `OrderTicket.svelte:submit()`'s trigger-effect reruns when `submitting`
flips back to `false` and sees the trigger counter still mismatched, calling
`submit()` again. Nothing disables the submit button or shows a loading state
during a slow submit (`SymbolPanel.svelte`'s footer submit is only disabled on
`basketSubmitting`, a different flag), so a re-click during the D3 delay is a
natural operator reaction. The derivatives page keeps its modal open after
success, so the queued second order genuinely fires; for a close, that's a
second, unwanted order.
**Fix**: disable the submit button and show an explicit loading/pending state
for the whole duration of `submitting`, and make the trigger-counter update
atomic with the guard check so a rerun after `submitting` flips false can't
re-fire a stale trigger.

**D5 — A ticket that reports "failed" can still have its chase place the order
anyway.** `orders_helpers.py`'s ticket future resolves with an exception on
certain pre-placement errors (e.g. a `ValueError` from a zero lot size), and
the ticket returns 400 — but `chase_order` treats non-`BrokerInputError`
exceptions as retryable and keeps going (sleep, retry, up to
`_MAX_CHASE_ERRORS`). If market depth returns a non-positive price, the chase
sleeps and continues silently with no event emitted, so the ticket's 15s
`wait_for` times out (near-blank error) while the chase task keeps running
unaffected — `wait_for` only cancels the *future*, not the chase task itself.
Operator sees "failed", retries, and can end up with two live orders.
**Fix**: when the ticket-side `wait_for` times out or the future resolves with
an error that isn't a genuine terminal broker rejection, actually track and
cancel the underlying `chase_order` asyncio task (not just its future) so a
"failed" ticket response can't leave a live chase running unattended; at
minimum, this requires the ticket handler to hold a reference to the chase task
it spawned.

**D6 — A lot-size cache-miss silently sends NFO/BFO/CDS orders in contracts
instead of lots.** `kite.py:get_lot_size` returns `0` (safe "unknown" sentinel,
correctly triggers a 503) for an MCX cache miss, but `1` for a non-MCX miss —
verified still present. `_resolve_fno_qty` accepts `1` as valid (never triggers
the 503 guard), so the frontend's own `lot_size_hint` is discarded and a 1-lot
NIFTY order goes out as `quantity=1` (rejected by the broker as not a lot
multiple) instead of `75`. The "1 is a safe no-op" premise predates the v2
lots-in-requests convention change and is now false for F&O.
**Constraint verified before fixing**: `_rebuild_lot_index` only stores entries
with `lot_size > 1` (by design, to avoid a bad response overwriting a real F&O
lot size with a stray `1`). This means a genuine equity/CDS/BCD instrument
(real `lot_size == 1`) is currently indistinguishable from a true cache miss —
naively changing the miss-fallback to `0` for all exchanges would make every
CDS order 503.
**Fix**: change `_rebuild_lot_index` to also store confirmed `lot_size == 1`
entries (so the index can tell "confirmed 1" apart from "not in the index at
all"), then change `get_lot_size`'s miss-fallback to `0` (unknown → 503) for
ALL exchanges, not just MCX. Before landing, grep every other consumer of
`_LOT_INDEX` to confirm none of them relies on the old "only `>1` entries are
ever stored" contract.

## Also fixing (real waste / quick / operator-reported UX)

**R1 — every preflight margin-check and live ticket downloads the FULL
instruments dump (NFO ≈90k rows) with no caching**, on every debounced margin
preview (350ms) and every live ticket (`actions_preflight.py:606-613,747-752`).
Pushes tickets toward the D3 timeout and duplicates the OOM-incident concern
("no T+0 broker downloads"). The dead `_preflight_check_qty_freeze` check
(Kite's instrument dump has no `freeze_qty` field) never fires, so this cost
buys nothing.
**Fix**: cache the instruments dump behind a short TTL (module-level cache,
matching the pattern already used for e.g. the holiday-calendar 4-tier read),
reused across preview/ticket calls within the TTL window instead of a fresh
fetch every call.

**R6 — error messages are truncated to ~32 characters** (`api.js:102`), so a
422 preflight block, a broker rejection, a 503 lot-size guard, and a chase
timeout are all indistinguishable in the UI.
**Fix**: raise the truncation limit (or show the full message in a tooltip/
expandable detail) so the operator can actually tell which failure they hit —
directly useful for diagnosing D3/D5 if they recur.

**R7 — the CLOSE/BUY buttons are side selectors, not submit buttons (the
operator's reported symptom).** `SymbolPanel.svelte`'s footer side button and
`SideToggle.svelte`'s pills only flip the ticket's side; clicking the
already-active CLOSE option switches it to ADD and resets lots to 1. With
chase on (default), the actual submit button just says "Submit" — nothing
labeled CLOSE places an order, which reads as "I clicked close and nothing
happened."
**Fix (operator-approved, clarify-only)**: the submit button's label reflects
the real pending action (e.g. "SUBMIT — CLOSE BUY 75", mirroring the existing
`submitOk` success-string convention already used elsewhere in
`OrderTicket.svelte`), and the side-selector buttons/pills get a visual or
textual cue (e.g. a distinct style or a "select side" microcopy) that
disambiguates them from a submit action. No change to when an order actually
fires. Also fix the stale drift note flagged alongside this
(`MarketPulse.svelte:3845-3846` claims the footer shows "CLOSE BUY"/"CLOSE
SELL" — update to match the corrected label).

## Explicitly out of scope for this plan

- **R4** (chase ignores the entered limit price) — by design (chase computes
  its own price from live depth), not a bug.
- **R5** (margin chip for close orders can blank the "available" figure /
  cash-mode comparison) — ties into the already-approved-but-unimplemented
  cash/margin fix plan from earlier this session; not duplicated here.
- **Drift/cleanup items** (unused `_sideBtnLabel`/`_modalFlipSide`, stale
  ADMIN_GUIDE ticker-health claim) — cosmetic, no behavior risk; left for a
  future pass.
- **Price chart audit findings** — separate audit, separate plan, not covered
  here.

## Files

- `backend/api/routes/orders_place.py`, `backend/api/routes/orders_helpers.py` — D1 (thread product/variety/validity), D5 (track+cancel chase task on ticket-side failure/timeout)
- `backend/brokers/adapters/kite.py` — D6 (`_rebuild_lot_index` + `get_lot_size` miss-fallback)
- `backend/api/algo/actions_preflight.py` — R1 (instruments-dump caching)
- `frontend/src/lib/MarketPulse.svelte`, `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — D2 (await instrument readiness before close-ticket open), R7 (stale label drift)
- `frontend/src/lib/order/OrderTicket.svelte` — D2 (lot-size repair on cache-resolve), D3 (false-success rendering), D4 (submit button loading/disabled state, atomic trigger guard), R7 (submit button label reflects action)
- `frontend/src/lib/SymbolPanel.svelte`, `frontend/src/lib/order/SideToggle.svelte` — R7 (side-selector visual/label cue)
- `frontend/src/lib/api.js` — D3 (timeout vs. false-success), R6 (error message truncation)
- `backend/api/algo/chase.py` — new regression test only for the already-fixed R3 scenario, no source change expected

## Agents

- **backend**: D1, D5, D6, R1 (bundled — all backend/api + brokers, no file overlap risk since nothing else is in flight right now)
- **frontend**: D2, D3, D4, R6, R7 (bundled — all frontend, one coherent order-ticket UX pass; write/update its own Playwright spec per the standing frontend-change-loop rule)
- **backend-test**: pytest coverage for D1, D5, D6, R1, plus the R3 regression test confirming the already-shipped fix
- **doc**: no CLAUDE.md entry needed unless the implementer's D6 fix changes a documented invariant beyond what's already there — check first, only add if something genuinely new needs recording

## Tests

- pytest: yes.
- svelte-check: yes.
- playwright: yes — targeted specs for D2 (close ticket on cold cache doesn't oversize), D4 (double-click doesn't duplicate), R7 (submit button label matches pending action).

## Commit message

fix(orders): order-ticket/chase pipeline — product/variety on chased orders,
cold-cache close oversize, false-success timeout, double-submit, orphaned
chase on ticket failure, NFO/BFO/CDS lot-size cache-miss, and close-button
label clarity (D1-D6, R1, R6, R7)

## Done when

D1–D6, R1, R6, R7 each have a passing regression test reproducing the original
failure mode; `venv/bin/pytest backend/tests/ -q --tb=line`,
`npx svelte-check`, and `npx vitest run` all green; self-audit confirms D6's
`_LOT_INDEX` change doesn't break any other consumer of that cache.
