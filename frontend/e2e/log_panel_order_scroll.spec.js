/**
 * log_panel_order_scroll.spec.js
 *
 * LogPanel.svelte's Order tab was not scrollable (operator report,
 * 2026-09-30).
 *
 * Root cause: the Order tab renders its rows inside a NESTED
 * `.log-panel.log-rows` div (no `{heightClass}` passed to it, unlike
 * every other tab) sitting inside the real, bounded scroll container
 * `.lp-order-scroll`. The shared global rule `.log-panel.log-rows {
 * overflow-y: auto; overscroll-behavior: contain; }` still applied to
 * that inner div even though it never actually overflows itself (it's a
 * plain block sized to fit its own content — scrollHeight always equals
 * clientHeight). Chromium's touch-scroll hit-testing picked that inner,
 * non-overflowing element as the scroll target (nearest CSS
 * overflow:auto ancestor to the touch point), and its own
 * `overscroll-behavior: contain` then blocked the unused scroll delta
 * from chaining up to `.lp-order-scroll` — the one level out that
 * genuinely has more content than height. Net effect: a real touch
 * swipe over the Order tab did nothing at all (list didn't scroll, page
 * didn't scroll either) — even though naive checks (scrollHeight >
 * clientHeight, overflow-y: auto present) looked completely correct.
 * Programmatic `el.scrollTop = x` writes also looked fine, which is why
 * this needed a genuine synthesized touch gesture (CDP
 * Input.synthesizeScrollGesture, same technique
 * position_strip_heartbeat_and_mobile_overflow.spec.js uses for its
 * "container scrolls, not the page" assertion) to actually reproduce.
 *
 * Fix (`frontend/src/lib/LogPanel.svelte`): `.lp-order-scroll
 * .log-panel.log-rows` now overrides the inherited overflow/overscroll
 * rules (`overflow-y: visible; overscroll-behavior: auto;`) so
 * `.lp-order-scroll` is the only scroll container Chromium considers
 * for that tab — matching how every other LogPanel tab is a single,
 * directly-bounded-and-scrolling element.
 *
 * Verified across all three real mount points (ActivityLogModal via the
 * `h` shortcut, the dashboard card, and the standalone /activity page)
 * on a 390×780 mobile viewport — the size class CLAUDE.md calls out for
 * mobile-first cell layout.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const MOBILE_VIEWPORT = { width: 390, height: 780 };

function bigOrders(n) {
  const rows = [];
  for (let i = 0; i < n; i++) {
    rows.push({
      order_id: `ORD${1000 + i}`,
      tradingsymbol: 'NIFTY26JUN25000CE',
      exchange: 'NFO',
      transaction_type: i % 2 === 0 ? 'BUY' : 'SELL',
      quantity: 50,
      status: 'COMPLETE',
      order_timestamp: new Date(Date.now() - i * 60_000).toISOString(),
      average_price: 210.25,
      // LogPanel gates order rows on executionMode when gateByMode is
      // active. Boot mode on localhost defaults to 'idle' until the
      // layout's first /admin/execution/mode poll "upgrades" it (see
      // mockExecutionMode below, which mocks that poll to 'paper') — the
      // fixture rows must carry the SAME mode the app will actually be
      // running in by the time the poll lands, or the (unrelated) gating
      // filter zeroes the list out before the scroll bug can even be
      // exercised.
      mode: 'paper',
    });
  }
  return rows;
}

/** Mocks the /admin/execution/mode poll the (algo) layout fires on
 *  mount — without this, boot mode stays 'idle' (the localhost
 *  default) regardless of what the order-row fixtures claim. */
async function mockExecutionMode(page) {
  await page.route('**/api/admin/execution/mode', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ mode: 'paper', allowed_modes: ['paper', 'live', 'shadow', 'sim', 'replay'], branch: 'dev' }),
    })
  );
}

async function mockOrdersFeed(page) {
  await mockExecutionMode(page);
  await page.route('**/api/orders/', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: bigOrders(120) }) })
  );
  await page.route('**/api/orders/algo/recent**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) })
  );
  await page.route('**/api/orders/events/recent**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) })
  );
}

