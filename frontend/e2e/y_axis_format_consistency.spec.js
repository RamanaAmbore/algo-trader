/**
 * y_axis_format_consistency.spec.js
 *
 * Y-axis label rules across chart components:
 *   1. Every Y-axis label is slanted -45° (same convention as the price chart).
 *   2. No Y-axis label carries a rupee symbol.
 *   3. Payoff labels are plain numbers (no Cr/L/k suffix).
 *   4. NavTab keeps its Cr/L/k suffix format; dashboard keeps aggCompact.
 *
 * Source-level check: reads the component files directly, so it needs no
 * backend or browser session.
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dir = dirname(fileURLToPath(import.meta.url));
const SRC = (rel) => readFileSync(join(__dir, '../src', rel), 'utf8');

const SLANTED_CHARTS = [
  'lib/ChartWorkspace.svelte',
  'lib/NavTab.svelte',
  'lib/EquityCurve.svelte',
  'lib/PriceChart.svelte',
  'lib/PnlAnalysis.svelte',
  'lib/MultiPriceChart.svelte',
  'lib/OptionsPayoff.svelte',
  'routes/(algo)/dashboard/+page.svelte',
];

test('SSOT: every chart Y-axis label is slanted -45°', () => {
  for (const f of SLANTED_CHARTS) {
    expect(SRC(f), f).toMatch(/transform="rotate\(-45 /);
  }
});

test('SSOT: no rupee symbol in NavTab Y-axis formatter', () => {
  const src = SRC('lib/NavTab.svelte');
  const fn = src.match(/function _fmtChipInr\([\s\S]*?\n  \}/);
  expect(fn, '_fmtChipInr present').not.toBeNull();
  expect(fn[0]).not.toContain('₹');
  expect(fn[0]).toMatch(/Cr`/);
});

test('SSOT: no rupee symbol in EquityCurve Y-axis labels', () => {
  const src = SRC('lib/EquityCurve.svelte');
  const block = src.match(/<!-- y-axis grid \+ labels -->([\s\S]*?)<!-- x-axis labels -->/);
  expect(block, 'y-axis block present').not.toBeNull();
  expect(block[1]).not.toContain('₹');
});

test('Payoff Y-axis labels are plain numbers, no Cr/L/k suffix', () => {
  const src = SRC('lib/OptionsPayoff.svelte');
  const fn = src.match(/function _axisFmt\([\s\S]*?\n  \}/);
  expect(fn, '_axisFmt present').not.toBeNull();
  expect(fn[0]).not.toContain('aggCompact');
  expect(fn[0]).not.toContain('₹');
  expect(fn[0]).toContain("toLocaleString('en-IN')");
});

test('Dashboard equity Y-axis keeps aggCompact (Cr/L/K) format', () => {
  const src = SRC('routes/(algo)/dashboard/+page.svelte');
  expect(src).toMatch(/label: aggCompact\(val\)/);
});
