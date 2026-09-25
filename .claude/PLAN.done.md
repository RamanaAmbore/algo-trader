# Plan: NavStrip vs Snapshot Exp P&L SSOT Fix

## Context

Operator: NavStrip's Exp P&L total doesn't match the sum of the Snapshot grid's
per-row Exp P&L values — "ssot issue. everything possible has to come from
store, with no additional derivation. any derivation has to be applied within
store for the store value not outside." A read-only audit traced the exact
mechanism: this is genuinely a duplicated-derivation bug, not a spot-basis
issue (the anchor-vs-front-month split from the just-shipped Payoff-chart fix,
commit `1467c082`, was checked and ruled out — neither surface reads
`payoffSpot`).

**Root cause (confirmed)**: NavStrip and the Snapshot grid derive the
*realised* component of Exp P&L from two different places, independently:
- **Snapshot** (`frontend/src/lib/derivatives/pageLoad.js`) runs every position
  through `splitClosedReopened`, which rebuilds a precise realised P&L for
  partial closes, full closes, and intraday round-trips from the frontend's
  own entry/exit-price math — then feeds that into `expiryPnlWithRealised`
  (`frontend/src/lib/data/expiryPnl.js`).
- **NavStrip** (`frontend/src/lib/data/portfolioStore.svelte.js`) uses the
  UNSPLIT position rows and passes the broker's raw `realised` field straight
  through into the same `expiryPnlWithRealised` helper.

These only agree when the broker's raw `realised` happens to equal the
frontend's own precise derivation — and per `docs/specs/PULSE_SPEC.md:1343`,
Kite is already documented to ship `realised=0` alongside a non-zero `pnl` on
settlement, meaning the two are known to diverge on ordinary same-day partial/
full closes. Worked example from the audit: overnight short 150 NIFTY CE @200,
75 bought back today @150, OTM — Snapshot correctly shows 18,750 (3,750 closed
+ 15,000 open); NavStrip shows 15,000 + whatever Kite's `realised` happens to
be (documented to often be 0) — a ₹3,750 gap on this one position alone.

This is exactly the SSOT violation the operator flagged: the more precise
derivation (`splitClosedReopened` + `expiryPnlWithRealised`) exists in only ONE
of the two consuming surfaces instead of living once in the store both read
from.

**Also found (secondary, lower severity)**: when Snapshot has an account/
strategy/sim/search filter active, or during a brief degraded-fetch window, it
and NavStrip legitimately show different numbers (NavStrip = whole book,
Snapshot = filtered subset) — this is by design, not a bug, but Snapshot's own
column tooltip currently claims unconditional equality ("Sums to the NavStrip P
slot 3 value"), which is only true when unfiltered. Also found: the two
surfaces use slightly different underlying-spot fallback chains and strike-
resolution sources, causing rare additional drift in closed-hours or
weekly-contract-on-digit-root edge cases.

## Fix approach

**1. Make `portfolioStore.svelte.js` use the SAME realised-P&L derivation
Snapshot already uses, instead of the raw broker `realised` field.**
`splitClosedReopened` (`pageLoad.js:213-384`) is currently only ever called
from the derivatives page's own load path. Move it (or extract its pure
computation) into `frontend/src/lib/data/expiryPnl.js` — the file that's
already the shared, page-agnostic home for `expiryPnlWithRealised` — so it's
importable by `portfolioStore.svelte.js` without a derivatives-page dependency.
Apply it to the raw position rows `portfolioStore` already holds, BEFORE
computing each row's Exp P&L, so the store's own `exp_pnl` values (and
therefore `positions.total.exp_pnl`, which `PositionStrip.svelte`'s NavStrip
already reads verbatim with no further math) are now built from the precise
split-aware realised figure — matching Snapshot's derivation exactly, computed
once, in the store.

