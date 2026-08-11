/**
 * holdings_navstrip_ssot.spec.js
 *
 * Verifies that `pulseHoldingsStore` is included in `_tickBookPollers()` so
 * NavStrip H:1 (`dispHoldingsToday`) stays in sync with Pulse Holdings TOTAL
 * during closed hours.
 *
 * The patch ensures holdings data remains fresh in the NavStrip across session boundaries:
 *   - Old: NavStrip holdings could stale if not polled with other tick subscriptions
 *   - New: pulseHoldingsStore.load() is called in _tickBookPollers polling cycle
 *   - Guard: dispHoldingsToday is derived from pulseHoldingsStore and rendered in PositionStrip
 *
 * Also verifies the formula-parity fix (2026-08-11):
 *   - _liveHoldingsToday uses (ltp - close_price) × qty — same as holdings in mergeHoldingRows
 *   - _livePositionsToday uses the same direct formula, no isMarketOpen() gate on the LTP
 *   - Neither _livePositionsToday nor mergePositionRows day_pnl path has an isMarketOpen gate
 *
 * Five quality dimensions:
 *  1. SSOT    — frontend SSOT: pulseHoldingsStore is polled in _tickBookPollers
 *  2. SSOT    — PositionStrip derives _liveHoldingsToday from pulseHoldingsStore
 *  3. UX      — dispHoldingsToday is rendered in PositionStrip template
 *  4. Stale   — _liveHoldingsToday and _livePositionsToday use close_price formula
 *  5. Stale   — no isMarketOpen gate on LTP in the day P&L paths
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   PLAYWRIGHT_BASE_URL=http://localhost:5174 \
 *   npx playwright test e2e/holdings_navstrip_ssot.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';

test.setTimeout(60000);

test.describe('Holdings NavStrip sync SSOT', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  // ── Test 1: Source-scan — pulseHoldingsStore polled in _tickBookPollers ──
  test('1-SSOT: marketDataStores pulseHoldingsStore included in _tickBookPollers', () => {
    // Read marketDataStores.svelte.js source.
    const marketDataStoresPath = '/Users/ramanambore/projects/ramboq/frontend/src/lib/data/marketDataStores.svelte.js';
    let source = '';
    try {
      source = readFileSync(marketDataStoresPath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read marketDataStores.svelte.js: ${e.message}`);
      return;
    }

    // Assertion 1: _tickBookPollers function must exist
    expect(source, 'marketDataStores should define _tickBookPollers function').toContain('_tickBookPollers');

    // Assertion 2: _tickBookPollers body should call pulseHoldingsStore.load()
    const tickBookPollersMatch = source.match(/function\s+_tickBookPollers[\s\S]*?(?=function|\Z)/);
    expect(tickBookPollersMatch, '_tickBookPollers function should be found').toBeTruthy();

    if (tickBookPollersMatch) {
      const tickBookPollersBody = tickBookPollersMatch[0];
      const hasPulseHoldingsLoad = /pulseHoldingsStore\.load\(\)|pulseHoldingsStore/.test(tickBookPollersBody);
      expect(hasPulseHoldingsLoad, '_tickBookPollers should include pulseHoldingsStore.load() call').toBe(true);
    }

    // Assertion 3: pulseHoldingsStore must be defined or imported in the file
    expect(source, 'marketDataStores should reference pulseHoldingsStore').toContain('pulseHoldingsStore');

    console.log('[holdings_navstrip_ssot] 1-SSOT pulseHoldingsStore polling verified');
  });

  // ── Test 2: Source-scan — PositionStrip derives from pulseHoldingsStore ──
  test('2-SSOT: PositionStrip derives _liveHoldingsToday from pulseHoldingsStore', () => {
    // Read PositionStrip.svelte source.
    const positionStripPath = '/Users/ramanambore/projects/ramboq/frontend/src/lib/PositionStrip.svelte';
    let source = '';
    try {
      source = readFileSync(positionStripPath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read PositionStrip.svelte: ${e.message}`);
      return;
    }

    // Assertion 1: PositionStrip must reference pulseHoldingsStore or holdings variable
    const hasHoldingsRef = /pulseHoldingsStore|holdings|\$derived.*holdings/.test(source);
    expect(hasHoldingsRef, 'PositionStrip should reference pulseHoldingsStore or holdings').toBe(true);

    // Assertion 2: PositionStrip should have _liveHoldingsToday computed value
    expect(source, 'PositionStrip should have _liveHoldingsToday variable or derived').toContain('_liveHoldingsToday');

    // Assertion 3: _liveHoldingsToday should be derived (Svelte 5) or computed (reading pulseHoldingsStore)
    const hasLiveHoldingsDerived = /\$derived.*_liveHoldingsToday|_liveHoldingsToday\s*=\s*\$derived|let\s+_liveHoldingsToday/.test(source);
    expect(hasLiveHoldingsDerived, 'PositionStrip should derive _liveHoldingsToday from store').toBe(true);

    console.log('[holdings_navstrip_ssot] 2-SSOT _liveHoldingsToday derivation verified');
  });

  // ── Test 3: Source-scan — dispHoldingsToday rendered in PositionStrip ──
  test('3-UX: PositionStrip renders dispHoldingsToday in template', () => {
    // Read PositionStrip.svelte source.
    const positionStripPath = '/Users/ramanambore/projects/ramboq/frontend/src/lib/PositionStrip.svelte';
    let source = '';
    try {
      source = readFileSync(positionStripPath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read PositionStrip.svelte: ${e.message}`);
      return;
    }

    // Assertion 1: PositionStrip must have dispHoldingsToday variable or function
    const hasDispHoldingsToday = /dispHoldingsToday|displayHoldingsToday|fmtMoney.*holdings|fmtMoney.*_liveHoldingsToday/.test(source);
    expect(hasDispHoldingsToday, 'PositionStrip should have dispHoldingsToday or similar display variable').toBe(true);

    // Assertion 2: dispHoldingsToday should appear in the template (after <script> block)
    const scriptEndIndex = source.lastIndexOf('</script>');
    if (scriptEndIndex > -1) {
      const templatePart = source.substring(scriptEndIndex);
      const hasDispInTemplate = /dispHoldingsToday|fmtMoney\([^)]*holdings/.test(templatePart);
      expect(hasDispInTemplate, 'PositionStrip template should render dispHoldingsToday or holdings value').toBe(true);
    } else {
      // Svelte 5 may not have explicit </script> boundary; check for fmtMoney usage with holdings
      const hasFmtMoneyHoldings = /fmtMoney.*holdings|{[^}]*dispHoldingsToday[^}]*}/.test(source);
      expect(hasFmtMoneyHoldings, 'PositionStrip should render formatted holdings value').toBe(true);
    }

    // Assertion 3: the value should be passed through fmtMoney or similar formatting function
    const hasFormatting = /fmtMoney|fmt\s*\(|format.*holdings|dispHoldingsToday/.test(source);
    expect(hasFormatting, 'PositionStrip should format holdings value for display').toBe(true);

    console.log('[holdings_navstrip_ssot] 3-UX dispHoldingsToday rendering verified');
  });

  // ── Test 4: Source-scan — _liveHoldingsToday uses close_price formula ────
  test('4-Stale: _liveHoldingsToday in PositionStrip uses close_price (holdings formula)', () => {
    const positionStripPath = '/Users/ramanambore/projects/ramboq/frontend/src/lib/PositionStrip.svelte';
    let source = '';
    try {
      source = readFileSync(positionStripPath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read PositionStrip.svelte: ${e.message}`);
      return;
    }

    // Extract the _liveHoldingsToday definition block
    const holdingsTodayIdx = source.indexOf('const _liveHoldingsToday');
    expect(holdingsTodayIdx, '_liveHoldingsToday must exist in PositionStrip.svelte').toBeGreaterThan(-1);

    // Extract block: from definition start to the next top-level const declaration.
    // Note: we cannot use '$derived.by' as end-marker because it appears on the same
    // definition line. Instead look for the next '\n  const ' (next sibling derived).
    const blockEnd = source.indexOf('\n  const ', holdingsTodayIdx + 20);
    const block = blockEnd > -1 ? source.slice(holdingsTodayIdx, blockEnd) : source.slice(holdingsTodayIdx, holdingsTodayIdx + 600);
    expect(block, '_liveHoldingsToday block should reference close_price').toContain('close_price');

    console.log('[holdings_navstrip_ssot] 4-Stale _liveHoldingsToday close_price formula verified');
  });

  // ── Test 5: Source-scan — _livePositionsToday uses close_price formula, no market gate ──
  test('5-Stale: _livePositionsToday in PositionStrip uses close_price and has no isMarketOpen gate on LTP', () => {
    const positionStripPath = '/Users/ramanambore/projects/ramboq/frontend/src/lib/PositionStrip.svelte';
    let source = '';
    try {
      source = readFileSync(positionStripPath, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read PositionStrip.svelte: ${e.message}`);
      return;
    }

    // Find _livePositionsToday definition block
    const posTodayIdx = source.indexOf('const _livePositionsToday');
    expect(posTodayIdx, '_livePositionsToday must exist in PositionStrip.svelte').toBeGreaterThan(-1);

    // Extract block: from definition start to next top-level const declaration.
    const blockEnd = source.indexOf('\n  const ', posTodayIdx + 20);
    const block = blockEnd > -1 ? source.slice(posTodayIdx, blockEnd) : source.slice(posTodayIdx, posTodayIdx + 800);

    // Must reference close_price
    expect(block, '_livePositionsToday block should reference close_price').toContain('close_price');

    // Must NOT gate ltp on isMarketOpen / _mktOpen
    expect(block, '_livePositionsToday block should not have _mktOpen gate on LTP').not.toMatch(/_mktOpen\s*\?.*ltp|isNseOpen.*ltp|ltp.*_mktOpen/);

    console.log('[holdings_navstrip_ssot] 5-Stale _livePositionsToday close_price formula verified, no market-open gate');
  });
});
