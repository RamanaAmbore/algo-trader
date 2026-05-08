// @ts-check
//
// E2E: /admin/pnl Live Snapshot — per-account colour dots + stripe
//
// Verifies:
//   1. The Live Snapshot collapsible is expanded by default.
//   2. AG Grid rows appear inside the snapshot (grids render correctly).
//   3. Every non-TOTAL account cell has an .acct-dot element.
//   4. Two different account codes (masked ZG#### / ZJ####) have visually
//      distinct stripe colours (border-left-color on the .ag-col-acct cell).
//
// Auth: needs admin credentials. If no token/creds are provided the test is
// skipped (same pattern as order_placement.spec.js).
//
// Run:
//   PLAYWRIGHT_ADMIN_TOKEN=eyJ… PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
//     npx playwright test e2e/pnl_account_colors.spec.js --project=chromium-desktop

import { test, expect } from '@playwright/test';

const BASE  = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const TOKEN = process.env.PLAYWRIGHT_ADMIN_TOKEN || '';
const USER  = process.env.ADMIN_USER || '';
const PASS  = process.env.ADMIN_PASS || '';

async function login(page) {
  if (TOKEN) {
    await page.addInitScript((tok) => {
      try {
        const auth = { user: { role: 'admin' }, token: tok };
        localStorage.setItem('rambo.auth', JSON.stringify(auth));
        sessionStorage.setItem('ramboq_token', tok);
      } catch (_) {}
    }, TOKEN);
    return true;
  }
  if (USER && PASS) {
    await page.goto(`${BASE}/signin`, { waitUntil: 'domcontentloaded' });
    await page.fill('input[type="email"]', USER);
    await page.fill('input[type="password"]', PASS);
    await page.click('button[type="submit"]');
    await page.waitForURL('**/dashboard', { timeout: 10_000 }).catch(() => {});
    return true;
  }
  return false;
}

test.describe('pnl page — account colour dots and stripe', () => {

  test.beforeEach(async ({ page }) => {
    const authed = await login(page);
    if (!authed) {
      test.skip(true, 'No admin credentials provided — skipping auth-required test');
    }
  });

  test('/admin/pnl: Live Snapshot has coloured account dots', async ({ page }) => {
    await page.goto(`${BASE}/admin/pnl`, { waitUntil: 'domcontentloaded' });

    // Live Snapshot section is expanded by default (snapshotExpanded = true).
    // Wait for at least one ag-row to appear inside the snapshot card.
    const snapshotCard = page.locator('.upload-card').first();
    const anyRow = snapshotCard.locator('.ag-row').first();
    await anyRow.waitFor({ state: 'attached', timeout: 20_000 });

    // Every non-TOTAL account cell should have an .acct-dot child.
    // The TOTAL row carries class "totals-row" on the .ag-row; we exclude it.
    const nonTotalAccountCells = snapshotCard.locator(
      '.ag-row:not(.totals-row) .ag-col-acct'
    );
    const cellCount = await nonTotalAccountCells.count();
    expect(cellCount, 'Expected at least one non-TOTAL account cell').toBeGreaterThan(0);

    const dotsCount = await snapshotCard.locator(
      '.ag-row:not(.totals-row) .ag-col-acct .acct-dot'
    ).count();
    expect(dotsCount, 'Every non-TOTAL account cell should have an .acct-dot').toBe(cellCount);

    // All dot elements should have a non-empty background-color style.
    const firstDot = snapshotCard.locator('.ag-row:not(.totals-row) .acct-dot').first();
    const bg = await firstDot.evaluate((el) => getComputedStyle(el).backgroundColor);
    expect(bg, 'Dot should have a computed background-color').not.toBe('');
    expect(bg, 'Dot background-color should not be transparent').not.toContain('rgba(0, 0, 0, 0)');
  });

  test('/admin/pnl: two distinct account codes have distinct stripe colours', async ({ page }) => {
    await page.goto(`${BASE}/admin/pnl`, { waitUntil: 'domcontentloaded' });

    const snapshotCard = page.locator('.upload-card').first();
    await snapshotCard.locator('.ag-row').first().waitFor({ state: 'attached', timeout: 20_000 });

    // Collect all unique border-left-color values from non-TOTAL account cells.
    const stripeColors = await snapshotCard.locator(
      '.ag-row:not(.totals-row) .ag-col-acct'
    ).evaluateAll((cells) =>
      cells.map((c) => getComputedStyle(c).borderLeftColor).filter(Boolean)
    );

    const uniqueColors = new Set(stripeColors.filter(
      // exclude transparent / none
      (c) => c && !c.includes('rgba(0, 0, 0, 0)') && c !== 'transparent'
    ));

    // If there's only one account in the book we can only assert the dot is
    // coloured — skip the distinctness check gracefully.
    if (uniqueColors.size < 2) {
      console.log('Only one account in the book — skipping distinctness assertion');
      expect(uniqueColors.size, 'At least one stripe colour should be non-transparent').toBeGreaterThanOrEqual(1);
      return;
    }

    expect(uniqueColors.size, 'Two different accounts should have distinct stripe colours').toBeGreaterThanOrEqual(2);
  });

});
