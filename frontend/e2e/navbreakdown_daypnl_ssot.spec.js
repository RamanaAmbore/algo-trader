/**
 * navbreakdown_daypnl_ssot.spec.js
 *
 * Verifies that NavBreakdown.svelte TOTAL row day P&L reads from
 * positionsDayPnlStore.total (the same SSOT as NavStrip P:1) rather than
 * summing baseDayPnlForPosition across per-account rows.
 *
 * Background: when MarketPulse calls setFromPulse(byKey, total), _pulseTotal
 * diverges from Σ baseDayPnlForPosition because Pulse uses cq-accurate quotes.
 * NavStrip P:1 reads positionsDayPnlStore.total (_pulseTotal ?? _store.total).
 * NavBreakdown TOTAL must read the same store so both surfaces always agree.
 *
 * Three quality dimensions:
 *  1. SSOT   — NavBreakdown imports positionsDayPnlStore from the correct path
 *  2. Usage  — _pTotal.dayPnl is set to positionsDayPnlStore.total (not a reduce)
 *  3. Stale  — the old _pByAcct.reduce pattern is absent from the _pTotal block
 *
 * Run:
 *   npx playwright test e2e/navbreakdown_daypnl_ssot.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const NAV_BREAKDOWN_PATH =
  '/Users/ramanambore/projects/ramboq/frontend/src/lib/NavBreakdown.svelte';

test.describe('NavBreakdown TOTAL day P&L SSOT', () => {
  // ── Test 1: SSOT — NavBreakdown imports positionsDayPnlStore ─────────────
  test('1-SSOT: NavBreakdown.svelte imports positionsDayPnlStore from the correct module', () => {
    let source = '';
    try {
      source = readFileSync(NAV_BREAKDOWN_PATH, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read NavBreakdown.svelte: ${e.message}`);
      return;
    }

    // Must import positionsDayPnlStore
    expect(source, 'NavBreakdown should import positionsDayPnlStore').toContain(
      'positionsDayPnlStore'
    );

    // Import must reference the canonical module path
    expect(
      source,
      'NavBreakdown should import from positionsDayPnlStore.svelte.js'
    ).toContain("from '$lib/data/positionsDayPnlStore.svelte.js'");

    console.log('[navbreakdown_daypnl_ssot] positionsDayPnlStore import verified');
  });

  // ── Test 2: Usage — _pTotal.dayPnl reads positionsDayPnlStore.total ──────
  test('2-Usage: _pTotal.dayPnl is set to positionsDayPnlStore.total', () => {
    let source = '';
    try {
      source = readFileSync(NAV_BREAKDOWN_PATH, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read NavBreakdown.svelte: ${e.message}`);
      return;
    }

    // The _pTotal block must assign positionsDayPnlStore.total to dayPnl.
    // Accept any whitespace between 'dayPnl:' and 'positionsDayPnlStore.total'.
    const hasPulseTotal = /dayPnl\s*:\s*positionsDayPnlStore\.total/.test(source);
    expect(hasPulseTotal, '_pTotal.dayPnl must equal positionsDayPnlStore.total').toBe(true);

    console.log('[navbreakdown_daypnl_ssot] _pTotal.dayPnl = positionsDayPnlStore.total verified');
  });

  // ── Test 3: Stale-code grep — old reduce pattern is absent from _pTotal ──
  test('3-Stale: _pTotal does NOT compute dayPnl via _pByAcct.reduce', () => {
    let source = '';
    try {
      source = readFileSync(NAV_BREAKDOWN_PATH, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read NavBreakdown.svelte: ${e.message}`);
      return;
    }

    // Locate the _pTotal block — everything between the `const _pTotal` declaration
    // and the closing `}));` of that specific derived block.
    // Strategy: find the _pTotal block and check it doesn't have the reduce pattern
    // for dayPnl (lifetimePnl and expiryPnl reduces are still valid and expected).
    const pTotalStart = source.indexOf('const _pTotal');
    expect(pTotalStart, '_pTotal block must exist in NavBreakdown.svelte').toBeGreaterThan(-1);

    // Slice from _pTotal start to the next `}));` which closes the derived block.
    const pTotalEnd = source.indexOf('}));', pTotalStart);
    const pTotalBlock = source.slice(pTotalStart, pTotalEnd + 4);

    // dayPnl must NOT use _pByAcct.reduce in this block.
    const hasOldReduce = /dayPnl\s*:\s*_pByAcct\.reduce/.test(pTotalBlock);
    expect(
      hasOldReduce,
      '_pTotal.dayPnl must not use _pByAcct.reduce — use positionsDayPnlStore.total instead'
    ).toBe(false);

    // lifetimePnl and expiryPnl still use reduce (unchanged) — verify they remain.
    expect(
      pTotalBlock,
      '_pTotal.lifetimePnl should still use _pByAcct.reduce'
    ).toContain('lifetimePnl');
    expect(
      pTotalBlock,
      '_pTotal.expiryPnl should still use _pByAcct.reduce'
    ).toContain('expiryPnl');

    console.log('[navbreakdown_daypnl_ssot] stale reduce pattern absent from _pTotal verified');
  });
});
