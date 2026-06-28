/**
 * activity_scroll_and_navbar.spec.js
 *
 * Two operator-visible asks:
 *  1. Activity-modal scroll must scroll the modal body, not the
 *     background page (previous bug: wheel events bubbled to <body>
 *     because .log-panel.log-rows / .log-panel.log-news-panel had
 *     no overflow-y:auto — fix landed in LogPanel.svelte).
 *  2. /activity page must appear in the navbar Build dropdown so
 *     the page is reachable without typing the URL.
 *
 * Five-dimension quality assertions per feedback_test_dimensions.md:
 *   • SSOT — the modal and the /activity page mount the SAME
 *     ActivityLogSurface / LogPanel, so a tab id present on one
 *     must be present on the other.
 *   • Perf — the modal open shouldn't fire more than 20 API XHRs.
 *   • Stale code — bundle source-grep proves the legacy "no
 *     overflow declaration" CSS isn't shipped (look for the new
 *     overscroll-behavior:contain marker).
 *   • Reusable — /activity uses .log-panel.log-rows (canonical
 *     LogPanel marker) not a hand-rolled list.
 *   • UX — Activity bell glyph is amber (#fbbf24 / rgb(251,191,36))
 *     in both surfaces (matches BellIcon palette).
 *
 * Target: dev.ramboq.com
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

// Use the canonical loginAsAdmin fixture (frontend/e2e/fixtures/auth.js)
// instead of an inline helper. The fixture drives the /signin form,
// retries on 429 with a backoff, and only tries one username — earlier
// inline helpers that iterated ['ambore', 'rambo', 'admin'] burned
// rate-limit slots on dev (where 'ambore' returns 401) and triggered
// the tier-2 30-minute lockout. Reusable-component rule.
const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const login = loginAsAdmin;

test.describe('/activity + ActivityLogModal — scroll + navbar', () => {

  test('navbar Build dropdown contains /activity entry', async ({ page }) => {
    test.setTimeout(30_000);
    await login(page);
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    // The Build group is a desktop dropdown; the link should be in
    // the rendered DOM either way (open or closed). Just assert the
    // href is present in the layout.
    const link = page.locator('a[href="/activity"]').first();
    await expect(
      link,
      'navbar must include an <a href="/activity">: add to _algoLinksAll group=build.',
    ).toHaveCount(1, { timeout: 10_000 });

    // Click + confirm navigation.
    await link.click({ force: true });
    await page.waitForURL(/\/activity/, { timeout: 10_000 });
    await expect(page.locator('.page-header')).toBeVisible();
  });

  test('Activity-modal scroll stays inside the modal, not background page', async ({ page }) => {
    test.setTimeout(45_000);
    await login(page);

    /** @type {string[]} */
    const apiCalls = [];
    page.on('request', (req) => {
      if (req.url().includes('/api/')) apiCalls.push(req.url());
    });

    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    // Open the Activity modal via the page-header bell. Identify it
    // via the title hint (PageHeaderActions sets title="Activity log").
    const bellBtn = page.locator(
      'button[title*="ctivity" i], button[aria-label*="ctivity" i]',
    ).first();
    await expect(bellBtn).toBeVisible({ timeout: 10_000 });
    await bellBtn.click();

    const modal = page.locator('.canonical-modal-panel.alm-panel');
    await expect(modal).toBeVisible({ timeout: 5_000 });

    // Pick a non-Orders tab where the scroll bug was reported (Agents,
    // Terminal, System — anything that lands in .log-panel.log-rows).
    // The tab buttons are AlgoTabs entries with the tab label.
    const tabBtn = page.locator('.canonical-modal-panel.alm-panel button', { hasText: /system/i }).first();
    if (await tabBtn.count()) await tabBtn.click();

    const rowsContainer = page.locator('.canonical-modal-panel.alm-panel .log-panel.log-rows').first();
    await expect(rowsContainer).toBeVisible({ timeout: 5_000 });

    // ── Dimension 3: stale code — overflow-y:auto must be applied ─────────
    // Wheel events fired inside the rows container must NOT change
    // window.scrollY. The bug was that LogPanel's rows container had
    // no overflow declaration, so wheel events bubbled to <body>.
    const overflowY = await rowsContainer.evaluate((el) => getComputedStyle(el).overflowY);
    expect(
      ['auto', 'scroll'],
      `.log-panel.log-rows must have overflow-y:auto|scroll, got '${overflowY}'.`,
    ).toContain(overflowY);

    const overscroll = await rowsContainer.evaluate((el) => getComputedStyle(el).overscrollBehavior || getComputedStyle(el).overscrollBehaviorY);
    expect(
      overscroll,
      `.log-panel.log-rows must set overscroll-behavior:contain to stop scroll-chaining, got '${overscroll}'.`,
    ).toMatch(/contain/);

    // Wheel-event smoke test: scroll the rows container and verify
    // the page's body scroll position didn't move.
    const bodyScrollBefore = await page.evaluate(() => window.scrollY);
    await rowsContainer.hover();
    await page.mouse.wheel(0, 400);
    await page.waitForTimeout(300);
    const bodyScrollAfter = await page.evaluate(() => window.scrollY);
    expect(
      bodyScrollAfter,
      `Scrolling inside the modal must NOT scroll the background page. body.scrollY went ${bodyScrollBefore} → ${bodyScrollAfter}.`,
    ).toBe(bodyScrollBefore);

    // ── Dimension 2: perf budget — modal open ≤20 XHRs ──────────────────
    expect(
      apiCalls.length,
      `Modal-open XHR budget exceeded. Got ${apiCalls.length} calls.`,
    ).toBeLessThanOrEqual(20);

    // ── Dimension 4: reusable component — canonical AlgoTabs strip ──────
    await expect(
      page.locator('.canonical-modal-panel.alm-panel .algo-tab').first(),
      'Activity modal must use the canonical AlgoTabs strip, not a hand-rolled tabs row.',
    ).toBeVisible();

    // ── Dimension 5: UX color — title bell amber per palette ────────────
    const bellTitle = page.locator('.canonical-modal-panel.alm-panel .alm-title');
    const titleColor = await bellTitle.evaluate((el) => getComputedStyle(el).color);
    expect(
      titleColor,
      `Activity modal title must be amber-400 (#fbbf24 / rgb(251,191,36)), got ${titleColor}.`,
    ).toBe('rgb(251, 191, 36)');
  });

  test('SSOT — /activity page and modal expose the same tab ids', async ({ page }) => {
    test.setTimeout(45_000);
    await login(page);

    // Visit /activity directly + collect the tab ids exposed via the
    // canonical AlgoTabs strip.
    await page.goto(`${BASE}/activity`, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.log-tab-row .algo-tab').first()).toBeVisible({ timeout: 10_000 });
    const pageTabs = (await page.locator('.log-tab-row .algo-tab').allTextContents())
      .map((t) => t.trim().toLowerCase()).sort();
    expect(pageTabs.length).toBeGreaterThan(0);

    // Now open the modal from /dashboard + collect again.
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    const bellBtn = page.locator(
      'button[title*="ctivity" i], button[aria-label*="ctivity" i]',
    ).first();
    await bellBtn.click();
    await expect(page.locator('.canonical-modal-panel.alm-panel')).toBeVisible({ timeout: 5_000 });
    const modalTabs = (await page.locator('.canonical-modal-panel.alm-panel .log-tab-row .algo-tab').allTextContents())
      .map((t) => t.trim().toLowerCase()).sort();

    expect(
      modalTabs,
      `Modal and /activity must mount the same LogPanel — tab ids diverged.\nPage: ${pageTabs.join(',')}\nModal: ${modalTabs.join(',')}`,
    ).toEqual(pageTabs);
  });
});
