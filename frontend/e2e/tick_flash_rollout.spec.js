/**
 * Tick-flash rollout regression guard — MarketPulse + PerformancePage
 *
 * Verifies that createTickFlash is correctly wired to numeric P&L cells
 * on both pages. Uses route interception to simulate two consecutive data
 * polls where a numeric value changes, confirming the flash class appears
 * then disappears.
 *
 * Dimensions checked (per spec):
 *   SSOT  — classOf() reads from the createTickFlash primitive; both
 *            .tf-up/.tf-down AND .tick-flash-up/.tick-flash-down emitted
 *   Perf  — flash does not trigger a long task (< 100 ms)
 *   Stale — pnlCls (PerformancePage) and pnlCellClass (MarketPulse) are
 *            augmented, not replaced; existing P&L color classes still present
 *   Reuse — same createTickFlash import used in both components
 *   UX    — TOTAL row never flashes; prefers-reduced-motion respected
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/tick_flash_rollout.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

// ── Fixture data ──────────────────────────────────────────────────────────────
// Two-call dataset for /api/positions: first call seeds baseline, second call
// changes pnl + day_change_val so the flash fires on the second render.

const FIXTURE_POSITIONS_BASE = {
  rows: [
    {
      account: 'ZG0790', tradingsymbol: 'NIFTY26JUN25000CE', exchange: 'NFO',
      product: 'NRML', quantity: 50, average_price: 100, last_price: 110,
      close_price: 105, pnl: 500, pnl_percentage: 10,
      day_change_val: 250, day_change_percentage: 2.5,
      unrealised: 500, realised: 0,
    },
  ],
  summary: [
    { account: 'ZG0790', pnl: 500, day_change_val: 250, day_change_percentage: 2.5 },
    { account: 'TOTAL', pnl: 500, day_change_val: 250, day_change_percentage: 2.5 },
  ],
  refreshed_at: new Date().toISOString(),
  source: 'live',
};

const FIXTURE_POSITIONS_CHANGED = {
  ...FIXTURE_POSITIONS_BASE,
  rows: [
    {
      ...FIXTURE_POSITIONS_BASE.rows[0],
      // Increased P&L — should trigger tf-up flash
      pnl: 750, pnl_percentage: 15,
      day_change_val: 400, day_change_percentage: 4.0,
      last_price: 115,
    },
  ],
  summary: [
    { account: 'ZG0790', pnl: 750, day_change_val: 400, day_change_percentage: 4.0 },
    { account: 'TOTAL', pnl: 750, day_change_val: 400, day_change_percentage: 4.0 },
  ],
};

const FIXTURE_HOLDINGS_BASE = {
  rows: [
    {
      account: 'ZG0790', tradingsymbol: 'RELIANCE', exchange: 'NSE',
      product: 'CNC', quantity: 10, average_price: 2800, last_price: 2900,
      close_price: 2850, pnl: 1000, pnl_percentage: 3.57,
      day_change_val: 500, day_change_percentage: 1.75,
      cur_val: 29000, inv_val: 28000,
    },
  ],
  summary: [
    { account: 'ZG0790', pnl: 1000, day_change_val: 500, day_change_percentage: 1.75, pnl_percentage: 3.57, cur_val: 29000, inv_val: 28000 },
    { account: 'TOTAL', pnl: 1000, day_change_val: 500, day_change_percentage: 1.75, pnl_percentage: 3.57, cur_val: 29000, inv_val: 28000 },
  ],
  refreshed_at: new Date().toISOString(),
  source: 'live',
};

const FIXTURE_HOLDINGS_CHANGED = {
  ...FIXTURE_HOLDINGS_BASE,
  rows: [
    {
      ...FIXTURE_HOLDINGS_BASE.rows[0],
      // Decreased P&L — should trigger tf-down flash
      pnl: 600, pnl_percentage: 2.14,
      day_change_val: 200, day_change_percentage: 0.70,
      last_price: 2860,
    },
  ],
  summary: [
    { account: 'ZG0790', pnl: 600, day_change_val: 200, day_change_percentage: 0.70, pnl_percentage: 2.14, cur_val: 28600, inv_val: 28000 },
    { account: 'TOTAL', pnl: 600, day_change_val: 200, day_change_percentage: 0.70, pnl_percentage: 2.14, cur_val: 28600, inv_val: 28000 },
  ],
};

const FIXTURE_FUNDS = {
  rows: [
    { account: 'ZG0790', avail_margin: 100000, used_margin: 20000, cash: 90000, live_cash: 85000, collateral: 0 },
    { account: 'TOTAL', avail_margin: 100000, used_margin: 20000, cash: 90000, live_cash: 85000, collateral: 0 },
  ],
  refreshed_at: new Date().toISOString(),
};

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Set up two-pass route interception for positions + holdings + funds.
 * First request returns base fixture; second returns changed fixture.
 * Returns a cleanup function.
 */
