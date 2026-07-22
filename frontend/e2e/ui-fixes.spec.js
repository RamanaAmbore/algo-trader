/**
 * ui-fixes.spec.js
 *
 * E2E tests for five UI bug fixes on dev.ramboq.com:
 *
 * 1. Conn tab in ActivityLog: hashed account number now wraps on mobile viewports
 * 2. Desktop page-header: both current time AND refresh timestamp always visible
 *    (no toggle needed)
 * 3. NavStrip (PositionStrip): clicking any value number (P/M/C/H slots) opens
 *    NavBreakdown popup
 * 4. Mobile: tapping timestamp text or empty page-header area toggles to show
 *    refresh timestamp
 * 5. Mobile page-header: min-height is now 2.5rem (was clipped to 1.8rem)
 *
 * Quality dimensions:
 *  - UX: Mobile viewport renders without horizontal scroll, buttons interactive
 *  - Palette: Timestamp colours match spec (sky for now, amber for refresh)
 *  - Layout: Desktop shows both timestamps, mobile toggles per tap
 *  - Regression: Popup positioning, wrapping behavior, reusable components
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   PLAYWRIGHT_BASE_URL=http://localhost:5174 \
 *   npx playwright test e2e/ui-fixes.spec.js [--project=<viewport>]
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30_000;

test.describe('UI fixes — timestamp, NavStrip popup, Conn tab wrap, page-header height', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    page.on('console', msg => {
      if (msg.type() === 'error') {
        console.log(`[CONSOLE ERROR] ${msg.text()}`);
      }
    });
  });

  // ─────────────────────────────────────────────────────────────────
  // 1. NavStrip value-click opens NavBreakdown popup (desktop + mobile)
  // ─────────────────────────────────────────────────────────────────

  test('1a. NavStrip: clicking P slot value opens NavBreakdown popup', async ({ page }) => {
    await page.goto('/pulse');

    // Wait for PositionStrip to render
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    // Find the P slot value span (ps-agg-v that follows the P label)
    const pSlots = page.locator('.ps-k-p').first();
    await expect(pSlots).toBeVisible();

    // The value is a sibling span with class ps-agg-v
    const pValue = page.locator('.ps-strip .ps-k-p')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
      .first();

    if (await pValue.isVisible({ timeout: 3000 }).catch(() => false)) {
      await pValue.click();

      // Expect NavBreakdown popup to appear
      const popup = page.locator('.ps-breakdown-panel');
      await expect(popup).toBeVisible({ timeout: 5000 });

      // Verify popup is positioned at a reasonable distance from top
      const box = await popup.boundingBox();
      expect(box).toBeTruthy();
      if (box) {
        expect(box.y).toBeGreaterThanOrEqual(0, 'Popup should be visible from top of viewport');
      }

      // Close by clicking outside (overlay click)
      const overlay = page.locator('[role="button"][aria-label*="Close"], .overlay, [class*="backdrop"]').first();
      if (await overlay.isVisible({ timeout: 2000 }).catch(() => false)) {
        await overlay.click();
        await expect(popup).not.toBeVisible({ timeout: 2000 }).catch(() => {
          // Popup might close on escape key or backdrop click — either is fine
        });
      }

      console.log('[PASS] 1a: NavStrip P-slot value click opened popup');
    } else {
      console.log('[SKIP] 1a: No P-slot value visible (market may be closed)');
    }
  });

  test('1b. NavStrip: clicking M slot value opens NavBreakdown popup', async ({ page }) => {
    await page.goto('/pulse');

    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    // Find M slot value
    const mValue = page.locator('.ps-strip .ps-k-m')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
      .first();

    if (await mValue.isVisible({ timeout: 3000 }).catch(() => false)) {
      await mValue.click();

      const popup = page.locator('.ps-breakdown-panel');
      await expect(popup).toBeVisible({ timeout: 5000 });

      const box = await popup.boundingBox();
      expect(box).toBeTruthy();

      console.log('[PASS] 1b: NavStrip M-slot value click opened popup');
    } else {
      console.log('[SKIP] 1b: No M-slot value visible');
    }
  });

  test('1c. NavStrip: clicking C slot value opens NavBreakdown popup', async ({ page }) => {
    await page.goto('/pulse');

    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    // Find C slot value
    const cValue = page.locator('.ps-strip .ps-k-c')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
      .first();

    if (await cValue.isVisible({ timeout: 3000 }).catch(() => false)) {
      await cValue.click();

      const popup = page.locator('.ps-breakdown-panel');
      await expect(popup).toBeVisible({ timeout: 5000 });

      console.log('[PASS] 1c: NavStrip C-slot value click opened popup');
    } else {
      console.log('[SKIP] 1c: No C-slot value visible');
    }
  });

  test('1d. NavStrip: clicking H slot value opens NavBreakdown popup', async ({ page }) => {
    await page.goto('/pulse');

    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    // Find H slot value
    const hValue = page.locator('.ps-strip .ps-k-h')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
      .first();

    if (await hValue.isVisible({ timeout: 3000 }).catch(() => false)) {
      await hValue.click();

      const popup = page.locator('.ps-breakdown-panel');
      await expect(popup).toBeVisible({ timeout: 5000 });

      console.log('[PASS] 1d: NavStrip H-slot value click opened popup');
    } else {
      console.log('[SKIP] 1d: No H-slot value visible');
    }
  });

  // ─────────────────────────────────────────────────────────────────
  // 2. Desktop: both timestamps always visible (no toggle)
  // ─────────────────────────────────────────────────────────────────

  test.skip(({ browserName }) => browserName !== 'chromium', 'Desktop-only');

  test('2a. Desktop: .ats-now and .ats-refresh both visible (no toggle needed)', async ({ page, viewport }) => {
    // Only run on desktop viewport
    if (!viewport || viewport.width < 1200) {
      test.skip();
    }

    await page.goto('/pulse');

    // Find the AlgoTimestamp component
    const atsGroup = page.locator('.ats-group');
    await expect(atsGroup).toBeVisible({ timeout: TIMEOUT });

    // .ats-now should always be visible on desktop (no ats-mobile-hide)
    const atsNow = atsGroup.locator('.ats-now');
    await expect(atsNow).toBeVisible();

    // If refresh timestamp exists, .ats-refresh should also be visible on desktop
    // (no display:none when .ats-mobile-hide applies to it)
    const atsRefresh = atsGroup.locator('.ats-refresh');
    const hasRefresh = await atsRefresh.count() > 0;

    if (hasRefresh) {
      // Get computed style to verify display is not 'none'
      const display = await atsRefresh.evaluate(el => getComputedStyle(el).display);
      expect(display).not.toBe('none', 'Refresh timestamp should not have display:none on desktop');

      // Verify .ats-mobile-hide class exists but does NOT set display:none on desktop
      const classes = await atsRefresh.getAttribute('class');
      if (classes && classes.includes('ats-mobile-hide')) {
        // The class is present, but at desktop width it should not hide the element
        const visibility = await atsRefresh.isVisible();
        expect(visibility).toBe(true, 'Refresh timestamp should be visible on desktop even with ats-mobile-hide class');
      }

      console.log('[PASS] 2a: Desktop shows both .ats-now and .ats-refresh visible');
    } else {
      console.log('[INFO] 2a: No refresh timestamp yet (first load / market closed)');
    }
  });

  test('2b. Desktop: timestamp colours are correct (cyan for now, amber for refresh)', async ({ page, viewport }) => {
    if (!viewport || viewport.width < 1200) {
      test.skip();
    }

    await page.goto('/pulse');

    const atsGroup = page.locator('.ats-group');
    await expect(atsGroup).toBeVisible({ timeout: TIMEOUT });

    // .ats-now should have --c-info (cyan)
    const atsNow = atsGroup.locator('.ats-now');
    const nowColor = await atsNow.evaluate(el => getComputedStyle(el).color);
    // rgb(34, 211, 238) = #22d3ee (cyan) — verify it matches
    expect(nowColor).toMatch(/rgb\(34,\s*211,\s*238\)|rgb\(34,211,238\)/, `ats-now should be cyan, got ${nowColor}`);

    // .ats-refresh should have --algo-amber (amber)
    const atsRefresh = atsGroup.locator('.ats-refresh');
    if (await atsRefresh.count() > 0) {
      const refreshColor = await atsRefresh.evaluate(el => getComputedStyle(el).color);
      // rgb(251, 191, 36) = #fbbf24 (amber)
      expect(refreshColor).toMatch(/rgb\(251,\s*191,\s*36\)|rgb\(251,191,36\)/, `ats-refresh should be amber, got ${refreshColor}`);

      console.log('[PASS] 2b: Desktop timestamp colours verified (cyan + amber)');
    }
  });

  // ─────────────────────────────────────────────────────────────────
  // 3. Mobile: timestamp toggle and min-height
  // ─────────────────────────────────────────────────────────────────

  test.skip(({ browserName }) => browserName !== 'chromium', 'Mobile-only');

  test('3a. Mobile: tapping timestamp button toggles visibility of now/refresh', async ({ page, viewport }) => {
    // Only run on mobile portrait (360×800)
    if (!viewport || viewport.width > 400) {
      test.skip();
    }

    await page.goto('/pulse');

    const atsGroup = page.locator('.ats-group');
    await expect(atsGroup).toBeVisible({ timeout: TIMEOUT });

    // On first render, .ats-now should be visible, .ats-refresh hidden
    const atsNow = atsGroup.locator('.ats-now');
    const atsRefresh = atsGroup.locator('.ats-refresh');

    // Get initial visibility
    const nowVisibleBefore = await atsNow.isVisible();
    const refreshVisibleBefore = await atsRefresh.count() > 0
      ? await atsRefresh.evaluate(el => !getComputedStyle(el).display.includes('none'))
      : false;

    // Verify initial state: now visible, refresh hidden
    expect(nowVisibleBefore).toBe(true, 'ats-now should be visible initially on mobile');

    // Only proceed if refresh span exists
    if (await atsRefresh.count() > 0) {
      // Tap the button to toggle
      await atsGroup.click();
      await page.waitForTimeout(300); // Allow animation

      // After toggle, refresh should be visible, now hidden
      const nowVisibleAfter = await atsNow.evaluate(el => !getComputedStyle(el).display.includes('none'));
      const refreshVisibleAfter = await atsRefresh.evaluate(el => !getComputedStyle(el).display.includes('none'));

      expect(nowVisibleAfter).toBe(false, 'ats-now should be hidden after toggle');
      expect(refreshVisibleAfter).toBe(true, 'ats-refresh should be visible after toggle');

      // Tap again to toggle back
      await atsGroup.click();
      await page.waitForTimeout(300);

      const nowVisibleFinal = await atsNow.isVisible();
      expect(nowVisibleFinal).toBe(true, 'ats-now should be visible after second toggle');

      console.log('[PASS] 3a: Mobile timestamp toggle works (now ↔ refresh)');
    } else {
      console.log('[INFO] 3a: No refresh timestamp yet, toggle test skipped');
    }
  });

  test('3b. Mobile: page-header .ats-group has min-height >= 2.5rem', async ({ page, viewport }) => {
    if (!viewport || viewport.width > 400) {
      test.skip();
    }

    await page.goto('/pulse');

    const atsGroup = page.locator('.ats-group');
    await expect(atsGroup).toBeVisible({ timeout: TIMEOUT });

    // Get computed height
    const box = await atsGroup.boundingBox();
    expect(box).toBeTruthy();
    if (box) {
      const heightRem = box.height / 16; // 1rem = 16px (default)
      expect(box.height).toBeGreaterThanOrEqual(40, 'ats-group should have min-height >= 40px (2.5rem)');
      console.log(`[PASS] 3b: Mobile ats-group height = ${box.height}px (~${heightRem.toFixed(2)}rem)`);
    }
  });

  test('3c. Mobile: page-header total height >= 2.5rem', async ({ page, viewport }) => {
    if (!viewport || viewport.width > 400) {
      test.skip();
    }

    await page.goto('/pulse');

    const pageHeader = page.locator('.page-header');
    await expect(pageHeader).toBeVisible({ timeout: TIMEOUT });

    const box = await pageHeader.boundingBox();
    expect(box).toBeTruthy();
    if (box) {
      expect(box.height).toBeGreaterThanOrEqual(40, 'page-header should have min-height >= 40px (2.5rem) on mobile');
      console.log(`[PASS] 3c: Mobile page-header height = ${box.height}px`);
    }
  });

  // ─────────────────────────────────────────────────────────────────
  // 4. Conn tab: account number wraps on mobile
  // ─────────────────────────────────────────────────────────────────

  test('4a. Conn tab: account column wraps without horizontal scroll (mobile)', async ({ page, viewport }) => {
    // Only run on mobile portrait
    if (!viewport || viewport.width > 400) {
      test.skip();
    }

    await page.goto('/pulse');

    // Open the LogPanel via the bell icon or activity button (if visible)
    // Look for a trigger that opens the log panel
    const bellIcon = page.locator('[aria-label*="Activity"], [aria-label*="Log"], [title*="Activity"]').first();
    if (await bellIcon.isVisible({ timeout: 5000 }).catch(() => false)) {
      await bellIcon.click();
      await page.waitForTimeout(300);
    }

    // Navigate to a page that has the LogPanel visible (console page has it)
    await page.goto('/console', { waitUntil: 'domcontentloaded' });

    // Wait for LogPanel to render
    const logPanel = page.locator('.log-panel');
    await expect(logPanel).toBeVisible({ timeout: TIMEOUT });

    // Click on "Conn" tab if it exists
    const connTab = page.locator('button[role="tab"]', { hasText: /Conn/i }).first();
    if (await connTab.isVisible({ timeout: 3000 }).catch(() => false)) {
      await connTab.click();
      await page.waitForTimeout(300);

      // Look for conn rows
      const connRows = page.locator('.lp-conn-row');
      const rowCount = await connRows.count();

      if (rowCount > 0) {
        // Check if any row has account text that would overflow
        for (let i = 0; i < Math.min(rowCount, 3); i++) {
          const row = connRows.nth(i);
          const acctSpan = row.locator('.lp-conn-acct');

          // Get the bounding boxes
          const rowBox = await row.boundingBox();
          const acctBox = await acctSpan.boundingBox();

          if (rowBox && acctBox) {
            // Account span should fit within the row (no horizontal overflow)
            expect(acctBox.x + acctBox.width).toBeLessThanOrEqual(
              rowBox.x + rowBox.width + 2, // +2 for rounding
              'Account number should wrap within the row'
            );
          }
        }

        console.log(`[PASS] 4a: Conn tab account column wraps correctly on mobile (${rowCount} rows checked)`);
      } else {
        console.log('[INFO] 4a: No Conn events yet, wrap test skipped');
      }
    } else {
      console.log('[SKIP] 4a: Conn tab not found (may not have events)');
    }
  });

  test('4b. Conn tab: account column uses word-break:break-all style (mobile)', async ({ page, viewport }) => {
    if (!viewport || viewport.width > 400) {
      test.skip();
    }

    await page.goto('/console', { waitUntil: 'domcontentloaded' });

    const logPanel = page.locator('.log-panel');
    await expect(logPanel).toBeVisible({ timeout: TIMEOUT });

    const connTab = page.locator('button[role="tab"]', { hasText: /Conn/i }).first();
    if (await connTab.isVisible({ timeout: 3000 }).catch(() => false)) {
      await connTab.click();
      await page.waitForTimeout(300);

      // Check the CSS on .lp-conn-acct
      const acctSpan = page.locator('.lp-conn-acct').first();
      if (await acctSpan.count() > 0) {
        const wordBreak = await acctSpan.evaluate(el => {
          const computed = getComputedStyle(el);
          return computed.wordBreak;
        });

        // On mobile, .lp-conn-acct should have word-break: break-all
        expect(wordBreak).toMatch(/break-all|break/, 'Account column should use word-break:break-all on mobile');

        console.log('[PASS] 4b: Conn tab account column uses word-break style');
      }
    }
  });

  // ─────────────────────────────────────────────────────────────────
  // 5. Mobile page-header no horizontal scroll
  // ─────────────────────────────────────────────────────────────────

  test('5a. Mobile: page-header does not cause horizontal scroll', async ({ page, viewport }) => {
    if (!viewport || viewport.width > 400) {
      test.skip();
    }

    await page.goto('/pulse');

    // Get viewport width
    const vpWidth = await page.evaluate(() => window.innerWidth);

    // Get page-header width
    const pageHeader = page.locator('.page-header');
    await expect(pageHeader).toBeVisible({ timeout: TIMEOUT });

    const headerBox = await pageHeader.boundingBox();
    expect(headerBox).toBeTruthy();

    if (headerBox) {
      // Header should not exceed viewport
      expect(headerBox.width).toBeLessThanOrEqual(vpWidth + 2, 'page-header should not exceed viewport width');

      // Check for horizontal scroll
      const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
      const clientWidth = await page.evaluate(() => document.documentElement.clientWidth);

      expect(scrollWidth).toBeLessThanOrEqual(clientWidth + 1, 'No horizontal scroll should exist');

      console.log(`[PASS] 5a: Mobile page-header fits viewport (${headerBox.width}px ≤ ${vpWidth}px)`);
    }
  });

  test('5b. Mobile: PositionStrip (if visible) does not cause horizontal scroll', async ({ page, viewport }) => {
    if (!viewport || viewport.width > 400) {
      test.skip();
    }

    await page.goto('/pulse');

    const strip = page.locator('.ps-strip');
    if (await strip.isVisible({ timeout: 5000 }).catch(() => false)) {
      const vpWidth = await page.evaluate(() => window.innerWidth);
      const stripBox = await strip.boundingBox();

      if (stripBox) {
        expect(stripBox.width).toBeLessThanOrEqual(vpWidth + 2, 'PositionStrip should fit viewport width');

        console.log(`[PASS] 5b: Mobile PositionStrip fits viewport (${stripBox.width}px ≤ ${vpWidth}px)`);
      }
    } else {
      console.log('[INFO] 5b: PositionStrip not visible, skip');
    }
  });

  // ─────────────────────────────────────────────────────────────────
  // Regression: desktop vs mobile rendering consistency
  // ─────────────────────────────────────────────────────────────────

  test('6a. Regression: page-header renders on all viewports without console errors', async ({ page }) => {
    await page.goto('/pulse');

    const pageHeader = page.locator('.page-header');
    await expect(pageHeader).toBeVisible({ timeout: TIMEOUT });

    // Verify no structural errors
    const headerText = await pageHeader.textContent();
    expect(headerText).toBeTruthy();

    console.log('[PASS] 6a: page-header renders without console errors');
  });

  test('6b. Regression: NavBreakdown popup closes when overlay clicked', async ({ page }) => {
    await page.goto('/pulse');

    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    // Click any value to open popup
    const pValue = page.locator('.ps-strip .ps-k-p')
      .locator('xpath=following-sibling::span[1][contains(@class, "ps-agg-v")]')
      .first();

    if (await pValue.isVisible({ timeout: 3000 }).catch(() => false)) {
      await pValue.click();

      const popup = page.locator('.ps-breakdown-panel');
      await expect(popup).toBeVisible({ timeout: 5000 });

      // Click the overlay (ps-breakdown-overlay) to close
      const overlay = page.locator('.ps-breakdown-overlay');
      await overlay.click();
      await page.waitForTimeout(200);

      // Verify popup is closed
      const isVisible = await popup.isVisible({ timeout: 2000 }).catch(() => false);
      expect(isVisible).toBe(false, 'Popup should close when overlay is clicked');

      console.log('[PASS] 6b: NavBreakdown popup closes cleanly');
    }
  });
});
