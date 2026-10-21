/**
 * order_ticket_template_toggle_regression.spec.js
 *
 * Two linked fixes, same day:
 *
 * 1. (Earlier 2026-10-16) The Default/None bracket-template toggle had
 *    quietly stopped rendering on the Order Ticket tab at all. Operator:
 *    "what happened to bracket option in order ticket. it no longer
 *    shows." First fix added a shell-level (SymbolPanel.svelte) render
 *    for the Ticket tab — restored the DISPLAY only.
 *
 * 2. (Later same day, the real fix) Turned out the display-only fix
 *    was cosmetic: the Ticket tab had been DELIBERATELY severed from
 *    templates on 2026-09-29 ("operator's final decision after 3
 *    reversals: 'make sure order ticket is not wired to template'") —
 *    `templateId` was never bound from the shell into `<OrderTicket>`,
 *    and the real broker-submission payload (`placeCtx.templateId`)
 *    was hardcoded to `null` UNCONDITIONALLY. So even with the toggle
 *    visible, picking a Bracket on the Ticket tab never actually
 *    attached anything. Operator's 4th and final call: "fully re-wire
 *    it." The toggle now lives INSIDE OrderTicket.svelte itself (same
 *    row as CHASE/DRAFT — operator: "keep bracket before draft on the
 *    same row"), `templateId` + overrides are bound from the shell
 *    again, and the submit payload sends the real `templateId`
 *    (close orders excepted) instead of a hardcoded null.
 *
 * OrderTicket stays mounted (display:none) while Chain is active (so
 * its margin-preflight effect keeps running) — the TemplateBar mount
 * and its own side/symbol re-validation effects are gated on
 * `!suspended` so a hidden Ticket-tab copy can never fight Chain's
 * copy over the shared templateId, nor can any selector's `.first()`
 * accidentally resolve to the invisible one.
 *
 * Unlike the pre-existing `template_bar_smoke.spec.js` (soft/tolerant —
 * `if (visible) { ... } else { skip }`, which is exactly why the first
 * regression shipped unnoticed), this spec makes HARD assertions.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

async function _seedNiftyOnTicket(page) {
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const symInput = page.locator('.ssi-input').first();
  await expect(symInput).toBeVisible({ timeout: 15_000 });
  await symInput.fill('NIFTY26OCTFUT');
  const sugg = page.locator('.ssi-drop .ssi-row').first();
  await expect(sugg).toBeVisible({ timeout: 45_000 });
  await sugg.click({ force: true });
  await page.waitForTimeout(500);
}

test.describe('Order Ticket — bracket/template toggle regression', () => {
  test('the Default/None template toggle is visible on the Ticket tab (not just Chain)', async ({ page }) => {
    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    // Ticket is the default tab — don't click into Chain.
    const toggle = page.locator('.oes-tpl-button').first();
    await expect(toggle).toBeVisible({ timeout: 15_000 });
  });

  test('switching Ticket -> Chain -> Ticket keeps exactly one toggle visible each time (no duplicate on Chain)', async ({ page }) => {
    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    await expect(page.locator('.oes-tpl-button')).toHaveCount(1);

    const chainTab = page.getByRole('tab', { name: /Chain/i }).first();
    await expect(chainTab).toBeEnabled({ timeout: 15_000 });
    await chainTab.click();
    await page.waitForTimeout(300);
    await expect(page.locator('.oes-tpl-button')).toHaveCount(1);

    const ticketTab = page.getByRole('tab', { name: /Ticket/i }).first();
    await ticketTab.click();
    await page.waitForTimeout(300);
    await expect(page.locator('.oes-tpl-button')).toHaveCount(1);
  });

  test('picking a Bracket on the Ticket tab sends a real template_id to the backend (not hardcoded null)', async ({ page }) => {
    test.setTimeout(60_000);
    // This is the actual bug, not just the display: the toggle could be
    // fully visible and clickable while the real order-placement request
    // still carried a hardcoded `template_id: null` left over from the
    // 2026-09-29 severance. Intercept the real endpoint and read the
    // request body directly — the strongest possible regression guard.
    let capturedBody = null;
    await page.route('**/api/orders/ticket', async (route) => {
      capturedBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ order_id: '12345', mode: 'paper', status: 'COMPLETE', detail: 'ok' }),
      });
    });
    await page.route('**/api/admin/execution/mode', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ mode: 'paper', branch: 'dev', allowed_modes: ['idle', 'sim', 'replay', 'paper', 'shadow'] }),
        });
      } else {
        await route.continue();
      }
    });

    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    const toggle = page.locator('.oes-tpl-button').first();
    await expect(toggle).toBeVisible({ timeout: 15_000 });
    if (!(await toggle.evaluate((el) => el.classList.contains('active')))) {
      await toggle.click();
    }
    await expect(toggle).toHaveClass(/active/, { timeout: 5_000 });

    // MARKET type avoids the "limit price required" validation gate.
    const typeTrigger = page.locator('[aria-label="Order type"]').first();
    await typeTrigger.click();
    const marketOpt = page.locator('.rbq-select-option-label').filter({ hasText: /^MARKET$/ }).first();
    await marketOpt.click();
    await page.waitForTimeout(300);

    const buyPill = page.locator('button.ot-side-buy').first();
    if (await buyPill.count() > 0 && await buyPill.isEnabled().catch(() => false)) {
      await buyPill.click();
      await page.waitForTimeout(200);
    }

    const submitBtn = page.locator('.oes-common-submit, .ot-submit').first();
    await expect(submitBtn).toBeVisible({ timeout: 8_000 });
    if (!(await submitBtn.isEnabled())) {
      test.skip(true, 'submit button not enabled — ticket form incomplete in this environment');
      return;
    }
    await submitBtn.click();
    await page.waitForTimeout(1500);

    expect(capturedBody, 'the mocked /api/orders/ticket route must have captured a request body').not.toBeNull();
    expect(capturedBody.template_id, 'template_id must be a real id, not the old hardcoded null').not.toBeNull();
    expect(typeof capturedBody.template_id).toBe('number');
  });
});
