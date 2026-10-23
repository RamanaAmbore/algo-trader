/**
 * order_header_chase_gesture.spec.js
 *
 * 2026-10-23 relocation — the standalone header `.chase-chip` is removed.
 * The chase surface is now reached via long-press (mobile) / right-click
 * (desktop) on the per-page Order button (`.pha-order`,
 * PageHeaderActions.svelte), reusing the `longPress.js` action the same
 * way OrderCard.svelte already combines it with a primary `onclick`. The
 * button's normal click/tap is unchanged — still opens a fresh order
 * ticket (SymbolPanel, `.oes-overlay`).
 *
 * This file covers exactly the plan's checklist:
 *   1. Right-click / long-press opens the chase drawer with the same
 *      ChaseCard content the old chip showed (smoke check — the rich
 *      content assertions live in chase_badge_live_status_drawer.spec.js).
 *   2. A plain click/tap still opens the order ticket, unaffected, and
 *      does NOT also open the chase drawer.
 *   3. Conversely, the gesture does NOT also open the order ticket.
 *   4. The indicator dot + one-time discoverability hint only appear
 *      when a chase is actually active.
 *   5. `.chase-chip` no longer exists anywhere in the DOM, on any algo
 *      page, including /orders (which hides the Order button entirely
 *      but keeps its own always-visible embedded <ChaseCard>).
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/order_header_chase_gesture.spec.js --project=chromium-desktop
 *   cd frontend && npx playwright test \
 *     e2e/order_header_chase_gesture.spec.js --project=mobile-portrait
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { loginAsAdmin } from './fixtures/auth.js';

const _NOW_SEC = Math.floor(Date.now() / 1000);
const _ACTIVE_CHASE = {
  id: 601, account: 'T1', transaction_type: 'BUY', quantity: 50,
  symbol: 'RBQ-HEADERGESTURE', current_limit: 102, initial_price: 100,
  attempts: 2, status: 'OPEN', next_attempt_at: _NOW_SEC + 12,
  mode: 'live', child_order_ids: [],
};

/** @param {import('@playwright/test').Page} page */
async function mockOrdersApis(page, { chases = [] } = {}) {
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    const url = req.url();
    if (req.method() !== 'GET') { await route.continue(); return; }
    if (url.includes('/orders/chases/active')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(chases) });
      return;
    }
    if (url.includes('/orders/events/recent')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
      return;
    }
    if (url.includes('/orders/algo/recent')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
      return;
    }
    if (url.includes('/orders/gtts')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ gtts: [], count: 0 }) });
      return;
    }
    if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: [], refreshed_at: new Date().toISOString() }) });
      return;
    }
    await route.continue();
  });
}

/** Simulates a touch long-press (longPress.js fires at 500ms; hold 650ms
 *  with margin) by dispatching raw touch events — Playwright has no
 *  built-in "hold" gesture for touch. Mirrors the dispatch shape
 *  longPress.js itself reads (`e.touches[0].clientX/clientY`). */
async function touchLongPress(locator) {
  const box = await locator.boundingBox();
  if (!box) throw new Error('element not visible for touchLongPress');
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await locator.dispatchEvent('touchstart', {
    touches: [{ clientX: x, clientY: y, identifier: 0, pageX: x, pageY: y }],
  });
  await locator.page().waitForTimeout(650);
  await locator.dispatchEvent('touchend', { touches: [], changedTouches: [] });
}

