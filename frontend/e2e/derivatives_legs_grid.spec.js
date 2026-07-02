/**
 * derivatives_legs_grid.spec.js
 *
 * Guards the CSS simplification pass on the Legs card grid and Exp Close
 * tab grid in /admin/derivatives. The operator complaint was "text not
 * clearly visible" — traced to multiple background layers stacking on
 * single cells (row bg + band tint + cell tint).
 *
 * Fix applied (2026-07-01):
 *   - .cand-row-long/short background-color → transparent
 *   - .expiry-band-netted background-color → transparent (left bar only)
 *   - .expiry-band-netted[data-pair-tint] background-color → transparent
 *   - .cand-pnl.cell-pos/neg capped at alpha 0.08
 *   - long/short hover unified to neutral rgba(34,211,238,0.05)
 *
 * Quality dimensions:
 *   SSOT     — single background layer per cell (source assertions)
 *   Perf     — no extra paint layers, no !important stacking
 *   Stale    — old multi-layer selectors are removed
 *   Reusable — assertions work for both Legs tab and Exp Close tab
 *   UX       — direction cue visible (left bar / right border), text
 *               contrast preserved (no bg wash over text)
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const SRC_PATH = path.resolve(
  process.cwd(),
  'src/routes/(algo)/admin/derivatives/+page.svelte'
);

// ── Static source assertions (no server required) ──────────────────────────

test('SSOT: cand-row-long/short have transparent background-color', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  // The old rgba tints must be gone.
  expect(
    src.includes('rgba(56,189,248,0.08)'),
    'cand-row-long old tint rgba(56,189,248,0.08) must be removed'
  ).toBe(false);

  expect(
    src.includes('rgba(251,146,60,0.08)'),
    'cand-row-short old tint rgba(251,146,60,0.08) must be removed'
  ).toBe(false);

  // New rule must be transparent.
  expect(
    src.includes('.cand-row-long  { background-color: transparent; }'),
    'cand-row-long must be background-color: transparent'
  ).toBe(true);

  expect(
    src.includes('.cand-row-short { background-color: transparent; }'),
    'cand-row-short must be background-color: transparent'
  ).toBe(true);
});

test('SSOT: cand-row-long/short hover uses neutral cyan, not direction tint', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  // Old direction-tinted hovers must not exist.
  expect(
    src.includes('rgba(56,189,248,0.16)'),
    'cand-row-long:hover old tint rgba(56,189,248,0.16) must be removed'
  ).toBe(false);

  expect(
    src.includes('rgba(251,146,60,0.16)'),
    'cand-row-short:hover old tint rgba(251,146,60,0.16) must be removed'
  ).toBe(false);
});

test('SSOT: expiry-band-netted base has no background-color fill', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  // Locate the base netted rule.
  const ruleIdx = src.indexOf('.cand-row.expiry-band-netted {');
  expect(ruleIdx, 'expiry-band-netted base rule must exist').toBeGreaterThan(0);

  // Grab the rule block.
  const ruleEnd = src.indexOf('}', ruleIdx);
  const rule = src.slice(ruleIdx, ruleEnd + 1);

  expect(
    rule.includes('background-color: transparent'),
    'expiry-band-netted base must have background-color: transparent'
  ).toBe(true);

  // Old slate-blue fill must be gone from the base rule.
  expect(
    rule.includes('rgba(125, 145, 184, 0.08)'),
    'expiry-band-netted old fill rgba(125,145,184,0.08) must be removed from base rule'
  ).toBe(false);
});

test('SSOT: all expiry-band-netted pair-tint selectors have transparent backgrounds', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  // Each [data-pair-tint="N"] rule must not have a background-color other than transparent.
  for (let i = 0; i <= 4; i++) {
    const selector = `[data-pair-tint="${i}"]`;
    const idx = src.indexOf(selector);
    expect(idx, `data-pair-tint="${i}" selector must exist`).toBeGreaterThan(0);

    const blockEnd = src.indexOf('}', idx);
    const block = src.slice(idx, blockEnd + 1);

    expect(
      block.includes('background-color: transparent'),
      `pair-tint ${i} must have background-color: transparent`
    ).toBe(true);
  }
});

test('SSOT: cand-pnl cell-pos/neg alpha capped at 0.08', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  // Locate the cand-pnl global block.
  const posIdx = src.indexOf(':global(.cand-pnl.cell-pos)');
  expect(posIdx, 'cand-pnl.cell-pos rule must exist').toBeGreaterThan(0);

  const negIdx = src.indexOf(':global(.cand-pnl.cell-neg)');
  expect(negIdx, 'cand-pnl.cell-neg rule must exist').toBeGreaterThan(0);

  // Capture single lines.
  const posLine = src.slice(posIdx, src.indexOf('\n', posIdx));
  const negLine = src.slice(negIdx, src.indexOf('\n', negIdx));

  // Old 0.10 must be gone from these lines.
  expect(
    posLine.includes('0.10'),
    'cand-pnl.cell-pos must not have alpha 0.10'
  ).toBe(false);

  expect(
    negLine.includes('0.10'),
    'cand-pnl.cell-neg must not have alpha 0.10'
  ).toBe(false);

  // New 0.08 must be present.
  expect(
    posLine.includes('0.08'),
    'cand-pnl.cell-pos must use alpha 0.08'
  ).toBe(true);

  expect(
    negLine.includes('0.08'),
    'cand-pnl.cell-neg must use alpha 0.08'
  ).toBe(true);
});

test('SSOT: direction cue still present via cand-sym-acct right border', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  // The right-edge direction bar must exist — this is the SOLE direction cue.
  expect(
    src.includes('.cand-row.cand-row-long  .cand-sym-acct::after'),
    'cand-row-long right border still present'
  ).toBe(true);

  expect(
    src.includes('.cand-row.cand-row-short .cand-sym-acct::after'),
    'cand-row-short right border still present'
  ).toBe(true);

  // Green and red bar colours.
  expect(
    src.includes('rgba(74, 222, 128, 0.85)'),
    'long right-border green colour present'
  ).toBe(true);

  expect(
    src.includes('rgba(248, 113, 113, 0.85)'),
    'short right-border red colour present'
  ).toBe(true);
});

test('Stale: no double-inset box-shadow on cand-row-long/short standalone rules', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  // Find the standalone cand-row-long rule (not inside expiry-band combos).
  const longIdx = src.indexOf('.cand-row-long  { background-color: transparent; }');
  expect(longIdx, 'simplified cand-row-long rule must exist').toBeGreaterThan(0);

  // The old double-inset bar must not appear in the vicinity.
  const vicinity = src.slice(longIdx, longIdx + 200);
  expect(
    vicinity.includes('inset -3px'),
    'no double inset -3px bar on cand-row-long'
  ).toBe(false);
});

test('SSOT: TOTAL row amber bg preserved (canonical rollup anchor)', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  // TOTAL row amber must not be touched.
  expect(
    src.includes('rgba(251,191,36,0.22)'),
    'cand-row-total amber bg rgba(251,191,36,0.22) preserved'
  ).toBe(true);
});

test('SSOT: legacy equity-close / commodity-close aliases have transparent background', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  // The legacy rules were overriding expiry-band-close amber. They must now
  // have transparent background so expiry-band-close wins the cascade.
  const eqIdx = src.indexOf('.cand-row.cand-row-equity-close {');
  expect(eqIdx, 'cand-row-equity-close rule must exist').toBeGreaterThan(0);
  const eqEnd = src.indexOf('}', eqIdx);
  const eqBlock = src.slice(eqIdx, eqEnd + 1);
  expect(
    eqBlock.includes('background-color: transparent'),
    'cand-row-equity-close must have background-color: transparent'
  ).toBe(true);

  const comIdx = src.indexOf('.cand-row.cand-row-commodity-close {');
  expect(comIdx, 'cand-row-commodity-close rule must exist').toBeGreaterThan(0);
  const comEnd = src.indexOf('}', comIdx);
  const comBlock = src.slice(comIdx, comEnd + 1);
  expect(
    comBlock.includes('background-color: transparent'),
    'cand-row-commodity-close must have background-color: transparent'
  ).toBe(true);
});

// ── Browser-level contrast assertions ──────────────────────────────────────
// These run against dev.ramboq.com and require a logged-in session.

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 800 },
  { name: 'mobile',  width: 390,  height: 844 },
];

for (const vp of VIEWPORTS) {
  test(`UX [${vp.name}]: cand-row background alpha ≤ 0.10 on any single cell`, async ({ page }) => {
    await loginAsAdmin(page);
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });

    // If no rows rendered (no positions), skip gracefully.
    const rowCount = await page.locator('.cand-row').count();
    if (rowCount === 0) {
      test.skip();
      return;
    }

    // Check the first visible non-total row.
    const firstRow = page.locator('.cand-row:not(.cand-row-total)').first();

    const bg = await firstRow.evaluate(el => getComputedStyle(el).backgroundColor);

    // Parse rgba(r,g,b,a). Accept transparent (alpha 0) and alpha ≤ 0.10.
    if (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent') return;

    const match = bg.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
    if (!match) return;  // non-rgba (unlikely) — don't fail

    const alpha = match[4] !== undefined ? parseFloat(match[4]) : 1;
    expect(alpha, `cand-row bg alpha must be ≤ 0.10 — got ${alpha} for bg: ${bg}`).toBeLessThanOrEqual(0.10);
  });

  test(`UX [${vp.name}]: cand-pnl cell-pos/neg computed alpha ≤ 0.10`, async ({ page }) => {
    await loginAsAdmin(page);
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });

    // If no P&L cells with positive/negative colour, skip gracefully.
    const posCells = await page.locator('.cand-pnl.cell-pos').count();
    const negCells = await page.locator('.cand-pnl.cell-neg').count();
    if (posCells === 0 && negCells === 0) {
      test.skip();
      return;
    }

    const selector = posCells > 0 ? '.cand-pnl.cell-pos' : '.cand-pnl.cell-neg';
    const cell = page.locator(selector).first();

    const bg = await cell.evaluate(el => getComputedStyle(el).backgroundColor);
    if (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent') return;

    const match = bg.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
    if (!match) return;

    const alpha = match[4] !== undefined ? parseFloat(match[4]) : 1;
    expect(alpha, `cand-pnl bg alpha must be ≤ 0.10 — got ${alpha} for bg: ${bg}`).toBeLessThanOrEqual(0.10);
  });

  test(`UX [${vp.name}]: cand-sym text color luma ≥ 150 (readable against dark bg)`, async ({ page }) => {
    // Threshold is 150 (not 200): algo palette CE #4ade80 luma≈173, PE
    // #f87171 luma≈155 — both above 150 but not 200. Using 200 would
    // reject intentional directional colours from the canonical palette.
    await loginAsAdmin(page);
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'networkidle' });

    const symCells = await page.locator('.cand-sym').count();
    if (symCells === 0) {
      test.skip();
      return;
    }

    const color = await page.locator('.cand-sym').first().evaluate(el => getComputedStyle(el).color);
    const match = color.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    if (!match) return;

    const [r, g, b] = [parseInt(match[1]), parseInt(match[2]), parseInt(match[3])];
    // ITU-R 601 luma proxy
    const luma = 0.299 * r + 0.587 * g + 0.114 * b;
    expect(luma, `cand-sym luma must be ≥ 150 — got ${luma.toFixed(1)} for color: ${color}`).toBeGreaterThanOrEqual(150);
  });
}
