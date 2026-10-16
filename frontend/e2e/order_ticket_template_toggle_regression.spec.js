/**
 * order_ticket_template_toggle_regression.spec.js
 *
 * 2026-10-16 fix — the Default/None bracket-template toggle had quietly
 * stopped rendering on the Order Ticket tab. Operator: "what happened to
 * bracket option in order ticket. it no longer shows."
 *
 * Root cause: a string of well-intentioned incremental UI changes ended
 * with the toggle (`<TemplateBar>`) relocated into OptionChainTab's own
 * expiry row (2026-09-30, for Chain's own layout reasons) — but nothing
 * was left rendering it for the Ticket tab. OrderTicket.svelte itself had
 * already dropped its own copy in an earlier refactor on the assumption
 * the shell would always render one. SymbolPanel.svelte's shell-level
 * block had been reduced to ONLY the on-fill preview chip + cap-warning
 * strip, which requires a resolved template to show anything — with no
 * toggle anywhere, there was no way to resolve one on the Ticket tab.
 *
 * Fix: SymbolPanel.svelte now also renders <TemplateBar> directly at
 * shell level when the Ticket tab is active, reusing the exact same
 * `_showTemplateBar`/`_showDemoTplNote` gates and `_onTemplSelect*`
 * handlers Chain already uses — single source, so the two render sites
 * can never drift again. Chain's own embedded copy (inside
 * OptionChainTab) is unaffected and gated separately, so this must not
 * produce two toggles on the Chain tab.
 *
 * Unlike the pre-existing `template_bar_smoke.spec.js` (soft/tolerant —
 * `if (visible) { ... } else { skip }`, which is exactly why this
 * regression shipped unnoticed), this spec makes a HARD assertion that
 * the toggle is visible on the Ticket tab.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

async function _seedNiftyOnTicket(page) {
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const symInput = page.locator('.ssi-input').first();
  await expect(symInput).toBeVisible({ timeout: 15_000 });
  await symInput.fill('NIFTY');
  const sugg = page.locator('.ssi-drop .ssi-row').first();
  await expect(sugg).toBeVisible({ timeout: 10_000 });
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
});
