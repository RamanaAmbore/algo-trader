# Plan: Remove WHEN-clause clutter from Agents/Alerts rows + frontend-only lot-aware pair split for Legs/Positions

## Task
Two independent frontend display changes. (1) Remove the "Condition" (WHEN-tree) block from the
Agents/Alerts expanded row view in the automation page — it's redundant clutter in the read-only
list; full detail stays available via Edit (Console tab, real CLI text) and Delete (unaffected).
Brackets rows never had a condition section, so nothing changes there. (2) The Positions grid
(MarketPulse) and the Derivatives Legs grid currently show one row per broker position even when
a position's quantity is matched (lot-waterfall) across multiple different counter-positions —
losing visibility into one of the pair groups it belongs to, and conflating "paired" and "orphan"
state on a single row. Add a pure, frontend-only `splitPairedLots()` function that re-derives the
pairing correctly (fixing the multi-group loss) from data already in the `/api/positions`
response, and split any row that needs it into separate, correctly-labeled, pro-rated display
rows — mirroring the existing `splitClosedReopened` pattern. No backend or schema changes.

## Agents
- backend: skip
- frontend: Task 1 — frontend/src/routes/(algo)/automation/+page.svelte: delete the Condition
  block from the `agentRowView` snippet (lines ~715-720, between the description and the
  Alert-via/tier/topic footer). Remove `renderCondNode`/`leafLabel` only if grep confirms no
  other caller uses them after the removal.
  ALSO Task 2 — new frontend/src/lib/data/lotPairSplit.js exporting `splitPairedLots(rows)`:
  group by (account, rootOf(tradingsymbol, exchange)) using the existing `rootOf()` helper
  (frontend/src/lib/data/rootOf.js); waterfall-match longs vs shorts by abs(quantity) within each
  group exactly like backend/api/routes/positions.py:_auto_pair_positions, but ACCUMULATE every
  match a row participates in (list of pieces), not overwrite; rows with exactly one piece and no
  leftover pass through unchanged; rows with 2+ pieces and/or orphan leftover get split into N
  rows, each pro-rated on pnl/realised/unrealised/day_change_val (N-1 pieces by
  piece_qty/total_abs_qty, last piece = exact remainder so the sum reconciles exactly), each given
  a deterministic (not match-order-dependent) pair label, `pair_group_key`/`paired_qty`/
  `orphan_qty`/`is_orphan` set correctly per piece, `_isLotSplit:true`, `lot_piece_label`, and a
  unique `key` = `${original.key || original.tradingsymbol}::${pieceLabel}` (tradingsymbol itself
  must stay unmutated). Wire into: MarketPulse (apply to the raw positions array before
  mergePositionRows() in pulseUnified.js, so pulseRowId's existing `data.key || data.tradingsymbol`
  resolves split rows automatically) and the Derivatives Legs grid
  (frontend/src/lib/derivatives/pageLoad.js:buildPagePositionRows — alongside the existing
  splitClosedReopened() call; extend the Legs grid's composite `{#each}` key in
  admin/derivatives/+page.svelte to include the new piece label). Add a small inline label tag
  for `lot_piece_label` in pulseColumns.js's symbol cell renderer and in CandidateLegRow.svelte.
  Explicitly do NOT touch PerformancePage.svelte (documented separate scope).
- broker: skip
- doc: skip
- backend-test: skip
- playwright: add a regression spec covering (a) the Agents/Alerts row no longer shows a
  Condition section while Edit still opens full CLI text, (b) a multi-counterparty position
  renders as separate labeled rows in both MarketPulse Positions and the Derivatives Legs grid
  with totals unchanged, (c) two consecutive polls produce stable row keys (no flicker/re-add).

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes
- vitest (new frontend/src/lib/__tests__/data/lotPairSplit.test.js): clean single-pair match (no
  split), multi-counterparty match (both pair groups preserved, not overwritten), partial-match-
  plus-orphan split, pro-rating sum-back-exactly invariant, stable-key-under-input-reorder.

## Commit message
fix(automation,positions): declutter agent row display; fix lot-pairing to show all matched groups separately

## Done when
- Agents/Alerts expanded rows no longer show the Condition tree; Edit/Delete flows unaffected;
  Brackets rows unchanged (never had one).
- A position matched across multiple counterparties renders as separate, correctly-labeled rows
  in both MarketPulse Positions and the Derivatives Legs grid, with totals unchanged and zero
  backend/schema changes.
- `npx vitest run`, `npx svelte-check --output machine`, and the new Playwright spec all pass on
  `workshop`.
