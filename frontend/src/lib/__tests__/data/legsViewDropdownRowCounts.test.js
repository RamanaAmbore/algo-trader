/**
 * legsViewDropdownRowCounts.test.js — pins the derivatives page's
 * Legs/Paired dropdown labels to their OWN independently-computed row
 * counts, instead of sharing `displayedCandidates.length` (which depends
 * on whichever mode is currently active).
 *
 * Before `consolidateLotSplitPieces` existed, 'legs' and 'paired' always
 * produced the same row count, so sharing one expression between both
 * labels was harmless. Once 'legs' started genuinely consolidating rows
 * (see `legsViewMergedCheckboxWiring.test.js`), the INACTIVE option's
 * label would otherwise show the wrong count — e.g. selecting "Legs"
 * (consolidated, fewer rows) would also show "Paired (N)" using that same
 * smaller N, even though Paired would actually render more rows.
 *
 * `+page.svelte` can't be mounted under this project's Vitest config
 * (same established limitation as every other `?raw` wiring test in this
 * directory) — the underlying row-count computation itself
 * (`composeDisplayedCandidates` per mode) is already fully covered
 * directly by `candidateRowComposition.test.js`; this file only guards
 * the page's OWN wiring: that each label reads its own independently
 * computed count, not a shared, mode-dependent one.
 */

import { describe, it, expect } from 'vitest';
// @ts-ignore — Vite raw-import suffix has no TS module declaration here.
import pageSrc from '../../../routes/(algo)/admin/derivatives/+page.svelte?raw';

describe('derivatives +page.svelte — Legs/Paired dropdown row-count labels', () => {
  it('_legsViewCount and _pairedViewCount are each computed via composeDisplayedCandidates with their own mode', () => {
    expect(pageSrc).toMatch(
      /const _legsViewCount = \$derived\.by\(\(\) =>\s*\n\s*composeDisplayedCandidates\(_filteredLegCandidateRows, \{\s*\n\s*mode: 'legs',/
    );
    expect(pageSrc).toMatch(
      /const _pairedViewCount = \$derived\.by\(\(\) =>\s*\n\s*composeDisplayedCandidates\(_filteredLegCandidateRows, \{\s*\n\s*mode: 'paired',/
    );
  });

  it('_filteredLegCandidateRows derives from _applyLegFilters(candidatePositions) — the same filter steps displayedCandidates itself uses', () => {
    expect(pageSrc).toMatch(
      /const _filteredLegCandidateRows = \$derived\.by\(\(\) => _applyLegFilters\(candidatePositions\)\)/
    );
  });

  it("the dropdown options array reads _legsViewCount/_pairedViewCount for the Legs/Paired labels, never displayedCandidates.length", () => {
    const optIdx = pageSrc.indexOf("id=\"opt-legs-view\"");
    expect(optIdx).toBeGreaterThan(-1);
    const block = pageSrc.slice(optIdx, optIdx + 400);
    expect(block).toMatch(/label: `Legs \(\$\{_legsViewCount\}\)`/);
    expect(block).toMatch(/label: `Paired \(\$\{_pairedViewCount\}\)`/);
    expect(block).not.toMatch(/displayedCandidates\.length/);
  });

  it('expiryCloseTotal (unaffected by this fix) is still its own independent derivation, not displayedCandidates.length', () => {
    expect(pageSrc).toMatch(/const expiryCloseTotal = \$derived\(/);
  });
});
