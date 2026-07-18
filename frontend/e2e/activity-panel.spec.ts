/**
 * activity-panel.spec.ts
 *
 * Verifies the post-fix behavior of the Activity panel:
 *
 *   1. Per-tab filter visibility — account filter and level filter are shown
 *      only on tabs where they are meaningful (post-fix correct behavior):
 *        - Orders tab:    account filter VISIBLE, level filter HIDDEN
 *        - Agents tab:    BOTH filters VISIBLE
 *        - System tab:    BOTH filters VISIBLE
 *        - Conn tab:      BOTH filters VISIBLE
 *        - Terminal tab:  NEITHER filter visible
 *        - Ticks tab:     NEITHER filter visible
 *        - News tab:      NEITHER filter visible
 *
 *   2. Card button group — Search, Expand/Contract, Fullscreen, Download
 *      buttons are present in the ActivityLogSurface header area on:
 *        - /orders activity card
 *        - /activity page
 *      NOTE: buttons are built into ActivityLogSurface itself, NOT inside
 *      a CardHeader .ch-right zone.
 *
 * Quality dimensions (per feedback_test_dimensions.md):
 *
 *   SSOT       — filters are driven by a single active-tab prop in
 *                ActivityLogSurface; no per-page filter-wiring needed.
 *
 *   Performance — switching tabs and checking filter visibility
 *                completes within 5 s per assertion.
 *
 *   Stale code — both filters are hidden for Terminal/Ticks/News (they
 *                have no account or level semantics in the data layer).
 *
 *   Reusable   — button group lives inside ActivityLogSurface so every
 *                surface (modal, card, page, console) inherits it.
 *
 *   UX         — correct filter chrome on each tab prevents the operator
 *                from seeing controls that have no effect on visible rows.
 *
 * Target: http://localhost:5174 (local dev-server, default)
 *   npx playwright test e2e/activity-panel.spec.ts \
 *     --project=chromium-desktop
 *   Override: PLAYWRIGHT_BASE_URL=https://dev.ramboq.com (cloud runs only,
 *   after the activity panel feature has been deployed)
 */

import { test, expect, type Page, type Locator } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

// Default to the local dev-server so the spec works out-of-the-box against
// local code (the activity panel feature may not yet be deployed on dev).
// Override with PLAYWRIGHT_BASE_URL=https://dev.ramboq.com for cloud runs.
const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

// ── helpers ────────────────────────────────────────────────────────────────

/**
 * Click a tab by its visible label text inside the given panel locator.
 * Retries up to timeoutMs for the tab to become visible.
 * Tabs in LogPanel use the `.algo-tab` class (from AlgoTabs component).
 */
async function clickTab(panel: Locator, label: RegExp | string, timeoutMs = 10_000) {
  const tab = panel.locator('.algo-tab', { hasText: label }).first();
  await expect(tab, `tab "${label}" is visible`).toBeVisible({ timeout: timeoutMs });
  await tab.click();
  // Give Svelte a tick to update derived visibility state.
  await tab.page().waitForTimeout(200);
}

/**
 * Returns the ActivityHeaderFilters span (.act-filters) scoped to the
 * given container. This is the parent of both the account multiselect
 * (.act-acct) and the level selector (.act-level-sel).
 */
function filtersLocator(container: Locator): Locator {
  return container.locator('.act-filters').first();
}

/**
 * Asserts account filter (.act-acct) visibility inside the given container.
 * Uses toBeVisible / toHaveCount(0) so failed assertions name the element.
 */
async function assertAccountFilter(
  container: Locator,
  visible: boolean,
  ctx: string,
) {
  const el = container.locator('.act-acct').first();
  if (visible) {
    await expect(el, `[${ctx}] account filter (.act-acct) VISIBLE`).toBeVisible({ timeout: 5_000 });
  } else {
    await expect(el, `[${ctx}] account filter (.act-acct) HIDDEN`).toHaveCount(0, { timeout: 5_000 });
  }
}

/**
 * Asserts level filter (.act-level-sel) visibility inside the given container.
 */
