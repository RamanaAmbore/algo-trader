/**
 * derivatives_expired_leg_valuation.spec.js — source-grep guards for the
 * 2026-09 final-audit fix (Defect 1 + Defect 2) to the GOLD/GOLDM
 * chart-vs-Snapshot Exp P&L divergence.
 *
 * The actual functional correctness (store-vs-page parity, the frozen
 * valuation formula, the narrowed expired-detection predicate) is covered
 * directly by vitest — these are plain, importable pure functions
 * (src/lib/__tests__/data/pageLoad_expired.test.js,
 * expiryPnl_expired.test.js, decomposeSymbol_guessExpiry.test.js,
 * portfolioStore.test.js). This file only guards that +page.svelte
 * actually WIRES UP that fix correctly — i.e. passes `hasFNO` through
 * (Defect 1's blocker #1: silently defaulting it here would resurrect
 * the exact bug this fix closes) and keys the four valuation sites on
 * the NARROW `_expiredFrozen` flag, not the broad `_expired` tag.
 *
 * Five quality dimensions:
 *  1. SSOT  — same buildPageLegs/sumExpiredFrozenLegsPnl pageLoad.js
 *             exports the vitest suite exercises directly.
 *  2. Perf  — synchronous source-grep, no page load.
 *  3. Stale — guards the exact regression path (hasFNO silently
 *             defaulted / omitted) that would reintroduce the bug.
 *  4. Reuse — buildPageLegs replaces the old inline, untested mapping.
 *  5. UX    — all four `_expired` call-sites use the SAME three-way rule.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const PAGE_PATH = '/Users/ramanambore/projects/ramboq/frontend/src/routes/(algo)/admin/derivatives/+page.svelte';
const PAGELOAD_PATH = '/Users/ramanambore/projects/ramboq/frontend/src/lib/derivatives/pageLoad.js';

function readSrc(path) {
  return readFileSync(path, 'utf-8');
}

test.describe('derivatives +page.svelte — hasFNO wiring (Defect 1 blocker #1)', () => {
  test('imports hasFNO from $lib/data/instruments', () => {
    const src = readSrc(PAGE_PATH);
    expect(src).toMatch(/hasFNO/);
    expect(src).toMatch(/from\s+['"]\$lib\/data\/instruments['"]/);
  });

  test('passes hasFNO explicitly into buildCandidatePositions (not silently defaulted/omitted)', () => {
    const src = readSrc(PAGE_PATH);
    const callIdx = src.indexOf('return buildCandidatePositions({');
    expect(callIdx, 'buildCandidatePositions call site not found').toBeGreaterThan(0);
    const callBlock = src.slice(callIdx, src.indexOf('});', callIdx));
    expect(callBlock, 'hasFNO must be passed to buildCandidatePositions').toMatch(/\bhasFNO\b/);
    expect(callBlock).toContain('getInstrument');
  });
});

test.describe('derivatives +page.svelte — legs mapping uses buildPageLegs (Defect 1)', () => {
  test('legs is assigned via buildPageLegs, not an inline stripped mapping', () => {
    const src = readSrc(PAGE_PATH);
    expect(src).toContain('legs = buildPageLegs(candidatePositions, _isLegEnabled, showDraftInPayoff)');
    // The old bug: an inline `.map()` that dropped pnl/realised/unrealised.
    // Must not reappear as a competing/duplicate mapping.
    expect(src).not.toMatch(/legs\s*=\s*candidatePositions\s*\n?\s*\.filter/);
  });

  test('_clientPayoffStub sources its expired-constant from sumExpiredFrozenLegsPnl (not an inline recompute)', () => {
    const src = readSrc(PAGE_PATH);
    expect(src).toContain('const _expiredConstant = sumExpiredFrozenLegsPnl(legs);');
  });
});

test.describe('derivatives +page.svelte — four valuation sites key on _expiredFrozen (Defect 1/2)', () => {
  test('candidatesActualPnl (site 1) uses the three-way _expiredFrozen/_expired/default rule', () => {
    const src = readSrc(PAGE_PATH);
    const idx = src.indexOf('const candidatesActualPnl = $derived.by(() => {');
    expect(idx, 'candidatesActualPnl not found').toBeGreaterThan(0);
    const block = src.slice(idx, src.indexOf('return s;', idx));
    expect(block).toContain('if (c._expiredFrozen)');
    expect(block).toContain('expiredLegFrozenPnl(c)');
  });

  test('_chartExpPnlAtSpot (site 2) checks _expiredFrozen before _expired', () => {
    const src = readSrc(PAGE_PATH);
    const idx = src.indexOf('const _chartExpPnlAtSpot = $derived.by(');
    expect(idx, '_chartExpPnlAtSpot not found').toBeGreaterThan(0);
    const block = src.slice(idx, idx + 400);
    const frozenIdx = block.indexOf('_expiredFrozen');
    const expiredIdx = block.indexOf('if (c._expired)');
    expect(frozenIdx, '_expiredFrozen check not found').toBeGreaterThan(-1);
    expect(expiredIdx, '_expired exclusion not found').toBeGreaterThan(-1);
    expect(frozenIdx < expiredIdx, '_expiredFrozen must be checked BEFORE the broader _expired exclusion').toBe(true);
  });

  test('_expiryPnlOffset (site 3) checks _expiredFrozen before _expired', () => {
    const src = readSrc(PAGE_PATH);
    const idx = src.indexOf('const _expiryPnlOffset = $derived.by(');
    expect(idx, '_expiryPnlOffset not found').toBeGreaterThan(0);
    const block = src.slice(idx, idx + 700);
    const frozenIdx = block.indexOf('c._expiredFrozen');
    const excludeIdx = block.indexOf('if (c._expired) continue;');
    expect(frozenIdx, '_expiredFrozen check not found').toBeGreaterThan(-1);
    expect(excludeIdx, '_expired exclusion not found').toBeGreaterThan(-1);
    expect(frozenIdx < excludeIdx).toBe(true);
  });

  test('_clientPayoffStub (site 4) activeLegs still excludes on the BROADER _expired tag', () => {
    const src = readSrc(PAGE_PATH);
    const idx = src.indexOf('const activeLegs = legs.filter(l => {');
    expect(idx, 'activeLegs filter not found').toBeGreaterThan(0);
    const block = src.slice(idx, idx + 150);
    expect(block).toContain('if (l._expired) return false;');
  });
});

test.describe('pageLoad.js — buildCandidatePositions computes _expiredFrozen via the shared predicate', () => {
  test('imports isExpiredHeldContract from expiryPnl.js and tags both _expired and _expiredFrozen', () => {
    const src = readSrc(PAGELOAD_PATH);
    expect(src).toContain('isExpiredHeldContract');
    expect(src).toMatch(/from\s+['"]\$lib\/data\/expiryPnl\.js['"]/);
    expect(src).toContain('const isExpiredFrozen = qty !== 0 && isExpiredHeldContract(sym, qty, getInstrument, hasFNO);');
    expect(src).toContain("_expiredFrozen: true");
  });

  test('buildPageLegs carries pnl/realised/unrealised through (the exact field-drop this whole fix closes)', () => {
    const src = readSrc(PAGELOAD_PATH);
    const idx = src.indexOf('export function buildPageLegs(');
    expect(idx, 'buildPageLegs not found').toBeGreaterThan(0);
    const block = src.slice(idx, src.indexOf('\n}', idx));
    expect(block).toContain('pnl:        c.pnl');
    expect(block).toContain('realised:   c.realised');
    expect(block).toContain('unrealised: c.unrealised');
    expect(block).toContain('_expiredFrozen: c._expiredFrozen');
  });
});
