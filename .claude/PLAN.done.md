# Plan: Derivatives cross-surface SSOT consolidation (spot, prevClose, Day P&L, Exp P&L, position existence)

## Context

Trigger chain: a GOLDM screenshot showed the Payoff chart/Legs tab reporting
"all positions closed" the day after expiry, while Snapshot/NavStrip (reading
`portfolioStore`) correctly showed 17 legs / -36K P&L / -43K Exp P&L for the
same underlying. Root cause confirmed: `buildCandidatePositions` and
`buildCleanLegs` (`frontend/src/lib/derivatives/pageLoad.js`) silently drop
any row with nonzero quantity once its contract's expiry passes or it ages
out of Kite's instruments master — a rule `portfolioStore`'s own pipeline
doesn't have.

The operator rejected two narrower fixes (a direct patch to the filter
condition, then a single-field store export) and asked for the underlying
pattern to be eliminated everywhere: "single spot or ltp price should be
used from store all surfaces. similarly the other values. update plan for
all the issues mentioned." A full audit (agent `a5c58369565415989`) was run
across NavStrip, Snapshot, the Legs tab, and the Payoff chart, for: spot/LTP,
prevClose, Day P&L, Exp P&L, Extrinsic, position existence, and other values
(Greeks/IV/DTE/EV/POP/breakevens). 14 independently-derived values were
found; 9 can show a wrong or missing number, not just a duplicated one.

This plan implements the 9 confirmed defects as an **ordered sequence of
separate, independently-tested commits** (not one bundled commit — per the
standing rule that defect fixes ship one at a time) and defers 5 lower-risk/
cosmetic divergences to Phase 2 (documented, not implemented now).

DB check performed during planning: `positions` are not persisted (fetched
live from broker each request) so a live Groww exchange-value check isn't
possible via DB; `daily_book` is keyed per-account per-held-instrument, so
it has no row for an underlying that isn't itself a held position — this
resolved the scope of item 8 below (operator confirmed: held-instruments-
only basis, Kite fallback for pure/unheld underlyings, explicitly labeled).

## Commit sequence (each green before the next starts)

