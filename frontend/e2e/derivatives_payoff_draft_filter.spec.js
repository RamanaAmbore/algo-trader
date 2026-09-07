/**
 * derivatives_payoff_draft_filter.spec.js
 *
 * Verifies _legsExpPnlTotal and _expiryPnlOffset apply the showDraftInPayoff
 * filter so the Legs panel TOTAL and chart expiry offset match the backend
 * payoff curve when showDraftInPayoff=false.
 *
 * Source-scan tests only (no browser required).
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const PAGE_PATH =
  '/Users/ramanambore/projects/ramboq/frontend/src/routes/(algo)/admin/derivatives/+page.svelte';

test.describe('Derivatives payoff — showDraftInPayoff filter consistency', () => {
  test('1-SSOT: _legsExpPnlTotal applies showDraftInPayoff source filter', () => {
    const source = readFileSync(PAGE_PATH, 'utf-8');

    // _legsExpPnlTotal must check showDraftInPayoff && guard against provisional/draft_store/draft sources
    expect(source, '_legsExpPnlTotal must guard provisional source when showDraftInPayoff=false')
      .toMatch(/const _legsExpPnlTotal[\s\S]{0,500}showDraftInPayoff[\s\S]{0,200}provisional/);

    console.log('[derivatives_payoff_draft_filter] _legsExpPnlTotal filter verified');
  });

  test('2-SSOT: _expiryPnlOffset applies showDraftInPayoff source filter', () => {
    const source = readFileSync(PAGE_PATH, 'utf-8');

    // _expiryPnlOffset must also check showDraftInPayoff && guard against provisional/draft_store/draft sources
    expect(source, '_expiryPnlOffset must guard provisional source when showDraftInPayoff=false')
      .toMatch(/const _expiryPnlOffset[\s\S]{0,500}showDraftInPayoff[\s\S]{0,200}provisional/);

    console.log('[derivatives_payoff_draft_filter] _expiryPnlOffset filter verified');
  });

  test('3-SSOT: showDraftInPayoff guard pattern consistency', () => {
    const source = readFileSync(PAGE_PATH, 'utf-8');

    // Both must use the same guard: !showDraftInPayoff && (source checks)
    const draftGuard = /!showDraftInPayoff[\s\S]{0,150}\(c\.source\s*===\s*['"]provisional['"][\s\S]{0,100}draft['"]\)/g;
    const matches = source.match(draftGuard) || [];

    expect(matches.length, 'showDraftInPayoff guard must appear at least 2 times (_legsExpPnlTotal + _expiryPnlOffset)')
      .toBeGreaterThanOrEqual(2);

    console.log(`[derivatives_payoff_draft_filter] ${matches.length} guard occurrences found`);
  });
});
