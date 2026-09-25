# Plan: Fix Proxy-Hedge Spot-Basis Bug (Exp P&L 1211× Inflation)

## Context

Operator reported the Payoff chart and Exp P&L looked wrong for GOLDM today
(the screenshot shows Exp P&L of ₹76,41,46,994 — ₹76.4 crore — against a
position where the Snapshot grid, a moment later on the same screen, correctly
shows -43K for the identical underlying). A read-only audit traced the exact
mechanism and confirmed every number in the screenshot precisely.

**Root cause (D1, confirmed)**: when every F&O leg of a root has `qty=0`
(GOLDM's options settled at today's 25-SEP-2026 expiry, leaving only 2
GOLDBEES equity legs tagged as beta-hedge proxies — `proxy_for: 'GOLDM'`), the
frontend falls back to `synthEquityOnlyStrategy()`
(`frontend/src/lib/derivatives/pageLoad.js:386-432`) to build a payoff shell
from the equity legs alone. This function was written for the case where the
equity leg genuinely IS the plotted underlying (e.g. holding GOLDBEES under
its own "GOLDBEES" tab) — it takes `spot = primary.ltp` (whichever equity
leg's own live price). It was never made proxy-aware: when the equity legs are
actually a BETA HEDGE for a *different* underlying (GOLDM), `primary.ltp`
resolves to GOLDBEES's own price (₹124.43) instead of GOLDM's real spot
(₹1,50,736) — a ~1211× difference.

That one wrong value (`strategy.spot` = 124.43 instead of 150736) cascades
through three more places, all confirmed and all explained by the exact
screenshot numbers:

- **D2**: `payoffPrevClose` (`+page.svelte:2073-2085`) has no fallback tier
  matching `payoffSpot`'s Tier 1b (added earlier this session in `1467c082`) —
  it falls straight through to the wrong `strategy.spot_prev_close` (123.44),
  while `payoffSpot` itself correctly resolves to the real front-month LTP
  (150736) via its Tier 1b. The overlay ends up showing a REAL spot next to a
  FAKE previous-close from two different instruments → CHG% = +122,012.77%,
  matching the screenshot exactly.
- **D3**: the Exp P&L calculation (`_equityLinearLegs`, `+page.svelte:2587-2604`)
  sizes an "effective quantity" against `targetSpot = strategy.spot` (124.43)
  then VALUES that quantity at the real `liveSpot` (150736) — a price-basis
  mismatch between the sizing step and the valuation step, producing almost
  exactly the audit's traced 76.4-crore figure.
- **D4**: the Legs grid's "PROXY 119.40×/388.08×" multiplier chips
  (`CandidateLegRow.svelte:243-252`) compute against the same wrong
  `strategy.spot`, showing a multiplier ~1211× too large (should read ~0.10×).

**Not expiry-specific** — audit confirmed this triggers whenever a root's F&O
legs all reach `qty=0` (expiry settlement, or simply closing every option
intraday) while a `proxy_for` equity hedge leg stays enabled. Today's GOLDM
expiry just made it visible; it's a general proxy-hedge bug, not a date bug.
Backend is not involved (`strategy_analytics` is never called on this path,
since `cleanLegs` is empty — confirmed clean).

**Also found, same neighborhood, separate latent issue**: `_eqExpPnlByKey`
(`+page.svelte:~2639-2645`) is hardwired to always value equity/proxy legs at
`liveSpot`, ignoring whatever `spot` basis is actually passed to
`_legExpPnlDisplay` — meaning the anchor-contract-basis decision made earlier
this session (C1, commit `1467c082` — the chart's own Exp P&L should use
`payoffSpot`, the anchor basis, not always front-month `liveSpot`) is silently
NOT honored for equity/proxy legs specifically. Didn't cause today's incident
(both spots coincidentally equaled 150736 here, since GOLDM has no separate
anchor contract once its options are gone), but will silently diverge the
chart from the grid the next time a root's anchor contract differs from
front-month while proxy legs are involved. Fixing this is a natural extension
of D3's fix, not a separate investigation.

## Fix approach

**1. Make `synthEquityOnlyStrategy` proxy-aware (root fix, D1).** Change its
signature to accept the target root's own live spot/prev-close as explicit
parameters, sourced from `_undLive[underlying]` (the same SSOT `payoffSpot`
and the Snapshot grid already use) at the call site (`+page.svelte:~4113-4127`).
When any of the passed equity legs carries `proxy_for` matching the current
underlying, use the passed-in target spot/prev-close for `spot`/
`spot_prev_close`/the payoff grid center — NOT `primary.ltp`/`primary.prev_close`.
When no leg is a proxy (holdings genuinely ARE the plotted underlying), keep
the existing `primary.ltp`-based behavior unchanged — that case is correct
today and must not regress. Check whether the existing `getProxyRow` helper
(already used nearby in `_equityLinearLegs`, `+page.svelte:~2595-2598`) is
reusable here instead of writing new plumbing.

**2. Make `payoffPrevClose` symmetric with `payoffSpot` (defense-in-depth,
D2).** Add the same Tier 1b fallback `payoffSpot` already has (`_undLive[sel]?.close`
when the anchor is null) to `payoffPrevClose`, before it falls through to
`strategy.spot_prev_close`. This alone would have prevented the D2 half of
today's incident even if D1's root cause had been missed, and protects against
the same asymmetry recurring for any other null-anchor case.

**3. Fix `_eqExpPnlByKey` to honor its passed-in spot basis instead of
hardcoding `liveSpot` (D3 + the latent C1-consistency issue).** Once D1 lands,
`strategy.spot` is correct, so `targetSpot` in `_equityLinearLegs` stops being
wrong — but `_eqExpPnlByKey`'s valuation step should still use whatever spot
basis its caller intends (payoffSpot/anchor basis for the chart, per this
session's C1 decision) rather than being hardwired to `liveSpot` regardless of
argument. Thread the actual `spot` parameter through instead of ignoring it.

**4. Verify the cascade fixes, don't just assume.** After 1-3 land, explicitly
confirm (live, via the operator's own GOLDM scenario if still reproducible, or
an equivalent mock fixture): the Legs grid's PROXY multiplier chips read
sensible values (~0.10×, not ~1211×) — this should self-correct once
`strategy.spot` is fixed and needs no separate code change; the Legs-tab
per-row and TOTAL Exp P&L cells (not visible in the screenshot, flagged
SUSPECT by the audit) are no longer inflated; the payoff curve's x-axis is in
the real underlying's price space (~1,28,000–1,73,000 for GOLDM, not
~106–143); ADJ/P&L no longer clamp to a wrong-scale grid edge.

## Files

- `frontend/src/lib/derivatives/pageLoad.js` — `synthEquityOnlyStrategy` (D1).
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — the
  `synthEquityOnlyStrategy` call site (pass target spot/prev-close), 
  `payoffPrevClose` (D2), `_equityLinearLegs`/`_eqExpPnlByKey` (D3).
- `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte` — no
  code change expected (D4 self-corrects via D1), verify only.

## Agents

- **frontend**: single agent, all files above — the fixes are tightly
  interdependent (D1 is the root, D2/D3 are adjacent symmetric/consistency
  fixes in the same reactive graph) and splitting risks exactly the kind of
  scattered-derivation drift this session has been fixing all day.

## Tests

- vitest: yes — unit test for `synthEquityOnlyStrategy` with a `proxy_for`
  equity leg + an explicit target spot/prev-close, asserting the shell uses
  the target values not the proxy leg's own price (this is the exact
  regression test that would have caught today's incident); a second case
  confirming the no-proxy (holdings-are-the-underlying) path is unchanged.
- playwright: yes — reproduce the operator's exact scenario as a spec (mock
  GOLDM with all option legs at qty=0, two `proxy_for:'GOLDM'` GOLDBEES legs,
  `_undLive.GOLDM.ltp = 150736`), asserting the Payoff overlay's CHG% is a
  sane, small percentage (not five orders of magnitude off) and Exp P&L is
  within the same order of magnitude as the Snapshot grid's value for the
  same underlying — the end-to-end regression test.
- svelte-check: yes.

## Commit message

fix(derivatives): proxy-hedge equity legs no longer corrupt Exp P&L and the
Payoff overlay when all F&O legs of the hedged root reach qty=0 — spot basis
was silently the proxy ETF's own price instead of the hedged root's

## Done when

`synthEquityOnlyStrategy` is proxy-aware with a passing regression test;
`payoffPrevClose` has the same fallback symmetry as `payoffSpot`;
`_eqExpPnlByKey` honors its passed-in spot argument; the end-to-end Playwright
spec reproducing the operator's exact GOLDM scenario shows sane numbers;
`npx vitest run` and `npx svelte-check` green; self-audit confirms the
no-proxy (holdings-are-the-underlying) path is unchanged by diffing its
existing test coverage before/after.