async function assertLevelFilter(
  container: Locator,
  visible: boolean,
  ctx: string,
) {
  const el = container.locator('.act-level-sel').first();
  if (visible) {
    await expect(el, `[${ctx}] level filter (.act-level-sel) VISIBLE`).toBeVisible({ timeout: 5_000 });
  } else {
    await expect(el, `[${ctx}] level filter (.act-level-sel) HIDDEN`).toHaveCount(0, { timeout: 5_000 });
  }
}

// ── /orders activity card — per-tab filter visibility ─────────────────────

test.describe('per-tab filter visibility — /orders activity card', () => {
  test.describe.configure({ mode: 'serial' });
  test.use({ viewport: { width: 1440, height: 900 } });
  test.setTimeout(90_000);

  let page: Page;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    page = await ctx.newPage();
    await loginAsAdmin(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });
  });

  test.afterAll(async () => {
    await page.close();
  });

  async function getActivityCard() {
    const card = page.locator('section.bucket-card-activity');
    await expect(card, 'activity card renders').toBeVisible({ timeout: 25_000 });
    await card.scrollIntoViewIfNeeded();
    // Wait for the LogPanel tab row to mount (.lp-card-btns is always rendered
    // regardless of active tab; .log-panel class only appears on non-order tabs).
    const btns = card.locator('.lp-card-btns').first();
    await expect(btns, '.lp-card-btns inside activity card').toBeVisible({ timeout: 15_000 });
    return card;
  }

  // ── Orders tab ────────────────────────────────────────────────────────────
  test('Orders tab: account filter VISIBLE, level filter HIDDEN', async () => {
    const card = await getActivityCard();
    await clickTab(card, /orders?/i);

    // Account filter: visible on Orders (filters by which account placed the order).
    // Level filter: hidden on Orders (orders have their own status chip strip, not
    // an error/warning/info level concept).
    //
    // NOTE: ActivityAccountSelect renders nothing when availableAccounts.length <= 1
    // (single-account or demo environment). In that case the account filter is
    // correctly absent regardless of tab. We guard this with a conditional assertion
    // so the test doesn't spuriously fail in single-account CI environments.
    const multiAcct = await card.locator('.act-acct').count();
    if (multiAcct > 0) {
      await assertAccountFilter(card, true, 'Orders tab');
    } else {
      // Single-account env — correctly absent. Not a failure.
      console.log('[SKIP] account filter absent — single-account environment');
    }
    await assertLevelFilter(card, false, 'Orders tab');
  });

  // ── Agents tab ────────────────────────────────────────────────────────────
  test('Agents tab: BOTH account + level filters VISIBLE', async () => {
    const card = await getActivityCard();
    await clickTab(card, /agents?/i);

    const multiAcct = await card.locator('.act-acct').count();
    if (multiAcct > 0) {
      await assertAccountFilter(card, true, 'Agents tab');
    } else {
      console.log('[SKIP] account filter absent — single-account environment');
    }
    await assertLevelFilter(card, true, 'Agents tab');
  });

  // ── System tab ────────────────────────────────────────────────────────────
  test('System tab: BOTH account + level filters VISIBLE', async () => {
    const card = await getActivityCard();
    await clickTab(card, /system/i);

    const multiAcct = await card.locator('.act-acct').count();
    if (multiAcct > 0) {
      await assertAccountFilter(card, true, 'System tab');
    } else {
      console.log('[SKIP] account filter absent — single-account environment');
    }
    await assertLevelFilter(card, true, 'System tab');
  });

  // ── Conn tab ──────────────────────────────────────────────────────────────
  test('Conn tab: BOTH account + level filters VISIBLE', async () => {
    const card = await getActivityCard();
    await clickTab(card, /conn/i);

    const multiAcct = await card.locator('.act-acct').count();
    if (multiAcct > 0) {
      await assertAccountFilter(card, true, 'Conn tab');
    } else {
      console.log('[SKIP] account filter absent — single-account environment');
    }
    await assertLevelFilter(card, true, 'Conn tab');
  });

  // ── Terminal tab ──────────────────────────────────────────────────────────
  test('Terminal tab: NEITHER filter visible', async () => {
    const card = await getActivityCard();
    await clickTab(card, /terminal/i);

    await assertAccountFilter(card, false, 'Terminal tab');
    await assertLevelFilter(card, false, 'Terminal tab');
  });

  // ── Ticks tab (simulator) ─────────────────────────────────────────────────
  test('Ticks tab: NEITHER filter visible', async () => {
    const card = await getActivityCard();
    // Ticks tab label: "Ticks" or "Simulator" depending on the LogPanel tabs array.
    // The tab id is 'simulator' with label 'Ticks'. Try both.
    const ticksTab = card.locator('.algo-tab').filter({ hasText: /ticks|simulator/i }).first();
    const tabVisible = await ticksTab.isVisible({ timeout: 5_000 }).catch(() => false);
    if (!tabVisible) {
      console.log('[SKIP] Ticks/Simulator tab not present on this surface');
      return;
    }
    await ticksTab.click();
    await page.waitForTimeout(200);

    await assertAccountFilter(card, false, 'Ticks tab');
    await assertLevelFilter(card, false, 'Ticks tab');
  });

  // ── News tab ──────────────────────────────────────────────────────────────
  test('News tab: NEITHER filter visible', async () => {
    const card = await getActivityCard();
    const newsTab = card.locator('.algo-tab').filter({ hasText: /news/i }).first();
    const tabVisible = await newsTab.isVisible({ timeout: 5_000 }).catch(() => false);
    if (!tabVisible) {
      console.log('[SKIP] News tab not present on this surface');
      return;
    }
    await newsTab.click();
    await page.waitForTimeout(200);

    await assertAccountFilter(card, false, 'News tab');
    await assertLevelFilter(card, false, 'News tab');
  });
});

