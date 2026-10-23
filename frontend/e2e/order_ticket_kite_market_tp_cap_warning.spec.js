/**
 * order_ticket_kite_market_tp_cap_warning.spec.js
 *
 * Audit item T17 (2026-10-23) — Kite's `place_gtt`
 * (backend/brokers/adapters/kite.py:561) raises BrokerCapabilityError when
 * ANY leg of a GTT carries order_type=MARKET. A TP+SL bracket is placed as
 * ONE two-leg OCO `place_gtt` call, so picking MARKET as the TP order type
 * fails the WHOLE GTT and silently drops the SL too. Pre-fix, nothing on
 * OrderTicket/TemplateBar warned the operator before submit — the failure
 * only surfaced after a real fill, as a `template_attach_failed` alert.
 *
 * This spec confirms the proactive UI warning chip (`.oes-tpl-cap-warn`,
 * rendered in SymbolPanel.svelte, fed by `capWarningFor()` in
 * brokerCapWarnings.js) appears the moment the operator picks MKT in the
 * TemplateBar's TP-order-type toggle, for a Kite-routed account, with an
 * active SL configured — and disappears again when they switch back to
 * LIMIT. The broker-capabilities endpoint is intercepted so the test is
 * deterministic regardless of which real broker the dev/test account is
 * actually wired to.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

const KITE_CAPS = {
  broker_id: 'zerodha_kite',
  display_name: 'Zerodha Kite',
  gtt_single: true,
  gtt_oco: true,
  gtt_modify: true,
  gtt_cap_per_account: 100,
  gtt_validity_days: 365,
  gtt_supports_mcx: true,
  bracket_order: false,
  cover_order: true,
  atomic_basket: false,
  order_tag: true,
  margin_preview: true,
  postback_gtt: 'reliable',
  rate_limit_orders_sec: 10,
};

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

test.describe('Order Ticket — Kite MARKET-leg GTT cap warning (T17)', () => {
  test('picking MKT as the TP order type warns on a Kite-routed account with an active SL', async ({ page }) => {
    test.setTimeout(60_000);

    // Force every broker-capabilities lookup to resolve as Kite, no
    // matter which real account the dev/test environment routes through.
    await page.route('**/api/admin/brokers/*/capabilities', async (route) => {
      if (route.request().method() !== 'GET') { await route.continue(); return; }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(KITE_CAPS),
      });
    });

    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    // Activate the Bracket (Default template) toggle if it isn't already on.
    const bracketToggle = page.locator('.oes-tpl-button').first();
    await expect(bracketToggle).toBeVisible({ timeout: 15_000 });
    if (!(await bracketToggle.evaluate((el) => el.classList.contains('active')))) {
      await bracketToggle.click();
    }
    await expect(bracketToggle).toHaveClass(/active/, { timeout: 5_000 });

    // No warning chip yet — the default template may or may not carry
    // tp_order_type=MARKET, but nothing has been touched yet.
    const warnChip = page.locator('.oes-tpl-cap-warn').first();

    // Force an active SL so the "SL will also fail to attach" branch is
    // reachable regardless of the real default template's own sl_pct.
    const slInput = page.locator('.oes-basket-tpl-param', { hasText: 'SL %' }).locator('input').first();
    await expect(slInput).toBeVisible({ timeout: 10_000 });
    await slInput.fill('3');
    await page.waitForTimeout(150);

    // Expand the Bracket panel to reach the TP order-type toggle.
    const expandBtn = page.locator('.oes-tpl-expand-btn').first();
    await expect(expandBtn).toBeVisible({ timeout: 5_000 });
    await expandBtn.click();
    await page.waitForTimeout(150);

    // Also force a TP% so the "has a TP target" branch is unambiguous.
    const tpInput = page.locator('.oes-basket-tpl-param', { hasText: 'TP %' }).locator('input').first();
    await tpInput.fill('5');
    await page.waitForTimeout(150);

    const mktBtn = page.locator('.oes-tpl-type-btn', { hasText: 'MKT' }).first();
    await expect(mktBtn).toBeVisible({ timeout: 5_000 });
    await mktBtn.click();
    await page.waitForTimeout(400);

    await expect(warnChip).toBeVisible({ timeout: 10_000 });
    await expect(warnChip).toContainText(/rejects MARKET legs/i);
    await expect(warnChip).toContainText(/SL will also fail to attach/i);

    // Switch back to LIMIT — the warning must clear.
    const limitBtn = page.locator('.oes-tpl-type-btn', { hasText: 'LIMIT' }).first();
    await limitBtn.click();
    await page.waitForTimeout(400);
    await expect(page.locator('.oes-tpl-cap-warn', { hasText: /rejects MARKET legs/i })).toHaveCount(0);
  });
});
