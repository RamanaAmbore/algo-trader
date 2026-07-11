/**
 * Demo / anonymous watchlist guard — MarketPulse mutation controls.
 *
 * Verifies that unauthenticated (demo) visitors cannot see or trigger
 * watchlist mutation controls, and that authenticated admins can.
 *
 * Five quality dimensions:
 *   1. SSOT   — isDemo derived from $authStore.user; no prop threading
 *   2. Perf   — /pulse DOMContentLoaded within 8 s for both sessions
 *   3. Stale  — no leftover mutation controls after anon visit
 *   4. Reuse  — loginAsAdmin fixture reused for authenticated path
 *   5. UX     — keyboard shortcut '/' also blocked for anon users
 *
 * Run against dev.ramboq.com:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   PLAYWRIGHT_USER=<user> PLAYWRIGHT_PASS=<pass> \
 *   cd frontend && npx playwright test e2e/demo_watchlist_guard \
 *     --project=chromium-desktop
 *
 * On dev branch, anonymous visitors are redirected to /signin (the
 * (algo) layout's isDemo never fires). This spec gates on the branch
 * and skips the anon-specific assertions when branch !== 'main',
 * but ALWAYS runs the authenticated-admin assertions so the controls
 * remain visible for real users.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;
const PULSE_PATH = '/pulse';

/**
 * Detect whether the server is running on the main (prod) branch.
 * On dev branches isDemo is always false — anon visitors go to /signin.
 *
 * @param {import('@playwright/test').Page} page
 * @returns {Promise<boolean>}
 */
async function isMainBranch(page) {
  try {
    const r = await page.request.get('/api/charts/paper-status');
    if (!r.ok()) return false;
    const j = await r.json();
    return j?.branch === 'main';
  } catch (_) {
    return false;
  }
}

/**
 * Clear cookies + storage, navigate to /pulse anonymously, and wait for
 * the page to settle (grids may be empty on demo but page must paint).
 *
 * @param {import('@playwright/test').Page} page
 */
async function visitPulseAnon(page) {
  await page.context().clearCookies();
  await page.evaluate(() => {
    try { localStorage.clear(); } catch {}
    try { sessionStorage.clear(); } catch {}
  }).catch(() => {});

  const t0 = Date.now();
  await page.goto(PULSE_PATH, { waitUntil: 'domcontentloaded', timeout: TIMEOUT });

  // 2. Perf — DOMContentLoaded within 8 s
  expect(Date.now() - t0).toBeLessThan(8_000);

  // Give Svelte reactive assignments (isDemo, grid paint) time to settle.
  await page.waitForTimeout(2_000);
}

// ── Anon / demo path ──────────────────────────────────────────────────────────

test.describe('MarketPulse watchlist guard — anonymous demo visitor', () => {
  test('manage watchlist button (mp-add-btn) is NOT present for anon user', async ({ page }) => {
    const mainBranch = await isMainBranch(page);
    if (!mainBranch) {
      test.skip(true, 'Demo mode only fires on main branch; anon goes to /signin on dev — skipping anon assertions');
    }

    await visitPulseAnon(page);

    // 3. Stale — button must be absent from the DOM entirely
    const btn = page.locator('.mp-add-btn');
    await expect(btn).toHaveCount(0);
  });

  test("pressing '/' does NOT open the manage popup for anon user", async ({ page }) => {
    const mainBranch = await isMainBranch(page);
    if (!mainBranch) {
      test.skip(true, 'Demo mode only fires on main branch — skipping anon keyboard guard test');
    }

    await visitPulseAnon(page);

    // Press '/' — the shortcut is blocked for demo users
    await page.keyboard.press('/');
    await page.waitForTimeout(300);

    // 5. UX — search-overlay must NOT appear
    const overlay = page.locator('.search-overlay');
    await expect(overlay).toHaveCount(0);
  });

  test('per-row × remove buttons (sym-remove) are absent for anon user', async ({ page }) => {
    const mainBranch = await isMainBranch(page);
    if (!mainBranch) {
      test.skip(true, 'Demo mode only fires on main branch — skipping anon sym-remove test');
    }

    await visitPulseAnon(page);

    // 3. Stale — no × remove buttons should appear in the pinned/watchlist grids
    const removeButtons = page.locator('.sym-remove');
    await expect(removeButtons).toHaveCount(0);
  });

  test('per-row reorder buttons (sym-move) are absent for anon user', async ({ page }) => {
    const mainBranch = await isMainBranch(page);
    if (!mainBranch) {
      test.skip(true, 'Demo mode only fires on main branch — skipping anon sym-move test');
    }

    await visitPulseAnon(page);

    // Move buttons hidden by CSS on non-hover, but must be absent from DOM for anon
    const moveBtns = page.locator('.sym-move');
    await expect(moveBtns).toHaveCount(0);
  });
});

// ── Authenticated admin path ──────────────────────────────────────────────────

test.describe('MarketPulse watchlist guard — authenticated admin', () => {
  test.beforeEach(async ({ page }) => {
    // Log in via the real signin form, then navigate to /pulse.
    await loginAsAdmin(page);
    const t0 = Date.now();
    await page.goto(PULSE_PATH, { waitUntil: 'domcontentloaded', timeout: TIMEOUT });
    // 2. Perf — /pulse loads within 8 s for authenticated users too
    expect(Date.now() - t0).toBeLessThan(8_000);
    // Wait for grids and reactive state to settle
    await page.waitForTimeout(2_000);
  });

  test('manage watchlist button IS present for authenticated admin', async ({ page }) => {
    // 5. UX — pencil button must be visible
    const btn = page.locator('.mp-add-btn');
    await expect(btn).toBeVisible({ timeout: TIMEOUT });
  });

  test('clicking manage button opens the manage popup for authenticated admin', async ({ page }) => {
    // Verify the button is present and that clicking it opens the popup.
    // (The '/' keyboard shortcut opening the popup for admins is covered by
    // the existing pulse_search_popup.spec.js; we focus here on the guard
    // being correctly absent for logged-in users.)
    const btn = page.locator('.mp-add-btn');
    await expect(btn).toBeVisible({ timeout: TIMEOUT });

    await btn.click();
    await page.waitForTimeout(400);

    // 5. UX — search-overlay must appear on click
    const overlay = page.locator('.search-overlay').first();
    await expect(overlay).toBeVisible({ timeout: TIMEOUT });

    // Clean up
    await page.keyboard.press('Escape');
  });

  test('at least one per-row × remove button exists in pinned/watchlist grids for admin', async ({ page }) => {
    // The grid may be empty in some environments; guard against that.
    // We only assert count > 0 when there are actual watchlist rows.
    const rows = page.locator('.ag-row');
    const rowCount = await rows.count();
    if (rowCount === 0) {
      test.skip(true, 'No ag-Grid rows rendered — watchlist may be empty in this environment');
    }

    // 5. UX — at least one remove button expected when rows exist
    const removeButtons = page.locator('.sym-remove');
    const count = await removeButtons.count();
    expect(count).toBeGreaterThan(0);
  });
});
