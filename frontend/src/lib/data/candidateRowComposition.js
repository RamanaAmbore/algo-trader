/**
 * candidateRowComposition.js — pure composition for the derivatives
 * Legs/Candidates grid's `displayedCandidates` (column-sort plan,
 * Section E). Pulled out of +page.svelte's own `$derived.by` so the
 * exact composition order can be exercised directly by Vitest, rather
 * than by a hand-mirrored copy.
 *
 * CRITICAL invariant (CLAUDE.md documents two prior live incidents from
 * corrupting this exact pair-group ordering): when no explicit column
 * sort is active, this is BYTE-IDENTICAL to the pre-column-sort code —
 * `sortRowsByPairGroup` runs over every row (drafts included), and
 * `applyColumnSort` is never even called. An active column sort replaces
 * `sortRowsByPairGroup` at the SAME structural point, so the
 * draft-reshuffle step still runs unconditionally afterward either way.
 * The Exp Close tab is never touched by either branch.
 */

import { sortRowsByPairGroup } from './pairGroupSort.js';
import { applyColumnSort } from './columnSort.js';

/**
 * @param {any[]} rows - already filtered (holdings toggle, text filter)
 * @param {{
 *   isExpiryTab: boolean,
 *   sortState: import('./columnSort.js').SortState | null | undefined,
 *   columnSpecs: Record<string, import('./columnSort.js').ColumnSpec>,
 * }} opts
 * @returns {any[]}
 */
export function composeDisplayedCandidates(rows, { isExpiryTab, sortState, columnSpecs }) {
  // Stamp BEFORE any sort — stable key material for the {#each} fallback
  // key (see +page.svelte's own comment at the call site). Applies to
  // BOTH tabs since the {#each} below is shared.
  rows = rows.map((r, i) => ({ ...r, _ord: i }));

  if (isExpiryTab) return rows;

  rows = (!sortState || sortState.direction == null)
    ? sortRowsByPairGroup(rows)
    : applyColumnSort(rows, sortState, columnSpecs);

  // Draft rows (payoff store, display-only) sort to the BOTTOM of the
  // Legs grid, below every real position — 2026-10 operator design
  // decision. Array.filter preserves relative order, so this is a pure
  // reshuffle — each group keeps its own existing order from whichever
  // sort ran above. Unconditional regardless of sort state.
  return rows.filter((c) => c.source !== 'draft').concat(rows.filter((c) => c.source === 'draft'));
}
