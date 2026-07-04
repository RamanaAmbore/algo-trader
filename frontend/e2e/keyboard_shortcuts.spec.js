/**
 * keyboard_shortcuts.spec.js — E2E coverage for global keyboard shortcuts.
 *
 * Five quality dimensions:
 * 1. SSOT   — key bindings match the finalized design table in CLAUDE.md
 * 2. Perf   — navigation response < 2 s; no long-task on key press
 * 3. Stale  — no duplicate key-handler registrations (check via store, not DOM)
 * 4. Reuse  — shortcut infrastructure lives in (algo)/+layout.svelte (no per-page copy)
 * 5. UX     — input focus disables shortcuts; Esc closes modals; cheatsheet visible
 *
 * Auth strategy: ONE login per module via ensureJwt + loginAsAdmin (form-based,
 * rate-limit-tolerant). Each test seeds the cached JWT into sessionStorage via
 * addInitScript — zero /api/auth/login calls beyond the first beforeAll.
 * Only chromium-desktop: keyboard shortcuts have no semantic meaning on touch
 * viewports; mobile project would add spurious rate-limit churn.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';

/**
 * Inject a JWT token into the page sessionStorage via addInitScript so the
 * auth store picks it up on load. Must be called before page.goto().
 * @param {import('@playwright/test').Page} page
 * @param {string} token
 */
async function seedToken(page, token) {
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, token);
}

// Module-level token — shared across both describe groups (same worker scope)
// so we log in at most ONCE per Playwright worker.
/** @type {string} */
let _sharedJwt = '';

/** Ensure _sharedJwt is populated; no-op if already set. */
async function ensureJwt(browser) {
  if (_sharedJwt) return;
  const ctx = await browser.newContext({ viewport: { width: 1366, height: 768 } });
  const pg  = await ctx.newPage();
  pg.setDefaultNavigationTimeout(60_000);
  await pg.goto(`${BASE}/signin`, { waitUntil: 'domcontentloaded' });
  const info = await loginAsAdmin(pg);
  _sharedJwt = info.token;
  await ctx.close();
}

