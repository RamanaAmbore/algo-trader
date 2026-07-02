import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Static grep assertions that enforce the tick-flash scope contract
// after the sitewide rollout (2026-07-01).
//
// Sitewide rollout added createTickFlash to:
//   - MarketPulse (P&L columns on positions/holdings grids)
//   - PerformancePage (P&L columns on holdings + positions detail grids)
//   - dashboard/+page.svelte (Equity card day_pnl/pnl columns)
//
// Pre-rollout baseline (PositionStrip, NavCard, derivatives) unchanged.
//
// The tick_flash_sitewide.spec.js covers full runtime + SSOT dimensions.
// This file retains the static-grep shape for fast CI feedback.

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

const readSrc = (relPath) =>
  readFileSync(path.resolve(__dirname, '..', relPath), 'utf-8');

test.describe('tick-flash scope invariants (post sitewide rollout)', () => {
  test('MarketPulse imports createTickFlash (sitewide rollout)', () => {
    const src = readSrc('src/lib/MarketPulse.svelte');
    expect(src).toContain('createTickFlash');
    expect(src).toContain('_mpFlash');
  });

  test('PerformancePage imports createTickFlash (sitewide rollout)', () => {
    const src = readSrc('src/lib/PerformancePage.svelte');
    expect(src).toContain('createTickFlash');
    expect(src).toContain('_perfFlash');
  });

  test('dashboard imports createTickFlash (sitewide rollout)', () => {
    const src = readSrc('src/routes/(algo)/dashboard/+page.svelte');
    expect(src).toContain('createTickFlash');
    expect(src).toContain('_dashFlash');
  });

  test('derivatives +page.svelte imports createTickFlash (invariant preserved)', () => {
    const src = readSrc('src/routes/(algo)/admin/derivatives/+page.svelte');
    expect(src).toContain("from '$lib/data/tickFlash.svelte.js'");
    expect(src).toContain('createTickFlash');
  });

  test('PositionStrip preserves its flash import (pre-rollout baseline untouched)', () => {
    expect(readSrc('src/lib/PositionStrip.svelte')).toContain('createTickFlash');
  });

  test('NavCard preserves its flash import (pre-rollout baseline untouched)', () => {
    expect(readSrc('src/lib/NavCard.svelte')).toContain('createTickFlash');
  });

  test('MarketPulse excludes TOTAL rows from flash (_isTotal guard)', () => {
    const src = readSrc('src/lib/MarketPulse.svelte');
    // pnlCellClass must have an _isTotal guard
    expect(src).toMatch(/pnlCellClass[\s\S]{0,300}_isTotal/);
  });

  test('PerformancePage excludes pinned TOTAL rows from flash (rowPinned guard)', () => {
    const src = readSrc('src/lib/PerformancePage.svelte');
    expect(src).toContain('rowPinned');
    expect(src).toMatch(/pnlClsFlash[\s\S]{0,500}rowPinned/);
  });

  test('dashboard excludes TOTAL rows from flash (account === TOTAL guard)', () => {
    const src = readSrc('src/routes/(algo)/dashboard/+page.svelte');
    expect(src).toMatch(/_dashDirCell[\s\S]{0,300}TOTAL/);
  });

  test('app.css global .tf-up / .tf-down with alpha <= 0.15', () => {
    const css = readSrc('src/app.css');
    expect(css).toContain('.tf-up');
    expect(css).toContain('.tf-down');
    // Alpha in tf-pnl keyframes must be <= 0.15 (subtle, not alarming)
    const alphaMatches = css.match(/tf-pnl-(?:up|down)[\s\S]{0,300}rgba\([^)]+\)/g) ?? [];
    for (const m of alphaMatches) {
      const match = m.match(/rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*([\d.]+)\s*\)/);
      if (match) {
        expect(parseFloat(match[1])).toBeLessThanOrEqual(0.15);
      }
    }
  });

  test('prefers-reduced-motion block present in app.css for .tf-up/.tf-down', () => {
    const css = readSrc('src/app.css');
    expect(css).toContain('prefers-reduced-motion');
    // The reduced-motion block should mention .tf-up and .tf-down
    const rmBlock = css.match(/@media \(prefers-reduced-motion[\s\S]{0,200}\.tf-(?:up|down)/);
    expect(rmBlock).not.toBeNull();
  });
});
