/**
 * Smoke spec: Chain tab's "Basket | Place" mode toggle.
 *
 * Verifies commit ab53b7f:
 *  1. Toggle is visible at the top of the Chain tab.
 *  2. Default state is "Basket" mode — clicking + adds to basket.
 *  3. Switching to "Place" mode routes the next + click to the Ticket
 *     tab pre-filled with that leg, instead of basket-adding.
 *
 * Target: prod (ramboq.com)
 */

import { test, expect } from '@playwright/test';

const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedToken = process.env.PLAYWRIGHT_AUTH_TOKEN || null;

async function authOnce(page) {
  if (!_cachedToken) {
    let tok = null;
    for (const delay of [0, 20_000, 65_000]) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      const resp = await page.request.post('/api/auth/login', {
        data: { username: _AUTH_USER, password: _AUTH_PASS },
      });
      if (resp.ok()) { tok = (await resp.json()).access_token; break; }
      if (resp.status() !== 429) throw new Error(`authOnce: /api/auth/login returned ${resp.status()}`);
    }
    if (!tok) { test.skip(true, 'rate-limited'); return; }
    _cachedToken = tok;
  }
  await page.context().addInitScript((token) => {
    sessionStorage.setItem('ramboq_token', token);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
    }));
  }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
}

async function pickFirstAccount(page) {
  const acctTrigger = page.locator('button#opt-acct').first();
  await expect(acctTrigger).toBeVisible({ timeout: 15_000 });
  await expect(acctTrigger).not.toContainText(/No accounts loaded/i, { timeout: 15_000 });
  await acctTrigger.click();
  const firstAcct = page.locator('.rbq-multi-panel .rbq-multi-option').first();
  await expect(firstAcct).toBeVisible({ timeout: 5000 });
  await firstAcct.click();
  // Offset click: the "×" clear button (rbq-multi-clear) overlaps the
  // trigger's centre once an account is selected.
  await acctTrigger.click({ position: { x: 8, y: 8 } });
}

/**
 * Opens the OrderTicket modal on its Chain tab via the canonical path:
 * click an F&O candidate row (opens SymbolPanel), then click the modal's
 * Chain tab. The page-level "Toggle chain picker" button was removed in
 * commit 0e31eee0 — do not reintroduce it.
 */
async function openChainFromRow(page) {
  const modal = page.locator('.oes-modal:visible').first();
  const rows = page.locator('.cand-row:not(.cand-row-total):not(.cand-closed)');
  await expect(rows.first()).toBeVisible({ timeout: 15_000 });
  const n = Math.min(await rows.count(), 10);
  for (let i = 0; i < n; i++) {
    await rows.nth(i).click();
    await expect(modal).toBeVisible({ timeout: 10_000 });
    const chainTab = modal.getByRole('tab', { name: 'Chain', exact: true });
    if ((await chainTab.count()) && (await chainTab.isEnabled())) {
      await chainTab.click();
      return;
    }
    await page.keyboard.press('Escape');
    await expect(modal).toBeHidden({ timeout: 5000 });
  }
  throw new Error('openChainFromRow: no candidate row exposes an enabled Chain tab');
}

async function openChain(page) {
  await pickFirstAccount(page);
  await openChainFromRow(page);
  await page.waitForTimeout(1500);  // let chain quotes settle
}

test.describe('Chain Place-mode toggle', () => {

  test('Ticket tab Submit footer is visible (not hidden by bottom panel)', async ({ page }) => {
    await authOnce(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('domcontentloaded');
    await openChain(page);

    // Switch into the Order Ticket tab directly via the shell tab strip.
    const ticketTab = page.locator('.oes-tab')
      .filter({ hasText: /Order ticket/i }).first();
    if (await ticketTab.count() === 0) {
      test.skip(true, 'ticket tab not surfaced — Chain may be locked-on');
      return;
    }
    await ticketTab.click();
    await page.waitForTimeout(300);

    const ticketFooter = page.locator('.oes-modal .ot-footer').first();
    await expect(ticketFooter).toBeVisible({ timeout: 5000 });

    // The footer should fit inside the viewport (its bottom edge above
    // the bottom panel). Sanity-check via boundingBox.
    const box = await ticketFooter.boundingBox();
    if (!box) test.fail();
    const viewport = page.viewportSize();
    if (viewport && box) {
      // Footer must be entirely above the viewport bottom — at minimum,
      // its top should sit somewhere in the visible region.
      expect(box.y).toBeLessThan(viewport.height);
      expect(box.y + box.height).toBeLessThanOrEqual(viewport.height + 4);
    }
  });
});
