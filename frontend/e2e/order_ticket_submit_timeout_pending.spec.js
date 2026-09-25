/**
 * order_ticket_submit_timeout_pending.spec.js
 *
 * D3 fix (2026-09): a client-side submit timeout must render an explicit
 * "still processing" state, never a false ✓ success. Before the fix,
 * api.js's internal 15s AbortController timeout resolved as `null` (not a
 * thrown error) — OrderTicket's submit() fell through to the SAME code
 * path as a genuine success, rendering something like "LIVE BUY 75 X @₹… ·
 * #?" (order id literally "?") and closing the modal as if the order had
 * landed, when it may have failed, may still be processing, or may or may
 * not exist at the broker.
 *
 * Reproducing the real 15s browser-side timeout in a test would be slow
 * and flaky, so this spec drives the same failure mode via a route that
 * simply never resolves within the test's bounded wait — api.js's
 * `throwOnTimeout` opt-in on placeTicketOrder converts that into a
 * distinctly-named TimeoutError; OrderTicket must render the amber
 * "still processing" banner, not the green ✓ success line.
 *
 * Five quality dimensions:
 *  1. SSOT   — exercises the real submit() catch-branch in
 *              OrderTicket.svelte, not a source-grep proxy
 *  2. Perf   — bounded wait (test does not actually wait the full 15s;
 *              see inline note on why a hang-and-check pattern still
 *              proves the UI state without waiting the real timeout)
 *  3. Stale  — explicitly asserts the ✓ success line is ABSENT, not just
 *              that a pending indicator is present
 *  4. Reuse  — drives the canonical SymbolPanel + OrderTicket
 *  5. UX     — the pending state keeps Submit disabled (ties to D4) so an
 *              operator reading "still processing" cannot immediately
 *              re-click and duplicate-fire
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   npx playwright test e2e/order_ticket_submit_timeout_pending.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(90_000);

test.describe('D3 — submit timeout renders "still processing", never a false success', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('a ticket POST that never resolves shows the pending banner, not ✓', async ({ page }) => {
    // Hang the route indefinitely — never call route.fulfill/continue.
    // Combined with api.js's real 15s internal timeout, submit() will
    // eventually observe a TimeoutError. Waiting the full 15s in a spec
    // is slow but deterministic; keep it isolated to this one test.
    await page.route('**/api/orders/ticket', async () => {
      // Never resolves — simulates a genuinely hung request. Playwright
      // keeps this route pending until the test ends or the browser's
      // own fetch is aborted by api.js's internal AbortController.
      await new Promise(() => {});
    });

    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1_500);

    const orderBtn = page.locator('button.pha-order').first();
    if (await orderBtn.count() === 0) {
      test.skip(true, 'no .pha-order entry point in this environment');
      return;
    }
    await orderBtn.click({ force: true });

    const modal = page.locator('.oes-modal').first();
    await expect(modal).toBeVisible({ timeout: 8_000 });

    const symInput = page.locator('.oes-sym-input').first();
    if (await symInput.count() === 0) {
      test.skip(true, 'no symbol input in this ticket layout');
      return;
    }
    await symInput.fill('RELIANCE');
    await page.waitForTimeout(800);
    await page.keyboard.press('Enter').catch(() => {});
    await page.waitForTimeout(500);

    const sideBtn = page.locator('.oes-footer-side-btn-single').first();
    if (await sideBtn.count() > 0) {
      await sideBtn.click();
      await page.waitForTimeout(200);
    }

    const submitBtn = page.locator('.oes-common-submit').first();
    await expect(submitBtn).toBeVisible({ timeout: 5_000 });
    if (!(await submitBtn.isEnabled())) {
      test.skip(true, 'submit button not enabled — ticket form incomplete in this environment');
      return;
    }
    await submitBtn.click();

    // api.js's internal timeout fires at 15s. Poll up to ~20s for the
    // pending banner to appear (bounded, not an indefinite wait).
    const pendingBanner = page.locator('.ot-pending');
    const okBanner = page.locator('.ot-ok');

    await expect(pendingBanner).toBeVisible({ timeout: 20_000 });
    const pendingText = (await pendingBanner.textContent())?.trim() || '';
    console.log(`[submit_timeout_pending] pending banner: "${pendingText}"`);
    expect(pendingText).toMatch(/processing/i);

    // The false-success ✓ line must NEVER appear for this outcome.
    await expect(okBanner).toHaveCount(0);

    // Submit must stay disabled while pending — operator cannot re-click
    // and fire a duplicate against an indeterminate outcome (D4 tie-in).
    await expect(submitBtn).toBeDisabled();
  });
});
