/**
 * strategy_detail_metrics_infohint.spec.js
 *
 * /strategies/[id]'s "Risk-adjusted metrics" grid (Sharpe, Sortino,
 * Max DD, Max DD %, Win rate, Daily avg, Daily vol, Cumulative) used
 * plain `title=` attributes on each `.metric-lbl` with no InfoHint
 * import at all. Fix: each label now carries an `<InfoHint popup>`
 * chip with the same wording the `title=` attribute used to carry —
 * same house pattern as ChartWorkspace.svelte's Greeks strip and
 * OptionsPayoff.svelte's stat overlay (sibling fix, same batch).
 *
 * Five quality dimensions:
 *  1. SSOT    — n/a (display-only copy).
 *  2. Perf    — n/a.
 *  3. Stale   — no `.metric-lbl` carries a `title=` attribute any
 *               more (would show a second, native tooltip stacked on
 *               the new InfoHint popover).
 *  4. Reuse   — exercises the shared InfoHint open/close +
 *               `[role="tooltip"]` contract.
 *  5. UX      — all 8 metrics get a chip, not a partial subset.
 *
 * Run:
 *   npx playwright test e2e/strategy_detail_metrics_infohint.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const METRIC_LABELS = ['Sharpe', 'Sortino', 'Max DD', 'Max DD %', 'Win rate', 'Daily avg', 'Daily vol', 'Cumulative'];

test.describe('/strategies/[id] — Risk-adjusted metrics labels get InfoHint chips', () => {
  test('all 8 metric labels carry a clickable InfoHint chip, none left on a bare title=', async ({ page }) => {
    await loginAsAdmin(page);

    // Resolve a real strategy id via the same REST endpoint the page
    // itself calls (fetchStrategies) — avoids hardcoding a fixture id
    // that could rot as seed data changes. loginAsAdmin already set
    // the Authorization header on the browser context, so page.request
    // (Playwright's APIRequestContext) carries it automatically.
    const res = await page.request.get('/api/strategies/');
    const body = res.ok() ? await res.json() : null;
    const strategies = Array.isArray(body) ? body : (body?.rows ?? []);

    if (!Array.isArray(strategies) || strategies.length === 0) {
      test.skip(true, 'No strategies exist on this environment — nothing to check live');
      return;
    }

    const id = strategies[0].id;
    await page.goto(`/strategies/${id}`, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const metricsGrid = page.locator('.strat-metrics-grid');
    const gridVisible = await metricsGrid.waitFor({ state: 'visible', timeout: 15_000 }).then(() => true).catch(() => false);
    if (!gridVisible) {
      test.skip(true, 'Metrics grid never rendered (needs ≥2 daily snapshots per the empty-state copy) — nothing to check live');
      return;
    }

    // Stale check first — no leftover title= on any metric label.
    await expect(page.locator('.metric-lbl[title]')).toHaveCount(0);

    for (const label of METRIC_LABELS) {
      const metricLbl = page.locator('.metric-lbl', { has: page.locator('.metric-lbl-txt', { hasText: label }) }).first();
      await expect(metricLbl).toBeVisible();
      const infoBtn = metricLbl.locator('button.info-btn');
      await expect(infoBtn).toHaveCount(1);
    }

    // Spot-check one chip's full open/close + wording contract.
    const sharpeLbl = page.locator('.metric-lbl', { has: page.locator('.metric-lbl-txt', { hasText: 'Sharpe' }) }).first();
    const sharpeBtn = sharpeLbl.locator('button.info-btn');
    await sharpeBtn.click();
    const popover = page.locator('[role="tooltip"]').first();
    await expect(popover).toBeVisible();
    await expect(popover).toContainText('Annualised Sharpe ratio');
    await sharpeBtn.click();
    await expect(page.locator('[role="tooltip"]')).toHaveCount(0);
  });
});
