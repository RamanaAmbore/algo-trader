/**
 * console_ui_polish_bundle.spec.js — UI-polish bundle (2026-10-11, 4 items).
 *
 * 1. Console CLI order placement stays inline in the current panel (no
 *    SymbolPanel ticket modal) and shows success/failure in the
 *    AgentCliEditor's own `.agent-cli-submit-status` result area — same
 *    mechanism OrderCliTab already uses (see `console_relocation_cli_scope
 *    .spec.js`, which already proves submission itself is inline; this
 *    spec is the status-line + no-modal regression guard on top of that).
 * 2. Both Guide links (`ConsoleTab.svelte`, `OrderCliTab.svelte`) render
 *    as a button-shaped `.btn-secondary` pill, not a bare underlined
 *    text link.
 * 3. `CardControls` (via `CardHeader`) renders on the Console tab's card,
 *    with Fullscreen wired (Search/Collapse intentionally omitted — no
 *    grid, no reason to hide a tab's only content).
 * 4. The three Agents-tab kind-count cards (Agents/Alerts/Brackets —
 *    Templates folded into Agents 2026-10-12) each render with a
 *    distinct `.kind-card-n` AND `.kind-card-l` color keyed off
 *    `data-kind`.
 *
 * Mocking — order submission is mocked at the network boundary
 * (`POST /api/orders/ticket`), same approach as the sibling
 * `console_relocation_cli_scope.spec.js` — never places a real order.
 *
 * Viewport: chromium-desktop only — CLI authoring + card chrome are not
 * mobile-first surfaces (same convention as every sibling CLI spec).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.use({ projects: ['chromium-desktop'] });

const TIMEOUT = 20_000;

/** Mocks a write endpoint, capturing the posted JSON body into `out.body`. */
async function mockPost(/** @type {import('@playwright/test').Page} */ page, /** @type {string} */ pattern, /** @type {{body: any}} */ out, /** @type {any} */ responseBody, /** @type {number} */ status = 200) {
  await page.route(pattern, async (route) => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    out.body = route.request().postDataJSON();
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(responseBody) });
  });
}

test.describe('Item 1 — Console CLI order placement stays inline, no modal', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('successful order(...) submit shows the result in .agent-cli-submit-status, no modal opens', async ({ page }) => {
    const captured = { body: null };
    await mockPost(page, '**/api/orders/ticket', captured, { status: 'ok', order_id: 'ZZZ-E2E-POLISH-1' });

    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", qty=1, price=100)');

    const placeBtn = page.locator('.agent-cli-place-order');
    await expect(placeBtn).toBeVisible({ timeout: TIMEOUT });
    await placeBtn.click();

    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();

    // Result shown inline, in the shared submitStatus mechanism.
    const status = page.locator('.agent-cli-submit-status');
    await expect(status).toBeVisible({ timeout: TIMEOUT });
    await expect(status).toContainText('Order placed');
    await expect(status).not.toHaveClass(/is-error/);

    // No ticket modal / SymbolPanel overlay ever appears on this path.
    await expect(page.locator('.oes-modal')).toHaveCount(0);
  });

  test('a failed order(...) submit shows an error in .agent-cli-submit-status, no modal opens', async ({ page }) => {
    const captured = { body: null };
    await mockPost(page, '**/api/orders/ticket', captured, { detail: 'margin shortfall' }, 400);

    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const cli = page.locator('.agent-cli-textarea');
    await expect(cli).toBeVisible({ timeout: TIMEOUT });
    await cli.fill('order(account="ZG0790", symbol="RELIANCE", exchange="NSE", side="SELL", qty=1, price=100)');

    const placeBtn = page.locator('.agent-cli-place-order');
    await expect(placeBtn).toBeVisible({ timeout: TIMEOUT });
    await placeBtn.click();

    await expect.poll(() => captured.body, { timeout: TIMEOUT }).not.toBeNull();

    const status = page.locator('.agent-cli-submit-status');
    await expect(status).toBeVisible({ timeout: TIMEOUT });
    await expect(status).toHaveClass(/is-error/);
    await expect(status).toContainText('Order failed');

    // Still no ticket modal — failure is shown inline too, not a popup.
    await expect(page.locator('.oes-modal')).toHaveCount(0);
    // CLI text is preserved on failure so the operator can fix + retry.
    await expect(cli).not.toHaveValue('');
  });
});

