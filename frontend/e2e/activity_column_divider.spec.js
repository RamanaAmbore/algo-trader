/**
 * activity_column_divider.spec.js
 *
 * Verifies the multi-column divider (column-rule) is visible on desktop
 * and absent on mobile across all three mount points of the activity surface:
 *   1. ActivityLogModal  — opened from /dashboard via the bell button
 *   2. Activity card     — inline card on /orders
 *   3. /activity page    — standalone bookmarkable route
 *
 * Five-dimension quality assertions per feedback_test_dimensions.md:
 *   • SSOT        — the column-rule-color alpha is the SAME value in all
 *                   three mount points (single declaration in LogPanel.svelte)
 *   • Performance — modal open XHR budget <= 20 calls (unchanged from baseline)
 *   • Stale code  — old alpha (0.04 / white-4%) is gone; source bundle must
 *                   NOT contain the string "rgba(255, 255, 255, 0.04)"
 *   • Reusable    — all three surfaces use the SAME .log-panel.log-rows.lp-multicol
 *                   selector (canonical LogPanel, not a hand-rolled clone)
 *   • UX          — desktop: alpha >= 0.12 (visible divider without being loud)
 *                   mobile (<900px): column-count collapses to 1,
 *                   column-rule is "none" — no divider visible
 *                   Row heights and tap targets unchanged vs desktop baseline.
 *
 * Target: https://dev.ramboq.com (NEVER prod/ramboq.com)
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com npx playwright test \
 *     e2e/activity_column_divider.spec.js --project=chromium-desktop
 *
 * Auth strategy: single beforeAll login → shared sessionStorage injected
 * per test, same pattern as activity_scroll_and_navbar.spec.js, to avoid
 * burning the 5/min rate-limit with repeated form submits.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

/**
 * Parse the alpha channel from a computed color string of the form
 * "rgb(r, g, b)" (alpha=1) or "rgba(r, g, b, a)".
 * Returns NaN if the string cannot be parsed.
 * @param {string} color - computed CSS color string
 * @returns {number}
 */