**Commit 1 — Split-path Day P&L (must land first: item 1's row-source
switch depends on this being correct first, per the audit's own ordering).**
`expiryPnl.js`'s `splitClosedReopened`, for a position fully closed today
(`oq≠0`, `brokerQty===0`), currently re-derives Day P&L as
`(exit − prev_close) × min(oq, dsq)` instead of the canonical baseline-diff
formula (`current_total_profit − base_pnl`, CLAUDE.md's "Frontend Day P&L
SSOT"). Fix: compute `baseDayPnlForPosition(rawRow)` once on the **unsplit**
raw row before splitting, and assign that value to the closed row when the
position is fully closed. This sidesteps the `prev_close ≤ 0` sentinel
problem entirely (the baseline-diff formula doesn't depend on `prev_close`).
Test: vitest — closed-row Day P&L equals `baseDayPnlForPosition` on the raw
row, including the `prev_close` sentinel case.

**Commit 2 — Fix the GOLDM bug: one position-existence source.**
Read the handler behavior first (`/strategy-analytics` backend route) to
confirm whether an expired/unrecognized leg fails the whole request or is
skipped per-leg — this determines the exact mechanics below; do not guess.
- `+page.svelte`'s own `positions` array (built from `positionsStore.value`,
  falling back to `pulsePositionsStore`, restored from sessionStorage,
  frozen on empty reads with no `degraded` check) is replaced by reading a
  new `portfolioStore` export — the full-detail F&O row array (`_posTier3`,
  currently internal), exposed as e.g. `portfolioStore.positions.rows` —
  plus the page's own sim/draft rows and holdings rows (see Commit 6) merged
  in on top.
  - Before removing the sessionStorage restore / empty-freeze guard,
    verify `portfolioStore`/`positionsStore` already covers (a) first paint
    on a cold load and (b) the closed-hours frozen-snapshot requirement
    (memory `feedback_market_close_snapshot`: an empty grid after close is
    a defect). If it doesn't, port the equivalent guard into the store
    (once, canonically) rather than deleting page-side resilience with
    nothing replacing it.
  - Rows must stay unsplit at the store level; the page still runs
    `splitClosedReopened` (now fixed by Commit 1) for CLOSED/OPEN display.
  - Copy `exchange` onto every row (currently dropped by
    `buildPositionRowFromBroker`).
  - Derive `kind` (`fut`/`opt`) on the page from the symbol (not carried by
    `_posTier3`).
  - Per-leg LTP falls out of this for free: once Legs/Snapshot/store share
    one row source, each row's `ltp`/`last_price` field is the same value
    everywhere — add a vitest assertion for this instead of leaving it
    unverified.
- Remove the three qty-gated exclusion checks in `buildCandidatePositions`
  (`pageLoad.js:171,174,176`) — do not hide a row from the Legs
  grid/candidates list for expiry-mismatch / missing-from-instruments-
  master / past-expiry; tag it instead (e.g. `_expired: true`) so it stays
  visible and counted in totals.
  - In `buildCleanLegs` (`pageLoad.js:324`, feeds the `/strategy-analytics`
    request payload): based on what the handler-read above shows — if one
    bad leg fails the whole request, EXCLUDE tagged-expired legs from the
    request payload only (keep them in the display/candidates list, and in
    Day/Exp P&L totals, computed client-side from the row's own fields
    since the backend curve won't include them); if the backend already
    skips bad legs per-leg safely, legs may be sent through untagged for
    the request too. Document whichever behavior is implemented as a code
    comment referencing this decision, since it's non-obvious.
  - The Payoff curve itself: an expired-but-held leg is excluded from the
    curve shape (it has no forward payoff) but its current MTM/Exp P&L
    still counts in the totals shown alongside the chart.
- Fix the account matcher in `buildCandidatePositions` (`pageLoad.js:151-153`,
  exact-match) to use `buildAcctMatcher` (`derivativesMath.js:25`,
  trim+uppercase) like the rest of the page.
- Adopt `rootOf.js` (exists, currently unused here) as the single
  root-identity function on this page/store, replacing the four ad-hoc
  derivations (`decomposeSymbol().root`, `symbol.replace(/\d.*$/,'')`, the
  `^${target}\d` prefix regex, `inst.u`).
Additional symptom reported (same root cause): the OPEN/CLOSED chip labels
in the Legs tab, and in the Expiry Close analysis (`expiryCloseAnalysis`,
touched again in Commit 4), also disappeared for GOLDM — a direct
consequence of the qty-gated filters dropping the rows those chips are
rendered on (`splitClosedReopened`'s output rows never reached
`CandidateLegRow` at all, so there was nothing to label). No separate fix
needed beyond Commit 1 (correct split Day P&L) + Commit 2 (rows no longer
dropped) — but add explicit verification: once fixed, confirm the
CLOSED/OPEN chips render correctly on the restored split rows, not just
that the rows/numbers reappear.

Tests: vitest — GOLDM scenario row (nonzero qty, expiry dated yesterday,
symbol absent from mocked instruments) is included and tagged, not dropped;
`portfolioStore.positions.rows` and the Legs candidate list agree on leg
count for a given root under identical mock data. playwright — reproduce
the GOLDM scenario end-to-end: Legs tab and Payoff chart show the same leg
count and P&L as Snapshot/NavStrip, not the "all positions closed"
placeholder, AND the CLOSED/OPEN chip labels are present on the
corresponding split rows in both the Legs tab and Expiry Close analysis.

**Commit 3 — Legs per-row P&L cell.**
`CandidateLegRow.svelte:110-114` reads `positionsDerivedStore.get(c.symbol).pnl`
— summed across accounts and both halves of a CLOSED/OPEN split row, so it
double-counts whenever more than one account holds the symbol or a split
exists. Change to read the row's own `c.pnl` (per-row, post-split), matching
the pattern already used for Day P&L (`+page.svelte:1188-1197`). Update the
TOTAL sum (`+page.svelte:5268`) and the flash-key cross-account reads
(`:1112/1116/1845`) to match.
Test: vitest — two-account same-symbol scenario, TOTAL equals sum of
distinct per-row values, not a multiple of the aggregate.

**Commit 4 — Single spot resolver.**
Nine independent spot chains found (`getUnderlyingSpot`, `_undLive`,
`liveSpot`, `payoffSpot`, `_clientPayoffStub`, `expiryCloseAnalysis`'s
resolver, `strategy.spot`, `_pollSpot`, the Extrinsic anchor). Consolidate
on `getUnderlyingSpot` (`underlyingSpotStore.svelte.js`) — already has the
most complete fallback chain (resolved tradingsymbol → bare root →
batchQuote cache). Rebuild `_undLive`, `liveSpot`, `_clientPayoffStub`, and
`expiryCloseAnalysis`'s spot resolver on top of it (this also fixes
`expiryCloseAnalysis`'s index-name mismatch, e.g. "NIFTY" vs "NIFTY 50", and
its spot-defaults-to-0 band-misclassification bug). Keep exactly one
deliberate exception: `payoffSpot`'s anchor-contract tier (prior operator
decision, referenced as C1) stays as-is. Make `_equityLinearLegs`' proxy
`effQty` value at the same spot basis its caller passes, instead of mixing
the anchor price with a different valuation spot on contango MCX roots.
Test: vitest — cold-start/no-tick scenario where `getUnderlyingSpot` and the
old `liveSpot` chain previously disagreed now agree via the shared
resolver.

**Commit 5 — Legs Exp P&L cell reads the store.**
Snapshot already reads `portfolioStore.expPnlRows` in live mode
(confirmed single-source). The Legs per-row cell (`+page.svelte:5200`),
Legs TOTAL (`:2282`), and the chart readout (`:2292`) still independently
compute via `_legExpPnlDisplay`. With Commits 1–2 landed (unified row
source), read the store's per-row Exp P&L for live rows in the Legs cell
and TOTAL too. Keep a local compute only for: draft/unsaved legs, sim-mode
rows, and the chart's own `payoffSpot`-basis readout (deliberately
different spot, per Commit 4).
Test: vitest — Legs TOTAL for a live root equals Snapshot's Exp P&L for the
same root under identical data.

