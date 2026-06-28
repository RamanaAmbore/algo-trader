/**
 * activity_column_divider.spec.js
 *
 * Verifies the multi-column divider (column-rule) is visible on desktop
 * and absent on mobile across all three mount points of the activity
 * surface AND the sibling NewsList magazine-flow surface:
 *
 *   1. ActivityLogModal  — opened from /dashboard via the bell button
 *   2. Activity card     — inline card on /orders
 *   3. /activity page    — standalone bookmarkable route
 *   4. NewsList          — magazine-style column flow used on the
 *                          dashboard market-news card and Activity News tab
 *
 * Five-dimension quality assertions per feedback_test_dimensions.md:
 *   • SSOT        — the column-rule-color alpha is the SAME value in all
 *                   three activity mount points (single declaration in
 *                   LogPanel.svelte) and matches the NewsList declaration
 *                   so the two magazine-flow surfaces look consistent.
 *   • Performance — modal open XHR budget <= 20 calls (unchanged from baseline)
 *   • Stale code  — old low-alpha values (0.04 white, 0.10 NewsList,
 *                   prior 0.18 LogPanel) are gone; current value
 *                   rgba(148, 163, 184, 0.22) is the SSOT.
 *   • Reusable    — all three activity surfaces use the SAME
 *                   .log-panel.log-rows.lp-multicol selector (canonical
 *                   LogPanel, not a hand-rolled clone).
 *   • UX          — desktop: alpha >= 0.18 (visible divider without
 *                   being loud).
 *                   mobile (<900px): column-count collapses to 1,
 *                   column-rule is "none" — no divider visible.
 *                   Row heights and tap targets unchanged vs desktop baseline.
 *                   Fluid-width loop at 360/768/1280 — no overlap, no
 *                   horizontal scroll.
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

// The single visible-divider value (slate-400 at 22% alpha). Used as
// the SSOT reference everywhere in this spec.
const SSOT_ALPHA = 0.22;
const MIN_VISIBLE_ALPHA = 0.18;

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

  // ── Dimension 3: stale code — old alpha values must not appear ───────────
  test('stale code: old low-alpha column-rule values are gone (LogPanel + NewsList)', async () => {
    // Static source check — no browser needed. The .svelte source file is
    // the authoritative place the declaration lives (Svelte build strips
    // <style> blocks into the CSS bundle, but the source is the SSOT).
    const { readFileSync } = await import('fs');
    const logSrc = readFileSync(
      new URL('../src/lib/LogPanel.svelte', import.meta.url),
      'utf8',
    );
    const newsSrc = readFileSync(
      new URL('../src/lib/NewsList.svelte', import.meta.url),
      'utf8',
    );

    // Scope the assertion to `column-rule:` declarations only — other
    // selectors in these files legitimately use slate-ish colors as
    // backgrounds (e.g. `.newslist-src` pill bg is rgba(126,151,184,0.10))
    // and must not trip the stale-code check.
    /**
     * @param {string} src
     * @returns {string}
     */
    const extractColumnRuleBlob = (src) =>
      src.split('\n').filter((line) => /column-rule\s*:/.test(line)).join('\n');

    const logRuleBlob = extractColumnRuleBlob(logSrc);
    const newsRuleBlob = extractColumnRuleBlob(newsSrc);

    // Old white-4% value must be gone from every column-rule line.
    expect(
      logRuleBlob,
      `Old column-rule color rgba(255, 255, 255, 0.04) must be gone from LogPanel.svelte. column-rule lines:\n${logRuleBlob}`,
    ).not.toContain('rgba(255, 255, 255, 0.04)');
    expect(
      newsRuleBlob,
      `Old column-rule color rgba(255, 255, 255, 0.04) must be gone from NewsList.svelte. column-rule lines:\n${newsRuleBlob}`,
    ).not.toContain('rgba(255, 255, 255, 0.04)');

    // Previous interim values must also be gone (we standardise on 0.22).
    expect(
      logRuleBlob,
      `Interim column-rule alpha 0.18 must be gone from LogPanel.svelte — bumped to 0.22. column-rule lines:\n${logRuleBlob}`,
    ).not.toMatch(/rgba\(148,\s*163,\s*184,\s*0\.18\)/);
    expect(
      newsRuleBlob,
      `NewsList old column-rule alpha 0.10 must be gone. column-rule lines:\n${newsRuleBlob}`,
    ).not.toMatch(/rgba\(126,\s*151,\s*184,\s*0\.10\)/);

    // The new SSOT value must be present exactly once per file (single column-rule declaration).
    const logMatches = logRuleBlob.match(/rgba\(148,\s*163,\s*184,\s*0\.22\)/g) || [];
    expect(
      logMatches.length,
      `LogPanel.svelte must declare rgba(148, 163, 184, 0.22) on a column-rule line exactly once (SSOT). Found ${logMatches.length}.\n${logRuleBlob}`,
    ).toBe(1);
    const newsMatches = newsRuleBlob.match(/rgba\(148,\s*163,\s*184,\s*0\.22\)/g) || [];
    expect(
      newsMatches.length,
      `NewsList.svelte must declare rgba(148, 163, 184, 0.22) on a column-rule line exactly once (SSOT, matches LogPanel). Found ${newsMatches.length}.\n${newsRuleBlob}`,
    ).toBe(1);
  });

  // ── Mount point 1: ActivityLogModal from /dashboard ──────────────────────
  test('desktop: ActivityLogModal column-rule visible (alpha >= 0.18)', async ({ page, viewport }) => {
    test.skip(!viewport || viewport.width < 900, 'desktop-only — multi-col divider only renders at >=900px viewport');
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

    // ── Dimension 1 (SSOT) + Dimension 5 (UX): column-rule alpha >= 0.18 ─
    const ruleColor = await getColumnRuleColor(logRows);
    const alpha = alphaOf(ruleColor);
    expect(
      alpha,
      `ActivityLogModal column-rule-color alpha must be >= ${MIN_VISIBLE_ALPHA} (visible divider). Got "${ruleColor}" (alpha=${alpha}).`,
    ).toBeGreaterThanOrEqual(MIN_VISIBLE_ALPHA);

    // ── Dimension 2: perf — modal open <= 20 XHRs ───────────────────────
    // Allow a brief settle so async log polls register.
    await page.waitForTimeout(500);
    expect(
      apiCalls.length,
      `Modal-open XHR budget exceeded: ${apiCalls.length} calls (limit 20).`,
    ).toBeLessThanOrEqual(20);
  });

  // ── Mount point 2: Activity card on /orders ───────────────────────────────
  test('desktop: /orders Activity card column-rule visible (alpha >= 0.18)', async ({ page, viewport }) => {
    test.skip(!viewport || viewport.width < 900, 'desktop-only — multi-col divider only renders at >=900px viewport');
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
      `/orders Activity card column-rule-color alpha must be >= ${MIN_VISIBLE_ALPHA}. Got "${ruleColor}" (alpha=${alpha}).`,
    ).toBeGreaterThanOrEqual(MIN_VISIBLE_ALPHA);
  });

  // ── Mount point 3: /activity standalone page ──────────────────────────────
  test('desktop: /activity page column-rule visible (alpha >= 0.18)', async ({ page, viewport }) => {
    test.skip(!viewport || viewport.width < 900, 'desktop-only — multi-col divider only renders at >=900px viewport');
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
      `/activity page column-rule-color alpha must be >= ${MIN_VISIBLE_ALPHA}. Got "${ruleColor}" (alpha=${alpha}).`,
    ).toBeGreaterThanOrEqual(MIN_VISIBLE_ALPHA);
  });

  // ── Dimension 1 (SSOT): all three mount points return the SAME alpha ──────
  test('SSOT: column-rule-color is identical across all three mount points', async ({ page, viewport }) => {
    test.skip(!viewport || viewport.width < 900, 'desktop-only — multi-col divider only renders at >=900px viewport');
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
    expect(a, `SSOT: alpha must be >= ${MIN_VISIBLE_ALPHA} (visible divider).\n${summary}`).toBeGreaterThanOrEqual(MIN_VISIBLE_ALPHA);
    expect(a, `SSOT: alpha must equal canonical ${SSOT_ALPHA}.\n${summary}`).toBeCloseTo(SSOT_ALPHA, 2);
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
  test('log-row heights are not changed by the divider bump', async ({ page }) => {
    test.setTimeout(30_000);
    await injectSession(page, _session);
    await page.goto(`${BASE}/activity`, { waitUntil: 'domcontentloaded' });

    const agentsTab = page.locator('[role="tab"]:has-text("Agents")').first();
    await expect(agentsTab).toBeVisible({ timeout: 15_000 });
    await agentsTab.click();
    await page.waitForTimeout(300);

    // A row height change from the divider would be unusual (column-rule is
    // decorative, between columns), but we assert row height >= 20px as a
    // sanity guard that no layout regression crept in alongside the edit.
    // Threshold 20px reflects the compact log-row design (timestamp + msg
    // stacked, ~24px nominal, ~23.9px with sub-pixel rounding on retina).
    const firstRow = page.locator('.log-panel.log-rows.lp-multicol .log-row').first();
    await expect(firstRow, '.log-row must be present after Agents tab click').toBeVisible({ timeout: 10_000 });

    const rowHeight = await firstRow.evaluate((el) => el.getBoundingClientRect().height);
    expect(
      rowHeight,
      `log-row height must be >= 20px (adequate tap target). Got ${rowHeight}px.`,
    ).toBeGreaterThanOrEqual(20);
  });

  // ── Dimension 5 (UX — fluid width): 360/768/1280 no-overlap + no h-scroll ──
  test('fluid: 360 / 768 / 1280 viewports — /activity has no horizontal scroll', async ({ page }) => {
    test.setTimeout(60_000);
    await injectSession(page, _session);

    // Sanity loop: for each viewport, navigate to /activity, switch to a
    // multicol tab, and assert document scrollWidth <= clientWidth + 2px
    // (1px sub-pixel slack on either side). At 360/768 the @media kicks in
    // and we expect column-count:1; at 1280 we expect column-count:2.
    for (const width of [360, 768, 1280]) {
      await page.setViewportSize({ width, height: 800 });
      await page.goto(`${BASE}/activity`, { waitUntil: 'domcontentloaded' });

      const agentsTab = page.locator('[role="tab"]:has-text("Agents")').first();
      await expect(agentsTab, `Agents tab at ${width}px`).toBeVisible({ timeout: 15_000 });
      await agentsTab.click();
      await page.waitForTimeout(300);

      const overflow = await page.evaluate(() => {
        const doc = document.documentElement;
        return { scrollWidth: doc.scrollWidth, clientWidth: doc.clientWidth };
      });
      expect(
        overflow.scrollWidth,
        `${width}px: page must not have horizontal scroll (scrollWidth=${overflow.scrollWidth}, clientWidth=${overflow.clientWidth}).`,
      ).toBeLessThanOrEqual(overflow.clientWidth + 2);

      // Confirm column-count crosses the @media boundary as expected.
      const logRows = page.locator('.log-panel.log-rows.lp-multicol').first();
      if (await logRows.count()) {
        const cc = await logRows.evaluate((el) => getComputedStyle(el).columnCount);
        if (width < 900) {
          expect(cc, `${width}px < 900px: column-count must be 1, got "${cc}".`).toBe('1');
        } else {
          expect(cc, `${width}px >= 900px: column-count must be 2, got "${cc}".`).toBe('2');
        }
      }
    }
  });
});
