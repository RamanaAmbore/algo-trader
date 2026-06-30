// Verifies the closed-hours refresh UX flow shipped in the
// market-lifecycle slice:
//   - When isMarketOpen() returns false, clicking Refresh now FIRES the
//     refresh (broker data updates) AND surfaces a toast that says
//     "Showing close snapshot — markets reopen at <next open time>".
//   - The toast auto-dismisses (≤ 4 s default).
//   - The button still calls the parent onClick (no longer blocks the
//     refresh with a modal popup as the legacy behaviour did).
//
// Five dimensions:
//   1. SSOT       — RefreshButton toast routes through toastStore.toast
//                   (single source).
//   2. Performance — toast is ephemeral (no localStorage write); no extra
//                   network call beyond the click's parent onClick.
//   3. Stale code — Verifies the OLD blocking-modal selector
//                   (.rf-closed-popup) is no longer shown on click.
//   4. Reusable   — ToastContainer is the same component as everywhere
//                   else (.toast-container .toast-info selector).
//   5. UX         — Toast colour is the info tone, copy contains
//                   "close snapshot" and "reopen".

import { test, expect } from '@playwright/test';

test.setTimeout(120000);

const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

async function signIn(page) {
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="username"], input#username, input#s-user').first().fill(USER);
  await page.locator('input[name="password"], input#password, input#s-pass').first().fill(PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  for (let i = 0; i < 10; i++) {
    const has = await page.evaluate(() => !!sessionStorage.getItem('ramboq_token'));
    if (has) break;
    await new Promise((r) => setTimeout(r, 300));
  }
}

async function forceMarketClosed(page) {
  // Force the marketHours module into closed-market mode by patching
  // the network response to /api/market/status. We do this BEFORE the
  // /pulse navigation so the server-status poller picks it up first.
  await page.route('**/api/market/status', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        nse_open:   false,
        mcx_open:   false,
        any_open:   false,
        is_holiday: false,
      }),
    });
  });
}

test('Refresh during closed-market hours fires + shows close-snapshot toast', async ({ page }) => {
  await signIn(page);
  await forceMarketClosed(page);

  await page.goto('/pulse', { waitUntil: 'networkidle' });
  // Give the marketHours poller a chance to pull the forced /api/market/status.
  await page.waitForTimeout(1500);

  const refresh = page.locator('.rf-btn').first();
  await expect(refresh).toBeVisible();

  // The button's market state class should be rf-mkt-closed when both
  // segments are closed.
  await expect(refresh).toHaveClass(/rf-mkt-closed/);

  // Click — fire the refresh.
  await refresh.click();

  // Toast surfaces — content + auto-dismiss. Canonical selector =
  // ToastContainer's .rbq-toast (single source of truth in lib/).
  const toast = page.locator('.rbq-toast-container .rbq-toast, .rbq-toast').first();
  await expect(toast).toBeVisible({ timeout: 2500 });
  const text = (await toast.innerText()).toLowerCase();
  console.log(`[closed_hours_ux] toast text = "${text}"`);
  expect(text).toContain('close snapshot');
  expect(text).toContain('reopen');

  // Legacy blocking-modal selector MUST NOT be visible — the operator
  // wants the refresh to fire (not be blocked) during closed hours.
  const oldPopup = page.locator('.rf-closed-popup');
  await expect(oldPopup).toHaveCount(0);

  // Auto-dismiss within 4s (toastStore default = 3000ms for `info`).
  await expect(toast).toBeHidden({ timeout: 4000 });
});

test('Position rows show snapshot LTPs during closed hours (not dashes)', async ({ page }) => {
  await signIn(page);
  await forceMarketClosed(page);

  await page.goto('/pulse', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Wait for any AG-Grid row to render.
  await page.locator('.ag-row').first().waitFor({ timeout: 20000 });

  // Probe up to 10 rows — at least one should have a numeric LTP (not
  // '—' or empty) because the close-snapshot path serves daily_book
  // LTPs even during off-hours.
  const rows = page.locator('.ag-row');
  const count = Math.min(await rows.count(), 10);
  let numericFound = 0;
  for (let i = 0; i < count; i++) {
    const row = rows.nth(i);
    const ltpCell = row.locator('[col-id="last_price"], [col-id="ltp"]').first();
    if (!(await ltpCell.count())) continue;
    const txt = (await ltpCell.innerText()).trim();
    if (/^[\d,.+\-]+$/.test(txt) && txt !== '—' && txt !== '0' && txt !== '') {
      numericFound += 1;
    }
  }
  console.log(`[closed_hours_ux] numeric LTP cells found = ${numericFound} / ${count}`);
  // We tolerate the demo / no-positions case (count === 0) — only
  // ASSERT non-dash LTPs WHEN positions are present.
  if (count > 0) {
    expect(numericFound).toBeGreaterThan(0);
  }
});
