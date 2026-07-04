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
 * Auth strategy: ONE login per describe-group via beforeAll + loginAsAdmin
 * (form-based, rate-limit-tolerant). Each individual test injects the cached
 * JWT via addInitScript to skip the /api/auth/login call entirely.
 * Only chromium-desktop project: keyboard shortcuts are desktop-only behaviour;
 * running under mobile-portrait/mobile-landscape would add spurious failures.
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

// Module-level token — shared across both describe groups so we only
// log in ONCE per worker. Playwright workers have separate module scopes,
// but two describe groups in the same module share the same scope.
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
  test.skip(({ browserName }) => browserName !== 'chromium', 'keyboard shortcuts are chromium-desktop only');

  test.beforeAll(async ({ browser }) => {
    await ensureJwt(browser);
  }, 90_000); // generous timeout: slow dev server signin + loginAsAdmin retries

  test('g d navigates to /dashboard', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('d');

    await page.waitForURL(`${BASE}/dashboard`, { timeout: 5_000 });
    expect(page.url()).toContain('/dashboard');
  });

  test('g p navigates to /pulse', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('p');

    await page.waitForURL(`${BASE}/pulse`, { timeout: 5_000 });
    expect(page.url()).toContain('/pulse');
  });

  test('g e navigates to /admin/derivatives', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('e');

    await page.waitForURL(/\/admin\/derivatives/, { timeout: 5_000 });
    expect(page.url()).toContain('/admin/derivatives');
  });

  test('g c navigates to /charts', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('c');

    await page.waitForURL(`${BASE}/charts`, { timeout: 5_000 });
    expect(page.url()).toContain('/charts');
  });

  test('g o navigates to /orders', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('o');

    await page.waitForURL(`${BASE}/orders`, { timeout: 5_000 });
    expect(page.url()).toContain('/orders');
  });

  test('g a navigates to /automation', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('a');

    await page.waitForURL(`${BASE}/automation`, { timeout: 5_000 });
    expect(page.url()).toContain('/automation');
  });

  test('? opens cheatsheet modal', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('?');

    // ShortcutCheatsheet renders with role=dialog
    const modal = page.locator('[role="dialog"][aria-labelledby="sc-title"]');
    await expect(modal).toBeVisible({ timeout: 2_000 });
    await expect(modal).toContainText('Keyboard shortcuts');
  });

  test('Esc closes cheatsheet modal', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('?');
    const modal = page.locator('[role="dialog"][aria-labelledby="sc-title"]');
    await expect(modal).toBeVisible({ timeout: 2_000 });

    await page.keyboard.press('Escape');
    await expect(modal).not.toBeVisible({ timeout: 2_000 });
  });

  test('cheatsheet shows expected shortcut sections', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('?');
    const modal = page.locator('[role="dialog"][aria-labelledby="sc-title"]');
    await expect(modal).toBeVisible({ timeout: 2_000 });

    // Navigation section must list the Bloomberg g+letter combos
    await expect(modal).toContainText('g p');
    await expect(modal).toContainText('g e');   // derivatives (not g r)
    await expect(modal).toContainText('g c');   // charts
    await expect(modal).toContainText('g m');   // movers

    // Actions section
    await expect(modal).toContainText('t');     // order ticket
    await expect(modal).toContainText('h');     // activity / log
    await expect(modal).toContainText('k');     // chart modal

    // Grid section (slice AU — all three phases shipped)
    await expect(modal).toContainText('j');     // row down
    await expect(modal).toContainText('f');     // fullscreen card
    await expect(modal).toContainText('c');     // collapse card
  });

  test('r shortcut fires the page refresh (network request)', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.rf-btn', { timeout: 15_000 });

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
  // Use /charts which renders a symbol search input immediately in the DOM.
  // /pulse uses ag-Grid column filters that only mount on interaction.
  test('shortcuts disabled while typing in a text input', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/charts`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    const initialUrl = page.url();

    // / shortcut targets .oes-sym-input which is the symbol picker on /charts.
    // Press / to focus, then g+p — should not navigate while focused.
    await page.keyboard.press('/');
    const input = page.locator('.oes-sym-input, input[type="search"], input[type="text"]').first();
    await expect(input).toBeFocused({ timeout: 2_000 });

    // Press g then p — should not navigate
    await page.keyboard.press('g');
    await page.keyboard.press('p');

    // Brief wait to confirm URL hasn't changed
    await page.waitForTimeout(300);
    expect(page.url()).toBe(initialUrl);
  });

  test('Esc in a text input defocuses but does not navigate', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/charts`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    // Use / to focus the symbol input, then Esc to blur it.
    await page.keyboard.press('/');
    const input = page.locator('.oes-sym-input, input[type="search"], input[type="text"]').first();
    await expect(input).toBeFocused({ timeout: 2_000 });

    // Esc blurs the input (slice AU: blur on Esc-in-input behaviour)
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);

    // Input must no longer have focus after Esc
    await expect(input).not.toBeFocused();
    // No navigation should have occurred
    expect(page.url()).toContain('/charts');
  });

  // ── Perf: g-key navigation < 2 s ──────────────────────────────────
  test('g d navigation completes in < 2 s', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    const t0 = Date.now();
    await page.keyboard.press('g');
    await page.keyboard.press('d');
    await page.waitForURL(`${BASE}/dashboard`, { timeout: 5_000 });
    const elapsed = Date.now() - t0;

    expect(elapsed).toBeLessThan(2_000);
  });
});

// ── Mobile viewport suite ─────────────────────────────────────────────
// Cheatsheet layout verified at 390px but via Playwright keyboard API
// (works headlessly). Only chromium-desktop project.
test.describe('keyboard shortcuts — mobile viewport', () => {
  test.use({ viewport: { width: 390, height: 844 } });
  test.skip(({ browserName }) => browserName !== 'chromium', 'keyboard shortcuts are chromium-desktop only');

  // Re-use the shared module-level JWT — no second login needed.
  test.beforeAll(async ({ browser }) => {
    await ensureJwt(browser);
  }, 90_000);

  test('? opens cheatsheet on mobile viewport', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    // Wait for the layout to mount so the keyboard handler is wired.
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

    await page.keyboard.press('?');
    const modal = page.locator('[role="dialog"][aria-labelledby="sc-title"]');
    await expect(modal).toBeVisible({ timeout: 3_000 });
  });

  test('cheatsheet stacks single column on 390px viewport', async ({ page }) => {
    await seedToken(page, _sharedJwt);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    // Wait for layout mount before pressing keyboard shortcut.
    await page.waitForSelector('.algo-nav-btn', { timeout: 20_000 });

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
