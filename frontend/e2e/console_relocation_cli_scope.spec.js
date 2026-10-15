/**
 * console_relocation_cli_scope.spec.js — Phase 2 Sprint B (2026-10,
 * "Console retirement, Agent/Bracket/Template unification").
 *
 * Covers the mandatory test matrix from the sprint brief that isn't
 * already exercised by the updated console_decompile_bracket_edit_flow
 * / console_event_token_submit / console_guide_page specs:
 *
 *   1. Automation's Console tab (full statement scope) — one statement
 *      of each kind (order / bracket / agent) compiles and submits via
 *      its real existing endpoint (mocked at the network boundary —
 *      see "Mocking" note below).
 *   2. Orders' CLI tab (order-scoped, mounted via SymbolPanel's
 *      restored `cli` tab — tabs.js) — an order(...) statement submits
 *      through the real pipeline (PAPER, direct; LIVE, through the
 *      existing confirm modal); a bracket(...)/WHEN... statement is
 *      REJECTED by the compiler with the scope error, visibly, in the
 *      UI — never a silent no-op.
 *   3. SymbolPanel order-ticket MODAL's own CLI tab — same order-only
 *      acceptance check, inside the modal context (opened via the
 *      page-header Order button, same mount component, different
 *      context than /orders' inline mount).
 *   4. The retired `/console` route redirects to the Automation Console
 *      tab rather than 404ing.
 *
 * Mocking — every write path (`POST /api/orders/ticket`,
 * `POST /api/admin/templates/`, `POST /api/agents/`) is intercepted via
 * `page.route()` so this spec never creates real rows or places a real
 * order against dev — consistent with the existing bracket-decompile
 * spec's own mocking approach for the same reason (POST write
 * endpoints require a capability the default `rambo` admin fixture may
 * lack, and a Playwright spec must never place a live order).
 * LIVE mode (for the confirm-modal check) is forced the same way — by
 * mocking `GET /api/admin/execution/mode`, never by mutating real
 * backend settings.
 *
 * Viewport: chromium-desktop only — CLI authoring is not a mobile-first
 * surface (same convention as every sibling CLI spec in this file set).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

/** Mocks a write endpoint, capturing the posted JSON body into `out.body`. */
async function mockPost(/** @type {import('@playwright/test').Page} */ page, /** @type {string} */ pattern, /** @type {{body: any}} */ out, /** @type {any} */ responseBody) {
  await page.route(pattern, async (route) => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    out.body = route.request().postDataJSON();
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(responseBody) });
  });
}

test.describe('Automation Console tab — full statement scope (Sprint B)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('order(...) statement compiles and submits via POST /api/orders/ticket', async ({ page }) => {
    const captured = { body: null };
    await mockPost(page, '**/api/orders/ticket', captured, { status: 'ok', order_id: 'ZZZ-E2E-1' });

    await page.goto('/automation?tab=console');
    await page.waitForLoadState('networkidle');

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", qty=1, price=100)');

    const placeBtn = page.locator('.agent-cli-place-order');
    await expect(placeBtn).toBeVisible({ timeout: TIMEOUT });
    await placeBtn.click();

    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();
    expect(captured.body.tradingsymbol).toBe('RELIANCE');
    expect(captured.body.side).toBe('SELL');

    // Submit clears the textarea on success (ConsoleTab._submitAgentCliOrder).
    await expect(cli).toHaveValue('', { timeout: TIMEOUT });
  });

  test('bracket(...) statement compiles and submits via POST /api/admin/templates/', async ({ page }) => {
    const captured = { body: null };
    await mockPost(page, '**/api/admin/templates/', captured, { id: 99001, name: 'ZZZ E2E Bracket' });

    await page.goto('/automation?tab=console');
    await page.waitForLoadState('networkidle');

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20) #name="ZZZ E2E Bracket"');

    const saveBtn = page.locator('.console-agent-cli-btn', { hasText: 'Save bracket' });
    await expect(saveBtn).toBeVisible({ timeout: TIMEOUT });
    await saveBtn.click();

    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();
    expect(captured.body.applies_to).toBe('buy_any');
    expect(captured.body.tp_pct).toBe(30);
    expect(captured.body.name).toBe('ZZZ E2E Bracket');
    await expect(cli).toHaveValue('', { timeout: TIMEOUT });
  });

  test('WHEN...ALERT...DO... agent statement compiles and submits via POST /api/agents/', async ({ page }) => {
    const captured = { body: null };
    await mockPost(page, '**/api/agents/', captured, { slug: 'zzz-e2e-agent', ok: true });

    await page.goto('/automation?tab=console');
    await page.waitForLoadState('networkidle');

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill(
      'WHEN pnl_pct@positions.total <= -5 ALERT telegram ' +
      'DO order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", qty=1) #name="ZZZ E2E Agent"'
    );

    const createBtn = page.locator('.console-agent-cli-btn', { hasText: 'Create agent' });
    await expect(createBtn).toBeVisible({ timeout: TIMEOUT });
    await createBtn.click();

    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();
    expect(captured.body.name).toBe('ZZZ E2E Agent');
    expect(captured.body.kind).toBe('threshold');
    await expect(cli).toHaveValue('', { timeout: TIMEOUT });
  });
});