**Commit 6 — Equity/proxy Day P&L + holdings single fetch.**
`buildHoldingRowFromBroker` carries no `realised`/`unrealised`/
`prev_settlement_pnl`, so `baseDayPnlForPosition(holdingRow)` falls back to
lifetime `pnl` instead of a real Day P&L. Rather than re-implementing the
holdings formula in the page (which would just create a new duplicate
derivation), read `portfolioStore`'s existing per-holding Day P&L value
directly. This requires collapsing the page's separate `holdingsStore`
fetch and the store's `pulseHoldingsStore` fetch onto one source (the audit
flagged this as a second, related divergence — fix both together since
they're the same root cause). This fixes the Legs eq-row Day P&L cell and
the Payoff overlay DAY row (`+page.svelte:2108-2124`), and should make
`_legsDayPnlTotal` (`:2222`) reconcile with the overlay DAY row.
Test: vitest — holdings Day P&L uses `(ltp−prev_close)×qty`, not lifetime
`pnl`; page and store read the same holdings snapshot (mock one fetch,
assert both consumers see it).

**Commit 7 — Single F&O predicate.**
`portfolioStore` gates F&O status on `exchange ∈ {NFO,MCX,CDS,BFO}`; the
page gates on a symbol regex. Groww's adapter passes through
`p.get("exchange")` unchanged (code comment: "exchange stays NSE" for F&O),
which could exclude Groww F&O rows from the store's gate while the page's
regex still includes them — live DB verification isn't possible (positions
aren't persisted), so resolve this by removing the exchange dependency
entirely: adopt the page's exchange-independent symbol-regex predicate as
the single shared function (home: `derivativesMath.js`, checked for import
cycles first), used by both `portfolioStore` and the page. Guard the regex
so it can't match an equity tradingsymbol (require an expiry/digit segment
before CE/PE/FUT, not just a suffix match).
Test: vitest — a Groww-sourced F&O row with `exchange: 'NSE'` is classified
as F&O by both the store's gate and the page's gate, using the same
function.

**Commit 8 — Underlying P.Close, held-instruments-only basis (operator-
confirmed scope).**
Per the standing "close_price / ltp invariant," a position's own
`prev_close` must be `daily_book.ltp` from the most recent settlement
snapshot, already correct since commit `93689676`. This commit extends that
basis to the **underlying's own P.Close/Chg%** display (Snapshot header,
Payoff `payoffPrevClose` non-anchor fallback tier) — but only when the
underlying itself is a held position/future (a `daily_book` row exists for
it). `daily_book` is keyed per-account per-held-instrument, so a pure index
or an unheld front-month future has no such row: for those, keep Kite
`ohlc.close`/`quote.prev_close` as an explicit, clearly-labeled fallback
(comment stating why, referencing this decision) — do not attempt a
backend change to snapshot unheld underlyings in this pass.
Test: vitest — held-future-as-underlying case uses `daily_book.ltp`;
pure-index case falls back to Kite close with the fallback path exercised
in the test (not just the happy path).

**Commit 9 — Inline cleanup (small, low-risk, bundled at the end).**
- Fix `expiryByAcct`'s key casing (`portfolioStore.svelte:256`, raw
  `p.account` vs uppercase everywhere else).
- Remove the computed-but-unused `_excluded`/`excluded` add-back
  (`+page.svelte:3954,3994`) and correct the stale "matches NavStrip P1
  exactly" docstrings (`:3746-3755`, `:1176`).
- Grep consumers of `_isLegExpired` (`<=` today) vs the candidates/
  `buildCleanLegs` cutoff (`<` today) before touching either — this governs
  expiry-day behavior itself (what the original bug report was about), not
  pure cleanup. Only unify if the grep shows they're meant to agree; if
  they serve genuinely different purposes (e.g. one for "still tradeable"
  and one for "still has time value"), leave both and document why they
  differ instead of forcing a match.
