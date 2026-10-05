/**
 * chart_popup_shared.spec.js
 *
 * Every hover-popup chart uses the shared ChartPopup component inside a
 * cp-frame wrapper, so placement and styling come from one place.
 * Content stays per chart; only the popup shell is shared.
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

const POPUP_CHARTS = [
  'lib/EquityCurve.svelte',
  'lib/PriceChart.svelte',
  'lib/PnlAnalysis.svelte',
  'lib/MultiPriceChart.svelte',
  'lib/NavTab.svelte',
  'routes/(algo)/dashboard/+page.svelte',
];

test('ChartPopup component exists and renders the shared chart-tooltip shell', () => {
  const src = SRC('lib/ChartPopup.svelte');
  expect(src).toMatch(/class="chart-tooltip chart-popup"/);
  expect(src).toMatch(/\{@render children\?\.\(\)\}/);
});

test('every hover-popup chart imports and uses ChartPopup inside a cp-frame', () => {
  for (const f of POPUP_CHARTS) {
    const src = SRC(f);
    expect(src, `${f} imports ChartPopup`).toMatch(/import ChartPopup from '\$lib\/ChartPopup\.svelte'/);
    expect(src, `${f} uses <ChartPopup`).toMatch(/<ChartPopup /);
    expect(src, `${f} has cp-frame`).toMatch(/class="[^"]*\bcp-frame\b/);
  }
});

test('no chart keeps a hand-drawn SVG popup box', () => {
  for (const f of POPUP_CHARTS) {
    expect(SRC(f), f).not.toMatch(/<rect[^>]*fill="#1d2a44"/);
  }
});
