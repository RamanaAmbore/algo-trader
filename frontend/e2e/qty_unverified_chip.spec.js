// Order card shows a warning chip when the broker quantity could not be converted from lots.
// Source-level guard: reads the real component source.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const src = readFileSync(
  new URL('../src/lib/order/OrderCard.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('order card — unverified quantity chip', () => {
  test('chip renders only when the row is flagged', () => {
    expect(src).toMatch(/\{#if order\.qty_unverified\}/);
    expect(src).toMatch(/class="log-chip qty-unverified"/);
  });

  test('chip explains the cause in its tooltip', () => {
    expect(src).toMatch(/Lot size not loaded/);
  });

  test('chip uses the warning token, not a literal colour', () => {
    expect(src).toMatch(/\.qty-unverified \{ color: var\(--c-action\); \}/);
  });
});