- Delete dead code superseded by this refactor:
  `byRoot`/`byRootPositions`/`byRootHoldings`/`getByRoot` (no consumers;
  `_byRootHoldings`'s proxy formula is dimensionally wrong anyway), the
  `setHoldingsFromPulse`/`_pulseHoldings*` override (no callers, superseded
  by Commit 6), the `pair_group_key` sort (page rows never carry the
  field), the dead `lots` fast path in `lotsForRow`, and `_rootSpot`
  fallback 4 (unreachable outside sim mode).
Test: svelte-check clean (no orphaned imports); vitest suite still green
after deletions (proves nothing was actually load-bearing).

## Phase 2 — deferred (documented, not implemented this pass)

- EV/POP: backend Python + a JS port used when eq legs are enabled — two
  implementations, not confirmed to diverge in practice.
- Breakevens/MaxP/MaxL: three derivations (backend `risk`, `_mergedRisk`,
  `OptionsPayoff.adjustedBreakevens`) — used under different, currently
  non-overlapping conditions.
- Snapshot's "EV" column mixing real EV for the selected root with Exp P&L
  for every other root in the same TOTAL.
- Extending `daily_book`-style settlement snapshots to unheld/index
  underlyings (would make Commit 8's fallback unnecessary) — real backend
  work, own plan if pursued.
- Broader dead-code sweep for anything not directly touched by this plan.

## Files

- `frontend/src/lib/data/expiryPnl.js` — Commit 1.
- `frontend/src/lib/data/portfolioStore.svelte.js` — Commits 2, 6, 7, 9
  (export `positions.rows`; holdings single-fetch; shared F&O predicate;
  `expiryByAcct` casing).
- `frontend/src/lib/derivatives/pageLoad.js` — Commits 2, 3 support (account
  matcher, `rootOf.js` adoption, candidate/cleanLegs filter removal).
- `frontend/src/lib/data/derivativesMath.js` — Commit 7 (shared predicate
  home).
- `frontend/src/lib/data/underlyingSpotStore.svelte.js` — Commit 4.
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — Commits 2,
  4, 5, 6, 8, 9.
- `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte` —
  Commit 3.
- `frontend/src/lib/rootOf.js` — reused (Commit 2), not modified unless a
  gap is found.
- `backend/api/routes/...` (the `/strategy-analytics` handler) — read-only
  investigation for Commit 2's expired-leg mechanics; no backend edit
  expected unless the investigation shows the whole-request-fails behavior
  needs a backend-side per-leg skip instead (would need operator sign-off
  to add backend scope — flag, don't just do it).

## Agents

- **frontend**: single agent working the commit sequence in order,
  committing after each item goes green, rather than one giant diff at the
  end — satisfies both "one bundled agent for coupled files" (avoids
  re-introducing the divergence via a split-team race) and "ship one defect
  at a time" (each commit is independently reviewable/revertable). Status
  checkpoint at the ~30 min mark (report which commits are done) rather
  than continuing silently past it.
- **backend**: skip for implementation; the Commit 2 handler read is a
  quick read-only check the frontend agent (or a fast Explore pass first)
  can do without a dedicated backend agent.
- **doc**: update `docs/specs/` derivatives spec if one exists (else the
  relevant CLAUDE.md Day P&L / Exp P&L reference rows), fix stale "matches
  NavStrip P1 exactly" doc references, and add the Commit 8 fallback
  decision to CLAUDE.md's close_price invariant section (it's a scoped
  extension of a protected rule, so it should be documented there too, not
  just in a commit message).
- **backend-test**: skip.
- **playwright**: yes — GOLDM scenario (nonzero-qty position, expiry dated
  yesterday, symbol absent from a mocked instruments response): Legs tab
  and Payoff chart show the same leg count and P&L as Snapshot/NavStrip
  under no filters, instead of the "all positions closed" placeholder.

## Tests

Per-commit tests specified above. Full gate before calling this plan done:
`npx vitest run`, `npx playwright test`, `npx svelte-check` all green.

## Commit messages

One per commit, prefixed `fix(derivatives):`, each naming the specific
divergence it removes (see each commit's heading above for the summary to
use) — not a single combined message, since these ship as separate commits.

## Done when

- Legs tab/Payoff chart and Snapshot/NavStrip agree on leg count, P&L, Exp
  P&L, and (for held-instrument underlyings) prevClose for the same
  underlying under identical filters, by construction, not coincidence.
- Today's GOLDM scenario is covered by a regression test (vitest +
  playwright) and passes, including the OPEN/CLOSED chip labels reappearing
  on the correct split rows in the Legs tab and Expiry Close analysis.
- All 9 commits landed separately, each with its own green test run.
- Phase 2 items are documented as deferred, not silently dropped.
- `npx vitest run`, `npx playwright test`, `npx svelte-check` all green at
  the end of the sequence.