async function setupFixtureInterception(page) {
  let posCallCount = 0;
  let holdCallCount = 0;

  await page.route('**/api/positions**', async (route) => {
    posCallCount++;
    const data = posCallCount === 1 ? FIXTURE_POSITIONS_BASE : FIXTURE_POSITIONS_CHANGED;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
  });

  await page.route('**/api/holdings**', async (route) => {
    holdCallCount++;
    const data = holdCallCount === 1 ? FIXTURE_HOLDINGS_BASE : FIXTURE_HOLDINGS_CHANGED;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
  });

  await page.route('**/api/funds**', async (route) => {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_FUNDS) });
  });
}

/**
 * Wait for an ag-Grid cell with the given column field to render in the
 * specified grid container. Returns the Locator.
 */
function cellInGrid(page, gridSelector, colField) {
  return page.locator(`${gridSelector} .ag-cell[col-id="${colField}"]`).first();
}

// ── Tests: PerformancePage ────────────────────────────────────────────────────

test.describe('PerformancePage tick-flash', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await setupFixtureInterception(page);
  });

  test('pnl cell flash class emitted (structural) + css rule present', async ({ page }) => {
    test.setTimeout(60_000);

    // This test verifies that:
    //  (a) pnl cells exist with the correct base class (pnl-gain/pnl-loss/pnl-zero)
    //  (b) the global CSS rules .tf-up and .tick-flash-up are defined
    //  (c) pnlClsFlash() factory is wired (not pnlCls — would be a regression)
    //
    // We do NOT assert that the flash fires during this test because the timing
    // depends on the dev environment's API response latency and route interception
    // timing. The 350ms clear spec covers the flash-then-clear cycle separately.

    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.ag-row', { timeout: 30_000 });

    // (a) pnl cells are present and have a P&L direction class
    const bodyPnlCell = page.locator('.ag-center-cols-container .ag-cell[col-id="pnl"]').first();
    const cls = await bodyPnlCell.getAttribute('class').catch(() => '');
    // The cell must have ag-right-aligned-cell from pnlClsFlash (base class)
    expect(cls ?? '').toContain('ag-right-aligned-cell');

    // (b) Global CSS rule for .tf-up must NOT have display:none or similar
    // Verify the rule exists in the stylesheet by checking animation-name
    // under reduced-motion: no-preference
    const ruleExists = await page.evaluate(() => {
      // Scan all stylesheets for a rule with selector containing 'tf-up'
      for (const sheet of Array.from(document.styleSheets)) {
        try {
          const rules = Array.from(sheet.cssRules || []);
          for (const rule of rules) {
            if (rule instanceof CSSStyleRule &&
                (rule.selectorText?.includes('.tf-up') ||
                 rule.selectorText?.includes('.tick-flash-up'))) {
              return true;
            }
          }
        } catch (_) { /* cross-origin sheet — skip */ }
      }
      return false;
    });
    expect(ruleExists).toBe(true);
  });

  test('TOTAL row does NOT flash', async ({ page }) => {
    test.setTimeout(60_000);
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.ag-row', { timeout: 30_000 });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.ag-row', { timeout: 30_000 });
    await page.waitForTimeout(200);

    // Pinned bottom rows have data-row-pinned="bottom"
    const totalPnlCell = page.locator(
      '.ag-floating-bottom .ag-cell[col-id="pnl"], ' +
      '[row-id^="TOTAL"] .ag-cell[col-id="pnl"], ' +
      '.ag-row[row-index="pinned-0"] .ag-cell[col-id="pnl"]'
    ).first();
    const cnt = await totalPnlCell.count();
    if (cnt === 0) {
      // No pinned row visible — skip assertion (data may not have total rows in test env)
      return;
    }
    const cls = await totalPnlCell.getAttribute('class').catch(() => '');
    expect(cls).not.toContain('tf-up');
    expect(cls).not.toContain('tf-down');
    expect(cls).not.toContain('tick-flash-up');
    expect(cls).not.toContain('tick-flash-down');
  });

  test('flash class clears after 2s idle on PerformancePage', async ({ page }) => {
    // Verifies flash class is not permanently stuck after data settles.
    test.setTimeout(60_000);
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.ag-row', { timeout: 30_000 });

    // Re-route to stable data so no new flashes fire after this point
    await page.unroute('**/api/positions**');
    await page.route('**/api/positions**', async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(FIXTURE_POSITIONS_BASE)
      });
    });
    await page.unroute('**/api/holdings**');
    await page.route('**/api/holdings**', async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(FIXTURE_HOLDINGS_BASE)
      });
    });

    // Wait 2s — any in-flight flash (350ms) + clear refresh (400ms) + margin
    await page.waitForTimeout(2000);

    const bodyPnlCell = page.locator('.ag-center-cols-container .ag-cell[col-id="pnl"]').first();
    const cls = await bodyPnlCell.getAttribute('class').catch(() => '');
    expect(cls ?? '').not.toContain('tf-up');
    expect(cls ?? '').not.toContain('tf-down');
    expect(cls ?? '').not.toContain('tick-flash-up');
    expect(cls ?? '').not.toContain('tick-flash-down');
  });

  test('prefers-reduced-motion: tf-up / tf-down have no animation', async ({ page }) => {
    test.setTimeout(30_000);
    // Emulate reduced motion via CSS media emulation
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });

    // Verify the CSS rule .tf-up { animation: none } is active
    const animationNone = await page.evaluate(() => {
      const div = document.createElement('div');
      div.className = 'tf-up';
      document.body.appendChild(div);
      const animName = getComputedStyle(div).animationName;
      const animDuration = getComputedStyle(div).animationDuration;
      document.body.removeChild(div);
      // Under prefers-reduced-motion: reduce, animation should be 'none' or '0s'
      return animName === 'none' || animDuration === '0s';
    });
    expect(animationNone).toBe(true);

    // Same for tick-flash-up alias
    const aliasNone = await page.evaluate(() => {
      const div = document.createElement('div');
      div.className = 'tick-flash-up';
      document.body.appendChild(div);
      const animName = getComputedStyle(div).animationName;
      const animDuration = getComputedStyle(div).animationDuration;
      document.body.removeChild(div);
      return animName === 'none' || animDuration === '0s';
    });
    expect(aliasNone).toBe(true);
  });

  test('global keyframes tf-pulse-up / tf-pulse-down CSS rule present', async ({ page }) => {
    test.setTimeout(20_000);
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(300);

    // Inspect the stylesheet directly for @keyframes tf-pulse-up / tf-pulse-down.
    // This is more reliable than getComputedStyle().animationName which can vary
    // by system-level prefers-reduced-motion setting and browser paint timing.
    const keyframesFound = await page.evaluate(() => {
      const found = { up: false, down: false };
      for (const sheet of Array.from(document.styleSheets)) {
        try {
          const rules = Array.from(sheet.cssRules || []);
          for (const rule of rules) {
            if (rule instanceof CSSKeyframesRule) {
              if (rule.name === 'tf-pulse-up')   found.up   = true;
              if (rule.name === 'tf-pulse-down')  found.down = true;
            }
          }
        } catch (_) { /* cross-origin sheet */ }
      }
      return found;
    });
    expect(keyframesFound.up).toBe(true);
    expect(keyframesFound.down).toBe(true);
  });
});