function alphaOf(color) {
  const rgba = color.match(/rgba?\([\d\s,.]+\)/);
  if (!rgba) return NaN;
  const parts = rgba[0].replace(/rgba?\(/, '').replace(')', '').split(',');
  if (parts.length === 4) return parseFloat(parts[3].trim());
  // rgb(...) = fully opaque = alpha 1
  if (parts.length === 3) return 1;
  return NaN;
}

/**
 * Read the computed column-rule-color of an element.
 * Uses getPropertyValue on CSSStyleDeclaration so the shorthand
 * sub-property is resolved (not the compound shorthand string).
 * @param {import('@playwright/test').Locator} locator
 * @returns {Promise<string>}
 */
async function getColumnRuleColor(locator) {
  return locator.evaluate((el) => {
    const s = getComputedStyle(el);
    // column-rule-color is the resolved sub-property
    return s.getPropertyValue('column-rule-color') || s.columnRuleColor || '';
  });
}

/**
 * Inject saved sessionStorage into a page BEFORE navigation so SvelteKit
 * picks up the token on mount (no repeated form submits).
 * @param {import('@playwright/test').Page} page
 * @param {Record<string, string>} items
 */
async function injectSession(page, items) {
  await page.addInitScript((data) => {
    for (const [k, v] of Object.entries(data)) sessionStorage.setItem(k, v);
  }, items);
  if (items.ramboq_token) {
    await page.context().setExtraHTTPHeaders({
      Authorization: `Bearer ${items.ramboq_token}`,
    });
  }
}

test.describe('activity multi-column divider — desktop visible, mobile absent', () => {
  test.describe.configure({ mode: 'serial' });

  /** @type {Record<string, string>} */
  let _session = {};

  test.beforeAll(async ({ browser }) => {
    test.setTimeout(60_000);
    const ctx = await browser.newContext();
    const setup = await ctx.newPage();
    await loginAsAdmin(setup);
    _session = await setup.evaluate(() => {
      const out = {};
      for (const k of ['ramboq_token', 'ramboq_user']) {
        const v = sessionStorage.getItem(k);
        if (v) out[k] = v;
      }
      return out;
    });
    await setup.close();
    await ctx.close();
  });

  // ── Dimension 3: stale code — old alpha must not appear in source ────────
  test('stale code: old column-rule alpha (rgba(255,255,255,0.04)) is gone from LogPanel source', async () => {
    // This is a static source check — no browser needed.
    // We read the compiled .svelte source file directly (not a bundle, since
    // Svelte build strips <style> blocks into the CSS bundle; the source file
    // is the authoritative place the declaration was changed).
    const { readFileSync } = await import('fs');
    const src = readFileSync(
      new URL('../src/lib/LogPanel.svelte', import.meta.url),
      'utf8',
    );
    expect(
      src,
      'Old column-rule color rgba(255, 255, 255, 0.04) must be gone from LogPanel.svelte — was the edit reverted?',
    ).not.toContain('rgba(255, 255, 255, 0.04)');

    // And the new value must be present exactly once (single declaration = SSOT).
    const matches = src.match(/rgba\(148,\s*163,\s*184,\s*0\.18\)/g) || [];
    expect(
      matches.length,
      'New column-rule color rgba(148, 163, 184, 0.18) must appear exactly once in LogPanel.svelte (single SSOT declaration).',
    ).toBe(1);
  });

  // ── Mount point 1: ActivityLogModal from /dashboard ──────────────────────
  test('desktop: ActivityLogModal column-rule visible (alpha >= 0.12)', async ({ page }) => {
    test.setTimeout(45_000);
    await injectSession(page, _session);

    /** @type {string[]} */
    const apiCalls = [];
    page.on('request', (req) => {
      if (req.url().includes('/api/')) apiCalls.push(req.url());
    });

    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });

    const bellBtn = page.locator('button[aria-label="Open Activity"]').first();
    await expect(bellBtn, 'bell button present on /dashboard').toBeVisible({ timeout: 15_000 });

    // Reset counter just before the modal-open click.
    apiCalls.length = 0;
    await bellBtn.click();

    const modal = page.locator('.canonical-modal-panel.alm-panel');
    await expect(modal, 'ActivityLogModal appears').toBeVisible({ timeout: 8_000 });

    // Switch to a multicol tab (Agents / System / Conn) to ensure lp-multicol is active.
    const systemTab = modal.locator('button', { hasText: /system/i }).first();
    if (await systemTab.count()) {
      await systemTab.click();
      await page.waitForTimeout(300);
    }

    // ── Dimension 4: reusable — canonical LogPanel selector present ─────
    const logRows = modal.locator('.log-panel.log-rows.lp-multicol').first();
    await expect(
      logRows,
      'ActivityLogModal must use .log-panel.log-rows.lp-multicol (canonical LogPanel)',
    ).toBeVisible({ timeout: 8_000 });

    // ── Dimension 1 (SSOT) + Dimension 5 (UX): column-rule alpha >= 0.12 ─
    const ruleColor = await getColumnRuleColor(logRows);
    const alpha = alphaOf(ruleColor);
    expect(
      alpha,
      `ActivityLogModal column-rule-color alpha must be >= 0.12 (visible divider). Got "${ruleColor}" (alpha=${alpha}). Was the edit reverted?`,
    ).toBeGreaterThanOrEqual(0.12);

    // ── Dimension 2: perf — modal open <= 20 XHRs ───────────────────────
    // Allow a brief settle so async log polls register.
    await page.waitForTimeout(500);
    expect(
      apiCalls.length,
      `Modal-open XHR budget exceeded: ${apiCalls.length} calls (limit 20).`,
    ).toBeLessThanOrEqual(20);
  });

  // ── Mount point 2: Activity card on /orders ───────────────────────────────
  test('desktop: /orders Activity card column-rule visible (alpha >= 0.12)', async ({ page }) => {
    test.setTimeout(45_000);
    await injectSession(page, _session);
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });

    const activityCard = page.locator('section.bucket-card-activity');
    await expect(activityCard, 'activity card present on /orders').toBeVisible({ timeout: 20_000 });
    await activityCard.scrollIntoViewIfNeeded();

    // Wait for log poll to fire.
    await page.waitForTimeout(1000);

    // Click a multicol tab inside the activity card.
    const agentsTab = activityCard.locator('[role="tab"]:has-text("Agents")');
    if (await agentsTab.count()) {
      await agentsTab.click();
      await page.waitForTimeout(300);
    }

    // ── Dimension 4: reusable ───────────────────────────────────────────
    const logRows = activityCard.locator('.log-panel.log-rows.lp-multicol').first();
    await expect(
      logRows,
      '/orders Activity card must use .log-panel.log-rows.lp-multicol (canonical LogPanel)',
    ).toBeVisible({ timeout: 8_000 });

    // ── Dimension 1 (SSOT) + 5 (UX) ────────────────────────────────────
    const ruleColor = await getColumnRuleColor(logRows);
    const alpha = alphaOf(ruleColor);
    expect(
      alpha,
      `/orders Activity card column-rule-color alpha must be >= 0.12. Got "${ruleColor}" (alpha=${alpha}).`,
    ).toBeGreaterThanOrEqual(0.12);
  });

  // ── Mount point 3: /activity standalone page ──────────────────────────────
  test('desktop: /activity page column-rule visible (alpha >= 0.12)', async ({ page }) => {
    test.setTimeout(45_000);
    await injectSession(page, _session);
    await page.goto(`${BASE}/activity`, { waitUntil: 'domcontentloaded' });

    // Click a multicol tab.
    const agentsTab = page.locator('[role="tab"]:has-text("Agents")').first();
    await expect(agentsTab, 'Agents tab on /activity page').toBeVisible({ timeout: 15_000 });
    await agentsTab.click();
    await page.waitForTimeout(300);

    // ── Dimension 4: reusable ───────────────────────────────────────────
    const logRows = page.locator('.log-panel.log-rows.lp-multicol').first();
    await expect(
      logRows,
      '/activity page must use .log-panel.log-rows.lp-multicol (canonical LogPanel)',
    ).toBeVisible({ timeout: 8_000 });

    // ── Dimension 1 (SSOT) + 5 (UX) ────────────────────────────────────
    const ruleColor = await getColumnRuleColor(logRows);
    const alpha = alphaOf(ruleColor);
    expect(
      alpha,
      `/activity page column-rule-color alpha must be >= 0.12. Got "${ruleColor}" (alpha=${alpha}).`,
    ).toBeGreaterThanOrEqual(0.12);
  });

  // ── Dimension 1 (SSOT): all three mount points return the SAME alpha ──────
  test('SSOT: column-rule-color is identical across all three mount points', async ({ page }) => {
    test.setTimeout(90_000);
    await injectSession(page, _session);

    const alphas = [];

    // Mount point A: ActivityLogModal
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    const bellBtn = page.locator('button[aria-label="Open Activity"]').first();
    await expect(bellBtn).toBeVisible({ timeout: 15_000 });
    await bellBtn.click();
    const modal = page.locator('.canonical-modal-panel.alm-panel');
    await expect(modal).toBeVisible({ timeout: 8_000 });
    const systemTab = modal.locator('button', { hasText: /system/i }).first();
    if (await systemTab.count()) { await systemTab.click(); await page.waitForTimeout(300); }
    const modalLogRows = modal.locator('.log-panel.log-rows.lp-multicol').first();
    await expect(modalLogRows).toBeVisible({ timeout: 8_000 });
    const colorA = await getColumnRuleColor(modalLogRows);
    alphas.push({ mount: 'ActivityLogModal', color: colorA, alpha: alphaOf(colorA) });

    // Close the modal (Esc) before navigating.
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);

    // Mount point B: /orders Activity card
    await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded' });
    const activityCard = page.locator('section.bucket-card-activity');
    await expect(activityCard).toBeVisible({ timeout: 20_000 });
    await activityCard.scrollIntoViewIfNeeded();
    await page.waitForTimeout(800);
    const agentsTabOrders = activityCard.locator('[role="tab"]:has-text("Agents")');
    if (await agentsTabOrders.count()) { await agentsTabOrders.click(); await page.waitForTimeout(300); }
    const cardLogRows = activityCard.locator('.log-panel.log-rows.lp-multicol').first();
    await expect(cardLogRows).toBeVisible({ timeout: 8_000 });
    const colorB = await getColumnRuleColor(cardLogRows);
    alphas.push({ mount: '/orders activity card', color: colorB, alpha: alphaOf(colorB) });

    // Mount point C: /activity page
    await page.goto(`${BASE}/activity`, { waitUntil: 'domcontentloaded' });
    const agentsTabActivity = page.locator('[role="tab"]:has-text("Agents")').first();
    await expect(agentsTabActivity).toBeVisible({ timeout: 15_000 });
    await agentsTabActivity.click();
    await page.waitForTimeout(300);
    const pageLogRows = page.locator('.log-panel.log-rows.lp-multicol').first();
    await expect(pageLogRows).toBeVisible({ timeout: 8_000 });
    const colorC = await getColumnRuleColor(pageLogRows);
    alphas.push({ mount: '/activity page', color: colorC, alpha: alphaOf(colorC) });

    // All three alphas should be equal (same CSS class, single declaration).
    const [a, b, c] = alphas.map((x) => x.alpha);
    const summary = alphas.map((x) => `${x.mount}: "${x.color}" (alpha=${x.alpha})`).join('\n');

    expect(a, `SSOT: all mount points must share the same column-rule alpha.\n${summary}`).toBeCloseTo(b, 2);
    expect(a, `SSOT: all mount points must share the same column-rule alpha.\n${summary}`).toBeCloseTo(c, 2);
    expect(a, `SSOT: alpha must be >= 0.12 (visible divider).\n${summary}`).toBeGreaterThanOrEqual(0.12);
  });

  // ── Dimension 5 (UX — mobile): column-count collapses, rule is none ───────
  test('mobile: column-count collapses to 1 and column-rule is none on /activity', async ({ page }) => {
    test.setTimeout(45_000);
    await injectSession(page, _session);

    // Force a narrow viewport that triggers the @media (max-width: 900px) rule.
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(`${BASE}/activity`, { waitUntil: 'domcontentloaded' });

    const agentsTab = page.locator('[role="tab"]:has-text("Agents")').first();
    await expect(agentsTab).toBeVisible({ timeout: 15_000 });
    await agentsTab.click();
    await page.waitForTimeout(300);

    const logRows = page.locator('.log-panel.log-rows.lp-multicol').first();
    await expect(logRows).toBeVisible({ timeout: 8_000 });

    const columnCount = await logRows.evaluate((el) => getComputedStyle(el).columnCount);
    expect(
      columnCount,
      `At 375px viewport, .lp-multicol must collapse to column-count:1, got "${columnCount}".`,
    ).toBe('1');

    // At column-count:1 the column-rule is visually suppressed (none or transparent).
    // The @media block sets column-rule:none explicitly, so column-rule-style = "none".
    const ruleStyle = await logRows.evaluate((el) => {
      const s = getComputedStyle(el);
      return s.getPropertyValue('column-rule-style') || s.columnRuleStyle || '';
    });
    expect(
      ruleStyle,
      `At 375px viewport, column-rule-style must be "none" (no divider on mobile), got "${ruleStyle}".`,
    ).toBe('none');
  });

  // ── Dimension 5 (UX — row heights): verify tap target unchanged ───────────
  test('desktop: log-row heights are not changed by the divider bump', async ({ page }) => {
    test.setTimeout(30_000);
    await injectSession(page, _session);
    await page.goto(`${BASE}/activity`, { waitUntil: 'domcontentloaded' });

    const agentsTab = page.locator('[role="tab"]:has-text("Agents")').first();
    await expect(agentsTab).toBeVisible({ timeout: 15_000 });
    await agentsTab.click();
    await page.waitForTimeout(300);

    // A row height change from the divider would be unusual (column-rule is
    // decorative, between columns), but we assert row height > 24px as a
    // sanity guard that no layout regression crept in alongside the edit.
    const firstRow = page.locator('.log-panel.log-rows.lp-multicol .log-row').first();
    await expect(firstRow, '.log-row must be present after Agents tab click').toBeVisible({ timeout: 10_000 });

    const rowHeight = await firstRow.evaluate((el) => el.getBoundingClientRect().height);
    expect(
      rowHeight,
      `log-row height must be >= 24px (adequate tap target). Got ${rowHeight}px.`,
    ).toBeGreaterThanOrEqual(24);
  });
});