test.describe('Orders CLI tab — order-only scope (Sprint B)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('order(...) statement submits (PAPER) via the same /api/orders/ticket path', async ({ page }) => {
    const captured = { body: null };
    await mockPost(page, '**/api/orders/ticket', captured, { status: 'ok', order_id: 'ZZZ-E2E-2' });

    await page.goto('/orders');
    await page.waitForLoadState('networkidle');

    const cliTab = page.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    // "orders only" scope sub-label TEXT was removed (2026-10, operator:
    // "remove account, exchange, symbol, console details. just keep
    // trader guide button for link and cli for order entry") — the
    // underlying scope ENFORCEMENT (compiler rejects non-order
    // statements) is unchanged and covered by the sibling "rejects
    // bracket/WHEN statements" test below.
    // Guide link itself later moved (2026-10, same day) out of
    // OrderCliTab's own body entirely, up into SymbolPanel's .oes-tabs
    // row (.oes-cli-guide-link) — replacing LTP/CHASE for the CLI tab
    // specifically (operator: "ltp and chase should not be displayed.
    // in its place, trader guide button for link should be displayed").
    await expect(page.locator('a.oes-cli-guide-link')).toContainText('Trader Guide');

    await cli.fill('order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", qty=1, price=100)');

    const placeBtn = page.locator('.agent-cli-place-order');
    await expect(placeBtn).toBeVisible({ timeout: TIMEOUT });
    await placeBtn.click();

    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();
    expect(captured.body.tradingsymbol).toBe('RELIANCE');
  });

  test('order(...) statement in LIVE mode submits through the existing live-order confirm modal', async ({ page }) => {
    // Force LIVE mode by mocking the status endpoint the navbar/mode
    // store polls — never by mutating a real backend setting.
    await page.route('**/api/admin/execution/mode', async (route) => {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ mode: 'live', allowed_modes: ['paper', 'live', 'shadow', 'sim', 'replay'], branch: 'main' }),
      });
    });
    const captured = { body: null };
    await mockPost(page, '**/api/orders/ticket', captured, { status: 'ok', order_id: 'ZZZ-E2E-3' });

    await page.goto('/orders');
    await page.waitForLoadState('networkidle');
    // Give the mode poll a moment to land before switching tabs.
    await page.waitForTimeout(1000);

    const cliTab = page.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", qty=1, price=100)');

    const placeBtn = page.locator('.agent-cli-place-order');
    await expect(placeBtn).toBeVisible({ timeout: TIMEOUT });
    await expect(placeBtn).toContainText('LIVE');
    await placeBtn.click();

    // The existing confirm-before-live-order modal must appear — the
    // order must NOT have been placed before this confirm.
    const confirmDialog = page.locator('.cm-modal', { hasText: /LIVE order/i });
    await expect(confirmDialog).toBeVisible({ timeout: TIMEOUT });
    expect(captured.body).toBeNull();

    await confirmDialog.locator('.cm-confirm').click();
    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();
  });

  test('bracket(...) statement is rejected with the scope error, visibly, not a silent no-op', async ({ page }) => {
    await page.goto('/orders');
    await page.waitForLoadState('networkidle');

    const cliTab = page.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20) #name="Should Not Save"');

    const errors = page.locator('.agent-cli-errors');
    await expect(errors).toBeVisible({ timeout: TIMEOUT });
    await expect(errors).toContainText(/Automation.*Console/i);

    // No "Save bracket"/"Create agent" affordance exists on this
    // order-only mount at all — the rejection is structural, not a
    // hidden button.
    await expect(page.locator('.console-agent-cli-btn')).toHaveCount(0);
  });

  test('WHEN...ALERT...DO... statement is rejected with the scope error, visibly, not a silent no-op', async ({ page }) => {
    await page.goto('/orders');
    await page.waitForLoadState('networkidle');

    const cliTab = page.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill(
      'WHEN pnl_pct@positions.total <= -5 ALERT telegram ' +
      'DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)'
    );

    const errors = page.locator('.agent-cli-errors');
    await expect(errors).toBeVisible({ timeout: TIMEOUT });
    await expect(errors).toContainText(/Automation.*Console/i);
  });
});

