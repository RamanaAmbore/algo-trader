/**
 * candidateRowComposition.js — pure composition for the derivatives
 * Legs/Candidates grid's `displayedCandidates` (column-sort plan,
 * Section E; widened to a 3-mode contract in 2026-11 when the
 * 2-option Legs/Exp-Close AlgoTabs strip became a 3-option
 * Legs/Paired/Exp Close dropdown). Pulled out of +page.svelte's own
 * `$derived.by` so the exact composition order can be exercised
 * directly by Vitest, rather than by a hand-mirrored copy.
 *
 * CRITICAL invariant (CLAUDE.md documents two prior live incidents from
 * corrupting this exact pair-group ordering): `mode` selects exactly
 * ONE of three behaviors, and nothing outside that selection may change
 * between modes.
 *
 *   mode === 'expiry' — early-return bypass, unchanged: `rows` (already
 *     carrying `_band`-tagged expiry rows) is returned as-is after the
 *     `_ord` stamp, with no sort and no draft reshuffle. Column sort is
 *     never consulted on this mode (the Exp Close tab's own band/theta
 *     ordering would be corrupted by it).
 *
 *   mode === 'paired' — today's pre-existing default behavior,
 *     unchanged: when no explicit column sort is active, this is
 *     BYTE-IDENTICAL to the original pre-column-sort code —
 *     `sortRowsByPairGroup` runs over every row (drafts included). An
 *     active column sort REPLACES `sortRowsByPairGroup` at the SAME
 *     structural point via `applyColumnSort`.
 *
 *   mode === 'legs' — the new unpaired, account-grouped view: when no
 *     explicit column sort is active, `sortRowsByAccountOnly` runs at
 *     the identical structural point `sortRowsByPairGroup` occupies for
 *     'paired'. An active column sort replaces it the same way.
 *
 * In EVERY mode except 'expiry', the draft-rows-last reshuffle still
 * runs unconditionally afterward, regardless of which sort ran.
 */

import { sortRowsByPairGroup, sortRowsByAccountOnly } from './pairGroupSort.js';
import { applyColumnSort } from './columnSort.js';

/**
 * @param {any[]} rows - already filtered (holdings toggle, text filter)
 * @param {{
 *   mode: 'legs' | 'paired' | 'expiry',
 *   sortState: import('./columnSort.js').SortState | null | undefined,
 *   columnSpecs: Record<string, import('./columnSort.js').ColumnSpec>,
 * }} opts
 * @returns {any[]}
 */
export function composeDisplayedCandidates(rows, { mode, sortState, columnSpecs }) {
  // Stamp BEFORE any sort — stable key material for the {#each} fallback
  // key (see +page.svelte's own comment at the call site). Applies to
  // ALL three modes since the {#each} below is shared.
  rows = rows.map((r, i) => ({ ...r, _ord: i }));

  if (mode === 'expiry') return rows;

  if (!sortState || sortState.direction == null) {
    rows = mode === 'legs' ? sortRowsByAccountOnly(rows) : sortRowsByPairGroup(rows);
  } else {
    rows = applyColumnSort(rows, sortState, columnSpecs);
  }

  // Draft rows (payoff store, display-only) sort to the BOTTOM of the
  // Legs grid, below every real position — 2026-10 operator design
  // decision. Array.filter preserves relative order, so this is a pure
  // reshuffle — each group keeps its own existing order from whichever
  // sort ran above. Unconditional regardless of sort state, and
  // unconditional across both the 'legs' and 'paired' modes.
  return rows.filter((c) => c.source !== 'draft').concat(rows.filter((c) => c.source === 'draft'));
}
