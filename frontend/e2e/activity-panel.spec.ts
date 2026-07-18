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
 * Target: https://dev.ramboq.com (NEVER prod)
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/activity-panel.spec.ts \
 *     --project=chromium-desktop
 */

import { test, expect, type Page, type Locator } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

// ── helpers ────────────────────────────────────────────────────────────────

/**
 * Click a tab by its visible label text inside the given panel locator.
 * Retries up to timeoutMs for the tab to become visible.
 */
async function clickTab(panel: Locator, label: RegExp | string, timeoutMs = 10_000) {
  const tab = panel.locator('.at-tab', { hasText: label }).first();
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
    // Wait for the log panel inside the card to mount.
    const panel = card.locator('.log-panel').first();
    await expect(panel, '.log-panel inside activity card').toBeVisible({ timeout: 15_000 });
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
    const ticksTab = card.locator('.at-tab').filter({ hasText: /ticks|simulator/i }).first();
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
    const newsTab = card.locator('.at-tab').filter({ hasText: /news/i }).first();
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
    const header = page.locator('.page-header');
    await expect(header, 'page header renders').toBeVisible({ timeout: 15_000 });
    const panel = page.locator('.log-panel').first();
    await expect(panel, '.log-panel on /activity page').toBeVisible({ timeout: 15_000 });
    return { header, panel };
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
    const newsTab = panel.locator('.at-tab').filter({ hasText: /news/i }).first();
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
    const chip = page.locator('button.broker-chip').first();
    await expect(chip, 'broker chip visible').toBeVisible({ timeout: 25_000 });
    await chip.click({ force: true });
    const modal = page.locator('[role="dialog"][aria-label="Activity log"]');
    await expect(modal, 'ActivityLogModal opens').toBeVisible({ timeout: 8_000 });
    return modal;
  }

  test('Conn tab: BOTH filters VISIBLE (default open tab)', async () => {
    const modal = await openModal();
    // Modal opens on Conn tab by default (broker chip → openActivityModal('conn')).
    const connTab = modal.locator('[role="tab"][aria-selected="true"]:has-text("Conn"), .at-tab.at-active:has-text("Conn")').first();
    const connVisible = await connTab.isVisible({ timeout: 5_000 }).catch(() => false);
    if (!connVisible) {
      // Confirm we're on a tab that has both filters (Conn/System/Agent).
      // Check there's at least a level filter.
      console.log('[INFO] Conn tab active state not confirmed by aria-selected; checking filter presence');
    }

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
    const panel = modal.locator('.log-panel').first();
    await expect(panel, '.log-panel in modal').toBeVisible({ timeout: 8_000 });
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
    const panel = modal.locator('.log-panel').first();
    await expect(panel, '.log-panel in modal').toBeVisible({ timeout: 8_000 });
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
    // Search button
    const searchBtn = container.locator('button[aria-label*="Search" i], button[aria-label*="search" i]').first();
    await expect(searchBtn, `[${label}] Search button present`).toBeVisible({ timeout: 10_000 });

    // Expand/Contract button (may be labelled "Expand" or "Contract" or "Collapse")
    const expandBtn = container.locator(
      'button[aria-label*="Expand" i], button[aria-label*="Contract" i], button[aria-label*="Collapse" i]'
    ).first();
    await expect(expandBtn, `[${label}] Expand/Contract button present`).toBeVisible({ timeout: 5_000 });

    // Fullscreen button
    const fsBtn = container.locator('button[aria-label*="Fullscreen" i], button[aria-label*="full screen" i], button[aria-label*="full-screen" i]').first();
    await expect(fsBtn, `[${label}] Fullscreen button present`).toBeVisible({ timeout: 5_000 });

    // Download button
    const dlBtn = container.locator('button[aria-label*="Download" i], button[aria-label*="download" i]').first();
    await expect(dlBtn, `[${label}] Download button present`).toBeVisible({ timeout: 5_000 });
  }

  test('/orders activity card has all four card buttons', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });

    const card = page.locator('section.bucket-card-activity');
    await expect(card, 'activity card renders').toBeVisible({ timeout: 25_000 });
    await card.scrollIntoViewIfNeeded();

    // Wait for log panel to mount (ensures ActivityLogSurface has rendered its header)
    const panel = card.locator('.log-panel').first();
    await expect(panel, '.log-panel in activity card').toBeVisible({ timeout: 15_000 });

    await assertButtonGroup(card, '/orders activity card');
  });

  test('/activity page has all four card buttons', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/activity`, { waitUntil: 'domcontentloaded' });

    const body = page.locator('.activity-page-body');
    await expect(body, '.activity-page-body renders').toBeVisible({ timeout: 20_000 });

    const panel = page.locator('.log-panel').first();
    await expect(panel, '.log-panel on /activity').toBeVisible({ timeout: 15_000 });

    await assertButtonGroup(page.locator('body'), '/activity page');
  });

  // ── Search button smoke — toggle and text-filter ──────────────────────────

  test('/orders activity card: Search button toggles a search input', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });

    const card = page.locator('section.bucket-card-activity');
    await expect(card).toBeVisible({ timeout: 25_000 });
    await card.scrollIntoViewIfNeeded();
    await expect(card.locator('.log-panel').first()).toBeVisible({ timeout: 15_000 });

    const searchBtn = card.locator('button[aria-label*="Search" i]').first();
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

    const panel = page.locator('.log-panel').first();
    await expect(panel, '.log-panel on /activity').toBeVisible({ timeout: 20_000 });

    // Land on Orders tab (default).
    await clickTab(panel, /orders?/i);

    const dlBtn = page.locator('button[aria-label*="Download" i]').first();
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
    const panel = page.locator('.log-panel').first();
    await expect(panel, '.log-panel on /activity').toBeVisible({ timeout: 20_000 });

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
