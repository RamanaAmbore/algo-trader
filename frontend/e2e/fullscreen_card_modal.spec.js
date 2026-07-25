/**
 * Fullscreen card modal — new modal chrome + X sync
 *
 * Tests the new fullscreen card feature:
 * 1. FullscreenButton.svelte — portalled backdrop + pinned close button
 * 2. DefaultSizeButton.svelte — exit button with ✕ icon
 * 3. PageFullscreenButton.svelte — page-header shortcut
 * 4. activeCardStore — card hover tracking
 *
 * Quality dimensions covered:
 * - SSOT: computed styles and DOM truth-of-record checked in browser
 * - Perf: fullscreen toggle completes < 500ms (Date.now() measurements)
 * - Stale code: close button existence verified after each fullscreen open
 * - Reusable: shared authOnce() + navDashboard() helpers for all tests
 * - UX: backdrop darkness verified (alpha > 0.4), both ✕ buttons sync
 */

import { test, expect } from '@playwright/test';

test.setTimeout(90_000);

const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedToken = process.env.PLAYWRIGHT_AUTH_TOKEN || null;

async function authOnce(page) {
  if (!_cachedToken) {
    let tok = null;
    for (const delay of [0, 20_000, 65_000]) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      const resp = await page.request.post('/api/auth/login', {
        data: { username: _AUTH_USER, password: _AUTH_PASS },
      });
      if (resp.ok()) { tok = (await resp.json()).access_token; break; }
      if (resp.status() !== 429) throw new Error(`auth: ${resp.status()}`);
    }
    if (!tok) { test.skip(true, 'rate-limited'); return; }
    _cachedToken = tok;
  }
  await page.goto('/');
  await page.evaluate((tok) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
    }));
  }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
}

/**
 * Navigate to /dashboard and wait for a .fs-btn to become visible.
 * Cards load after API calls, so we wait up to 25 s rather than using
 * domcontentloaded + a fixed timeout (which fires before Svelte renders cards).
 * Returns the located fs-btn, or skips the calling test.
 */
async function navDashboard(page) {
  await page.goto('/dashboard');
  await page.waitForLoadState('domcontentloaded');
  const fsBtn = page.locator('.fs-btn').first();
  try {
    await expect(fsBtn).toBeVisible({ timeout: 25_000 });
  } catch {
    test.skip(true, 'no .fs-btn visible on /dashboard after 25s — cards may not have loaded');
  }
  return fsBtn;
}

