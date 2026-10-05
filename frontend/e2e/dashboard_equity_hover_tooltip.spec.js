/**
 * dashboard_equity_hover_tooltip.spec.js
 *
 * The dashboard equity chart's hover popup matches the payoff and price
 * charts: an HTML .chart-tooltip showing the time (x) and the value of each
 * enabled series (y). No extra detail rows.
 *
 * Source-level check: reads the component file directly, so it needs no
 * backend or browser session.
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dir = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(
  join(__dir, '../src/routes/(algo)/dashboard/+page.svelte'),
  'utf8',
);

test('popup is the shared HTML .chart-tooltip, not an SVG rect', () => {
  expect(SRC).toMatch(/class="chart-tooltip eq-hover-tooltip"/);
  expect(SRC).not.toMatch(/<rect x=\{_tipX\}/);
});

test('popup shows the time x value and one row per enabled series', () => {
  expect(SRC).toMatch(/chart-tooltip-ts">\{_th\}:\{_tm\} IST</);
  expect(SRC).toMatch(/\{#each _eqActiveSeries as s \(s\.id\)\}/);
  expect(SRC).toMatch(/\{s\.label\}/);
});

test('popup has no extra detail rows', () => {
  expect(SRC).not.toMatch(/cum \{_hoverPt/);
  expect(SRC).not.toMatch(/>Day P&amp;L</);
});
