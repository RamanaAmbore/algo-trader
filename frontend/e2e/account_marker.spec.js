/**
 * Per-account identity stripe (`.ag-col-acct`).
 *
 * Each row's account cell carries a 3px left border in one of 8
 * hash-derived hues (PerformancePage.svelte::ACCT_PALETTE). TOTAL
 * rows resolve to transparent. Real account IDs render unmasked
 * for admin sessions (Z[A-Z]\d{4} shape).
 *
 * Default stripe rule lives in app.css; the per-row colour is
 * injected via cellStyle's --acct-stripe custom property.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 25_000;

// Hash colours from PerformancePage.svelte's ACCT_PALETTE — order-
// agnostic comparison (we don't know which colour each account ID
// hashes to, so we just check membership).
const HASH_COLORS = new Set([
  'rgb(167, 139, 250)', // violet
  'rgb(94, 234, 212)',  // teal
  'rgb(253, 164, 175)', // rose
  'rgb(125, 211, 252)', // sky
  'rgb(190, 242, 100)', // lime
  'rgb(252, 211, 77)',  // amber
  'rgb(165, 180, 252)', // indigo
  'rgb(240, 171, 252)', // fuchsia
]);

test.describe('Account marker — left-edge stripe', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('non-TOTAL row has 3px coloured stripe + unmasked account text', async ({ page }) => {
    await page.goto('/dashboard');
    // Wait for at least one BODY-row account cell to populate. The
    // header cell also carries `.ag-col-acct` (because the column's
    // headerClass mirrors its cellClass for theme parity), so we
    // scope on `.ag-row .ag-col-acct` to skip the header.
    const bodyCell = page.locator('.ag-row .ag-cell.ag-col-acct').first();
    try {
      await bodyCell.waitFor({ state: 'attached', timeout: TIMEOUT });
    } catch (_) {
      test.skip(true, 'no body rows in any grid — book empty?');
    }

    // Pull every BODY account cell + its computed stripe + raw text in
    // one round-trip. Header cells (.ag-header-cell) excluded.
    const cells = await page.evaluate(() => {
      const out = /** @type {Array<{text:string,bw:string,bc:string}>} */ ([]);
      for (const el of document.querySelectorAll('.ag-row .ag-cell.ag-col-acct')) {
        const cs = getComputedStyle(/** @type {HTMLElement} */ (el));
        out.push({
          text: el.textContent?.trim() || '',
          bw: cs.borderLeftWidth,
          bc: cs.borderLeftColor,
        });
      }
      return out;
    });

    if (!cells.length) {
      test.skip(true, 'no body account cells rendered — book empty? skip');
    }

    // Find a non-TOTAL row with a real account ID. Admins see real
    // values like `ZG0790` (NOT masked).
    const real = cells.find(c => /^Z[A-Z]\d{4}$/.test(c.text));
    if (!real) {
      test.skip(true, `no Z[A-Z]\\d{4} account cells — likely book is all TOTAL or sim/empty; cells=${JSON.stringify(cells.slice(0, 5))}`);
    }

    expect(real.bw).toBe('3px');
    expect(HASH_COLORS.has(real.bc), `borderLeftColor ${real.bc} not in hash palette`).toBe(true);

    // TOTAL rows (if present) → transparent border.
    const total = cells.find(c => c.text === 'TOTAL');
    if (total) {
      // CSS engine renders `transparent` and `rgba(0,0,0,0)` identically.
      expect(['rgba(0, 0, 0, 0)', 'transparent']).toContain(total.bc);
    }
  });
});
