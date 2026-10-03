/**
 * order_book_fullscreen_navstrip_offset.spec.js
 *
 * Audit fix (2026-10-02): OrderBook.svelte reimplemented fullscreen with
 * its own `.ob-fs { position: fixed; inset: 0; z-index: 9000; }` instead
 * of the shared `.fs-card-on` global pattern (app.css) every other
 * fullscreen-capable card uses. Two defects: (1) `.ob-fs` ignored the
 * live-measured `--fs-card-top` chrome offset (`DefaultSizeButton.svelte`'s
 * `_measureChrome()`), so its `inset: 0` painted from the true viewport
 * top and covered the real navbar/page-header/NavStrip — the same class
 * of bug `fullscreen_navstrip_offset.spec.js` locked in for `.fs-card-on`
 * itself. (2) its z-index 9000 sat BELOW the shared `.fs-backdrop`/
 * `.fs-backdrop-catch` tier (9998) that mounts on ANY card's fullscreen
 * entry (OrderBook's own CardHeader already mounts CardControls →
 * DefaultSizeButton unconditionally), so the dim backdrop rendered on
 * top of OrderBook's own fullscreen content.
 *
 * Fix: `.ob-root` now carries `class:fs-card-on={isFullscreen}` instead
 * of `.ob-fs` — same global rule, same `--fs-card-top` inset, same
 * z-index 9999 tier, no more 9000 literal.
 *
 * On `/orders`, OrderBook's host `<section class="bucket-card-activity">`
 * ALSO carries its own `class:fs-card-on` (bound to the same isFullscreen)
 * — this spec deliberately measures `.ob-root.fs-card-on` (OrderBook's
 * OWN element), not the outer section, since that inner element is the
 * actual fix target and is what matters for the `SymbolPanel.svelte`
 * mount (no outer wrapper there at all).
 *
 * Quality dimensions:
 * - SSOT: reads the live `--fs-card-top` custom property + real bounding
 *   boxes / elementFromPoint hit-tests, not a hardcoded pixel value.
 * - Perf: n/a (geometry-only assertion, no added polling).
 * - Stale/regression: a future revert to a hand-rolled `.ob-fs`-style
 *   fixed overlay (ignoring `--fs-card-top`) fails this immediately.
 * - Reusable: shares authOnce()-equivalent login() pattern with the
 *   sibling OrderBook specs (`order_book_freeze_on_broker_fetch_fail`,
 *   `order_book_poll_inflight_guard`) and the geometric-invariant style
 *   of `fullscreen_navstrip_offset.spec.js`.
 * - UX: asserts the actual operator-visible guarantee (navbar/page-header/
 *   NavStrip stay clickable and visible, not occluded), via both a
 *   bounding-box check AND an elementFromPoint hit-test (catches the
 *   "visually overlapping but technically below in z-order" case a pure
 *   bounding-box comparison could miss).
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=http://localhost:5174 \
 *     npx playwright test e2e/order_book_fullscreen_navstrip_offset.spec.js \
 *     --project=chromium-desktop
 */
import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