// ── /activity page — per-tab filter visibility ─────────────────────────────

test.describe('per-tab filter visibility — /activity page', () => {
  test.describe.configure({ mode: 'serial' });
  test.use({ viewport: { width: 1440, height: 900 } });
  test.setTimeout(90_000);

  let page: Page;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    page = await ctx.newPage();
    await loginAsAdmin(page);
    await page.goto(`${BASE}/activity`, { waitUntil: 'domcontentloaded' });
  });

  test.afterAll(async () => {
    await page.close();
  });

  async function getActivityPage() {
    // The /activity page header hosts ActivityHeaderFilters; the panel below
    // is ActivityLogSurface. Use the page header as the root for filter checks.
    // Use .lp-card-btns as the LogPanel mount sentinel — it is always rendered
    // regardless of active tab (.log-panel class only appears on non-order tabs).
    const header = page.locator('.page-header');
    await expect(header, 'page header renders').toBeVisible({ timeout: 15_000 });
    const panel = page.locator('.lp-card-btns').first();
    await expect(panel, '.lp-card-btns on /activity page').toBeVisible({ timeout: 15_000 });
    // Return the tab-row div as the "panel" locator for clickTab() — it contains .algo-tab elements.
    return { header, panel: page.locator('.log-tab-row').first() };
  }

  test('Orders tab: account filter VISIBLE, level filter HIDDEN', async () => {
    const { header, panel } = await getActivityPage();
    await clickTab(panel, /orders?/i);

    const multiAcct = await header.locator('.act-acct').count();
    if (multiAcct > 0) {
      await assertAccountFilter(header, true, '/activity Orders tab');
    } else {
      console.log('[SKIP] account filter absent — single-account environment');
    }
    await assertLevelFilter(header, false, '/activity Orders tab');
  });

  test('Agents tab: BOTH filters VISIBLE', async () => {
    const { header, panel } = await getActivityPage();
    await clickTab(panel, /agents?/i);

    const multiAcct = await header.locator('.act-acct').count();
    if (multiAcct > 0) {
      await assertAccountFilter(header, true, '/activity Agents tab');
    } else {
      console.log('[SKIP] account filter absent — single-account environment');
    }
    await assertLevelFilter(header, true, '/activity Agents tab');
  });

  test('System tab: BOTH filters VISIBLE', async () => {
    const { header, panel } = await getActivityPage();
    await clickTab(panel, /system/i);

    const multiAcct = await header.locator('.act-acct').count();
    if (multiAcct > 0) {
      await assertAccountFilter(header, true, '/activity System tab');
    } else {
      console.log('[SKIP] account filter absent — single-account environment');
    }
    await assertLevelFilter(header, true, '/activity System tab');
  });

  test('Terminal tab: NEITHER filter visible', async () => {
    const { header, panel } = await getActivityPage();
    await clickTab(panel, /terminal/i);

    await assertAccountFilter(header, false, '/activity Terminal tab');
    await assertLevelFilter(header, false, '/activity Terminal tab');
  });

  test('News tab: NEITHER filter visible', async () => {
    const { header, panel } = await getActivityPage();
    const newsTab = panel.locator('.algo-tab').filter({ hasText: /news/i }).first();
    const tabVisible = await newsTab.isVisible({ timeout: 5_000 }).catch(() => false);
    if (!tabVisible) {
      console.log('[SKIP] News tab not present on /activity');
      return;
    }
    await newsTab.click();
    await page.waitForTimeout(200);

    await assertAccountFilter(header, false, '/activity News tab');
    await assertLevelFilter(header, false, '/activity News tab');
  });
});

