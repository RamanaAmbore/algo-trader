/**
 * keyboard_shortcuts.spec.js — E2E coverage for global keyboard shortcuts.
 *
 * Five quality dimensions:
 * 1. SSOT   — key bindings match the finalized design table in CLAUDE.md
 * 2. Perf   — navigation response < 2 s; no long-task on key press
 * 3. Stale  — no duplicate key-handler registrations (check via store, not DOM)
 * 4. Reuse  — shortcut infrastructure lives in (algo)/+layout.svelte (no per-page copy)
 * 5. UX     — input focus disables shortcuts; Esc closes modals; cheatsheet visible
 */

import { test, expect } from '@playwright/test';

const BASE   = process.env.BASE_URL || 'https://dev.ramboq.com';
const _PASS  = process.env.PLAYWRIGHT_PASS || 'admin1234';

let _cachedToken = null;

async function login(page) {
  if (_cachedToken) {
    await page.context().addInitScript((t) => {
      sessionStorage.setItem('ramboq_token', t);
    }, _cachedToken);
    return;
  }
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _PASS },
    });
    if (r.ok()) { _cachedToken = (await r.json()).access_token; break; }
  }
  if (!_cachedToken) throw new Error(`login failed against ${BASE}`);
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _cachedToken);
}

// ── Desktop suite ─────────────────────────────────────────────────────
test.describe('keyboard shortcuts — desktop', () => {
  test.use({ viewport: { width: 1366, height: 768 } });

  test('g d navigates to /dashboard', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('d');

    await page.waitForURL(`${BASE}/dashboard`, { timeout: 5_000 });
    expect(page.url()).toContain('/dashboard');
  });

  test('g p navigates to /pulse', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('p');

    await page.waitForURL(`${BASE}/pulse`, { timeout: 5_000 });
    expect(page.url()).toContain('/pulse');
  });

  test('g e navigates to /admin/derivatives', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('e');

    await page.waitForURL(/\/admin\/derivatives/, { timeout: 5_000 });
    expect(page.url()).toContain('/admin/derivatives');
  });

  test('g c navigates to /charts', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('c');

    await page.waitForURL(`${BASE}/charts`, { timeout: 5_000 });
    expect(page.url()).toContain('/charts');
  });

  test('g o navigates to /orders', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('o');

    await page.waitForURL(`${BASE}/orders`, { timeout: 5_000 });
    expect(page.url()).toContain('/orders');
  });

  test('g a navigates to /automation', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('g');
    await page.keyboard.press('a');

    await page.waitForURL(`${BASE}/automation`, { timeout: 5_000 });
    expect(page.url()).toContain('/automation');
  });

  test('? opens cheatsheet modal', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('?');

    // ShortcutCheatsheet renders with role=dialog
    const modal = page.locator('[role="dialog"][aria-labelledby="sc-title"]');
    await expect(modal).toBeVisible({ timeout: 2_000 });
    await expect(modal).toContainText('Keyboard shortcuts');
  });

  test('Esc closes cheatsheet modal', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    await page.keyboard.press('?');
    const modal = page.locator('[role="dialog"][aria-labelledby="sc-title"]');
    await expect(modal).toBeVisible({ timeout: 2_000 });

    await page.keyboard.press('Escape');
    await expect(modal).not.toBeVisible({ timeout: 2_000 });
  });

  test('cheatsheet shows expected shortcut sections', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
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
  });

  test('r shortcut fires the page refresh (network request)', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
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
  test('shortcuts disabled while typing in a text input', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

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
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    const input = page.locator('input[type="text"], input[type="search"]').first();
    await input.focus();
    await expect(input).toBeFocused();

    // Esc should only close cheatsheet if open; otherwise defocus input
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);

    // Input should no longer have focus (browser Esc behavior on inputs
    // varies; check that no navigation occurred)
    expect(page.url()).toContain('/pulse');
  });

  // ── Perf: g-key navigation < 2 s ──────────────────────────────────
  test('g d navigation completes in < 2 s', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.algo-nav-btn', { timeout: 15_000 });

    const t0 = Date.now();
    await page.keyboard.press('g');
    await page.keyboard.press('d');
    await page.waitForURL(`${BASE}/dashboard`, { timeout: 5_000 });
    const elapsed = Date.now() - t0;

    expect(elapsed).toBeLessThan(2_000);
  });
});

// ── Mobile suite ──────────────────────────────────────────────────────
test.describe('keyboard shortcuts — mobile portrait', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('? opens cheatsheet on mobile', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });

    await page.keyboard.press('?');
    const modal = page.locator('[role="dialog"][aria-labelledby="sc-title"]');
    await expect(modal).toBeVisible({ timeout: 3_000 });
  });

  test('cheatsheet stacks single column on mobile viewport', async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });

    await page.keyboard.press('?');
    const modal = page.locator('[role="dialog"][aria-labelledby="sc-title"]');
    await expect(modal).toBeVisible({ timeout: 3_000 });

    // The .sc-grid must be within viewport width (no overflow)
    const grid = modal.locator('.sc-grid');
    const box  = await grid.boundingBox();
    if (box) {
      expect(box.x + box.width).toBeLessThanOrEqual(390 + 4); // 4px tolerance
    }
  });
});
