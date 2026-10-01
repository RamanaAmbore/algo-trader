/**
 * mobile_hamburger_over_order_modal.spec.js
 *
 * Regression test for: "the order modal gets displayed after pressing on
 * a symbol, the hamburger when pressed hides the menu underneath it."
 *
 * Root cause: `.algo-mobile-dropdown` (the hamburger's mobile nav drawer,
 * `frontend/src/routes/(algo)/+layout.svelte`) was `position: absolute`
 * inside `<header class="algo-navbar">` — a `position: fixed; z-index:
 * var(--z-nav)=50` element that establishes its OWN stacking context.
 * Any absolutely-positioned child is trapped inside that local context,
 * so the drawer's nominal `z-index: var(--z-dropdown)` (20000) never
 * actually competed against the order modal's portalled
 * `.canonical-modal-overlay` (`z-index: var(--z-command)=10500`, appended
 * directly to `document.body`) — the ENTIRE navbar box (including the
 * drawer) only ever contributed z-index 50 to the root stacking context,
 * so the modal always painted on top, hiding the drawer underneath it.
 *
 * Fix: `.algo-mobile-dropdown` is now `position: fixed` and portalled to
 * `document.body` via `use:portal` (the same established pattern already
 * used by `.mode-combo-dropdown` for an identical bug), with its `top`
 * offset computed from the navbar's own `getBoundingClientRect()` in
 * `toggleMobileMenu()`.
 *
 * This spec opens the order modal via the page-header `+Order` button
 * (`.pha-order`, same trigger `order_modal_default_symbol_and_layout.spec.js`
 * uses on mobile) rather than an ag-Grid symbol-row click. Both entry
 * points mount the IDENTICAL `<SymbolPanel>` component instance, which
 * renders the same portalled `.canonical-modal-overlay` /
 * `.canonical-modal-panel` pair (SymbolPanel.svelte ~line 2136) — a
 * clicked watchlist/position row on /pulse (MarketPulse.svelte's
 * `handleRowClick` → `_openTicketFromRow` → `openTicket()`, line ~4028)
 * sets the exact same `ticketProps` and mounts the exact same
 * `<SymbolPanel>` markup (MarketPulse.svelte ~line 4529) as the
 * page-header button does. `.pha-order` was used here because it
 * doesn't depend on live broker/watchlist data being present, which a
 * genuine grid row click does (confirmed live: in this environment a
 * real row click sometimes doesn't resolve while the instruments cache
 * / broker connectivity probe is in flight) — the stacking-context fix
 * under test is identical either way since it's the SAME modal markup.
 *
 * Then opens the hamburger drawer on top of the open modal, and asserts
 * the drawer is actually interactable (not intercepted by the modal) by
 * clicking a nav item and confirming real navigation happens.
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/mobile_hamburger_over_order_modal.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const API_HOST = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;

let _cachedToken = null;
async function login(page) {
  if (!_cachedToken && process.env.PLAYWRIGHT_TOKEN) _cachedToken = process.env.PLAYWRIGHT_TOKEN;
  if (!_cachedToken) {
    for (const u of ['rambo', 'ambore', 'admin']) {
      const r = await page.request.post(`${API_HOST}/api/auth/login`, {
        data: { username: u, password: _AUTH_PASS },
        timeout: 15_000,
      }).catch(() => null);
      if (r && r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
    if (!_cachedToken) throw new Error('login failed');
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _cachedToken);
}

test.describe('mobile hamburger drawer over order modal (390x844)', () => {
  test.use({ viewport: { width: 390, height: 844 } });
  test.setTimeout(60_000);

  test(`hamburger drawer renders ON TOP of an open order modal [${BASE}]`, async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2000);

    const orderBtn = page.locator('button.pha-order').first();
    await expect(orderBtn, '.pha-order button must be present on mobile /dashboard').toBeVisible({ timeout: 10_000 });
    await orderBtn.click({ force: true });
    await page.waitForTimeout(1200);

    const overlay = page.locator('.canonical-modal-overlay').first();
    await expect(overlay, 'order modal overlay should be open').toBeVisible({ timeout: 10_000 });

    // Open the hamburger drawer WHILE the order modal is open.
    const hamburger = page.locator('.algo-hamburger').first();
    await expect(hamburger).toBeVisible();
    await hamburger.click();

    const drawer = page.locator('.algo-mobile-dropdown').first();
    await expect(drawer, 'mobile drawer should open').toBeVisible({ timeout: 5_000 });

    // Guards the root cause directly: the drawer must actually be
    // portalled to document.body (use:portal), not left as a
    // position:absolute descendant of .algo-navbar's own stacking
    // context — that was the structural cause of the bug.
    const isPortalled = await drawer.evaluate((el) => el.parentElement === document.body);
    expect(isPortalled, 'drawer must be portalled directly to document.body').toBe(true);

    // Pick a stable nav item that navigates away from /dashboard.
    const chartsItem = page.locator('.algo-mobile-item:text-is("Charts")').first();
    await expect(chartsItem, 'drawer should contain a Charts item').toBeVisible();

    // The real assertion: elementFromPoint at the item's center must
    // resolve to the drawer item itself (or a descendant of it), not
    // the modal overlay/panel sitting on top of it. This is the exact
    // condition that makes the item genuinely clickable.
    const box = await chartsItem.boundingBox();
    expect(box, 'Charts item must have a bounding box').toBeTruthy();
    const cx = box.x + box.width / 2;
    const cy = box.y + box.height / 2;
    const hitOk = await page.evaluate(({ x, y }) => {
      const el = document.elementFromPoint(x, y);
      if (!el) return false;
      const drawerEl = el.closest('.algo-mobile-dropdown');
      const modalEl = el.closest('.canonical-modal-overlay, .canonical-modal-panel');
      // Must land inside the drawer, and NOT inside the modal overlay/panel.
      return !!drawerEl && !modalEl;
    }, { x: cx, y: cy });
    expect(hitOk, 'Charts item must not be occluded by the order modal').toBe(true);

    // Clicking it should actually navigate — proof it wasn't intercepted.
    await chartsItem.click();
    await page.waitForURL(/\/charts/, { timeout: 10_000 });
    expect(page.url()).toContain('/charts');

    // Drawer should have closed after navigation.
    await expect(page.locator('.algo-mobile-dropdown')).toHaveCount(0);
  });
});
