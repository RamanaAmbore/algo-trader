# Plan: Simplify draft positions to "a real position, with 3 exceptions"

## Context

This session built draft positions (hypothetical, no-broker-call legs for what-if payoff/Greeks
analysis) as a feature carefully *excluded* from every real financial aggregate — a dedicated
merge function, explicit totals-exclusion guards, a `showDraftInPayoff` toggle, CSV labeling, etc.
The operator asked to simplify this: a draft should behave like **any other position on every
surface**, with exactly three differences — (1) it's created via an order using live LTP with no
margin check, (2) it has no chase/no bracket, (3) it can be "entered as a real position"
(promoted) but can **never be closed** — only the existing Clear action removes it.

Two research passes (this session, verified directly against the code, not guessed) found the
risk here is much smaller than it first looked:

- **The close-action danger is already fully mitigated.** Every close-defaulting entry point in
  the app (`MarketPulse.svelte`'s row-click → `_openTicketFromRow`/`_openTicketFromDraftRow`
  branch, derivatives' `closePosition()`, `CandidateLegRow`'s `isClosable`) already explicitly
  checks `source === 'draft'` and routes to the promote-to-real path instead of a close — built
  earlier this session, confirmed correct by direct trace. Admin cancel-all (cancels broker open
  orders only), chase/agent-driven closes (read the broker raw-cache directly, never
  `/api/positions`), and `SymbolContextMenu` (not mounted in MarketPulse) are all structurally
  incapable of ever acting on a draft. **No new work needed here — this invariant already holds.**
- **NavStrip was never wired to see drafts at all.** It reads `portfolioStore`/
  `positionsDayPnlStore`, computed from the raw broker fetch, completely independent of
  `pulseUnified.js`'s draft-merge logic. So removing MarketPulse's totals-exclusion only affects
  MarketPulse's own grid-local pinned TOTAL row and its Exp-P&L-by-symbol lookup — **not** the
  NavStrip headline P value. These were always two separate things, never one risk.
- **The merge must stay display-layer-only, never baked into the broker-polled cache.** Operator
  confirmed: a draft can never be "synced with broker positions" or "updated using broker books."
  `fetchPositions()`/`positionsStore` must stay 100% pure real-broker data, so the existing
  staleness/degraded-freeze logic keeps working unaware that drafts exist. MarketPulse's existing
  `mergeDraftRows()` (in `pulseUnified.js`, called from `buildUnified()`) already does this
  correctly — it reads `payoffDrafts.value` live, at render time, never cached together with the
  broker response. This pattern is kept, not rewritten.
- Backend's `/positions` response (`PositionsResponse` — `rows` + independently-server-computed
  `summary`/`symbol_summary`/`stale_accounts`) is untouched by any of this — the frontend never
  recomputes `summary`/`symbol_summary` from `rows` either, so there's no consistency gap to fix.

## Approach

### 1. Remove MarketPulse's totals-exclusion (the only real behavior change)

- `frontend/src/lib/data/pulseUnified.js`: delete `accumTotalsRow()`'s early-return on
  `source === 'draft'` (~line 1112-1118) — a draft's qty/pnl/day_pnl now sums into MarketPulse's
  own grid-local pinned TOTAL row, like any other row.
- `frontend/src/lib/MarketPulse.svelte`: delete `_expPnlForRow()`'s draft-null guard (~line 718) —
  a draft now gets the same Exp P&L/Extrinsic lookup as a real row.
- Keep everything else exactly as built: `mergeDraftRows()`, `finalizeRows()`'s skip-recompute
  branch, `sortDraftsLast()`, the ag-Grid `postSortRows` bottom-grouping, `row-draft` CSS styling,
  the `symRenderer` hiding of ⋯/move buttons for drafts, the CSV `[DRAFT]` export prefix, and —
  critically — the close-action routing in `handleRowClick` (`_openTicketFromDraftRow` instead of
  the close-defaulting path). None of these are "exclusion from real aggregates" — they're either
  display mechanics (sort/style) or the close-safety invariant, and both stay unchanged.

### 2. Derivatives page — drop the `showDraftInPayoff` toggle, drafts always count

- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`: remove the 4
  `showDraftInPayoff`-gated exclusions (`candidatesDayPnl` ~2461, `_sumEnabledLegsExpPnl`'s filter
  ~2709 — wait, verify these are the *draft-source* checks, not the pre-existing `_isLegEnabled`
  per-row toggle, which stays — ~2790, ~2834) and the toggle UI itself (~5886). The existing
  per-row enable/disable checkbox (`enabledSymbols`/`_isLegEnabled`) already lets the operator
  exclude any individual leg — draft or real — from the payoff/Greeks calc; a separate
  draft-specific toggle is redundant once drafts are "just a position." Keep `closePosition()`'s
  `source === 'draft'` refusal and `CandidateLegRow`'s `isClosable` guard — those are the
  close-safety invariant, not the totals toggle, and must not be touched.
- Leave the existing bottom-grouping (`+page.svelte` ~823) and the `[DRAFT]`-equivalent visual
  tag (`.cand-draft` dashed border) as-is.

### 3. Extend the same merge pattern to PerformancePage (new coverage, not a rewrite)

- `PerformancePage.svelte` has no draft awareness today — it calls `fetchPositions()` directly
  (same shared function `positionsStore` uses) and builds its own rows independently of
  `pulseUnified.js`.
- Add the equivalent of `mergeDraftRows()` at PerformancePage's own display-derive step (read
  `payoffDrafts.value` live, same as MarketPulse does) — reuse the merge logic rather than
  duplicating it: extract `mergeDraftRows()`'s row-shaping logic from `pulseUnified.js` into a
  small shared helper (e.g. `frontend/src/lib/data/draftRowMerge.js`) that both `pulseUnified.js`
  and `PerformancePage.svelte` call, so there's one implementation, not two. Apply it strictly at
  PerformancePage's own render/derive boundary — never inside `fetchPositions()` or any shared
  cache, per the "never updated via broker books" invariant in the Context section.
- PerformancePage's own equivalent of a pinned TOTAL row (if one exists — verify directly before
  implementing) should include drafts, matching MarketPulse's new behavior from §1. Its own
  equivalent of a close action (if any) must get the same `source === 'draft'` refusal already
  proven correct in MarketPulse/derivatives — verify PerformancePage's close-entry-points directly
  (don't assume none exist) before calling this done.

### 4. NavStrip — no change, confirmed out of scope

NavStrip (`PositionStrip.svelte`) reads `portfolioStore`/`positionsDayPnlStore`/
`positionsDerivedStore`, none of which have any path to `payoffDrafts` or the
`pulseUnified.js`/PerformancePage merge logic. Nothing to do here — explicitly noting this so a
future reviewer doesn't assume NavStrip needs the same treatment; it structurally can't see drafts
regardless of what §1-3 do.

### 5. Self-audit before shipping

- Re-grep every `source === 'draft'`/`c.source === 'draft'` check across the touched files after
  the changes above — confirm exactly which ones were removed (totals-exclusion) vs. which were
  deliberately kept (close-safety, styling, sort order) matches this plan precisely, not more and
  not less.
- Confirm `payoffDrafts` is never read inside `fetchPositions()`, `positionsStore`'s own fetch/
  cache logic, or anything server-side — the merge stays 100% client-side, display-layer-only,
  everywhere it's applied.
- Confirm no double-counting: the derivatives page's own `_allDrafts`/`payoffDrafts.value` merge
  (for its candidatePositions/payoff pipeline) is independent of MarketPulse's/PerformancePage's
  merge (different consumers, different arrays) — no shared array where both would apply.

### Tests

- Update/add vitest coverage in `pulseUnified.test.js` for `accumTotalsRow` now summing draft rows
  (the test the second agent added this session asserting totals are UNaffected by drafts needs to
  flip to asserting they ARE now included).
- New Playwright coverage: a draft row's qty/pnl now appears in MarketPulse's pinned TOTAL row;
  the derivatives payoff/Greeks always include enabled drafts (no toggle); PerformancePage shows
  draft rows with the same styling/bottom-grouping as MarketPulse; the close-action refusal still
  holds on every surface after these changes (regression-guard the existing invariant, don't just
  assume it survives the totals-inclusion change).
- Keep the close-routing and promote-to-real Playwright coverage already written/in-progress from
  earlier this session — unaffected by this change.

### Process

- Dispatch one frontend agent for §1+§2 (same files already touched this session — bundle, don't
  split). Dispatch a second for §3 (PerformancePage, genuinely new file/area). Dispatch playwright
  agent for test updates after both land.
- Full gate suite (svelte-check, vitest, Playwright for touched specs) before commit. Push to
  `dev` only — no `main` push without an explicit "deploy" instruction.

## Done when

- MarketPulse's and PerformancePage's pinned totals/aggregates include drafts, same as any real
  position; the `showDraftInPayoff` toggle is gone from the derivatives page.
- Every close-action entry point (MarketPulse, derivatives, PerformancePage — verified, not
  assumed) still refuses to act on a draft row; only Clear and the promote-to-real click can
  remove/convert one.
- `payoffDrafts` is never read inside any broker-fetch/cache path — confirmed via grep, not
  assumption.
- svelte-check clean, vitest green, relevant Playwright specs green, pushed to `dev`.
