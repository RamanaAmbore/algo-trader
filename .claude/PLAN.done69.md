# Plan: Derivatives Legs consolidation + Chain spread-gate close exemption

## Task

Two independent, frontend-only bug fixes on the derivatives page, reported by the operator:

1. **Legs view shows split pieces, not consolidated rows.** The "Legs" dropdown option
   (`legsTab === 'legs'`, the new unpaired, account-grouped default) is supposed to show
   "positions consolidated on account, and option" but instead shows each lot-waterfall-split
   piece as its own row — e.g. a single 10-lot position that waterfall-matched against two
   different counterparties during CE/PE pairing (`splitPairedLots()`) renders as 2-3 separate
   small-qty rows (P1/P2/Unmatched) instead of one row carrying the full quantity.
2. **Chain tab's spread gate blocks closing legs.** `OptionChainTab.svelte`'s pre-submit
   bid/ask spread gate (`createSpreadGate`/`_buildGatePlan`) checks every CE/PE leg staged in
   the basket unconditionally, including a leg that CLOSES/reduces an existing position.
   Near-expiry ITM/OTM option spreads routinely exceed the 0.5% default threshold, so closing
   a flagged expiry position via Chain almost always trips the "⚠ Wide spread" warning +
   "Place anyway" double-confirmation. A close must never be blocked by a liquidity sanity
   check meant to protect new entries.

Investigated and ruled out (do not re-litigate): the Exp-Close grid's own row-click
`closePosition()` → `openTicket({defaultTab:'ticket', action:'close'})` path opens
`OrderTicket.svelte` on the Ticket tab with an empty `basketLegs`/fresh `SymbolPanel` mount —
confirmed via code trace that this path never reaches Chain's gate (the gate is wired only
inside `{#if _activeTab === 'chain'}`-mounted `OptionChainTab`, and the shared Submit button
only calls `runPreSubmitGate()` when `_activeTab === 'chain'`). `OrderTicket.svelte` itself has
no spread/confirm logic at all ("LIVE submits straight through — no confirm dialog"). The
Chain tab's gate is the ONLY spread-warning+confirmation mechanism anywhere in the codebase, so
fix #2 targets it — this covers the case where a Chain-tab basket submission is used to close a
flagged position (a legitimate, common way to execute a multi-leg/netted MCX close), which is
the actual live mechanism matching the operator's report.

## Status — IMPLEMENTED AND COMMITTED on `workshop` (archived here after the fact; this plan
file is a record, not an active to-do — both fixes shipped, see commits below)

- `974d2f25` — `fix(derivatives): consolidate lot-split pieces back to one row per account+symbol on the Legs view`
- `7988e301` — `fix(order): exempt close-intent legs from the Chain tab's pre-submit spread gate`
- `56e9761a` — `test(order): pin OptionChainTab's spread gate to call buildSpreadGatePlan, not the old inline loop`
- `f92130f1` — `fix(derivatives): Legs/Paired dropdown labels show their own row count, not whichever mode is active`

Final verification (run after commit 4): `npx vitest run` — 144 test files, 3887 tests, 0
failures. `npx svelte-check --output machine` — 0 errors, 5 pre-existing unrelated warnings.

Deliberate deviation from this plan's original spec, noted during implementation: the
consolidation grouping key is `(source, account, symbol, _splitTag, _storeKey)`, not the
literal `(source, account, symbol, _splitTag)` this plan originally specified — `_storeKey`
(carries broker product, e.g. NRML vs MIS) was added after an advisor review found it prevents a
rare but real cross-product merge that would corrupt `liveStoreExpPnl`'s per-piece Exp P&L
lookup. No-op in the common case (every real lot-split piece already shares `_storeKey` with its
siblings); covered by a dedicated regression test; documented in `lotPairSplit.js`'s own
docstring.

## Agents (as originally planned, for the historical record)

