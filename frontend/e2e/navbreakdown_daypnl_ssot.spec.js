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

// ── A6 (2026-09 audit): Day P&L / Lifetime P&L red-for-negative ──────────
// NavBreakdown's Day P&L / Lifetime P&L columns previously used
// agDirCellText's amber-for-negative default (a misapplied carryover of
// the Expiry P&L column's genuinely-flat-amber-regardless-of-sign
// exception) — should be red, matching PositionStrip's .ps-neg
// convention. Source-grep (not live-render) so this doesn't depend on
// the book carrying a negative P&L row at test time.

test.describe('NavBreakdown P&L sign colour (A6)', () => {
  test('Day P&L / Lifetime P&L columns use the red-for-negative variant; Expiry untouched', () => {
    let source = '';
    try {
      source = readFileSync(NAV_BREAKDOWN_PATH, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read NavBreakdown.svelte: ${e.message}`);
      return;
    }

    // mkDirCellText({ lossRed: true }) imported + assigned once at
    // module level (not recreated inline per column-def, per the file's
    // existing memoisation convention for cellStyle/cellClass helpers).
    expect(source).toContain('mkDirCellText');
    expect(source).toMatch(/const\s+_pnlLossRed\s*=\s*mkDirCellText\(\s*\{\s*lossRed:\s*true\s*\}\s*\)/);

    // day_pnl and lifetime column defs (P slot, _pCols) use the red variant.
    const dayPnlCol = source.match(/\{\s*field:\s*'day_pnl'[\s\S]*?\}/)?.[0] ?? '';
    const lifetimeCol = source.match(/\{\s*field:\s*'lifetime',\s*headerName:\s*'P&L'[\s\S]*?\}/)?.[0] ?? '';
    const expiryCol = source.match(/\{\s*field:\s*'expiry'[\s\S]*?\}/)?.[0] ?? '';

    expect(dayPnlCol, 'day_pnl column def not found').not.toBe('');
    expect(lifetimeCol, 'lifetime (P&L) column def not found').not.toBe('');
    expect(expiryCol, 'expiry column def not found').not.toBe('');

    expect(dayPnlCol, 'Day P&L column must use _pnlLossRed (red-for-negative)').toContain('cellClass: _pnlLossRed');
    expect(lifetimeCol, 'Lifetime P&L column must use _pnlLossRed (red-for-negative)').toContain('cellClass: _pnlLossRed');
    // Expiry column's call-site explicitly untouched — stays on the
    // plain (amber-for-negative) agDirCellText default.
    expect(expiryCol, 'Expiry P&L column call-site must be left untouched (agDirCellText)').toContain('cellClass: agDirCellText');

    console.log('[navbreakdown_daypnl_ssot] A6 red-for-negative Day/Lifetime P&L verified');
  });

  test('H-slot Today MTM / Lifetime columns also use the red-for-negative variant; Value untouched', () => {
    let source = '';
    try {
      source = readFileSync(NAV_BREAKDOWN_PATH, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read NavBreakdown.svelte: ${e.message}`);
      return;
    }

    // Extract the _hCols array block specifically — both P slot and H
    // slot declare a `field: 'lifetime', headerName: 'P&L'` column, so a
    // bare field-name regex would ambiguously match whichever comes
    // first in the file (P slot). Scope to _hCols's own block first.
    const hColsBlock = source.match(/const\s+_hCols\s*=\s*\[[\s\S]*?\n\s*\];/)?.[0] ?? '';
    expect(hColsBlock, '_hCols array block not found').not.toBe('');

    const todayMtmCol = hColsBlock.match(/\{\s*field:\s*'todayMtm'[\s\S]*?\}/)?.[0] ?? '';
    const valueCol = hColsBlock.match(/\{\s*field:\s*'value'[\s\S]*?\}/)?.[0] ?? '';
    const hLifetimeCol = hColsBlock.match(/\{\s*field:\s*'lifetime'[\s\S]*?\}/)?.[0] ?? '';

    expect(todayMtmCol, 'todayMtm column def not found in _hCols').not.toBe('');
    expect(valueCol, 'value column def not found in _hCols').not.toBe('');
    expect(hLifetimeCol, 'lifetime column def not found in _hCols').not.toBe('');

    expect(todayMtmCol, 'H-slot Today MTM column must use _pnlLossRed (red-for-negative)').toContain('cellClass: _pnlLossRed');
    expect(hLifetimeCol, 'H-slot Lifetime column must use _pnlLossRed (red-for-negative)').toContain('cellClass: _pnlLossRed');
    // Value is a magnitude (broker-reported current market value), not a
    // P&L — PositionStrip's own Value slot (.ps-cash) is non-directional,
    // the H-slot's equivalent of the P-slot's Expiry exception.
    expect(valueCol, 'H-slot Value column call-site must be left untouched (agDirCellText)').toContain('cellClass: agDirCellText');

    console.log('[navbreakdown_daypnl_ssot] A6 H-slot red-for-negative Today MTM/Lifetime verified');
  });

  test('the misleading "matches NavStrip pill values" colour-convention comment is corrected', () => {
    const path = '/Users/ramanambore/projects/ramboq/frontend/src/lib/data/algoGridUtils.js';
    let source = '';
    try {
      source = readFileSync(path, 'utf-8');
    } catch (e) {
      test.skip(true, `Could not read algoGridUtils.js: ${e.message}`);
      return;
    }
    // The old, wrong claim ("Color convention matches NavStrip pill
    // values" as a blanket statement for agDirCellText) must be gone —
    // it's now scoped/qualified, not a blanket claim.
    expect(source).not.toMatch(/Color convention matches NavStrip pill values:\s*\n\s*\*\s*positive/);
    expect(source).toContain('does NOT match');
  });
});
