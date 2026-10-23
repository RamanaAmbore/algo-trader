# Plan: Fix P1 + P2 findings from the orders/chase/template/derivatives audit (2026-10-23)

## Context

Three read-only fable-model audits (orders+chase, template/bracket, derivatives+payoff —
each covering backend and frontend, across backend correctness, frontend presentation,
race conditions, error handling, logging, and alert robustness) ran against the codebase
right after today's chase-robustness + order-poll consolidation work shipped to prod.
They found **8 P1 (real-money-risk) and 23 P2 (correctness/data-integrity) findings** —
operator-approved scope is P1+P2 (31 items total); P3 polish/logging-drift items (~34)
are explicitly deferred, not dropped, and listed at the bottom for later reference.

Operator-approved sequencing: **one plan, separate commits per logical fix — never
bundle unrelated defects into one commit** (standing rule). Items that share the same
root cause/mechanism/file are grouped into one fix-cluster (one commit); everything else
is its own commit. This plan organizes the 31 items into 24 fix-clusters across 3 waves
(Orders+Chase, Template/Bracket, Derivatives+Payoff), matching the audits' own domain
boundaries so each implementing agent has full context on its slice without re-deriving it.

**Scope discipline**: every cluster below cites the exact audit finding(s) it covers —
implementers fix exactly what's described, not adjacent code "while they're in there."

---

## Wave 1 — Orders + Chase (9 clusters)

### 1A. Kill / CANCEL_FAILED lifecycle (backend + frontend, one commit)
**Covers**: audit items O1, O6, O7.
- Kill is keyed only by `broker_order_id`; if chase has just placed a replacement order
  in the 1-4s window around a Kill click, Kill targets the dead old id, fails at the
  broker, and `_rco_apply_kill_status` writes `CANCEL_FAILED` onto a row chase is still
  actively re-pricing under a NEW id — operator sees "KILL FAILED" while live orders
  keep being placed. Fix: Kill must operate against the algo-order id (not just the
  broker_order_id snapshot read at click time) — re-resolve the row's CURRENT
  `broker_order_id` inside the same lock/operation that calls `mark_killed`, and/or add
  an algo-order-level kill intent chase's own loop checks every iteration (not just
  `_ch_post_replace_kill_check(old_id)`).
  Files: `backend/api/routes/orders.py` (`kill_chase`, `_rco_kill_live_mode`,
  `_rco_apply_kill_status`), `backend/api/algo/chase.py` (`_ch_post_replace_kill_check`,
  the kill registry near `mark_killed`).
- `kill_chase`'s HTTP response always returns `ok: true` even when the broker cancel
  actually failed (`err: "broker cancel failed: …"`) or the row was already terminal —
  `ChaseCard.svelte:273-291` removes the row locally on any `ok: true`, so the operator
  never sees the real failure until the row silently reappears on the next poll. Fix:
  response `ok` must reflect whether the kill actually succeeded; frontend must surface
  a real failure banner, not treat every response as success.
  Files: `backend/api/routes/orders.py` (kill response shape), `frontend/src/lib/order/ChaseCard.svelte`.