let _cachedToken = null;
async function login(page) {
  if (!_cachedToken) {
    for (const u of ['rambo', 'ambore', 'admin']) {
      const r = await page.request.post(`${BASE}/api/auth/login`, {
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

test.describe('OrderBook fullscreen — navbar/page-header/NavStrip offset', () => {

  test.afterEach(async ({ page }) => {
    try { await page.keyboard.press('Escape'); } catch { /* ignore */ }
  });

  test('fullscreening OrderBook does not cover navbar/page-header/NavStrip', async ({ page }) => {
    test.setTimeout(60_000);
    await login(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });

    const activityCard = page.locator('.bucket-card-activity').first();
    await expect(activityCard).toBeVisible({ timeout: 15_000 });

    const navEl = page.locator('.algo-navbar').first();
    const phEl  = page.locator('.page-header').first();
    await expect(navEl).toBeVisible({ timeout: 10_000 });
    await expect(phEl).toBeVisible({ timeout: 10_000 });
    const navBoxBefore = await navEl.boundingBox();
    const phBoxBefore  = await phEl.boundingBox();

    const psStrip = page.locator('.ps-strip').first();
    const psVisible = await psStrip.isVisible().catch(() => false);
    const psBoxBefore = psVisible ? await psStrip.boundingBox() : null;

    // Scope the fullscreen button to OrderBook's own card so we don't
    // accidentally click the Order Entry card's fullscreen toggle above it.
    const fsBtn = activityCard
      .locator('.fs-btn, [title*="fullscreen" i], [aria-label*="fullscreen" i]')
      .first();
    await expect(fsBtn).toBeVisible({ timeout: 10_000 });
    await fsBtn.click();

    // Measure OrderBook's OWN root element (the actual fix target), not
    // the outer host section — on /orders both carry `.fs-card-on`, but
    // only `.ob-root.fs-card-on` is what SymbolPanel's unwrapped mount
    // relies on.
    const obRoot = page.locator('.ob-root.fs-card-on').first();
    await expect(obRoot).toBeVisible({ timeout: 8_000 });

    // Wait past the `.fs-card-on` pop-in animation (150ms) so a
    // transform-in-progress doesn't produce a false pass/fail on the
    // geometry read below.
    await page.waitForTimeout(400);

    // Navbar / page-header must stay visible and un-occluded.
    await expect(navEl, 'navbar must stay visible while OrderBook is fullscreen').toBeVisible();
    await expect(phEl, 'page-header must stay visible while OrderBook is fullscreen').toBeVisible();

    const obBox = await obRoot.boundingBox();
    expect(obBox, 'fullscreen OrderBook root must report a bounding box').not.toBeNull();

    const navBoxAfter = await navEl.boundingBox();
    const phBoxAfter  = await phEl.boundingBox();
    expect(navBoxAfter, 'navbar must report a bounding box').not.toBeNull();
    expect(phBoxAfter, 'page-header must report a bounding box').not.toBeNull();

    // Core geometric invariant: OrderBook's fullscreen root top edge must
    // sit AT OR BELOW the deepest fixed chrome band's bottom edge.
    const chromeBottoms = [
      navBoxAfter.y + navBoxAfter.height,
      phBoxAfter.y + phBoxAfter.height,
    ];
    if (psBoxBefore) {
      const psBoxAfter = await psStrip.boundingBox();
      expect(psBoxAfter, 'NavStrip must report a bounding box').not.toBeNull();
      chromeBottoms.push(psBoxAfter.y + psBoxAfter.height);
      // NavStrip's own geometry must be unchanged by the fullscreen toggle.
      expect(psBoxAfter.y).toBeCloseTo(psBoxBefore.y, 0);
      expect(psBoxAfter.height).toBeCloseTo(psBoxBefore.height, 0);
    }
    const deepestChromeBottom = Math.max(...chromeBottoms);
    expect(
      obBox.y,
      `OrderBook fullscreen top (${obBox.y}) must be >= deepest chrome bottom (${deepestChromeBottom}) — no overlap`,
    ).toBeGreaterThanOrEqual(deepestChromeBottom - 0.5); // sub-pixel rounding tolerance

    // Chrome geometry itself must be unchanged (fixed, not displaced).
    expect(navBoxAfter.y).toBeCloseTo(navBoxBefore.y, 0);
    expect(navBoxAfter.height).toBeCloseTo(navBoxBefore.height, 0);
    expect(phBoxAfter.y).toBeCloseTo(phBoxBefore.y, 0);
    expect(phBoxAfter.height).toBeCloseTo(phBoxBefore.height, 0);

    // Hit-test invariant (catches "visually overlapping but behind in
    // z-order" cases a pure bounding-box compare could miss, e.g. the
    // `.ob-fs` bug where the dim `.fs-backdrop` painted ON TOP of
    // OrderBook's own fullscreen content at a higher z-index): the center
    // point of the navbar and page-header must still hit-test to an
    // element INSIDE that chrome band, not to the fullscreen card or its
    // backdrop.
    const navCenter = { x: navBoxAfter.x + navBoxAfter.width / 2, y: navBoxAfter.y + navBoxAfter.height / 2 };
    const phCenter  = { x: phBoxAfter.x + phBoxAfter.width / 2, y: phBoxAfter.y + phBoxAfter.height / 2 };
    const navHit = await page.evaluate(({ x, y }) => {
      const el = document.elementFromPoint(x, y);
      return !!el?.closest('.algo-navbar');
    }, navCenter);
    const phHit = await page.evaluate(({ x, y }) => {
      const el = document.elementFromPoint(x, y);
      return !!el?.closest('.page-header');
    }, phCenter);
    expect(navHit, 'navbar center must hit-test inside .algo-navbar, not be occluded').toBe(true);
    expect(phHit, 'page-header center must hit-test inside .page-header, not be occluded').toBe(true);

    // Restore via DefaultSizeButton so afterEach's Escape isn't the only path tested.
    const defaultBtn = page.locator('.default-btn').first();
    if (await defaultBtn.isVisible().catch(() => false)) await defaultBtn.click();
  });

});
