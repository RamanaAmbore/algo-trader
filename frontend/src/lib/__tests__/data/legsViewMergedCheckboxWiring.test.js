/**
 * legsViewMergedCheckboxWiring.test.js — pins the derivatives page's
 * checkbox-state carry-through for a merged Legs-view row
 * (`consolidateLotSplitPieces`, lotPairSplit.js).
 *
 * `+page.svelte`'s `_isLegChecked` / `onToggleEnabled` can't be exercised
 * directly under Vitest (the component has top-level runes and isn't
 * mounted here — same established limitation as `.svelte.js` modules with
 * top-level `$derived.by`, see `fundsAggregate.js`'s own header comment).
 * A `?raw` source-grep of the real shipped file is the established
 * workaround for pinning wiring like this (same convention as
 * `fillWatchLayoutWiring.test.js`'s `+layout.svelte?raw` scan) — the pure
 * logic itself (`mergedLegPieces`) is fully covered directly by
 * lotPairSplit.test.js; this file only guards that `+page.svelte` actually
 * calls it from both the checked-state read and the toggle-write path,
 * instead of reinventing a second key-building mechanism inline.
 */

import { describe, it, expect } from 'vitest';
// @ts-ignore — Vite raw-import suffix has no TS module declaration here.
import pageSrc from '../../../routes/(algo)/admin/derivatives/+page.svelte?raw';

describe('derivatives +page.svelte — merged Legs-view row checkbox wiring', () => {
  it('imports mergedLegPieces from lotPairSplit.js', () => {
    expect(pageSrc).toMatch(/import\s*\{\s*mergedLegPieces\s*\}\s*from\s*['"]\$lib\/data\/lotPairSplit\.js['"]/);
  });

  it('_isLegChecked reads mergedLegPieces before falling back to the ordinary per-leg checkbox logic', () => {
    const fnMatch = pageSrc.match(/function _isLegChecked\(c\) \{[\s\S]*?\n  \}/);
    expect(fnMatch).toBeTruthy();
    const body = fnMatch[0];
    expect(body).toMatch(/mergedLegPieces\(c\)/);
    // Every constituent piece must itself be re-checked — proves a
    // merged row's checked-state is derived from ALL its constituent
    // pieces, not a single new independent key.
    expect(body).toMatch(/\.every\(/);
  });

  it('onToggleEnabled writes EVERY constituent piece key for a merged row, not a single new key', () => {
    const effectIdx = pageSrc.indexOf('onToggleEnabled={(candidate, checked) => {');
    expect(effectIdx).toBeGreaterThan(-1);
    const body = pageSrc.slice(effectIdx, effectIdx + 900);
    expect(body).toMatch(/mergedLegPieces\(candidate\)/);
    expect(body).toMatch(/for \(const p of parts\)/);
    expect(body).toMatch(/next\[enKey\(p\)\] = checked/);
  });
});