**2. Have the Snapshot grid read its per-row Exp P&L from `portfolioStore`'s
now-canonical values instead of recomputing them independently in
`pageLoad.js`/`+page.svelte`.** Snapshot's TOTAL and per-row cells should
become "the store's per-row value, filtered by whatever account/strategy/sim/
search scope is currently active" — not an independent recomputation. This is
the concrete form of the operator's directive: derivation happens once, in the
store; every consumer (NavStrip's unfiltered total, Snapshot's filtered total,
Snapshot's per-row cells) reads that same pre-derived number and only adds
*filtering* (a scope decision, not a math derivation) on top.

**3. Handle the backend-strike-enrichment gap explicitly, don't silently drop
it.** Snapshot currently passes `legAnalyticsBySymbol` (backend-resolved
strikes) into its Exp P&L calc for symbols `decomposeSymbol` can't parse
(weekly contracts on digit-containing roots) — `portfolioStore` doesn't have
this data today. Check whether `legAnalyticsBySymbol` (or equivalent) is
already available somewhere the store could reach it without a new backend
call; if genuinely not, document this as a known, narrow, identical-on-both-
surfaces limitation (both NavStrip and Snapshot will now consistently omit
these rare unparseable legs, rather than one showing them and one not) —
consistency is the goal here, not necessarily fixing 100% of symbol parsing.

**4. Unify the underlying-spot fallback chain.** `portfolioStore.svelte.js`'s
`getUnderlyingSpot`/`_rootSpotCache` resolution (already the CLAUDE.md-
documented SSOT utility for underlying spot resolution — see the "Underlying
spot resolution" entry in the Common Tasks table) should be the ONE fallback
chain both surfaces use. Have Snapshot's `_rootSpot`/`_underlyingQuotes` path
delegate to `getUnderlyingSpot` instead of maintaining its own separate 30s-
batch-quote-cache fallback, eliminating the closed-hours/no-live-tick
divergence the audit found.

**5. Fix the misleading tooltip.** Snapshot's TOTAL column tooltip
(`+page.svelte:5333`, "Sums to the NavStrip P slot 3 value") should only claim
that when no account/strategy/sim/search filter is active — either make the
tooltip text conditional on filter state, or rephrase it to something
accurate regardless of filter state (e.g. "Sum of the rows shown below").

**6. Test fix**: `expiryPnl.test.js:408` currently locks in "realised=0 → 0"
as correct behavior — per the audit, this contradicts the established both-
zero-fields-fall-back-to-`pnl` convention already used elsewhere in this
codebase (`nav.js:currentTotalProfit`, the backend's
`baseline_diff_day_pnl_expr_with_fallback`). Update it to match that
convention. Add a regression test reproducing the audit's exact worked
example (Kite-style partial close, `realised` reported as 0 alongside a real
`pnl`) asserting NavStrip's total now equals Snapshot's TOTAL with no filters
active — this is the test that would have caught this bug.

## Explicitly out of scope

- Rank 2's scope-filter differences (account/strategy/sim/search) are by
  design, not touched — only the misleading tooltip (item 5) is fixed.
- The transient degraded-fetch-window divergence (NavStrip freezes to last-
  known-good per the just-shipped 0-value fix, commit `1467c082`; the
  derivatives page's own `positions` doesn't yet have that same freeze) — this
  is the SAME class of fix as `1467c082` but a different call site; flag as a
  follow-up candidate, not fixed in this pass, since it's a narrower, lower-
  frequency issue than the realised-P&L SSOT bug.

## Files

- `frontend/src/lib/data/expiryPnl.js` — home for the shared
  `splitClosedReopened` logic (moved/extracted from `pageLoad.js`), the
  both-zero-fallback fix to `expiryPnlWithRealised`.
- `frontend/src/lib/data/portfolioStore.svelte.js` — apply the split-aware
  realised derivation before computing `exp_pnl`; delegate spot resolution
  consistently (already the source of `getUnderlyingSpot`).
- `frontend/src/lib/derivatives/pageLoad.js` — `splitClosedReopened` becomes a
  thin re-export or is removed if fully subsumed by the moved version in
  `expiryPnl.js` — check call sites before deciding which.
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — Snapshot's
  per-row and TOTAL Exp P&L cells read from `portfolioStore`'s canonical
  values (filtered), not an independent recomputation; tooltip fix; spot-
  resolution delegation to `getUnderlyingSpot`.

## Agents

- **frontend**: single agent, all files above (one coherent SSOT
  consolidation — splitting risks exactly the kind of duplicated-logic drift
  this plan is fixing).

## Tests

- vitest: yes — `expiryPnl.test.js` update (both-zero fallback), new
  regression test for the Kite-style partial-close worked example.
- playwright: yes — a spec confirming NavStrip's Exp P&L total equals
  Snapshot's unfiltered TOTAL for a realistic multi-leg scenario (mock data,
  since no live account is available in this session).
- svelte-check: yes.

## Commit message

fix(derivatives): NavStrip/Snapshot Exp P&L SSOT — derive realised P&L once
in portfolioStore instead of independently in each surface

## Done when

Both surfaces read Exp P&L from the same store-level derivation with no
component-level recomputation; the Kite-partial-close regression test passes;
`npx vitest run` and `npx svelte-check` green; self-audit confirms no other
consumer of the old, now-removed/re-homed `splitClosedReopened` was left
calling a stale copy.
