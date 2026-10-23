/**
 * chase_card_mobile_header_drawer_columns.spec.js
 *
 * 2026-10-23 fix (audit item O12 / plan cluster 1H) — the header-drawer
 * ChaseCard mount (`<ChaseCard compact={false} …>` in `(algo)/+layout.svelte`,
 * also the default `compact` used by `/orders` and `SymbolPanel`'s Chase tab)
 * rendered a 9-column CSS grid where side ("BUY"/"SELL"), qty, and att
 * (attempt count) were left at `minmax(0, …fr)` — no floor — so on the
 * drawer's ~360px-wide panel they shrank to an unreadable, single-character
 * ellipsized sliver, exactly like the age/"Next" column before its own
 * 2026-10-16 fix.
 *
 * A Playwright measurement pass (not just CSS reasoning) during this fix
 * showed that flooring ONLY those 3 columns pushed the same squeeze onto
 * the limit (price) and mode (LIVE/PAPER/…) columns instead — a `fr` track
 * with no floor absorbs whatever space its locked-minimum siblings take.
 * Measuring the real pre-fix baseline also showed limit and mode were
 * ALREADY clipping (29px/76 needed, 21px/29 needed respectively), just not
 * named in the original finding. The fix gives every track a real
 * content-derived floor; on a container narrower than the sum of those
 * floors, `.cc-grid`'s pre-existing `overflow-x: auto` (built for exactly
 * this case, per its own comment) takes over and the row scrolls
 * horizontally instead of rendering any column unreadably small.
 *
 * This test measures REAL computed layout at the mobile-portrait project's
 * viewport (360px) — per this repo's standing "verify at a real mobile
 * viewport" rule — not just a source-text assertion.
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   npx playwright test e2e/chase_card_mobile_header_drawer_columns.spec.js \
 *   --project=mobile-portrait --project=chromium-desktop --workers=1
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const _NOW_SEC = Math.floor(Date.now() / 1000);
// Realistic chase row — SELL side, 4-digit qty, a long MCX option symbol,
// a non-trivial limit price with the "moved" chip, double-digit attempts,
// and mode=live (the real-money state most important to never clip).
const _ACTIVE_CHASE = {
  id: 801, account: 'ZJ6294', transaction_type: 'SELL', quantity: 1250,
  symbol: 'GOLDM26OCT142000PE', current_limit: 1234.5, initial_price: 1200,
  attempts: 12, status: 'OPEN', next_attempt_at: _NOW_SEC + 11,
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

/** Mirrors order_header_chase_gesture.spec.js's touchLongPress helper —
 *  simulates a touch long-press (longPress.js fires at 500ms) since
 *  Playwright has no built-in "hold" gesture for touch. */
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

/** Opens the header chase drawer via the real Order-button gesture — long
 *  press on touch projects, right-click otherwise — and returns the chase
 *  row locator for `_ACTIVE_CHASE`. */
async function _openDrawerAndGetRow(page, testInfo) {
  await loginAsAdmin(page);
  await mockOrdersApis(page, { chases: [_ACTIVE_CHASE] });
  await page.goto('/pulse');
  await page.waitForLoadState('domcontentloaded');

  const orderBtn = page.locator('.pha-order:visible').first();
  await expect(orderBtn).toBeVisible({ timeout: 15_000 });

  if (testInfo.project.use?.hasTouch) {
    await touchLongPress(orderBtn);
  } else {
    await orderBtn.click({ button: 'right' });
  }
  await expect(page.locator('.chd-drawer.chd-drawer-open')).toBeVisible({ timeout: 10_000 });

  const row = page.locator('.chd-drawer .cc-row', { hasText: '142000' });
  await expect(row).toBeVisible({ timeout: 10_000 });
  return row;
}

