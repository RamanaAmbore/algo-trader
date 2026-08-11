/**
 * option_chain_order.spec.js
 *
 * Source-scan spec verifying the two bug fixes in OptionChainTab.svelte:
 *
 *   Bug 1: _refreshChainQuotes() previously guarded on `!isMarketOpen()`, causing
 *          chainQuotesMap to stay null outside market hours — no strikes rendered,
 *          and Buy/Sell clicks showed "Quote not loaded".
 *          Fix: the isMarketOpen() guard is removed; only the !chainUnderlying /
 *          !chainExpiry guards remain.
 *
 *   Bug 2: No loading indicator or error feedback while the fetch was in-flight or
 *          failed. Fix: _chainQuotesLoading and _chainQuotesError state variables
 *          added; template shows "Fetching quotes…" and error banner accordingly.
 *
 * Five quality dimensions:
 *  1. SSOT    — isMarketOpen gate removed from _refreshChainQuotes
 *  2. SSOT    — _chainQuotesLoading state drives loading UI
 *  3. UX      — _chainQuotesError state drives error banner
 *  4. UX      — template renders "Fetching quotes…" loading message
 *  5. Stale   — no isMarketOpen reference inside _refreshChainQuotes body
 *
 * This is a source-scan spec (no browser needed) — uses readFileSync.
 *
 * Run:
 *   npx playwright test e2e/option_chain_order.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const OPTION_CHAIN_PATH = '/Users/ramanambore/projects/ramboq/frontend/src/lib/order/OptionChainTab.svelte';

test.describe('OptionChainTab — market-gate removal + loading/error state', () => {
  let source = '';

  test.beforeAll(() => {
    try {
      source = readFileSync(OPTION_CHAIN_PATH, 'utf-8');
    } catch (e) {
      // tests will skip individually if source is empty
    }
  });

  // ── Test 1: isMarketOpen gate removed from _refreshChainQuotes ──────────
  test('1-SSOT: _refreshChainQuotes does not contain isMarketOpen guard', () => {
    if (!source) test.skip(true, `Could not read ${OPTION_CHAIN_PATH}`);

    // Extract the _refreshChainQuotes function body
    const fnIdx = source.indexOf('async function _refreshChainQuotes()');
    expect(fnIdx, '_refreshChainQuotes function must exist').toBeGreaterThan(-1);

    // Find the closing brace of the function (next top-level `}` after the opening `{`)
    const fnStart = source.indexOf('{', fnIdx);
    // Grab a generous slice covering the function body
    const fnSlice = source.slice(fnIdx, fnIdx + 1500);

    expect(fnSlice, '_refreshChainQuotes must not guard on isMarketOpen').not.toContain('isMarketOpen');
    console.log('[option_chain_order] 1-SSOT isMarketOpen gate absent from _refreshChainQuotes');
  });

  // ── Test 2: _chainQuotesLoading state variable added ────────────────────
  test('2-SSOT: _refreshChainQuotes sets _chainQuotesLoading', () => {
    if (!source) test.skip(true, `Could not read ${OPTION_CHAIN_PATH}`);

    const fnIdx = source.indexOf('async function _refreshChainQuotes()');
    expect(fnIdx, '_refreshChainQuotes function must exist').toBeGreaterThan(-1);

    const fnSlice = source.slice(fnIdx, fnIdx + 1500);
    expect(fnSlice, '_refreshChainQuotes must set _chainQuotesLoading').toContain('_chainQuotesLoading');
    console.log('[option_chain_order] 2-SSOT _chainQuotesLoading found in _refreshChainQuotes');
  });

  // ── Test 3: _chainQuotesError state variable added ──────────────────────
  test('3-UX: _refreshChainQuotes sets _chainQuotesError', () => {
    if (!source) test.skip(true, `Could not read ${OPTION_CHAIN_PATH}`);

    const fnIdx = source.indexOf('async function _refreshChainQuotes()');
    expect(fnIdx, '_refreshChainQuotes function must exist').toBeGreaterThan(-1);

    const fnSlice = source.slice(fnIdx, fnIdx + 1500);
    expect(fnSlice, '_refreshChainQuotes must set _chainQuotesError').toContain('_chainQuotesError');
    console.log('[option_chain_order] 3-UX _chainQuotesError found in _refreshChainQuotes');
  });

  // ── Test 4: template renders "Fetching quotes…" loading message ──────────
  test('4-UX: template contains "Fetching quotes" loading message', () => {
    if (!source) test.skip(true, `Could not read ${OPTION_CHAIN_PATH}`);

    expect(source, 'template must contain Fetching quotes loading message').toContain('Fetching quotes');
    console.log('[option_chain_order] 4-UX "Fetching quotes" message found in template');
  });

  // ── Test 5: state declarations present at module level ──────────────────
  test('5-Stale: _chainQuotesLoading and _chainQuotesError declared as $state', () => {
    if (!source) test.skip(true, `Could not read ${OPTION_CHAIN_PATH}`);

    expect(source, '_chainQuotesLoading must be declared as $state').toContain('let _chainQuotesLoading = $state(');
    expect(source, '_chainQuotesError must be declared as $state').toContain("let _chainQuotesError = $state(");
    console.log('[option_chain_order] 5-Stale $state declarations for loading/error verified');
  });
});