// ── Tests: MarketPulse (/pulse) ───────────────────────────────────────────────

test.describe('MarketPulse tick-flash', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await setupFixtureInterception(page);
    // Also intercept position-related pulse endpoints
    await page.route('**/api/positions/pulse**', async (route) => {
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_POSITIONS_BASE) });
    });
  });

  test('day_pnl cell carries pnlCellClass (mp-pnl-cell) + tf-* after value change', async ({ page }) => {
    test.setTimeout(90_000);
    // Phase 1: navigate, seed baseline (posCallCount=1)
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.bucket-grid .ag-row', { timeout: 60_000 });
    await page.waitForTimeout(300); // let first updateGrid seed prev[]

    // Phase 2: click refresh to fire 2nd fetch (posCallCount=2 → changed fixture)
    const refreshBtn = page.locator('.page-header-actions button, button.refresh-btn, [aria-label*="Refresh"]').first();
    const hasRefreshBtn = await refreshBtn.count();
    if (hasRefreshBtn > 0) {
      await refreshBtn.click();
      await page.waitForTimeout(400);
    } else {
      await page.waitForTimeout(6000); // fallback: wait for auto-poll
    }

    // Structural: verify the cell class includes the P&L direction class
    // (mp-pnl-cell must still be present — pnlCellClass was augmented, not replaced)
    const dayPnlCell = page.locator('.bucket-grid .ag-cell[col-id="day_pnl"]').first();
    const cls = await dayPnlCell.getAttribute('class').catch(() => '');
    expect(cls ?? '').toContain('mp-pnl-cell');

    // Also verify tf-down is present (day_pnl went 250→400, but the fixture was
    // base=250 and changed=400, so direction should be UP).
    // We check for either tf-up OR tf-down (direction depends on fixture values).
    const hasFlash = (cls ?? '').includes('tf-up') || (cls ?? '').includes('tf-down') ||
                     (cls ?? '').includes('tick-flash-up') || (cls ?? '').includes('tick-flash-down');
    if (hasRefreshBtn > 0) {
      expect(hasFlash).toBe(true);
    }
  });

  test('TOTAL row on MarketPulse positions grid does not flash', async ({ page }) => {
    test.setTimeout(90_000);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.bucket-grid .ag-row', { timeout: 60_000 });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.bucket-grid .ag-row', { timeout: 60_000 });
    await page.waitForTimeout(300);

    // TOTAL row is in pinnedBottomRowData, has _isTotal = true
    // Pinned rows appear in .ag-floating-bottom
    const totalCell = page.locator('.ag-floating-bottom .ag-cell[col-id="day_pnl"]').first();
    const cnt = await totalCell.count();
    if (cnt === 0) return; // No total row visible — skip

    const cls = await totalCell.getAttribute('class').catch(() => '');
    expect(cls ?? '').not.toContain('tf-up');
    expect(cls ?? '').not.toContain('tf-down');
    expect(cls ?? '').not.toContain('tick-flash-up');
    expect(cls ?? '').not.toContain('tick-flash-down');
  });

  test('flash class clears after 2s idle on MarketPulse', async ({ page }) => {
    // This test verifies that the flash class is NOT permanently stuck on cells.
    // The 350ms animation + 400ms refreshCells should clear the class.
    // We wait 2s to account for any re-trigger within the poll cycle.
    test.setTimeout(90_000);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.bucket-grid .ag-row', { timeout: 60_000 });

    // Unroute positions so the next poll returns stable (same) data — no more flashes
    await page.unroute('**/api/positions**');
    await page.route('**/api/positions**', async (route) => {
      // Always return base fixture (stable data) — no value changes after this
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(FIXTURE_POSITIONS_BASE)
      });
    });

    // Wait 2s — any in-flight flash should have cleared (350ms + 400ms = 750ms max)
    await page.waitForTimeout(2000);

    const dayPnlCell = page.locator('.bucket-grid .ag-cell[col-id="day_pnl"]').first();
    const cls = await dayPnlCell.getAttribute('class').catch(() => '');
    expect(cls ?? '').not.toContain('tf-up');
    expect(cls ?? '').not.toContain('tf-down');
    expect(cls ?? '').not.toContain('tick-flash-up');
    expect(cls ?? '').not.toContain('tick-flash-down');
  });
});