// ── ActivityLogModal — per-tab filter visibility ───────────────────────────

test.describe('per-tab filter visibility — ActivityLogModal', () => {
  test.describe.configure({ mode: 'serial' });
  test.use({ viewport: { width: 1440, height: 900 } });
  test.setTimeout(90_000);

  let page: Page;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    page = await ctx.newPage();
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
  });

  test.afterAll(async () => {
    await page.close();
  });

  async function openModal(): Promise<Locator> {
    // Open ActivityLogModal via the `h` keyboard shortcut (global hotkey wired
    // in +layout.svelte). The broker-chip click opens the broker health popup,
    // not the activity modal, so we use the hotkey instead.
    //
    // Close any previously open modal first (Escape); then click the body to
    // ensure keyboard focus is on the document (headless browsers sometimes
    // start with no focused element, causing hotkeys to not fire).
    await page.keyboard.press('Escape');
    await page.waitForTimeout(150);
    // Click a neutral spot (page title) to ensure the page has focus and no
    // input element is active (the keydown guard in layout.svelte skips hotkeys
    // while an input/select/textarea is focused).
    await page.locator('body').click({ position: { x: 400, y: 100 } });
    await page.waitForTimeout(100);
    await page.keyboard.press('h');
    const modal = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(modal, 'ActivityLogModal opens').toBeVisible({ timeout: 10_000 });
    return modal;
  }

  test('Conn tab: BOTH filters VISIBLE', async () => {
    const modal = await openModal();
    // Navigate to the Conn tab explicitly (modal may open on any previously-active tab).
    await clickTab(modal, /conn/i);

    const multiAcct = await modal.locator('.act-acct').count();
    if (multiAcct > 0) {
      await assertAccountFilter(modal, true, 'Modal Conn tab');
    } else {
      console.log('[SKIP] account filter absent — single-account environment');
    }
    await assertLevelFilter(modal, true, 'Modal Conn tab');
  });

  test('Modal Orders tab: account filter VISIBLE, level filter HIDDEN', async () => {
    const modal = await openModal();
    // Use .log-tab-row as the panel locator for clickTab (always present in LogPanel).
    const panel = modal.locator('.log-tab-row').first();
    await expect(panel, '.log-tab-row in modal').toBeVisible({ timeout: 8_000 });
    await clickTab(modal, /orders?/i);

    const multiAcct = await modal.locator('.act-acct').count();
    if (multiAcct > 0) {
      await assertAccountFilter(modal, true, 'Modal Orders tab');
    } else {
      console.log('[SKIP] account filter absent — single-account environment');
    }
    await assertLevelFilter(modal, false, 'Modal Orders tab');
  });

  test('Modal Terminal tab: NEITHER filter visible', async () => {
    const modal = await openModal();
    const panel = modal.locator('.log-tab-row').first();
    await expect(panel, '.log-tab-row in modal').toBeVisible({ timeout: 8_000 });
    await clickTab(modal, /terminal/i);

    await assertAccountFilter(modal, false, 'Modal Terminal tab');
    await assertLevelFilter(modal, false, 'Modal Terminal tab');
  });
});

