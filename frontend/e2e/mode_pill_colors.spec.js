/**
 * Mode-pill computed colours.
 *
 * Five modes — sim / paper / live / replay / shadow — each carry
 * their own pill colour shipped via :global() rules in
 * frontend/src/lib/LogPanel.svelte. Mounting LogPanel anywhere on
 * the page exposes the rules; the /console route does this.
 *
 * Test injects one span per mode into the document and asserts the
 * computed `color` matches the spec.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

const EXPECTED = {
  sim:    'rgb(251, 191, 36)',  // #fbbf24
  paper:  'rgb(125, 211, 252)', // #7dd3fc
  live:   'rgb(110, 231, 183)', // #6ee7b7
  replay: 'rgb(74, 222, 128)',  // #4ade80
  shadow: 'rgb(251, 146, 60)',  // #fb923c
};

test.describe('Mode-pill computed colours', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('every mode pill renders its expected colour', async ({ page }) => {
    await page.goto('/console');
    // Wait for LogPanel to mount — the bottom Log tab is the
    // canonical signal.
    const logTab = page.locator('.oes-bottom-tab', { hasText: /Log/i }).first();
    await expect(logTab).toBeVisible({ timeout: TIMEOUT });

    // Inject one span per mode into the body so we can read the
    // computed colour directly.
    await page.evaluate((modes) => {
      const host = document.createElement('div');
      host.id = '__mode_pill_test__';
      host.style.position = 'fixed';
      host.style.left = '-9999px';
      for (const m of modes) {
        const span = document.createElement('span');
        span.className = `mode-pill mode-pill-${m}`;
        span.textContent = m.toUpperCase();
        span.dataset.mode = m;
        host.appendChild(span);
      }
      document.body.appendChild(host);
    }, Object.keys(EXPECTED));

    // Read each computed colour.
    /** @type {Record<string, string>} */
    const got = await page.evaluate((modes) => {
      /** @type {Record<string, string>} */
      const out = {};
      for (const m of modes) {
        const el = document.querySelector(`#__mode_pill_test__ [data-mode="${m}"]`);
        out[m] = el ? getComputedStyle(el).color : '';
      }
      return out;
    }, Object.keys(EXPECTED));

    for (const [mode, expectedRgb] of Object.entries(EXPECTED)) {
      expect(got[mode], `mode=${mode} got=${got[mode]}`).toBe(expectedRgb);
    }
  });
});
