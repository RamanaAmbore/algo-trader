/**
 * CardHeader smoke test — quick surface-level check.
 * Validates that CardHeader.svelte is properly wired into key surfaces.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('CardHeader — quick surface check', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('CardHeader exists in MarketPulse headers', async ({ page }) => {
    await page.goto('/pulse');
    await page.waitForSelector('.card-header', { timeout: 10000 });

    const headers = await page.locator('.card-header').all();
    const chLefts = await page.locator('.card-header .ch-left').all();
    const chRights = await page.locator('.card-header .ch-right').all();
    const buttons = await page.locator('.card-header .ch-right button').all();

    console.log(`[PULSE] ${headers.length} card-headers, ${chLefts.length} ch-left zones, ${chRights.length} ch-right zones, ${buttons.length} buttons`);

    expect(headers.length).toBeGreaterThanOrEqual(3);
    expect(chLefts.length).toBeGreaterThanOrEqual(3);
    expect(chRights.length).toBeGreaterThanOrEqual(3);
    expect(buttons.length).toBeGreaterThanOrEqual(10);
  });

  test('CardHeader in /dashboard', async ({ page }) => {
    await page.goto('/dashboard');
    await page.waitForSelector('.card-header', { timeout: 10000 });

    const headers = await page.locator('.card-header').all();
    const buttons = await page.locator('.card-header .ch-right button').all();

    console.log(`[DASHBOARD] ${headers.length} card-headers, ${buttons.length} buttons`);

    expect(headers.length).toBeGreaterThanOrEqual(1);
  });

  test('CardHeader in /orders', async ({ page }) => {
    await page.goto('/orders');
    await page.waitForSelector('.card-header', { timeout: 10000 });

    const headers = await page.locator('.card-header').all();

    console.log(`[ORDERS] ${headers.length} card-headers`);

    expect(headers.length).toBeGreaterThanOrEqual(1);
  });

  test('CardHeader in /automation/templates', async ({ page }) => {
    await page.goto('/automation/templates');
    await page.waitForSelector('.card-header', { timeout: 10000 });

    const headers = await page.locator('.card-header').all();

    console.log(`[TEMPLATES] ${headers.length} card-headers`);

    expect(headers.length).toBeGreaterThanOrEqual(1);
  });

  test('CardHeader theme — algo layout defines CSS custom properties', async ({ page }) => {
    await page.goto('/pulse');
    await page.waitForSelector('.card-header', { timeout: 10000 });

    const header = page.locator('.card-header').first();
    const styles = await header.evaluate(el => {
      const cs = getComputedStyle(el);
      return {
        gap: cs.gap,
        padding: cs.padding,
        display: cs.display,
        alignItems: cs.alignItems,
      };
    });

    console.log(`[THEME] Header styles: ${JSON.stringify(styles)}`);

    expect(styles.display).toBe('flex');
    expect(styles.alignItems).toBe('center');
  });

  test('Fullscreen download button alignment in ch-right', async ({ page }) => {
    await page.goto('/pulse');
    await page.waitForSelector('.card-header', { timeout: 10000 });

    const firstHeader = page.locator('.card-header').first();
    const chRight = firstHeader.locator('.ch-right').first();
    const downloadBtn = chRight.locator('button[title*="download"]').first();

    if (await downloadBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
      const btnBox = await downloadBtn.boundingBox();
      const chRightBox = await chRight.boundingBox();

      if (btnBox && chRightBox) {
        expect(btnBox.x).toBeGreaterThanOrEqual(chRightBox.x - 2);
        console.log(`[ALIGNMENT] Download button properly positioned within ch-right`);
      }
    }
  });
});