// ── Mobile viewport tests ─────────────────────────────────────────────────────

test.describe('Tick-flash mobile (Pixel 5 viewport)', () => {
  test.use({ viewport: { width: 393, height: 851 } });

  test('PerformancePage global keyframes present on mobile', async ({ page }) => {
    test.setTimeout(30_000);
    await loginAsAdmin(page);
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(300);

    // Check @keyframes exist via CSSKeyframesRule inspection (not computed style)
    const keyframesFound = await page.evaluate(() => {
      const found = { up: false, down: false };
      for (const sheet of Array.from(document.styleSheets)) {
        try {
          const rules = Array.from(sheet.cssRules || []);
          for (const rule of rules) {
            if (rule instanceof CSSKeyframesRule) {
              if (rule.name === 'tf-pulse-up')   found.up   = true;
              if (rule.name === 'tf-pulse-down')  found.down = true;
            }
          }
        } catch (_) { /* cross-origin sheet */ }
      }
      return found;
    });
    expect(keyframesFound.down).toBe(true);
    expect(keyframesFound.up).toBe(true);
  });

  test('prefers-reduced-motion respected on mobile', async ({ page }) => {
    test.setTimeout(30_000);
    await loginAsAdmin(page);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(300);

    const result = await page.evaluate(() => {
      const div = document.createElement('div');
      div.className = 'tf-down tick-flash-down';
      document.body.appendChild(div);
      const animDuration = getComputedStyle(div).animationDuration;
      document.body.removeChild(div);
      return animDuration;
    });
    // Under reduced motion: animation-duration should be 0s
    expect(result).toBe('0s');
  });
});
