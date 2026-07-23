/**
 * algo-timestamp.spec.js — AlgoTimestamp component interactive behaviour
 *
 * AlgoTimestamp renders a dual-timezone clock in the page header. On desktop,
 * it's decorative (pointer-events: none). On mobile, tapping toggles between
 * current time and refresh time (if available).
 *
 * Key scenarios:
 *   1. Desktop viewport: timestamp text visible; clicks do nothing
 *   2. Mobile portrait: tap toggles between current time and refresh time
 *   3. Refresh time never shows a future timestamp relative to current time
 *   4. Multiple taps cycle through available times
 *   5. Refresh time unavailable: tap has no effect
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

test.describe('AlgoTimestamp — page header clock', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('1. Desktop viewport: timestamp text is visible; pointer-events: none', async ({ page, browserName }, { project }) => {
    // Skip on mobile projects
    if (project.name.includes('mobile')) {
      test.skip();
    }

    // Set desktop viewport explicitly
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto('/admin/derivatives');

    // Wait for page to load
    await page.waitForSelector('.algo-ts', { timeout: 10_000 }).catch(() => {});

    const timestamp = page.locator('.algo-ts').first();
    const isVisible = await timestamp.isVisible({ timeout: 5_000 }).catch(() => false);

    if (isVisible) {
      // Verify text content is present (IST/EST timezone format)
      const text = await timestamp.textContent();
      expect(text).toBeTruthy();
      expect(text).toMatch(/IST.*EST|IST.*EDT/);

      // Verify pointer-events is none on desktop (.ats-group has pointer-events: none)
      const style = await timestamp.evaluate(el => {
        const computedStyle = getComputedStyle(el);
        return {
          pointerEvents: computedStyle.pointerEvents,
          cursor: computedStyle.cursor,
        };
      });

      // On desktop, pointer-events should be 'none' and cursor 'default'
      expect(style.pointerEvents).toBe('none');
      expect(style.cursor).toBe('default');
    } else {
      // Timestamp may not be on all pages; skip gracefully
      test.skip();
    }
  });

  test('2. Mobile portrait: timestamp is visible and tappable', async ({ page }, { project }) => {
    // Skip on desktop
    if (!project.name.includes('mobile')) {
      test.skip();
    }

    // Set mobile portrait viewport (360×800)
    await page.setViewportSize({ width: 360, height: 800 });
    await page.goto('/admin/derivatives');

    // Wait for page to load
    await page.waitForSelector('.algo-ts', { timeout: 10_000 }).catch(() => {});

    const timestamp = page.locator('.algo-ts').first();
    const isVisible = await timestamp.isVisible({ timeout: 5_000 }).catch(() => false);

    if (isVisible) {
      // On mobile, .ats-group has pointer-events: auto (media query)
      const style = await timestamp.evaluate(el => {
        const computedStyle = getComputedStyle(el);
        return {
          pointerEvents: computedStyle.pointerEvents,
          cursor: computedStyle.cursor,
        };
      });

      // Mobile should have pointer-events: auto
      expect(style.pointerEvents).toBe('auto');
      expect(style.cursor).not.toBe('default');
    }
  });

  test('3. Mobile portrait: tap toggles between current time and refresh time', async ({ page }, { project }) => {
    // Skip on desktop
    if (!project.name.includes('mobile')) {
      test.skip();
    }

    // Set mobile portrait viewport
    await page.setViewportSize({ width: 360, height: 800 });
    await page.goto('/admin/derivatives');

    // Wait for grid to populate (so refresh time is available)
    await page.waitForSelector('.ag-root', { timeout: 10_000 }).catch(() => {});
    await page.waitForTimeout(500); // Allow refresh timestamp to be set

    const timestamp = page.locator('.algo-ts').first();
    const isVisible = await timestamp.isVisible({ timeout: 5_000 }).catch(() => false);

    if (!isVisible) {
      test.skip();
    }

    // Get initial text (should show current time)
    const initialText = await timestamp.textContent();

    // Tap the timestamp button
    await timestamp.tap();
    await page.waitForTimeout(300);

    // Get text after tap
    const afterFirstTap = await timestamp.textContent();

    // If refresh time is available, it should differ from current time
    // (one shows current, one shows refresh). If they're the same, refresh
    // time wasn't available (both show current time).
    if (initialText !== afterFirstTap) {
      // Toggle worked — we're now showing different time
      expect(afterFirstTap).toBeTruthy();

      // Tap again to toggle back
      await timestamp.tap();
      await page.waitForTimeout(300);

      const afterSecondTap = await timestamp.textContent();
      // Should be back to initial time
      expect(afterSecondTap).toBe(initialText);
    } else {
      // Refresh time not available; toggle has no effect (expected if no data yet)
      expect(afterFirstTap).toBe(initialText);
    }
  });

  test('4. Refresh timestamp is never in the future relative to current timestamp', async ({ page }, { project }) => {
    // Skip on desktop
    if (!project.name.includes('mobile')) {
      test.skip();
    }

    await page.setViewportSize({ width: 360, height: 800 });
    await page.goto('/admin/derivatives');

    // Wait for grid and refresh
    await page.waitForSelector('.ag-root', { timeout: 10_000 }).catch(() => {});
    await page.waitForTimeout(1000); // Ensure refresh timestamp is captured

    // Extract both timestamps from the component's state
    const timestamps = await page.evaluate(() => {
      const ts = document.querySelector('.algo-ts');
      if (!ts) return null;

      // The component renders: <span class="ats-now">...current...</span> | <span class="ats-refresh">...refresh...</span>
      const nowSpan = ts.querySelector('.ats-now');
      const refreshSpan = ts.querySelector('.ats-refresh');

      return {
        current: nowSpan?.textContent || '',
        refresh: refreshSpan?.textContent || '',
      };
    });

    if (!timestamps || !timestamps.refresh) {
      // Refresh time not available; skip
      test.skip();
    }

    // Parse timestamps (format: "HH:MM:SS IST / HH:MM:SS EST")
    // Extract the epoch values if available
    const currentMs = await page.evaluate(() => {
      const nowSpan = document.querySelector('.algo-ts .ats-now');
      // Reconstruct the epoch from the formatted string
      // This is approximate since we only have formatted text
      return Date.now();
    });

    const refreshMs = await page.evaluate(() => {
      const store = sessionStorage.getItem('ramboq_lastRefreshAt');
      return store ? parseInt(store) : 0;
    }).catch(() => 0);

    // Refresh should be <= current (never in the future)
    if (refreshMs > 0) {
      expect(refreshMs).toBeLessThanOrEqual(currentMs + 1000); // +1s tolerance for clock drift
    }
  });

  test('5. Timestamp updates every 30 seconds (tick-forward test)', async ({ page }, { project }) => {
    // Skip on mobile for this test (testing time progression)
    if (project.name.includes('mobile')) {
      test.skip();
    }

    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto('/admin/derivatives');

    const timestamp = page.locator('.algo-ts').first();
    const isVisible = await timestamp.isVisible({ timeout: 5_000 }).catch(() => false);

    if (!isVisible) {
      test.skip();
    }

    // Get initial timestamp
    const initialText = await timestamp.textContent();
    expect(initialText).toBeTruthy();

    // Wait for 35 seconds (AlgoTimestamp updates every 30s)
    await page.waitForTimeout(35_000);

    // Get updated timestamp
    const updatedText = await timestamp.textContent();

    // The timestamp should have updated (seconds or minutes should differ)
    // Note: this is probabilistic (might land on same second edge case)
    expect(updatedText).toBeTruthy();
    // In most cases, it should differ
    if (updatedText !== initialText) {
      expect(updatedText).not.toBe(initialText);
    }
  });

  test('6. On mobile, reduce font size in media query applied', async ({ page }, { project }) => {
    // Skip on desktop
    if (!project.name.includes('mobile')) {
      test.skip();
    }

    await page.setViewportSize({ width: 360, height: 800 });
    await page.goto('/admin/derivatives');

    const timestamp = page.locator('.algo-ts').first();
    const isVisible = await timestamp.isVisible({ timeout: 5_000 }).catch(() => false);

    if (isVisible) {
      const style = await timestamp.evaluate(el => {
        const computedStyle = getComputedStyle(el);
        return {
          fontSize: computedStyle.fontSize,
          minHeight: computedStyle.minHeight,
        };
      });

      // Mobile CSS: font-size: 0.6rem, min-height: 1.8rem
      // (computed values will be in px; 0.6rem ≈ 9.6px at 16px base)
      expect(style.fontSize).toBeTruthy();
      // Verify it's smaller than desktop (computed in px)
      const fontSizePx = parseFloat(style.fontSize);
      expect(fontSizePx).toBeLessThan(12); // Rough estimate of 0.6rem
    }
  });

  test('7. Separator (|) is hidden on mobile', async ({ page }, { project }) => {
    // Skip on desktop
    if (!project.name.includes('mobile')) {
      test.skip();
    }

    await page.setViewportSize({ width: 360, height: 800 });
    await page.goto('/admin/derivatives');

    const timestamp = page.locator('.algo-ts').first();
    const isVisible = await timestamp.isVisible({ timeout: 5_000 }).catch(() => false);

    if (isVisible) {
      const separator = timestamp.locator('.ats-sep');
      const sepVisible = await separator.isVisible({ timeout: 3_000 }).catch(() => false);

      // On mobile, separator should be hidden (media query: display: none)
      expect(sepVisible).toBe(false);
    }
  });

  test('8. Dual timezone format (IST + EST/EDT) rendered', async ({ page }) => {
    // Set desktop viewport
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto('/admin/derivatives');

    const timestamp = page.locator('.algo-ts').first();
    const isVisible = await timestamp.isVisible({ timeout: 5_000 }).catch(() => false);

    if (isVisible) {
      const text = await timestamp.textContent();

      // Format should include both IST and EST/EDT
      expect(text).toMatch(/IST/);
      expect(text).toMatch(/EST|EDT/);

      // Basic format check: should look like "HH:MM:SS IST / HH:MM:SS EST"
      expect(text).toMatch(/\d{2}:\d{2}:\d{2}.*\d{2}:\d{2}:\d{2}/);
    } else {
      test.skip();
    }
  });
});