test.describe('SymbolPanel order-ticket modal — restored CLI tab (Sprint B)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('order(...) statement submits inside the modal context', async ({ page }) => {
    const captured = { body: null };
    await mockPost(page, '**/api/orders/ticket', captured, { status: 'ok', order_id: 'ZZZ-E2E-4' });

    await page.goto('/dashboard');
    await page.waitForLoadState('networkidle');

    const orderBtn = page.locator('button.pha-order').first();
    await expect(orderBtn).toBeVisible({ timeout: TIMEOUT });
    await orderBtn.click();

    const modal = page.locator('.oes-modal').first();
    await expect(modal).toBeVisible({ timeout: TIMEOUT });

    const cliTab = modal.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const cli = modal.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    // Scope sub-label TEXT removed, then the Guide link itself moved to
    // SymbolPanel's .oes-tabs row (2026-10, operator request) — see the
    // matching comment on the /orders-page test above.
    await expect(modal.locator('a.oes-cli-guide-link')).toContainText('Trader Guide');

    await cli.fill('order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", qty=1, price=100)');

    const placeBtn = modal.locator('.agent-cli-place-order');
    await expect(placeBtn).toBeVisible({ timeout: TIMEOUT });
    await placeBtn.click();

    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();
    expect(captured.body.tradingsymbol).toBe('RELIANCE');
  });

  test('bracket(...) statement is rejected inside the modal context too', async ({ page }) => {
    await page.goto('/dashboard');
    await page.waitForLoadState('networkidle');

    const orderBtn = page.locator('button.pha-order').first();
    await expect(orderBtn).toBeVisible({ timeout: TIMEOUT });
    await orderBtn.click();

    const modal = page.locator('.oes-modal').first();
    await expect(modal).toBeVisible({ timeout: TIMEOUT });

    const cliTab = modal.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const cli = modal.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20) #name="Should Not Save"');

    const errors = modal.locator('.agent-cli-errors');
    await expect(errors).toBeVisible({ timeout: TIMEOUT });
    await expect(errors).toContainText(/Automation.*Console/i);
  });
});

test.describe('/console redirect (Sprint B — Console retirement)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('old /console bookmark redirects to the Automation Console tab, not a 404', async ({ page }) => {
    const response = await page.goto('/console', { waitUntil: 'domcontentloaded' });
    expect(response?.status()).toBeLessThan(400);
    await expect(page).toHaveURL(/\/automation\?tab=console/, { timeout: TIMEOUT });
    await expect(page).toHaveTitle('Automation | RamboQuant Analytics', { timeout: TIMEOUT });
    await expect(page.locator('.agent-cli-textarea')).toBeVisible({ timeout: TIMEOUT });
  });

  test('old /console/guide bookmark redirects to /automation/guide, not a 404', async ({ page }) => {
    const response = await page.goto('/console/guide', { waitUntil: 'domcontentloaded' });
    expect(response?.status()).toBeLessThan(400);
    await expect(page).toHaveURL(/\/automation\/guide$/, { timeout: TIMEOUT });
    await expect(page.locator('h1.page-title-chip', { hasText: 'Console CLI Guide' }))
      .toBeVisible({ timeout: TIMEOUT });
  });
});