/** Real touch swipe over `.lp-order-scroll`'s centre; returns before/after
 *  scrollTop of the list AND window.scrollY, so a test can assert BOTH
 *  that the list moved and that the page never did. */
async function touchSwipeOrderList(page, { yDistance = -300 } = {}) {
  const box = await page.locator('.lp-order-scroll').boundingBox();
  const cdp = await page.context().newCDPSession(page);
  const before = await page.evaluate(() => ({
    listTop: document.querySelector('.lp-order-scroll')?.scrollTop ?? null,
    pageY: window.scrollY,
  }));
  await cdp.send('Input.synthesizeScrollGesture', {
    x: box.x + box.width / 2,
    y: box.y + box.height / 2,
    xDistance: 0,
    yDistance,
    gestureSourceType: 'touch',
    speed: 800,
  });
  await page.waitForTimeout(600);
  const after = await page.evaluate(() => ({
    listTop: document.querySelector('.lp-order-scroll')?.scrollTop ?? null,
    pageY: window.scrollY,
  }));
  return { before, after };
}

test.describe('LogPanel Order tab — scrolls on real touch input, not the page', () => {
  test.use({ viewport: MOBILE_VIEWPORT, isMobile: true, hasTouch: true });

  test('.lp-order-scroll is a bounded, auto-overflow container with real overflow content', async ({ page }) => {
    await loginAsAdmin(page);
    await mockOrdersFeed(page);
    await page.goto('/activity?tab=order', { waitUntil: 'domcontentloaded' });
    const list = page.locator('.lp-order-scroll');
    await expect(list).toBeVisible();

    const box = await page.evaluate(() => {
      const el = document.querySelector('.lp-order-scroll');
      const cs = getComputedStyle(el);
      return {
        overflowY: cs.overflowY,
        clientHeight: el.clientHeight,
        scrollHeight: el.scrollHeight,
      };
    });
    expect(box.overflowY).toBe('auto');
    // Precondition: content genuinely exceeds the box — otherwise the
    // rest of this suite would be vacuous.
    expect(box.scrollHeight).toBeGreaterThan(box.clientHeight);
  });

  test('/activity page: touch swipe scrolls the list, not the document', async ({ page }) => {
    await loginAsAdmin(page);
    await mockOrdersFeed(page);
    await page.goto('/activity?tab=order', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.lp-order-scroll')).toBeVisible();

    const { before, after } = await touchSwipeOrderList(page);
    expect(before.listTop).toBe(0);
    expect(after.listTop, 'order list did not scroll on a real touch swipe').toBeGreaterThan(0);
    expect(after.pageY, 'the page moved instead of (or in addition to) the internal list')
      .toBe(before.pageY);
  });

  test('dashboard card: touch swipe scrolls the list, not the document', async ({ page }) => {
    await loginAsAdmin(page);
    await mockOrdersFeed(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.locator('.dash-activity').getByText('Orders', { exact: true }).click();
    await expect(page.locator('.lp-order-scroll')).toBeVisible();

    const { before, after } = await touchSwipeOrderList(page, { yDistance: -150 });
    expect(before.listTop).toBe(0);
    expect(after.listTop, 'order list did not scroll on a real touch swipe').toBeGreaterThan(0);
    expect(after.pageY).toBe(before.pageY);
  });

  test('ActivityLogModal (navbar `h` shortcut): touch swipe scrolls the list, not the document', async ({ page }) => {
    await loginAsAdmin(page);
    await mockOrdersFeed(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.locator('body').click();
    await page.keyboard.press('h');
    await expect(page.locator('.canonical-modal-panel.alm-panel')).toBeVisible();
    await expect(page.locator('.lp-order-scroll')).toBeVisible();

    const { before, after } = await touchSwipeOrderList(page);
    expect(before.listTop).toBe(0);
    expect(after.listTop, 'order list did not scroll on a real touch swipe').toBeGreaterThan(0);
    expect(after.pageY).toBe(before.pageY);
  });
});
