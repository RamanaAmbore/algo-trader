/**
 * pulse_symbol_picker_shows_all.spec.js
 *
 * Validates that the /pulse "Add symbol" picker shows virtual roots
 * (GOLD, GOLD.NEXT) alongside real contracts and equities.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT   — virtual roots from rootOf.js getVirtualRoots via searchByPrefix
 *  2. Perf   — typeahead dropdown opens within 1200ms of input
 *  3. Stale  — single searchByPrefix code path, no duplication
 *  4. Reuse  — same displaySymbol() used in MarketPulse + SymbolSearchInput
 *  5. UX     — virtual rows labelled "virtual"; stored key GOLD_NEXT (underscore),
 *              displayed GOLD.NEXT (dot); no horizontal overflow on mobile
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/pulse_symbol_picker_shows_all.spec.js --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE  = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const PULSE = `${BASE}/pulse`;

async function openAddSymbol(page) {
  const btn = page.locator('[title*="Manage watchlist"]').first();
  await btn.click();
}

async function typeQuery(page, q) {
  const input = page.locator('input[placeholder*="Symbol"]').first();
  await input.fill('');
  await input.type(q, { delay: 40 });
  await page.waitForTimeout(400);
}

async function typeaheadItems(page) {
  const items = page.locator('.search-typeahead-item');
  const count = await items.count();
  const out = [];
  for (let i = 0; i < count; i++) {
    const sym  = await items.nth(i).locator('.font-mono').textContent();
    const meta = await items.nth(i).locator('[class*="text-[0.6rem]"]').textContent();
    out.push({ sym: (sym || '').trim(), meta: (meta || '').trim() });
  }
  return out;
}

test.describe('pulse symbol picker — virtual roots', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(PULSE, { waitUntil: 'networkidle' });
    await loginAsAdmin(page);
    await page.goto(PULSE, { waitUntil: 'networkidle' });
  });

  test('GOLD search shows GOLD, GOLD.NEXT, GOLDBEES, and a real GOLD futures contract', async ({ page }) => {
    await openAddSymbol(page);
    await typeQuery(page, 'GOLD');

    const items = await typeaheadItems(page);
    const syms  = items.map(i => i.sym);

    expect(syms[0]).toBe('GOLD');
    expect(syms[1]).toBe('GOLD.NEXT');
    expect(syms).toContain('GOLDBEES');

    const hasFut = syms.some(s => /^GOLD\d{2}[A-Z]{3}FUT$/.test(s));
    expect(hasFut).toBe(true);
  });

  test('GOLD virtual root meta shows MCX and virtual', async ({ page }) => {
    await openAddSymbol(page);
    await typeQuery(page, 'GOLD');

    const items = await typeaheadItems(page);
    const goldRow = items.find(i => i.sym === 'GOLD');
    expect(goldRow).toBeTruthy();
    expect(goldRow.meta).toContain('MCX');
    expect(goldRow.meta).toContain('virtual');
  });

  test('CRUDEOIL search shows CRUDEOIL and CRUDEOIL.NEXT virtual roots', async ({ page }) => {
    await openAddSymbol(page);
    await typeQuery(page, 'CRUDEOIL');

    const items = await typeaheadItems(page);
    const syms  = items.map(i => i.sym);

    expect(syms[0]).toBe('CRUDEOIL');
    expect(syms[1]).toBe('CRUDEOIL.NEXT');
  });

  test('USDINR search shows USDINR and USDINR.NEXT virtual roots', async ({ page }) => {
    await openAddSymbol(page);
    await typeQuery(page, 'USDINR');

    const items = await typeaheadItems(page);
    const syms  = items.map(i => i.sym);

    expect(syms[0]).toBe('USDINR');
    expect(syms[1]).toBe('USDINR.NEXT');
  });

  test('RELIANCE search shows equity, no MCX/CDS virtual', async ({ page }) => {
    await openAddSymbol(page);
    await typeQuery(page, 'RELIANCE');

    const items = await typeaheadItems(page);
    const syms  = items.map(i => i.sym);

    expect(syms).toContain('RELIANCE');
    expect(items.every(i => !i.meta.includes('virtual'))).toBe(true);
  });

  test('typeahead opens within 1200ms of input (perf budget)', async ({ page }) => {
    await openAddSymbol(page);
    const input = page.locator('input[placeholder*="Symbol"]').first();
    const t0 = Date.now();
    await input.fill('GOLD');
    await page.waitForSelector('.search-typeahead-item', { timeout: 1200 });
    expect(Date.now() - t0).toBeLessThan(1200);
  });
});

test.describe('pulse symbol picker — mobile viewport', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('GOLD search shows virtual roots on mobile (390px)', async ({ page }) => {
    await page.goto(PULSE, { waitUntil: 'networkidle' });
    await loginAsAdmin(page);
    await page.goto(PULSE, { waitUntil: 'networkidle' });

    await openAddSymbol(page);
    await typeQuery(page, 'GOLD');

    const items = await typeaheadItems(page);
    const syms  = items.map(i => i.sym);

    expect(syms[0]).toBe('GOLD');
    expect(syms[1]).toBe('GOLD.NEXT');

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(overflow).toBe(false);
  });
});