test.describe('Order button — chase drawer gesture, indicator, and click isolation', () => {
  test.setTimeout(60_000);

  test('desktop: right-click opens the chase drawer, plain click opens the order ticket — neither triggers the other', async ({ page }) => {
    await loginAsAdmin(page);
    await mockOrdersApis(page, { chases: [_ACTIVE_CHASE] });
    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    const orderBtn = page.locator('.pha-order:visible').first();
    await expect(orderBtn).toBeVisible({ timeout: 15_000 });

    // Gesture: right-click opens the drawer, not the ticket.
    await orderBtn.click({ button: 'right' });
    await expect(page.locator('.chd-drawer.chd-drawer-open')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.oes-overlay')).toHaveCount(0);

    // Close the drawer before testing the plain click.
    await page.locator('.chd-close').click();
    await expect(page.locator('.chd-drawer.chd-drawer-open')).toHaveCount(0);

    // Plain click: opens the ticket, not the drawer.
    await orderBtn.click();
    await expect(page.locator('.oes-overlay')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.chd-drawer.chd-drawer-open')).toHaveCount(0);
  });

  test('mobile: simulated long-press opens the chase drawer; a quick tap still opens the order ticket', async ({ page }, testInfo) => {
    test.skip(!testInfo.project.use?.hasTouch, 'touch-only gesture; run on a mobile-* project');
    await loginAsAdmin(page);
    await mockOrdersApis(page, { chases: [_ACTIVE_CHASE] });
    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    const orderBtn = page.locator('.pha-order:visible').first();
    await expect(orderBtn).toBeVisible({ timeout: 15_000 });

    await touchLongPress(orderBtn);
    await expect(page.locator('.chd-drawer.chd-drawer-open')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.oes-overlay')).toHaveCount(0);

    await page.locator('.chd-close').click();
    await expect(page.locator('.chd-drawer.chd-drawer-open')).toHaveCount(0);

    // A quick tap (no hold) is a normal click — still opens the ticket.
    await orderBtn.tap();
    await expect(page.locator('.oes-overlay')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.chd-drawer.chd-drawer-open')).toHaveCount(0);
  });

  test('indicator dot + one-time "(i)" hint only appear when a chase is actually active', async ({ page }) => {
    await loginAsAdmin(page);
    await mockOrdersApis(page, { chases: [] });
    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    const orderBtn = page.locator('.pha-order:visible').first();
    await expect(orderBtn).toBeVisible({ timeout: 15_000 });
    await expect(orderBtn.locator('.pha-chase-dot')).toHaveCount(0);
    // No "(i)" chip rendered at all while there's nothing to explain.
    await expect(page.locator('.pha-wrap .info-btn')).toHaveCount(0);
  });

  test('indicator dot appears and the discoverability hint auto-opens the first time, for an active chase', async ({ page }) => {
    await loginAsAdmin(page);
    await mockOrdersApis(page, { chases: [_ACTIVE_CHASE] });
    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    const orderBtn = page.locator('.pha-order:visible').first();
    await expect(orderBtn).toBeVisible({ timeout: 15_000 });
    await expect(orderBtn.locator('.pha-chase-dot')).toBeVisible({ timeout: 10_000 });

    // Auto-opened popover — no click needed — carries the gesture
    // explanation text, without the operator interacting with anything.
    const popout = page.locator('.info-popout', { hasText: 'Chase active' });
    await expect(popout).toBeVisible({ timeout: 10_000 });
    await expect(popout).toContainText(/long-press|right-click/i);
  });

  test('.chase-chip no longer exists anywhere in the DOM, on any algo page', async ({ page }) => {
    await loginAsAdmin(page);
    // Empty chases — this assertion doesn't depend on chase state, and
    // a non-empty row currently triggers an unrelated runaway refetch
    // loop in OrderBook.svelte's own chase-merge logic on /orders (see
    // the dedicated /orders test below for the full note); no reason to
    // pay that cost here.
    await mockOrdersApis(page, { chases: [] });
    for (const path of ['/pulse', '/orders', '/admin/derivatives']) {
      await page.goto(path);
      await page.waitForLoadState('domcontentloaded');
      await expect(page.locator('.chase-chip')).toHaveCount(0);
    }
  });

  test('/orders (hideOrder page): no Order button / gesture — the page embeds its own ChaseCard instead', async ({ page }) => {
    // NOTE: this deliberately mocks ZERO active chases, not one. A
    // sibling, already-landed commit (fc77a8f8, "chase-in-flight chip
    // fires for real live chases") changed OrderBook.svelte's own merge
    // logic on this exact page; investigated live and confirmed (via a
    // throwaway Playwright probe, not committed) that feeding a non-empty
    // `/orders/chases/active` row into /orders currently triggers a
    // runaway refetch loop (thousands of requests/sec) in that sibling,
    // unrelated code — reproducible with an UNMODIFIED OrderBook.svelte
    // at HEAD, nothing this plan's changes touch or introduce. Flagged to
    // the parent agent in the handback report rather than fixed here, per
    // this plan's explicit "do not touch OrderBook.svelte" scope. Using
    // an empty chase list here keeps this test meaningful (confirms the
    // Order button/gesture are genuinely absent) without depending on
    // that currently-broken path.
    await loginAsAdmin(page);
    await mockOrdersApis(page, { chases: [] });
    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');

    // No Order button at all on this page (hideOrder=true) — confirms
    // there is genuinely nothing to long-press/right-click here.
    await expect(page.locator('.pha-order')).toHaveCount(0);
  });

  test('source: /orders embeds its own <ChaseCard> directly, independent of PageHeaderActions — why no fallback trigger is needed there', () => {
    const src = readFileSync(
      '/Users/ramanambore/projects/ramboq/frontend/src/routes/(algo)/orders/+page.svelte',
      'utf-8',
    );
    expect(src).toContain("import ChaseCard from '$lib/order/ChaseCard.svelte'");
    expect(src).toMatch(/<ChaseCard[^>]*bind:activeCount/);
    expect(src).toContain('hideOrder={true}');
  });
});