// ── Card button group — /orders activity card ──────────────────────────────

test.describe('card button group — ActivityLogSurface', () => {
  test.describe.configure({ mode: 'serial' });
  test.use({ viewport: { width: 1440, height: 900 } });
  test.setTimeout(90_000);

  // The button group is built into ActivityLogSurface itself (NOT inside a
  // CardHeader .ch-right zone). Aria-labels on icon buttons are used as selectors.
  //
  // Expected buttons (canonical order): Search · Expand/Contract · Fullscreen · Download
  // Each has an aria-label that names its function.

  async function assertButtonGroup(container: Locator, label: string) {
    // Search button — aria-label="Search rows" (static, always "Search rows")
    const searchBtn = container.locator('button[aria-label="Search rows"]').first();
    await expect(searchBtn, `[${label}] Search button present`).toBeVisible({ timeout: 10_000 });

    // Expand/Contract button — aria-label is dynamic: "Expand panel" (default) or
    // "Contract panel" (when expanded). Match either.
    const expandBtn = container.locator(
      'button[aria-label="Expand panel"], button[aria-label="Contract panel"]'
    ).first();
    await expect(expandBtn, `[${label}] Expand/Contract button present`).toBeVisible({ timeout: 5_000 });

    // Fullscreen button — aria-label="Open in fullscreen modal" (only shown outside modal context)
    const fsBtn = container.locator('button[aria-label="Open in fullscreen modal"]').first();
    await expect(fsBtn, `[${label}] Fullscreen button present`).toBeVisible({ timeout: 5_000 });

    // Download button — aria-label="Download CSV" (when not on news tab)
    const dlBtn = container.locator('button[aria-label="Download CSV"]').first();
    await expect(dlBtn, `[${label}] Download button present`).toBeVisible({ timeout: 5_000 });
  }

  test('/orders activity card has all four card buttons', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });

    const card = page.locator('section.bucket-card-activity');
    await expect(card, 'activity card renders').toBeVisible({ timeout: 25_000 });
    await card.scrollIntoViewIfNeeded();

    // Wait for the button group to mount — .lp-card-btns is always present
    // regardless of active tab (.log-panel class only appears on non-order tabs).
    const btns = card.locator('.lp-card-btns').first();
    await expect(btns, '.lp-card-btns in activity card').toBeVisible({ timeout: 15_000 });

    await assertButtonGroup(card, '/orders activity card');
  });

  test('/activity page has all four card buttons', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/activity`, { waitUntil: 'domcontentloaded' });

    const body = page.locator('.activity-page-body');
    await expect(body, '.activity-page-body renders').toBeVisible({ timeout: 20_000 });

    // Wait for LogPanel to mount via .lp-card-btns — always present regardless of tab.
    const btns = page.locator('.lp-card-btns').first();
    await expect(btns, '.lp-card-btns on /activity').toBeVisible({ timeout: 15_000 });

    await assertButtonGroup(page.locator('body'), '/activity page');
  });

  // ── Search button smoke — toggle and text-filter ──────────────────────────

  test('/orders activity card: Search button toggles a search input', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });

    const card = page.locator('section.bucket-card-activity');
    await expect(card).toBeVisible({ timeout: 25_000 });
    await card.scrollIntoViewIfNeeded();
    await expect(card.locator('.lp-card-btns').first()).toBeVisible({ timeout: 15_000 });

    const searchBtn = card.locator('button[aria-label="Search rows"]').first();
    await expect(searchBtn, 'Search button present before click').toBeVisible({ timeout: 10_000 });

    // Clicking search should reveal a text input for filtering rows.
    await searchBtn.click();
    const searchInput = card.locator('input[type="search"], input[type="text"][placeholder*="search" i], input[type="text"][aria-label*="search" i]').first();
    await expect(searchInput, 'search input visible after clicking Search').toBeVisible({ timeout: 5_000 });

    // Clicking again should hide it (toggle).
    await searchBtn.click();
    await expect(searchInput, 'search input hidden after second click').not.toBeVisible({ timeout: 5_000 });
  });

  // ── Download button smoke — clicking Download on Orders tab exports CSV ───

  test('/activity page: Download button triggers a file download', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/activity`, { waitUntil: 'domcontentloaded' });

    // Wait for LogPanel to mount via .lp-card-btns; use .log-tab-row for clickTab.
    await expect(page.locator('.lp-card-btns').first(), '.lp-card-btns on /activity').toBeVisible({ timeout: 20_000 });
    const tabRow = page.locator('.log-tab-row').first();

    // Land on Orders tab (default) — Downloads on Orders tab produce CSV.
    await clickTab(tabRow, /orders?/i);

    const dlBtn = page.locator('button[aria-label="Download CSV"]').first();
    await expect(dlBtn, 'Download button visible').toBeVisible({ timeout: 10_000 });

    // Set up a download listener before clicking.
    const [ download ] = await Promise.all([
      page.waitForEvent('download', { timeout: 10_000 }),
      dlBtn.click(),
    ]);

    // Download should be triggered; filename should contain "activity" and the tab name.
    const filename = download.suggestedFilename();
    expect(filename, `download filename contains "activity": "${filename}"`).toMatch(/activity/i);
    expect(filename, `download filename is a CSV: "${filename}"`).toMatch(/\.csv$/i);
  });
});