- A `CANCEL_FAILED` row currently can never be re-killed (`kill_chase` returns
  `already_terminal` for anything not OPEN) and never ages out of the periodic sweep
  (`order_poll.py`'s broker snapshot is day-scoped, so a prior-day CANCEL_FAILED is
  "unavailable" forever) — it becomes a permanent, unexplained default-highlighted chip
  with no operator path to clear it. Fix: allow a genuine re-kill attempt against
  CANCEL_FAILED rows (they are explicitly non-final per the existing
  `ALGO_ORDER_FINAL_STATUSES` design), and give the sweep/watchdog or the admin UI a way
  to resolve a stale CANCEL_FAILED row that's aged well past any plausible retry window.
  Files: `backend/api/routes/orders.py` (`kill_chase` docstring promises "another Kill
  attempt" — honor it), `backend/api/algo/order_poll.py`.

### 1B. Late full fill at exhaustion/stuck-abort written as UNFILLED
**Covers**: audit item O2.
Neither `_ch_exhaust_max_attempts` nor `_ch_abort_stuck` checks
`cumulative_filled >= quantity` before emitting `chase_unfilled` — the in-loop path
(`_ch_zero_remaining_fill`) already does this check; apply the same check to both abort
paths so a late full fill is recorded as FILLED (with template-attach/auto-TP), not
UNFILLED. Critical for Dhan/Groww, which lack reliable postback self-healing.
File: `backend/api/algo/chase.py` (`_ch_exhaust_max_attempts`, `_ch_abort_stuck`).

### 1C. Restart recovery can place a duplicate order
**Covers**: audit item O3.
`_recover_query_and_cancel_resting` swallows a cancel exception at WARNING and still
proceeds with the pre-swallow `status` (OPEN) truthy — `already_filled` is derived
assuming the cancel succeeded, and `chase_order()` restarts with `current_order_id=None`,
placing a brand-new order for the full remaining quantity on top of the still-resting
original. Fix: a failed cancel during recovery must not be treated as "safe to restart
fresh" — either re-verify the order's live status after a cancel failure before deciding
whether to restart, or skip the restart and alert for manual review (mirroring the
`recover_live_chases()` fail-closed pattern already used elsewhere in this file).
File: `backend/api/background.py` (`_recover_query_and_cancel_resting`).

### 1D. ntfy/alert-routing hygiene
**Covers**: audit items O4, O9, O11.
- Every CANCELLED postback — including each of chase's own routine re-quote cancels —
  fires a direct `send_ntfy_alert(title, message)` with `priority=None`, which defaults
  to `urgent` (3x-repeat) for most of the IST trading day. A single LOW-tier chase (30
  max attempts) can generate ~87 urgent pushes for normal repricing. This bypasses the
  binary action-required gate entirely since it's a direct call, not a log-record alert.
  Fix: route this through the same `action_required`-gated path every other chase alert
  now uses (per today's "ntfy = binary action-required gate" invariant) — a chase's own
  internal re-quote cancel is not action-required; an operator-initiated or genuinely
  unexpected cancel still needs to reach ntfy.
  Files: `backend/api/routes/orders.py` (`_opp_send_cancel_alert`),
  `backend/shared/helpers/alert_utils.py`.
- Every ERROR-level log line auto-fires `ERROR_AGENT` (telegram + ntfy, since
  `action_required` is absent and defaults to pass-through) — chase's routine
  recoverable-retry log line and the exhaustion/close_position-FAILED paths are logged
  at ERROR and now double-alert (once via their own explicit alert call, once via the
  blanket ERROR_AGENT catch-all). Fix: either downgrade genuinely-recoverable/already-
  alerted log lines to WARNING, or give `ERROR_AGENT` an explicit `action_required`-aware
  gate/dedup so a line that's already been alerted through its own specific path doesn't
  also trigger the generic ERROR catch-all.
  Files: `backend/shared/helpers/log_store.py`, `backend/api/algo/event_agents.py`
  (`ERROR_AGENT`), `backend/api/algo/chase.py`, `backend/api/algo/actions_live.py`
  (`close_position`'s `raise RuntimeError`).
- An unmasked broker account number reaches operator-facing alert text in
  `_ch_abort_ambiguous_place_timeout`'s "Manually check {account}'s order book" message
  (routed through `error_short`, which `alert_utils.py` never passes through
  `mask_account_in_text`) — inconsistent with `_render_chase_cancel`, which does mask.
  Fix: mask the account in this one message, and confirm `error_short`/
  `_render_order_failure` mask consistently with every other alert renderer.
  File: `backend/api/algo/chase.py`, `backend/shared/helpers/alert_utils.py`,
  `backend/api/algo/event_agents.py` (`_render_order_failure`).

### 1E. Postback row-sync failure logged too quietly
**Covers**: audit item O10.
A Kite postback's DB write failure applying a COMPLETE/REJECTED status is swallowed at
DEBUG — the 5s follow-up verification catches the status mismatch but not the lost
ledger/template fan-out from the original failed write. Raise the log level to WARNING
(matching the file's own convention for a genuine write failure, distinct from an
expected skip) so this is traceable without waiting for a follow-up cycle.
File: `backend/api/routes/orders_postback.py`.

### 1F. Chase periodic/backstop fill paths skip template-attach/auto-TP/ws-resubscribe
**Covers**: audit item O5.
`_ch_periodic_broker_truth_check` and `_ch_backstop_reconcile_one` call
`apply_order_truth` directly (which deliberately omits these side effects per its own
docstring) instead of the shared `order_poll.py:reconcile_row_and_followup` wrapper that
every OTHER automatic corrector now uses. Migrate both chase callers onto the shared
wrapper so a fill caught by chase's own periodic check or ambiguous-abort backstop gets
the same template-attach/ws-resubscribe/alert treatment the watchdog and order-poll
sweep already get. Confirm no duplicate alert fires when chase's own normal fill path
ALSO runs for the same row (idempotency via the existing final-status guard should
already cover this — verify with a test, don't assume).
File: `backend/api/algo/chase.py` (`_ch_periodic_broker_truth_check`,
`_ch_backstop_reconcile_one`), `backend/api/algo/order_poll.py`.

### 1G. Live-chase confirm timeout budget vs circuit breaker false-trip
**Covers**: audit item O8.
`_ch_seed_pre_fill_net_qty` invalidates the raw positions cache and fetches positions
across ALL accounts before the first depth/placement call, inside the 15s
`asyncio.wait_for` ticket-confirm window — a slow broker (e.g. Dhan under load, 3+
accounts) can exceed 15s, the ticket returns 400, the order is marked abandoned, the
order then actually lands and gets immediately killed, and `_record_rejection` trips the
circuit breaker on what was never really a rejection. Fix: either widen the confirm
budget to account for this pre-fill-seed cost, move the seed call outside the 15s
window, or make `_record_rejection` distinguish a client-side confirm-timeout from a
genuine broker rejection so the breaker doesn't false-trip.
Files: `backend/api/routes/orders_helpers.py`, `backend/api/algo/chase.py`
(`_ch_seed_pre_fill_net_qty`), `backend/api/routes/orders_place.py`.

### 1H. ChaseCard header drawer unreadable on mobile
**Covers**: audit item O12.
`compact={false}` renders 9 grid columns with two locked-minimum columns at ~375px —
side/qty columns (`0.6fr`) ellipsize to one character, Attempts clips. Widen the
minimums (same pattern as the existing "Chase countdown column minimum-width guard"
fix) or collapse to fewer columns / a stacked layout below a width breakpoint.
File: `frontend/src/lib/order/ChaseCard.svelte`.

---

## Wave 2 — Template / Bracket (9 clusters)

### 2A. Retry-template race can place a second live GTT/wing set
**Covers**: audit item T13.
`_rco_run_template_attach` never re-checks `attached_gtts_json` INSIDE its own lock
(the fill-path `_fire_template_attach_on_fill` already does this correctly at
`orders_place.py:850`). Apply the exact same in-lock re-check pattern to the retry path.
File: `backend/api/routes/orders.py` (`_rco_run_template_attach`).

### 2B. Scale-out TP ladder + SL has no OCO pairing in the SL→TP direction
**Covers**: audit item T14.
`pair_two_singles` only pairs an exact `{"TP","SL"}` label set — a scale ladder's
labels (`TP+30% × 50`, etc.) never match, so when SL fires and flattens, the remaining
scale-ladder TP GTTs stay live and can later open a naked position. Extend the pairing
logic (or `_task_oco_pair_watcher`) to cover a scale-ladder-plus-SL group, not just the
single-TP-single-SL case.
File: `backend/api/algo/template_attach.py` (`pair_two_singles`, the sibling-pairing
write site around line 2444), `backend/api/background.py` (`_task_oco_pair_watcher`).

### 2C. Close-detection race arms exit orders on a flat position
**Covers**: audit item T15.
Postback and reconcile attach paths determine "is this a close?" with a POST-fill
`_is_offsetting_position` read — a full close reads net_qty=0 and is misclassified as
"not offsetting," arming exits on a flat position. Chase already has the correct fix
for this exact bug class (a PRE-fill position snapshot, documented at `chase.py:361-384`
as "Bug 1"). Apply the same pre-fill-snapshot pattern to the postback and reconcile
attach paths.
Files: `backend/api/routes/orders_postback.py`, `backend/api/routes/orders_place.py`
(`_maybe_fire_template_attach_for_reconcile`), reference `backend/api/algo/chase.py`'s
existing fix.

### 2D. GTT persist DB failure after broker placement is silent
**Covers**: audit item T16.
`_opp_persist_attached_gtts` runs after broker placement; a DB error here logs WARNING
only — no alert, no `template_attach_failed` event — leaving live GTTs at the broker
with `attached_gtts_json` still NULL, which keeps the idempotency guard open and risks
a duplicate on the next redelivered postback/reconcile/order-poll pass. Fix: a DB
failure at this step must fire the same `template_attach_failed` alert path as a broker
placement failure, and the idempotency guard must account for "placed but not yet
persisted" so a retry doesn't blindly re-place.
File: `backend/api/routes/orders_place.py` (`_opp_persist_attached_gtts`).

### 2E. Wing leg placed with wrong product on a BUY-option parent
**Covers**: audit item T18.
`_chase_wing` builds `ChaseConfig(exchange=plan.wing.exchange)` without passing
`product`, so it defaults to NRML regardless of the parent's actual product (e.g. MIS) —
same bug class CLAUDE.md already documents as fixed for `_chase_build_tasks`. Thread
`plan.wing.product` through explicitly, same as that existing fix.
File: `backend/api/algo/template_attach.py` (`_chase_wing`).

### 2F. Invalid template config passes CRUD, fails silently deep in the fill path
**Covers**: audit item T19.
Template CRUD only validates `tp_order_type`/`tp_scales_json`/`sl_trail_pct` — a saved
template with `wing_premium_pct<=0` (or `tp_pct<=0`) is accepted, then
`_parse_template_overrides` raises `HTTPException(422)` on every fill, caught by the
catch-all as a WARNING with no alert and no `template_attach_failed` event — every fill
on that template silently unarms forever. Fix: validate these fields at CRUD time
(reject the save, same as the fields already checked), AND make the fill-path 422 case
raise a real alert if it somehow still occurs (defense in depth).
Files: `backend/api/routes/templates.py` (CRUD validation), `backend/api/algo/template_attach.py`
(`_parse_template_overrides` error handling).

### 2G. Groww retry-attach doesn't write OCO sibling pairing
**Covers**: audit item T20.
`_retry_build_attached_payload` ignores `result.sibling_pairs` — its docstring claims
field-for-field parity with the fill path but never writes `sibling_id`. On Groww (no
native broker-side OCO), `_task_oco_pair_watcher` can't cancel the survivor when one leg
fires, so both TP and SL stay live. Fix: write `sibling_id` exactly as the fill path
does.
File: `backend/api/routes/orders.py` (`_retry_build_attached_payload`).

### 2H. Kite MARKET-leg GTT rejection has no UI warning
**Covers**: audit item T17.
Kite's `place_gtt` raises `BrokerCapabilityError` on ANY MARKET leg, silently losing the
SL too (one two-leg OCO, whole GTT fails) when `tp_order_type=MARKET` is picked —
templates/overrides/UI all allow this with zero warning on a Kite account. Add a
cap-warning (mirroring `brokerCapWarnings.js`'s existing pattern for other
broker-specific limitations) when a Kite account has MARKET selected as the TP order
type with an active SL, so the operator sees it before saving/attaching, not after a
silent total-attach failure.
Files: `backend/brokers/adapters/kite.py` (confirm exact rejection condition),
`frontend/src/lib/data/brokerCapWarnings.js`, `frontend/src/lib/TemplateBar.svelte`.

### 2I. Off-hours wing-gate alert text is false ("deferred" when nothing retries)
**Covers**: audit item T25.
The off-hours wing gate's error text promises "attach deferred" but there is no retry
scheduler anywhere in this codebase (only a `TODO(#2)` placeholder) — TP/SL are ALSO
dropped, not just the wing, and nothing ever retries. Fix the message to accurately
describe what happens (the whole plan is refused, not deferred) so an operator reading
the alert doesn't wait for something that will never happen.
File: `backend/api/algo/template_attach.py` (off-hours wing gate error text).

---

## Wave 3 — Derivatives + Payoff (6 clusters)

### 3A. MCX base-metal future hidden from positions on its own real last trading day (P1)
**Covers**: audit's single derivatives P1 finding — the most severe item in this plan
after the chase Kill-race.
`is_live_row_past_freeze_window` derives a future's expiry via `parse_tradingsymbol`'s
FUT branch, which approximates ALL MCX futures as expiring on the last Thursday of the
month — correct for CRUDEOIL/GOLDM/SILVER/NATURALGAS, WRONG for COPPER/ZINC/LEAD/
ALUMINIUM/NICKEL (and mini variants), whose real expiry falls later in the month. On
the real expiry day, the live-row filter thinks the contract already expired the day
before and drops a genuinely still-tradeable position from `/api/positions`, NavStrip,
the Legs grid, and the LKG cache for the entire day. Fix: `parse_tradingsymbol`'s FUT
expiry derivation (or `is_live_row_past_freeze_window`'s consumption of it) must resolve
the REAL contract-specific expiry date for base-metal MCX futures, not the blanket
last-Thursday approximation — check whether `symbol_resolver.py`'s instrument-table
lookup (already used elsewhere for exactly this reason, e.g. `is_mcx_future_still_current`)
can be reused here instead of the regex/calendar approximation.
Files: `backend/api/algo/expiry_freeze.py` (`is_live_row_past_freeze_window`),
`backend/api/algo/derivatives.py` (`parse_tradingsymbol` FUT branch),
`backend/api/routes/positions.py` (call site, confirmed at line ~1499).

### 3B. Frontend/backend disagree on front-month MCX contract on expiry day/overnight
**Covers**: audit item (frontend/backend front-month disagreement).
Backend `list_active_futures` keeps the expiring contract current through its own
expiry day via `is_mcx_future_still_current` (shipped 2026-10-16). Frontend
`findNearestFuture` (`instruments.js`) and `seedRootMapFromInstruments` (`rootOf.js`)
still roll at bare midnight of the expiry date — so the Snapshot/NavStrip spot flips
basis depending on which contract last ticked. Port the same "stays current through own
expiry day + the closed overnight window, rolls only after next 08:00 IST open" rule to
the frontend equivalents, reusing the backend's own logic/dates where reachable (don't
reimplement calendar math twice if a shared value can be read from the API response
instead).
Files: `frontend/src/lib/data/instruments.js` (`findNearestFuture`),
`frontend/src/lib/data/rootOf.js` (`seedRootMapFromInstruments`).

### 3C. Payoff chart dart position vs printed Exp P&L number disagree
**Covers**: audit item (C1 invariant violated).
The chart's dart (drawn via the backend curve + `expiryPnlOffset`, interpolated) and
the printed Exp P&L number (a separate frontend-local sum, `_chartExpPnlAtSpot`) are
computed by genuinely different formulas and visibly disagree for: (a) multi-month MCX
baskets (backend scales each leg by its own `scale_ratio`; the frontend readout uses
raw spot), (b) calendar/diagonal spreads (backend Black-Scholes-prices the far leg at
its own remaining time; the readout uses intrinsic value), (c) any futures leg (backend
values at scaled spot; the readout uses the future's own live LTP). This breaks the
CLAUDE.md invariant "chart value = marker position, always." Fix: the frontend readout
must use the SAME backend-computed curve value the dart already uses, for all three
cases, rather than a separately-computed local approximation.
Files: `frontend/src/lib/OptionsPayoff.svelte` (dart positioning),
`frontend/src/routes/(algo)/admin/derivatives/+page.svelte` (`_chartExpPnlAtSpot`),
reference `backend/api/algo/derivatives.py`'s actual per-leg valuation logic as the
source of truth both sides must agree on.

### 3D. Midnight-vs-08:00 session boundary reproduces the GOLDM bug for 8 hours
**Covers**: audit item (midnight vs 08:00 mismatch).
`pageLoad.js` uses bare `todayIST()` (midnight rollover) for its expired/unresolvable
checks, while `expiryPnl.js`'s `isExpiredHeldContract` correctly uses
`tradingSessionDateIST()` (08:00 IST rollover) per the documented session-boundary
convention. Between 00:00–08:00 IST the day after an expiry day, a leg is flagged
expired-but-not-yet-frozen by `pageLoad.js`, so it's excluded from the chart's Exp P&L
sum and the offset calc — reproducing the exact GOLDM "flat 0 curve" bug the original
fix targeted, just 8 hours early, while the Legs TOTAL row and NavStrip (which DO use
the correct boundary) keep showing the right mark-to-spot value. Fix: swap `pageLoad.js`'s
boundary checks to `tradingSessionDateIST()`, matching `expiryPnl.js`.
File: `frontend/src/lib/derivatives/pageLoad.js`.

### 3E. Snapshot rollup misclassifies equity positions as options
**Covers**: audit item (bare CE/PE regex in Snapshot rollup).
`rollupByUnderlying` and `perRootReduce` in `derivativesMath.js` still use the bare
`/(CE|PE)$/` regex that `isFOSymbol` was introduced specifically to replace — an equity
CNC/MIS position in a ticker ending in CE/PE (RELIANCE, BAJFINANCE, ACE) is misread as
an option, producing a spurious underlying row in the Snapshot grid whose qty/P&L don't
belong there, while the TOTAL row (already fixed to use `isFOSymbol`) correctly
excludes it — TOTAL stops matching the sum of displayed rows. Fix: swap both call sites
to `isFOSymbol`, matching the already-fixed TOTAL-row logic.
File: `frontend/src/lib/data/derivativesMath.js` (`rollupByUnderlying`, `perRootReduce`).

### 3F. TDAY alignment offset computed at the wrong leg's spot on a multi-month basket
**Covers**: audit item (TDAY offset wrong-x).
`_pollSpot` returns the FIRST enabled candidate's `underlying_ltp` regardless of which
month that leg belongs to, but the merged payoff curve's x-axis is anchored to the
NEAR-month contract price. If the first candidate in position order happens to be a
far-month leg, the chart's theoretical-at-spot value is interpolated at the wrong
price, making TDAY-at-spot disagree with the actual P&L it's meant to match (the
function's own stated purpose). Fix: resolve the near-month leg specifically (not
"whichever is first in array order") before reading its `underlying_ltp` for the
x-axis alignment.
File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` (`_pollSpot`).

---

## Agents

Each cluster above is a self-contained brief — dispatch agents by wave, in parallel
within a wave, bundling only the backend+frontend halves of a cluster that genuinely
require coordinated changes (e.g. 1A, 1H, 2H) into one agent; every other cluster gets
its own agent. Each agent produces ONE commit per cluster it's assigned — never bundle
two different clusters into one commit even if the same agent handles both sequentially.

- **Wave 1 (Orders+Chase)**: backend agent for 1B+1C+1E+1F+1G (backend-only, chase.py/
  background.py/orders_postback.py/orders_helpers.py/orders_place.py — sequential
  commits, one per cluster); backend+frontend agent for 1A (kill lifecycle, spans both);
  backend agent for 1D (alert routing); frontend agent for 1H (mobile layout).
- **Wave 2 (Template/Bracket)**: backend agent for 2A+2B+2C+2D+2E+2F+2G+2I (backend-only
  clusters, template_attach.py/orders.py/orders_place.py/orders_postback.py/templates.py
  — sequential commits, one per cluster); backend+frontend agent for 2H (Kite MARKET-leg
  warning, spans both).
- **Wave 3 (Derivatives+Payoff)**: backend agent for 3A (the P1 — highest priority in
  this wave, do first); frontend agent for 3B+3D+3E+3F (frontend-only clusters,
  sequential commits); backend+frontend agent for 3C (dart/number parity, spans both —
  the most structurally complex cluster in this plan, budget extra time).
- **backend-test**: pytest for every backend cluster — a regression test that fails
  against the pre-fix code is required for each, per standing rule (not just a green
  test post-fix).
- **playwright**: e2e for every frontend-touching cluster (1A/1H/2H/3B/3C/3D/3E/3F).

## Tests
- pytest: yes (every backend cluster)
- svelte-check: yes
- playwright: yes (every frontend-touching cluster)

## Commit message
Each cluster gets its own commit message, scoped to exactly that cluster — do not draft
one combined message. Convention: `fix(<area>): <what the cluster fixes>` matching this
session's existing commit style, with a `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`
trailer.

## Done when
- All 24 clusters (31 audit findings) are fixed, each as its own commit on `workshop`,
  each paired with a regression test confirmed to fail pre-fix and pass post-fix.
- Full backend suite green (`venv/bin/pytest backend/tests/ -q --tb=line`), broker
  coverage ≥80%, api coverage ≥45%, svelte-check 0 errors, vitest/playwright green, CC
  gate clean (no D/E/F).
- Self-audit per CLAUDE.md Bug Fix Workflow run on the full batch before the final
  commit: structurally unreachable code, SSOT consistency, and — specifically for 3A/3B
  — grep every consumer of the fixed MCX-expiry logic (positions, NavStrip, derivatives
  Legs/Snapshot, MarketPulse) to confirm the fix actually propagates everywhere, not
  just the primary reported surface.
- Ready for `/ddev` → `/dprod` once this plan's work is committed.

---

## Deferred (P3, ~34 items) — not in this plan's scope, logged for a future pass

**Orders+chase**: `order_correction` alert body lacks side/qty and fires urgent for a
plain OPEN→FILLED correction (policy call); dead non-prod-branch status-check branch;
`_ch_handle_attempt_error`'s false "may still be resting live" cancel-failure message;
postback follow-up re-busts the shared orders cache on every HIGH-tier chase re-quote;
`_task_open_order_watchdog`/`_task_order_poll` near-duplication (watchdog lacks the
idle gate); several DEBUG-level swallowed sweep/backstop failures; missing `exchange=`
on 3 cancel_order call sites (wrong Groww segment for MCX/NFO); `kill_chase` hardcodes
`variety="regular"` for AMO rows; ChaseCard mobile hint re-shows every route change
instead of once per session; CANCEL_FAILED/held status-label drift between OrderBook
and LogPanel; a stale "has NOT shipped yet" comment for `superseded_broker_order_ids`;
`list_active_chases` performs broker writes on an anonymous demo GET; unmasked `acct=`
in several WARNING/CRITICAL log lines; admin bulk reconcile only scans OPEN, not
CANCEL_FAILED (operator "Reconcile" button can't fix what the UI most needs fixed).

**Template/bracket**: double/triple alerts for one wing-skip/attach failure; alert
context gaps (wrong title direction, missing account/parent-id); Groww `get_gtts()`
active-only false-cancel risk (unverified, suspected); sim OCO-pairing gap (sim-only,
diverges from live); partial-attach dead-end (Retry-attach refuses a partially-attached
row, no path completes it); FILLED-with-null-fill-price silently skips attach with no
log; deleting a bracket under a resting templated order SET NULLs with no alert;
`release_template_exit` uses `row.quantity` not `filled_quantity`; two Bracket-default
resolvers (shell vs OrderTicket) can disagree; dead "resolve to side-aware default"
comments/pref path; PATCH can't clear wing_*/tp_pct/sl_pct back to NULL; stale docstring
claiming field-parity with the fill path; MCX options get the wrong TP limit-offset
default (futures offset instead of options offset).

**Derivatives+payoff**: no logging anywhere in `derivatives.py` (silent
less-accurate-fallback, untraceable later); stale comment about dropped
today-expiring contracts; dead `_fetch_baseline_pnl_map` helper (CLAUDE.md still
points to it — doc drift); `_fetch_snapshot_close_map` ignores both its own
parameters; a naive-datetime fallback that would TypeError if the tz-aware path
ever fails; Legs TOTAL / chart Exp P&L print ₹0 instead of "—" when every leg is
null; Exp-close tab ITM/OTM misclassification window after expiry but before Kite
purges the contract; holdings rollup reads `opening_quantity` as a P&L basis
(CLAUDE.md-prohibited); `positionsPnlFiltered` sums raw `pnl` while `dayTotal` uses
the SSOT path (Groww rows with no `pnl` field undercounted); suspected (unverified)
NSE Tuesday-expiry realignment assumption mismatch; minor batchQuote response-ordering
race (no generation guard, rare/low-impact); NaN/None exchange stringified to
"nan"/"None" in holiday lookup (fail-open, no crash).
