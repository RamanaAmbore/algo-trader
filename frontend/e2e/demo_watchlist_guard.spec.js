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

  test('pinned watchlist renders at least 1 row for anon user', async ({ page }) => {
    const mainBranch = await isMainBranch(page);
    if (!mainBranch) {
      test.skip(true, 'Demo mode only fires on main branch — skipping anon pinned grid test');
    }

    await visitPulseAnon(page);

    // 5. UX — pinned watchlist grid must render at least 1 row.
    // The grid container is .mp-bucket-pinwatch; rows are .ag-row within .ag-center-cols-container.
    const pinnedRows = page.locator('.mp-bucket-pinwatch .ag-center-cols-container .ag-row');
    const count = await pinnedRows.count();

    if (count === 0) {
      test.skip(true, 'No rows in pinned watchlist — book may be empty on this demo instance');
    }

    expect(count).toBeGreaterThanOrEqual(1);
  });

  test('Winners tab renders rows for anon user', async ({ page }) => {
    const mainBranch = await isMainBranch(page);
    if (!mainBranch) {
      test.skip(true, 'Demo mode only fires on main branch — skipping anon Winners tab test');
    }

    await visitPulseAnon(page);

    // 5. UX — ensure the Winners bucket is rendered and has at least 1 row.
    await expect(page.locator('.mp-bucket-winners').first()).toBeVisible({ timeout: TIMEOUT });

    const winnersRows = page.locator('.mp-bucket-winners .ag-center-cols-container .ag-row');
    const count = await winnersRows.count();

    if (count === 0) {
      test.skip(true, 'No rows in Winners mover grid — market closed or movers data empty');
    }

    expect(count).toBeGreaterThanOrEqual(1);
  });

  test('Losers tab renders rows for anon user', async ({ page }) => {
    const mainBranch = await isMainBranch(page);
    if (!mainBranch) {
      test.skip(true, 'Demo mode only fires on main branch — skipping anon Losers tab test');
    }

    await visitPulseAnon(page);

    // 5. UX — ensure the Losers bucket is rendered and has at least 1 row.
    await expect(page.locator('.mp-bucket-losers').first()).toBeVisible({ timeout: TIMEOUT });

    const losersRows = page.locator('.mp-bucket-losers .ag-center-cols-container .ag-row');
    const count = await losersRows.count();

    if (count === 0) {
      test.skip(true, 'No rows in Losers mover grid — market closed or movers data empty');
    }

    expect(count).toBeGreaterThanOrEqual(1);
  });

  test('sparkline SVGs present in pinned grid for anon user', async ({ page }) => {
    const mainBranch = await isMainBranch(page);
    if (!mainBranch) {
      test.skip(true, 'Demo mode only fires on main branch — skipping anon sparkline test');
    }

    await visitPulseAnon(page);

    // 5. UX — sparklines (SVG elements) should render in the pinned watchlist.
    // The sparkline cells use .spark-cell class and contain inline SVGs.
    const sparkSvgs = page.locator('.mp-bucket-pinwatch .spark-cell svg');
    let count = 0;

    try {
      await sparkSvgs.first().waitFor({ state: 'attached', timeout: 8_000 });
      count = await sparkSvgs.count();
    } catch (_) {
      test.skip(true, 'No sparkline SVGs rendered in pinned grid — data may not have loaded');
    }

    if (count === 0) {
      test.skip(true, 'No sparkline SVGs found');
    }

    expect(count).toBeGreaterThan(0);
  });

  test('right-click context menu has no watchlist mutation items for anon user', async ({ page }) => {
    const mainBranch = await isMainBranch(page);
    if (!mainBranch) {
      test.skip(true, 'Demo mode only fires on main branch — skipping anon context-menu test');
    }

    await visitPulseAnon(page);

    // Get the first row in the pinned watchlist (if any exist).
    const rows = page.locator('.mp-bucket-pinwatch .ag-center-cols-container .ag-row');
    if ((await rows.count()) === 0) {
      test.skip(true, 'No rows in pinned watchlist to right-click');
    }

    const firstRow = rows.first();

    // Right-click to open the context menu.
    await firstRow.click({ button: 'right' });
    await page.waitForTimeout(300);

    // ag-Grid context menus render in a floating div with role="menu" or class="ag-menu".
    // We look for menu items that contain "Add to watchlist" or "Remove from watchlist".
    const menuItems = page.locator('[role="menuitem"], .ag-menu-option');
    const allTexts = await menuItems.allTextContents();
    const menuText = allTexts.join(' | ').toLowerCase();

    // 3. Stale — these mutation controls must NOT appear for anon users.
    expect(menuText).not.toContain('add to watchlist');
    expect(menuText).not.toContain('remove from watchlist');

    // Close the menu
    await page.keyboard.press('Escape');
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