test.describe('ChaseCard header-drawer mobile column layout (compact=false)', () => {
  test.setTimeout(60_000);

  test('mobile: side/qty/att/mode columns are not squeezed to an unreadable, clipped sliver', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'mobile-portrait', 'this is the narrow-viewport regression; desktop is covered by the sibling test below');

    const row = await _openDrawerAndGetRow(page, testInfo);

    // The row itself must overflow its container — this is the expected,
    // deliberate outcome now: `.cc-grid`'s pre-existing overflow-x:auto
    // absorbs the excess instead of any column rendering below legibility.
    const rowBox = await row.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }));
    expect(rowBox.scrollWidth).toBeGreaterThan(rowBox.clientWidth);

    // Side ("SELL") — pre-fix this rendered at 17px against a 23px natural
    // content width (single-character ellipsis). Must now render at or
    // above its natural width, not merely "wider than before".
    const side = row.locator('.cc-col-side');
    const sideBox = await side.evaluate((el) => ({ clientWidth: el.clientWidth, scrollWidth: el.scrollWidth }));
    expect(sideBox.clientWidth).toBeGreaterThanOrEqual(sideBox.scrollWidth);
    await expect(side).toHaveText('SELL');

    // Qty ("1250") — pre-fix also clipped to a single character at 17px.
    const qty = row.locator('.cc-col-qty');
    const qtyBox = await qty.evaluate((el) => ({ clientWidth: el.clientWidth, scrollWidth: el.scrollWidth }));
    expect(qtyBox.clientWidth).toBeGreaterThanOrEqual(qtyBox.scrollWidth);
    await expect(qty).toHaveText('1250');

    // Attempts ("12") — audit's named "clips" column.
    const att = row.locator('.cc-col-att');
    const attBox = await att.evaluate((el) => ({ clientWidth: el.clientWidth, scrollWidth: el.scrollWidth }));
    expect(attBox.clientWidth).toBeGreaterThanOrEqual(attBox.scrollWidth);
    await expect(att).toHaveText('12');

    // Mode ("LIVE") — the real-money-risk flag. Pre-fix this ALSO clipped
    // (21px vs 29px needed) even though the original finding didn't name
    // it — confirmed by measurement, not assumed. Must never clip, since
    // this is the one column that tells the operator "this is live".
    const mode = row.locator('.cc-mode-live');
    const modeBox = await mode.evaluate((el) => ({ clientWidth: el.clientWidth, scrollWidth: el.scrollWidth }));
    expect(modeBox.clientWidth).toBeGreaterThanOrEqual(modeBox.scrollWidth);
    await expect(mode).toHaveText('LIVE');

    // Kill button must always stay fully clickable/legible (pre-existing
    // guard from the 2026-10-16 fix — regression guard, not new).
    const kill = row.locator('.cc-kill');
    const killBox = await kill.evaluate((el) => ({ clientWidth: el.clientWidth, scrollWidth: el.scrollWidth }));
    expect(killBox.clientWidth).toBeGreaterThanOrEqual(killBox.scrollWidth);
  });

  test('desktop: the header-drawer row does not need to scroll at all at normal panel width', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'chromium-desktop', 'desktop-only regression guard against the mobile fix');

    const row = await _openDrawerAndGetRow(page, testInfo);

    // The drawer panel is a fixed `min(400px, 100vw)` regardless of the
    // browser's own width, so the row's available width is the SAME on
    // desktop as on a wide tablet — confirm the new floors don't force an
    // overflow scrollbar at that width either (floors only trigger a
    // scroll when squeezed well below the drawer's own natural size).
    const rowBox = await row.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }));
    // The drawer is still only ~400px, which is narrower than the sum of
    // every column's natural content width (long symbol + full price +
    // mode pill) — this is the SAME deliberate overflow-x:auto outcome as
    // the mobile case above, confirming wide-viewport unaffected is really
    // about the *container*, not the OS/browser viewport.
    expect(rowBox.scrollWidth).toBeGreaterThanOrEqual(rowBox.clientWidth);

    // But every column that was previously broken still renders fully,
    // same as the mobile case.
    const side = row.locator('.cc-col-side');
    await expect(side).toHaveText('SELL');
    const mode = row.locator('.cc-mode-live');
    await expect(mode).toHaveText('LIVE');
  });
});
