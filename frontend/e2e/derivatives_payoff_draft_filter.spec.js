/**
 * derivatives_payoff_draft_filter.spec.js
 *
 * 2026-10 draft simplification: the `showDraftInPayoff` toggle is GONE. Drafts
 * are a real position on every surface, so the payoff, Greeks, Legs TOTAL and
 * chart expiry offset always include an enabled draft — gated only by its own
 * per-row enable checkbox (`_isLegEnabled`), exactly like any real leg.
 *
 * Source-scan tests (no browser required). Browser coverage of the same
 * behaviour lives in derivatives_draft_always_in_payoff.spec.js.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

/** Remove HTML, block and line comments so only executable code is scanned. */
function stripComments(src) {
  return src
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:'"`])\/\/[^\n]*/g, '$1');
}

const PAGE_PATH =
  '/Users/ramanambore/projects/ramboq/frontend/src/routes/(algo)/admin/derivatives/+page.svelte';

test.describe('Derivatives payoff — no draft-visibility toggle (simplified)', () => {
  test('1: showDraftInPayoff identifier no longer exists in the derivatives page', () => {
    // Comments may still mention the removed toggle by name for history.
    expect(stripComments(readFileSync(PAGE_PATH, 'utf-8')), 'showDraftInPayoff must not be referenced in executable code')
      .not.toMatch(/showDraftInPayoff/);
  });

  test('2: no draft-source exclusion guard remains in the payoff/expiry-offset aggregates', () => {
    // The old guard was `!showDraftInPayoff && (c.source === 'provisional' || … 'draft')`.
    // With the toggle gone, no `!showDraftInPayoff` guard may appear in code.
    expect(stripComments(readFileSync(PAGE_PATH, 'utf-8'))).not.toMatch(/!\s*showDraftInPayoff/);
  });

  test('3: buildPageLegs takes two args (candidatePositions, isLegEnabled) — no draft flag', () => {
    const source = readFileSync(PAGE_PATH, 'utf-8');
    expect(source).toContain('buildPageLegs(candidatePositions, _isLegEnabled)');
    expect(source).not.toMatch(/buildPageLegs\([^)]*,[^)]*,[^)]*\)/);
  });
});