- frontend:

  **Fix 1 — Legs-view consolidation**

  - New pure helper `consolidateLotSplitPieces(rows)` in
    `frontend/src/lib/data/lotPairSplit.js` (co-located with `splitPairedLots`, its exact
    inverse). Identify split pieces via `row._isLotSplit === true` (set by `splitPairedLots`);
    group candidate pieces by `(source, account, symbol, _splitTag)` — NOT by
    `lot_piece_label` (that's exactly what must collapse) and NOT by `pair_group_key`. A row
    with no `_isLotSplit` flag (never split, or a draft/proxy/provisional row that didn't go
    through `splitPairedLots`) passes through completely unchanged, in its original position
    in the array.
    - For each group of 2+ pieces: sum the qty field (`quantity`/`qty`, whichever the rows
      carry) and every field in the existing `_PRORATE_FIELDS` list (`pnl`, `realised`,
      `unrealised`, `day_change_val`, `day_pnl`, `prev_settlement_pnl`, `overnight_quantity`,
      `lots`) back together — a plain sum is exact here (no remainder handling needed; the
      pieces were produced by exact pro-rating on the way out).
    - Drop `pair_group_key`, `paired_qty`, `orphan_qty`, `is_orphan`, `_isLotSplit`,
      `lot_piece_label`, `_lotFraction` on the merged row — none are meaningful once un-split.
      Restore `key` by stripping the `::<label>` suffix `splitPairedLots` appends.
    - Every other field (symbol, account, ltp, avg_cost, underlying_ltp, kind, exchange,
      source, `_storeKey`, `_pieceIndex`, `_pieceCount`, `_storeExpPnl`, etc.) is identical
      across all pieces of one source row — carry it from the first piece in the group.
    - Set a new `_mergedPieceKeys: string[]` on the merged row — the `lot_piece_label` of each
      constituent piece (used by the checkbox fix below). Omit/leave `undefined` on an
      unmerged (pass-through) row.

  - Wire it into `frontend/src/lib/data/candidateRowComposition.js`'s
    `composeDisplayedCandidates()`, inside the `mode === 'legs'` branch ONLY, running BEFORE
    both the null-sort (`sortRowsByAccountOnly`) and the column-sort (`applyColumnSort`) paths.
    Do NOT touch the `'paired'` or `'expiry'` branches — those are test-pinned
    byte-identical to their pre-column-sort behavior per this module's own header comment; that
    invariant must still hold unmodified.

  - **Checkbox-state carry-through** (the real risk here — read carefully):
    `enKey()` in `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` includes
    `lot_piece_label` as a discriminator, so each split piece today gets its OWN independent
    checkbox entry in `enabledSymbols`. Every payoff/EV/Greeks/Exp-P&L aggregate reads
    `_isLegEnabled(c)` against the UNSPLIT `candidatePositions` array (NOT
    `displayedCandidates` — confirmed: `candidatePositions` is untouched by this fix; only
    `displayedCandidates`'s `'legs'`-mode output changes), so those aggregates are unaffected by
    this consolidation — it is a display-only change. But the merged row rendered in the grid
    still needs a sensible `checked`/`enabled` state and a working toggle:
    - Add a helper (e.g. `_isMergedLegChecked(c)` / fold into `_isLegChecked`) that, when
      `c._mergedPieceKeys` is present and non-empty, returns true only when EVERY constituent
      piece's own `enKey` (`${account}|${symbol}|${piece_label}`) reads as checked — mirror the
      existing per-piece default-on rule (`v !== false`) per constituent key, not a single new
      key nothing else reads.
    - `onToggleEnabled` for a merged row (passed to `CandidateLegRow`) must write EVERY
      constituent piece's key in `enabledSymbols`, not a new single key — so toggling a merged
      row in the Legs view actually changes what the payoff/EV aggregates compute from the
      unsplit pieces.
  - `_legExpPnlLive` (or wherever a live Exp-P&L lookup reads `_lotFraction` to scale a shared
    value down to one piece's share) must return the FULL, unscaled value for a merged row
    (which carries no `_lotFraction`) — matching how an already-unsplit row behaves today.
    Locate and check every `_lotFraction` read site in `+page.svelte` for this.
  - The `{#each displayedCandidates as c, _ci (...)}` key expression already includes
    `lot_piece_label ?? ''` — for a merged row (no `lot_piece_label`) this degrades to the
    pre-split row's own key shape; verify no collision with a genuinely-never-split row for the
    same symbol+account (there shouldn't be one, since they'd have waterfall-matched into the
    same group, but confirm).

  **Fix 2 — Chain spread-gate close exemption** (`frontend/src/lib/order/OptionChainTab.svelte`)

  - In `_buildGatePlan()`, skip any `chainBasket` leg whose side+qty would CLOSE/reduce an
    existing live position rather than open/add to one. Resolve the currently-held qty+side for
    `(leg.sym, account)` via `portfolioStore.positions.rows` (import from
    `$lib/data/portfolioStore.svelte.js` — already a globally-importable singleton store used
    directly by other modules in this codebase, e.g. `getUnderlyingSpot`; no new prop needs to
    be threaded through `SymbolPanel.svelte`). `account` for the basket is the Chain tab's own
    `account` prop (the shared account the whole basket submits under).
    - A leg is "close-intent" when its `side` is opposite the held position's sign AND its
      contract quantity (`leg.lots * leg.lotSize`, or however this file already derives
      contracts from a basket leg elsewhere — reuse that, don't invent a second qty-resolution
      path) is `<=` the held position's absolute quantity for that symbol+account (a genuine
      reduce/flatten, not a flip past flat into a new position on the other side — a flip's
      "new position" portion should still be gated).
    - No held position for that symbol+account (or portfolioStore not yet loaded) → NOT
      close-intent → gate it as today (fail toward the existing, safer behavior).
  - Do NOT raise the 0.5% threshold or disable the gate generally — only exempt genuinely
    close-intent legs. An entry/open leg, including a new leg added to an existing position in
    the SAME direction, must still be gated exactly as today.
  - A basket mixing one open leg and one close leg must still gate on the open leg alone.
  - Add a short code comment next to the exemption explaining why (near-expiry ITM/OTM spreads
    routinely exceed the threshold; a close must never be blocked by a liquidity sanity check
    meant for new entries).

  **For BOTH fixes**, for every file changed, write or update a Vitest test covering the
  changed behaviour (mandatory):
  - `frontend/src/lib/__tests__/data/lotPairSplit.test.js`: `consolidateLotSplitPieces` —
    round-trips `splitPairedLots` output back to one row per original position (qty + every
    `_PRORATE_FIELDS` entry sums exactly); a never-split row passes through unchanged; a mixed
    array of split + unsplit rows only merges the split ones; pairing-only fields
    (`pair_group_key`/`paired_qty`/`orphan_qty`/`is_orphan`/`_lotFraction`) are absent on the
    merged row.
  - `frontend/src/lib/__tests__/data/candidateRowComposition.test.js` (existing file for this
    module): a `'legs'`-mode input containing lot-split pieces for one (account, symbol)
    consolidates to exactly one displayed row; `'paired'` and `'expiry'` modes remain
    byte-identical to their current behaviour (do not weaken this existing pinned assertion).
  - A new or existing Vitest file covering `OptionChainTab.svelte`'s gate-plan building logic
    (check `frontend/src/lib/__tests__/data/spreadGate.test.js` / `chainQuotes.test.js` first for
    the right home, or extract `_buildGatePlan`'s close-detection into a small exported pure
    helper in `spreadGate.js` specifically so it's directly unit-testable without mounting the
    component): a close-intent leg (opposite side, qty <= held qty) is excluded from the check
    plan; an open/entry leg for the same symbol is still included; a basket mixing one open and
    one close leg still gates (on the open leg).

  **Make two separate commits** (standing one-issue-at-a-time rule — do not bundle):
  1. The Legs-view consolidation fix + its tests.
  2. The Chain spread-gate close exemption + its tests.

## Tests

- pytest: no (frontend-only change, no backend files touched)
- svelte-check: yes
- playwright: no — Vitest covers the pure-function behaviour directly; add a Playwright spec
  only if the frontend agent judges the merged-row checkbox DOM interaction can't be adequately
  proven by Vitest alone.

## Commit message

1. `fix(derivatives): consolidate lot-split pieces back to one row per account+symbol on the Legs view`
2. `fix(order): exempt close-intent legs from the Chain tab's pre-submit spread gate`

(Two more commits were added post-hoc for residual items found during a post-commit self-review
— see Status section above: a wiring-test commit and a dropdown-label-count fix commit.)

## Done when

- Switching to the "Legs" dropdown option on the derivatives page shows exactly one row per
  (account, symbol) position, with correctly summed qty/P&L/Exp P&L; the TOTAL row is unchanged
  from before this fix (same total P&L, qty, etc.) because the underlying `candidatePositions`/
  payoff computation is untouched — this is a display-only consolidation. — DONE
- Checking/unchecking a merged Legs-view row still correctly includes/excludes that leg's full
  quantity from the payoff curve and every aggregate (Greeks, EV, Exp P&L totals). — DONE
- "Paired" and "Exp Close" dropdown views render byte-identical to before this change when no
  column sort is active (existing test-pinned invariant holds, unmodified). — DONE
- Closing a position via the Chain tab (a leg that reduces/flattens an existing holding) never
  triggers the "Wide spread" warning or the "Place anyway" double-confirmation, regardless of
  how wide the live bid-ask spread is. Opening/adding to a position still triggers the gate
  exactly as before. — DONE
- `npx vitest run` and `npx svelte-check` both green; full Vitest suite has zero regressions. —
  DONE (3887 tests, 0 failures; 0 svelte-check errors)