// ── Filter state persists across tab switches ─────────────────────────────

test.describe('filter state persists across tab switches', () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  test.setTimeout(60_000);

  test('/activity page: level filter value persists when switching between tabbed views', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/activity`, { waitUntil: 'domcontentloaded' });

    const header = page.locator('.page-header');
    // Wait for LogPanel mount via .lp-card-btns; use .log-tab-row for clickTab.
    await expect(page.locator('.lp-card-btns').first(), '.lp-card-btns on /activity').toBeVisible({ timeout: 20_000 });
    const panel = page.locator('.log-tab-row').first();

    // Navigate to Agents tab (level filter visible).
    await clickTab(panel, /agents?/i);
    await expect(header.locator('.act-level-sel'), 'level filter visible on Agents tab').toBeVisible({ timeout: 5_000 });

    // Change level filter to "error".
    await header.locator('.act-level-sel').selectOption('error');
    const selectedAfterChange = await header.locator('.act-level-sel').inputValue();
    expect(selectedAfterChange, 'level filter set to "error"').toBe('error');

    // Switch to System tab — level filter should still show "error".
    await clickTab(panel, /system/i);
    await expect(header.locator('.act-level-sel'), 'level filter visible on System tab').toBeVisible({ timeout: 5_000 });
    const selectedOnSystem = await header.locator('.act-level-sel').inputValue();
    expect(selectedOnSystem, 'level filter value persists on System tab').toBe('error');

    // Switch to Orders tab — level filter should be hidden, but state is preserved.
    await clickTab(panel, /orders?/i);
    await expect(header.locator('.act-level-sel'), 'level filter hidden on Orders tab').toHaveCount(0, { timeout: 5_000 });

    // Switch back to Agents tab — level filter re-appears with "error" still set.
    await clickTab(panel, /agents?/i);
    await expect(header.locator('.act-level-sel'), 'level filter visible on Agents tab again').toBeVisible({ timeout: 5_000 });
    const selectedBackOnAgents = await header.locator('.act-level-sel').inputValue();
    expect(selectedBackOnAgents, 'level filter still "error" after round-trip').toBe('error');
  });
});