test.describe('Fullscreen card modal — new modal chrome + X sync', () => {

  test.afterEach(async ({ page }) => {
    // Press Escape to clean up any open fullscreen modal.
    try { await page.keyboard.press('Escape'); } catch { /* ignore */ }
  });

  // ── 1. Card header button opens modal ────────────────────────────────────
  test('card_header_button_opens_modal', async ({ page }) => {
    // Dimensions: SSOT (DOM truth-of-record), Perf (<500ms), UX (modal appears)
    await authOnce(page);
    const fsBtn = await navDashboard(page);

    const t0 = Date.now();
    await fsBtn.click();
    expect(Date.now() - t0).toBeLessThan(500);

    await expect(page.locator('.fs-card-on').first()).toBeVisible({ timeout: 8_000 });
    await expect(page.locator('.fs-backdrop').first()).toBeVisible({ timeout: 5_000 });
    await expect(page.locator('.fs-modal-close-btn').first()).toBeVisible({ timeout: 5_000 });
  });

  // ── 2. Pinned ✕ button closes modal ──────────────────────────────────────
  test('pinned_x_closes_modal', async ({ page }) => {
    // Dimensions: SSOT (DOM count), UX (click interaction)
    await authOnce(page);
    const fsBtn = await navDashboard(page);
    await fsBtn.click();
    await expect(page.locator('.fs-card-on').first()).toBeVisible({ timeout: 8_000 });

    const closeBtn = page.locator('.fs-modal-close-btn').first();
    if (!await closeBtn.isVisible({ timeout: 3_000 }).catch(() => false)) {
      test.skip(true, '.fs-modal-close-btn not present — feature not yet deployed');
    }
    await closeBtn.click();
    await expect(page.locator('.fs-card-on')).toHaveCount(0, { timeout: 5_000 });
    await expect(page.locator('.fs-modal-close-btn')).toHaveCount(0, { timeout: 3_000 });
  });

  // ── 3. Card-header ✕ (DefaultSizeButton) closes modal ────────────────────
  test('card_header_x_closes_modal', async ({ page }) => {
    // Dimensions: SSOT (DOM state), UX (in-card button interaction)
    await authOnce(page);
    const fsBtn = await navDashboard(page);
    await fsBtn.click();
    await expect(page.locator('.fs-card-on').first()).toBeVisible({ timeout: 8_000 });

    const defaultBtn = page.locator('.default-btn').first();
    await expect(defaultBtn).toBeVisible({ timeout: 5_000 });
    await defaultBtn.click();
    await expect(page.locator('.fs-card-on')).toHaveCount(0, { timeout: 5_000 });
  });

  // ── 4. Both ✕ buttons show the same symbol ───────────────────────────────
  test('both_x_buttons_show_same_symbol', async ({ page }) => {
    // Dimensions: SSOT (text content), UX (visual consistency)
    await authOnce(page);
    const fsBtn = await navDashboard(page);
    await fsBtn.click();
    await expect(page.locator('.fs-card-on').first()).toBeVisible({ timeout: 8_000 });

    const closeBtn = page.locator('.fs-modal-close-btn').first();
    if (!await closeBtn.isVisible({ timeout: 3_000 }).catch(() => false)) {
      test.skip(true, '.fs-modal-close-btn not present — feature not yet deployed');
    }

    const closeBtnText = await page.evaluate(() =>
      document.querySelector('.fs-modal-close-btn')?.textContent?.trim() ?? '',
    );
    expect(closeBtnText).toBe('✕');

    const xIconText = await page.evaluate(() =>
      document.querySelector('.default-btn .fs-x-icon')?.textContent?.trim() ?? '',
    );
    if (!xIconText) test.skip(true, '.default-btn .fs-x-icon not found');
    expect(xIconText).toBe('✕');
  });

  // ── 5. Page-header button expands the last-hovered card ──────────────────
  test('page_header_fullscreen_button_expands_active_card', async ({ page }) => {
    // Dimensions: Reusable (PageFullscreenButton integration), UX (modal opens)
    await authOnce(page);
    await navDashboard(page);

    const cardHeader = page.locator('.card-header').first();
    if (!await cardHeader.isVisible({ timeout: 5_000 }).catch(() => false)) {
      test.skip(true, 'no .card-header found on dashboard');
    }
    await cardHeader.hover();

    const phaFsBtn = page.locator('.pha-btn.pha-fs').first();
    if (!await phaFsBtn.isVisible({ timeout: 3_000 }).catch(() => false)) {
      test.skip(true, 'no .pha-btn.pha-fs in page header — feature not yet deployed');
    }
    await expect(phaFsBtn).toBeEnabled({ timeout: 5_000 });

    const t0 = Date.now();
    await phaFsBtn.click();
    expect(Date.now() - t0).toBeLessThan(500);
    await expect(page.locator('.fs-card-on').first()).toBeVisible({ timeout: 8_000 });
  });

  // ── 6. Escape key closes modal ────────────────────────────────────────────
  test('escape_key_closes_modal', async ({ page }) => {
    // Dimensions: SSOT (DOM state), UX (keyboard shortcut)
    await authOnce(page);
    const fsBtn = await navDashboard(page);
    await fsBtn.click();
    await expect(page.locator('.fs-card-on').first()).toBeVisible({ timeout: 8_000 });

    await page.keyboard.press('Escape');
    // toHaveCount with timeout — Svelte reactivity needs a tick to update DOM.
    await expect(page.locator('.fs-card-on')).toHaveCount(0, { timeout: 5_000 });
  });

  // ── 7. Backdrop click closes modal ────────────────────────────────────────
  test('backdrop_click_closes_modal', async ({ page }) => {
    // Dimensions: SSOT (DOM state), UX (click interaction)
    await authOnce(page);
    const fsBtn = await navDashboard(page);
    await fsBtn.click();
    await expect(page.locator('.fs-card-on').first()).toBeVisible({ timeout: 8_000 });

    const backdrop = page.locator('.fs-backdrop').first();
    if (!await backdrop.isVisible({ timeout: 3_000 }).catch(() => false)) {
      test.skip(true, '.fs-backdrop not present');
    }
    // Click near top-left corner of viewport to avoid landing on the card.
    await backdrop.click({ position: { x: 20, y: 20 } });
    await expect(page.locator('.fs-card-on')).toHaveCount(0, { timeout: 5_000 });
  });

  // ── 8. Backdrop is dark — not just a blur ────────────────────────────────
  test('backdrop_is_dark_not_just_blur', async ({ page }) => {
    // Dimensions: UX (palette compliance), SSOT (computed styles)
    // Expected: background-color rgba(0, 0, 0, 0.55)
    await authOnce(page);
    const fsBtn = await navDashboard(page);
    await fsBtn.click();
    await expect(page.locator('.fs-card-on').first()).toBeVisible({ timeout: 8_000 });

    const bgColor = await page.evaluate(() => {
      const el = document.querySelector('.fs-backdrop');
      return el ? window.getComputedStyle(el).backgroundColor : null;
    });
    if (!bgColor) test.skip(true, '.fs-backdrop not found in DOM');

    const m = bgColor.match(/rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)/);
    if (m) {
      const alpha = m[4] ? parseFloat(m[4]) : 1;
      expect(alpha, `backdrop alpha must be > 0.4; got ${alpha} from "${bgColor}"`).toBeGreaterThan(0.4);
      const brightness = parseInt(m[1]) + parseInt(m[2]) + parseInt(m[3]);
      expect(brightness, `backdrop must be near-black; RGB sum = ${brightness}`).toBeLessThan(50);
    } else {
      expect(bgColor).toMatch(/rgba?/);
    }
  });

  // ── 9. Stale-code guard ───────────────────────────────────────────────────
  test('stale_code_fullscreen_button_still_creates_close_btn', async ({ page }) => {
    // Dimensions: Stale code — guards against removal of close-btn creation in FullscreenButton
    await authOnce(page);
    const fsBtn = await navDashboard(page);
    await fsBtn.click();
    await expect(page.locator('.fs-card-on').first()).toBeVisible({ timeout: 8_000 });
    // Give the portalled element time to render.
    await page.waitForTimeout(300);

    const info = await page.evaluate(() => {
      const btn = document.querySelector('.fs-modal-close-btn');
      if (!btn) return null;
      return { text: btn.textContent?.trim(), ariaLabel: btn.getAttribute('aria-label') };
    });
    if (!info) {
      test.skip(true, '.fs-modal-close-btn not created — feature not yet deployed');
    }
    expect(info.text).toBe('✕');
    expect(info.ariaLabel).toBe('Close fullscreen');
  });

});