test.describe('Item 2 — Guide link renders as a button, not a bare link', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('Console tab Guide link has button-pill chrome', async ({ page }) => {
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const guideLink = page.locator('a[href="/automation/guide"]').first();
    await expect(guideLink).toBeVisible({ timeout: TIMEOUT });
    await expect(guideLink).toHaveClass(/btn-secondary/);
    await expect(guideLink).toContainText('Guide: CLI grammar reference');

    // A real button-shaped affordance has padding + a border, not an
    // underline-only text link.
    const style = await guideLink.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { textDecorationLine: cs.textDecorationLine, borderWidth: cs.borderTopWidth };
    });
    expect(style.textDecorationLine).toBe('none');
    expect(parseFloat(style.borderWidth)).toBeGreaterThan(0);
  });

  test('Orders CLI tab Guide link has button-pill chrome', async ({ page }) => {
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    const cliTab = page.getByRole('tab', { name: 'CLI' });
    await expect(cliTab).toBeVisible({ timeout: TIMEOUT });
    await cliTab.click();

    const guideLink = page.locator('a.order-cli-guide-link');
    await expect(guideLink).toBeVisible({ timeout: TIMEOUT });
    await expect(guideLink).toHaveClass(/btn-secondary/);

    const style = await guideLink.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { textDecorationLine: cs.textDecorationLine, borderWidth: cs.borderTopWidth };
    });
    expect(style.textDecorationLine).toBe('none');
    expect(parseFloat(style.borderWidth)).toBeGreaterThan(0);
  });
});

test.describe('Item 3 — CardControls on the Console tab panel', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('Console tab renders a CardHeader with a wired Fullscreen control', async ({ page }) => {
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const header = page.locator('.console-card .card-header');
    await expect(header).toBeVisible({ timeout: TIMEOUT });
    await expect(header.locator('.ch-title')).toContainText('Console');

    const fsBtn = header.locator('button[aria-label*="fullscreen" i], button[title*="fullscreen" i]');
    await expect(fsBtn).toBeVisible({ timeout: TIMEOUT });

    await fsBtn.click();
    await expect(page.locator('.console-card.fs-card-on')).toBeVisible({ timeout: TIMEOUT });

    // The either/or pair — DefaultSizeButton replaces FullscreenButton
    // while fullscreen is active.
    const restoreBtn = header.locator('button[aria-label*="restore" i], button[title*="default size" i], button[title*="restore" i]');
    await expect(restoreBtn).toBeVisible({ timeout: TIMEOUT });
  });

  test('Console tab card omits Search/Download (no grid to act on)', async ({ page }) => {
    await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });

    const header = page.locator('.console-card .card-header');
    await expect(header).toBeVisible({ timeout: TIMEOUT });

    await expect(header.locator('button[title*="search" i], button[aria-label*="search" i]')).toHaveCount(0);
    await expect(header.locator('button[title*="download" i], button[aria-label*="download" i]')).toHaveCount(0);
  });
});

test.describe('Item 4 — Agents-tab kind-cards each get a distinct color', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('Agents/Alerts/Brackets kind-cards render 3 distinct .kind-card-n colors (Templates folded into Agents, 2026-10-12)', async ({ page }) => {
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });

    const kinds = ['agents', 'alerts', 'brackets'];
    const colors = new Set();
    for (const kind of kinds) {
      const card = page.locator(`.kind-card[data-kind="${kind}"] .kind-card-n`);
      await expect(card).toBeVisible({ timeout: TIMEOUT });
      const color = await card.evaluate((el) => getComputedStyle(el).color);
      colors.add(color);
    }
    // All three must be distinct — no two kinds share a color.
    expect(colors.size).toBe(3);
  });

  // 2026-10-12 — operator: "alerts and brackets background, border, text
  // colors can be different from agents". The number already was
  // distinct (test above); the LABEL text ("AGENTS"/"ALERTS"/"BRACKETS")
  // was still a flat muted grey for all three until this fix.
  test('Agents/Alerts/Brackets kind-cards also render 3 distinct .kind-card-l (label) colors', async ({ page }) => {
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });

    const kinds = ['agents', 'alerts', 'brackets'];
    const colors = new Set();
    for (const kind of kinds) {
      const label = page.locator(`.kind-card[data-kind="${kind}"] .kind-card-l`);
      await expect(label).toBeVisible({ timeout: TIMEOUT });
      const color = await label.evaluate((el) => getComputedStyle(el).color);
      colors.add(color);
    }
    expect(colors.size).toBe(3);
  });
});