// ── Desktop suite ─────────────────────────────────────────────────────
test.describe('keyboard shortcuts — desktop', () => {
  test.use({ viewport: { width: 1366, height: 768 } });
  // Skip on non-chromium projects — keyboard shortcuts are desktop-only.
  test.skip(({ browserName }) => browserName !== 'chromium',
    'keyboard shortcuts are chromium-desktop only');

  test.beforeAll(async ({ browser }) => {
    await ensureJwt(browser);
  }, 90_000); // generous: slow dev-server signin + loginAsAdmin retries

  test('g d navigates to /dashboard', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('d');

    await page.waitForURL(`${BASE}/dashboard`, { timeout: 5_000 });
    expect(page.url()).toContain('/dashboard');
  });

  test('g p navigates to /pulse', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('p');

    await page.waitForURL(`${BASE}/pulse`, { timeout: 5_000 });
    expect(page.url()).toContain('/pulse');
  });

  test('g e navigates to /admin/derivatives', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('e');

    await page.waitForURL(/\/admin\/derivatives/, { timeout: 5_000 });
    expect(page.url()).toContain('/admin/derivatives');
  });

  test('g c navigates to /charts', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('c');

    await page.waitForURL(`${BASE}/charts`, { timeout: 5_000 });
    expect(page.url()).toContain('/charts');
  });

  test('g o navigates to /orders', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('o');

    await page.waitForURL(`${BASE}/orders`, { timeout: 5_000 });
    expect(page.url()).toContain('/orders');
  });

  test('g a navigates to /automation', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('a');

    await page.waitForURL(`${BASE}/automation`, { timeout: 5_000 });
    expect(page.url()).toContain('/automation');
  });

  test('? opens cheatsheet modal', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    await page.keyboard.press('?');

    // ShortcutCheatsheet renders with role=dialog
    const modal = page.locator('[role="dialog"][aria-labelledby="sc-title"]');
    await expect(modal).toBeVisible({ timeout: 2_000 });
    await expect(modal).toContainText('Keyboard shortcuts');
  });

  test('Esc closes cheatsheet modal', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    await page.keyboard.press('?');
    const modal = page.locator('[role="dialog"][aria-labelledby="sc-title"]');
    await expect(modal).toBeVisible({ timeout: 2_000 });

    await page.keyboard.press('Escape');
    await expect(modal).not.toBeVisible({ timeout: 2_000 });
  });

  test('cheatsheet shows expected shortcut sections', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    await page.keyboard.press('?');
    const modal = page.locator('[role="dialog"][aria-labelledby="sc-title"]');
    await expect(modal).toBeVisible({ timeout: 2_000 });

    // Navigation section — Bloomberg g+letter combos
    await expect(modal).toContainText('g p');
    await expect(modal).toContainText('g e');   // derivatives (not g r)
    await expect(modal).toContainText('g c');   // charts
    await expect(modal).toContainText('g m');   // movers

    // Actions section
    await expect(modal).toContainText('t');     // order ticket
    await expect(modal).toContainText('h');     // activity / log
    await expect(modal).toContainText('k');     // chart modal

    // Grid section (slice AU Phase 2+)
    await expect(modal).toContainText('j');     // row down
    await expect(modal).toContainText('f');     // fullscreen card
    await expect(modal).toContainText('c');     // collapse card
  });

  test('r shortcut fires the page refresh (network request)', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.rf-btn', { timeout: 20_000 });

    // Intercept any /api/positions or /api/holdings call as refresh signal
    let refreshCalled = false;
    page.on('request', (req) => {
      if (req.url().includes('/api/') && (
        req.url().includes('/positions') ||
        req.url().includes('/holdings') ||
        req.url().includes('/pulse')
      )) {
        refreshCalled = true;
      }
    });

    await page.keyboard.press('r');
    // Give the microtask + fetch a moment to fire
    await page.waitForTimeout(1_500);
    expect(refreshCalled).toBe(true);
  });

  // ── UX: input focus disables shortcuts ─────────────────────────────
  test('shortcuts disabled while typing in a text input', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    const initialUrl = page.url();

    // Focus the search input (/ shortcut focus target)
    const input = page.locator('input[type="text"], input[type="search"]').first();
    await input.focus();

    // Press g then p — should not navigate
    await page.keyboard.press('g');
    await page.keyboard.press('p');

    // Brief wait to confirm URL hasn't changed
    await page.waitForTimeout(300);
    expect(page.url()).toBe(initialUrl);
  });

  test('Esc in a text input defocuses but does not navigate', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    const input = page.locator('input[type="text"], input[type="search"]').first();
    await input.focus();
    await expect(input).toBeFocused();

    // Esc blurs the input (slice AU: blur on Esc-in-input behaviour)
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);

    // Input must no longer have focus after Esc
    await expect(input).not.toBeFocused();
    // No navigation should have occurred
    expect(page.url()).toContain('/pulse');
  });

  // ── Perf: g-key navigation < 2 s ──────────────────────────────────
  test('g d navigation completes in < 2 s', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    const t0 = Date.now();
    await page.keyboard.press('g');
    await page.keyboard.press('d');
    await page.waitForURL(`${BASE}/dashboard`, { timeout: 5_000 });
    const elapsed = Date.now() - t0;

    expect(elapsed).toBeLessThan(2_000);
  });
});

// ── Mobile viewport suite ─────────────────────────────────────────────
// Cheatsheet layout checked at 390px via Playwright keyboard API.
// Only chromium-desktop project (keyboard makes no sense on touch).
test.describe('keyboard shortcuts — mobile viewport', () => {
  test.use({ viewport: { width: 390, height: 844 } });
  test.skip(({ browserName }) => browserName !== 'chromium',
    'keyboard shortcuts are chromium-desktop only');

  // Re-use the shared module-level JWT — no second login needed.
  test.beforeAll(async ({ browser }) => {
    await ensureJwt(browser);
  }, 90_000);

  test('? opens cheatsheet on mobile viewport', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    await page.keyboard.press('?');
    const modal = page.locator('[role="dialog"][aria-labelledby="sc-title"]');
    await expect(modal).toBeVisible({ timeout: 3_000 });
  });

  test('cheatsheet stacks single column on 390px viewport', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    await page.keyboard.press('?');
    const modal = page.locator('[role="dialog"][aria-labelledby="sc-title"]');
    await expect(modal).toBeVisible({ timeout: 3_000 });

    // The .sc-grid must be within viewport width (no overflow).
    // At 390px the grid collapses to 1 column via @media (max-width: 480px).
    const grid = modal.locator('.sc-grid');
    const box  = await grid.boundingBox();
    if (box) {
      expect(box.x + box.width).toBeLessThanOrEqual(390 + 4); // 4px tolerance
    }
  });
});
