/**
 * ltp_flash_cascade.spec.js
 *
 * Verifies that:
 *  Part A — the LTP cell flashes (ltp-flash-up / ltp-flash-down) when LTP changes.
 *  Part B — derived cells (Day P&L, P&L, Day %, P&L %) cascade the same direction
 *            class from the LTP source event on the same poll cycle.
 *
 * Cascade direction policy: SOURCE-based. If LTP ticked UP, all derived cells in
 * the same row flash ltp-flash-up regardless of the derived cell's own sign.
 * Rationale: the causal event is the LTP tick; the eye tracks cause not effect.
 * Alternative (per-cell-diff) noted in commit body for operator override.
 *
 * Pages covered:
 *  - /performance (positions + holdings detail grids)
 *  - /pulse (MarketPulse positions grid)
 *
 * Pages NOT covered (no live-LTP columns):
 *  - /admin/history — only fill_price / avg_cost (static after fill)
 *  - /orders        — no LTP column in order rows
 *  - /charts        — Greeks LTP is a one-time fetch, not live-ticking
 *
 * Quality dimensions:
 *  SSOT    — ltp-flash classes come from the shared app.css keyframes
 *  Perf    — flash fires within 700ms of LTP change; clears within 1400ms
 *  Stale   — TOTAL row never gains ltp-flash-*
 *  Reuse   — _ltpFlashUp/_ltpFlashDown in MarketPulse; _perfFlash in PerformancePage
 *  UX      — prefers-reduced-motion disables animation
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/ltp_flash_cascade.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

// ── Fixture data ──────────────────────────────────────────────────────────────
// Two-call dataset: first call seeds baseline; second call changes last_price
// and derived P&L so the LTP flash fires on the second render cycle.

const POSITION_SYMBOL = 'NIFTY26JUN25000CE';

const FIXTURE_POSITIONS_BASE = {
  rows: [
    {
      account: 'ZG0790', tradingsymbol: POSITION_SYMBOL, exchange: 'NFO',
      product: 'NRML', quantity: 50, average_price: 100, last_price: 110,
      close_price: 105, pnl: 500, pnl_percentage: 10,
      day_change_val: 250, day_change_percentage: 2.5,
      unrealised: 500, realised: 0,
    },
  ],
  summary: [
    { account: 'ZG0790', pnl: 500, day_change_val: 250, day_change_percentage: 2.5 },
    { account: 'TOTAL',  pnl: 500, day_change_val: 250, day_change_percentage: 2.5 },
  ],
  refreshed_at: new Date().toISOString(),
  source: 'live',
};

// LTP ticked UP (110 → 120) — all cascade cells should flash ltp-flash-UP.
const FIXTURE_POSITIONS_LTP_UP = {
  ...FIXTURE_POSITIONS_BASE,
  rows: [{
    ...FIXTURE_POSITIONS_BASE.rows[0],
    last_price: 120,
    pnl: 1000, pnl_percentage: 20,
    day_change_val: 750, day_change_percentage: 7.5,
  }],
  summary: [
    { account: 'ZG0790', pnl: 1000, day_change_val: 750, day_change_percentage: 7.5 },
    { account: 'TOTAL',  pnl: 1000, day_change_val: 750, day_change_percentage: 7.5 },
  ],
};

// LTP ticked DOWN (110 → 100) — cascade cells flash ltp-flash-DOWN.
const FIXTURE_POSITIONS_LTP_DOWN = {
  ...FIXTURE_POSITIONS_BASE,
  rows: [{
    ...FIXTURE_POSITIONS_BASE.rows[0],
    last_price: 100,
    pnl: 0, pnl_percentage: 0,
    day_change_val: -250, day_change_percentage: -2.5,
  }],
  summary: [
    { account: 'ZG0790', pnl: 0, day_change_val: -250, day_change_percentage: -2.5 },
    { account: 'TOTAL',  pnl: 0, day_change_val: -250, day_change_percentage: -2.5 },
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
    { account: 'TOTAL',  pnl: 1000, day_change_val: 500, day_change_percentage: 1.75, pnl_percentage: 3.57, cur_val: 29000, inv_val: 28000 },
  ],
  refreshed_at: new Date().toISOString(),
  source: 'live',
};

const FIXTURE_FUNDS = {
  rows: [
    { account: 'ZG0790', avail_margin: 100000, used_margin: 20000, cash: 90000, live_cash: 85000, collateral: 0 },
    { account: 'TOTAL',  avail_margin: 100000, used_margin: 20000, cash: 90000, live_cash: 85000, collateral: 0 },
  ],
  refreshed_at: new Date().toISOString(),
};

const FIXTURE_WATCHLISTS = { lists: [], items: {} };
const FIXTURE_MOVERS     = { items: [], refreshed_at: new Date().toISOString() };
const FIXTURE_ACCTS      = [{ account: 'ZG0790', broker: 'kite', loaded: true }];

// ── Auth cache ────────────────────────────────────────────────────────────────
let _cachedToken = /** @type {string | null} */ (null);
async function ensureAuth(page) {
  if (_cachedToken) {
    await page.goto(`${BASE}/signin`, { waitUntil: 'domcontentloaded' });
    await page.evaluate((tok) => { sessionStorage.setItem('ramboq_token', tok); }, _cachedToken);
    await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
  } else {
    const info = await loginAsAdmin(page);
    _cachedToken = info.token;
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
async function setupPerfInterception(page, secondFixture) {
  let posCount = 0;
  await page.route('**/api/positions**', async (route) => {
    posCount++;
    const data = posCount === 1 ? FIXTURE_POSITIONS_BASE : secondFixture;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
  });
  await page.route('**/api/holdings**', async (route) => {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_HOLDINGS_BASE) });
  });
  await page.route('**/api/funds**', async (route) => {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_FUNDS) });
  });
}

async function setupPulseInterception(page, secondFixture) {
  let posCount = 0;
  await page.route('**/api/positions**', async (route) => {
    posCount++;
    const data = posCount === 1 ? FIXTURE_POSITIONS_BASE : secondFixture;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
  });
  await page.route('**/api/holdings**', async (route) => {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_HOLDINGS_BASE) });
  });
  await page.route('**/api/funds**', async (route) => {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_FUNDS) });
  });
  await page.route('**/api/watchlists**', async (route) => {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_WATCHLISTS) });
  });
  await page.route('**/api/movers**', async (route) => {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_MOVERS) });
  });
  await page.route('**/api/accounts**', async (route) => {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_ACCTS) });
  });
}

// ── Static checks ─────────────────────────────────────────────────────────────

test('SSOT: ltp-flash keyframes defined in app.css (global), not per-component only', async ({ page }) => {
  // networkidle ensures CSS link tags are fully loaded before checking styleSheets.
  await page.goto(`${BASE}/performance`, { waitUntil: 'networkidle' });
  const keyframeFound = await page.evaluate(() => {
    for (const sheet of Array.from(document.styleSheets)) {
      try {
        for (const rule of Array.from(sheet.cssRules || [])) {
          if (rule instanceof CSSKeyframesRule &&
              (rule.name === 'ltp-flash-up' || rule.name === 'ltp-flash-down')) {
            return true;
          }
        }
      } catch (_) { /* cross-origin */ }
    }
    return false;
  });
  expect(keyframeFound, 'ltp-flash-up / ltp-flash-down @keyframes must be in app.css').toBe(true);
});

test('SSOT: .ltp-flash-up and .ltp-flash-down CSS rules are globally defined', async ({ page }) => {
  await page.goto(`${BASE}/performance`, { waitUntil: 'networkidle' });
  const rulesFound = await page.evaluate(() => {
    const found = { up: false, down: false };
    for (const sheet of Array.from(document.styleSheets)) {
      try {
        for (const rule of Array.from(sheet.cssRules || [])) {
          if (rule instanceof CSSStyleRule) {
            if (rule.selectorText?.includes('.ltp-flash-up'))   found.up   = true;
            if (rule.selectorText?.includes('.ltp-flash-down')) found.down = true;
          }
        }
      } catch (_) { /* cross-origin */ }
    }
    return found;
  });
  expect(rulesFound.up,   '.ltp-flash-up CSS rule missing').toBe(true);
  expect(rulesFound.down, '.ltp-flash-down CSS rule missing').toBe(true);
});

// ── PerformancePage tests ─────────────────────────────────────────────────────

const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 800 },
  { name: 'mobile',  width: 393,  height: 851 },
];

for (const vp of VIEWPORTS) {
  test.describe.serial(`LTP flash cascade — /performance [${vp.name}]`, () => {
    test.use({ viewport: { width: vp.width, height: vp.height } });

    test.beforeEach(async ({ page }) => {
      await ensureAuth(page);
    });

    test('Part A: LTP cell gains ltp-flash-up on LTP increase', async ({ page }) => {
      test.setTimeout(90_000);

      // Phase 1: serve BASELINE on all initial fetches so _perfFlash seeds
      // last_price=110 as its baseline.
      await page.route('**/api/positions**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_POSITIONS_BASE) });
      });
      await page.route('**/api/holdings**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_HOLDINGS_BASE) });
      });
      await page.route('**/api/funds**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_FUNDS) });
      });

      await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.ag-row', { timeout: 30_000 });
      // Let the initial load and any early background poll stabilize on the BASELINE.
      await page.waitForTimeout(800);

      // Phase 2: switch routes to serve LTP_UP on the next refresh click.
      await page.unroute('**/api/positions**');
      await page.route('**/api/positions**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_POSITIONS_LTP_UP) });
      });

      // Click the RefreshButton to trigger the LTP_UP fetch.
      const refreshBtn = page.locator('[aria-label^="Refresh performance"]');
      const [response] = await Promise.all([
        page.waitForResponse(r => r.url().includes('/api/positions'), { timeout: 15_000 }),
        refreshBtn.click(),
      ]);
      expect(response.status()).toBe(200);

      // Poll for ltp-flash-up within the 600ms animation window.
      // The positions DETAIL section (Breakdown) contains last_price cells with flash.
      // The positions SUMMARY section above it does not. We use a page-wide locator
      // scoped to cells that actually have last_price — the one in the detail grid.
      let ltpCls = '';
      const deadline = Date.now() + 650;
      while (Date.now() < deadline) {
        ltpCls = await page.locator('.ag-cell[col-id="last_price"]').first().getAttribute('class').catch(() => '') ?? '';
        if (ltpCls.includes('ltp-flash-up') || ltpCls.includes('ltp-flash-down')) break;
        await page.waitForTimeout(30);
      }
      expect(ltpCls, `LTP cell should carry ltp-flash-up after LTP tick-up; got: "${ltpCls}"`).toContain('ltp-flash-up');
    });

    test('Part B: derived columns cascade ltp-flash-up on LTP tick-up', async ({ page }) => {
      test.setTimeout(90_000);

      // Phase 1: seed baseline (last_price=110) so _perfFlash has a reference.
      await page.route('**/api/positions**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_POSITIONS_BASE) });
      });
      await page.route('**/api/holdings**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_HOLDINGS_BASE) });
      });
      await page.route('**/api/funds**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_FUNDS) });
      });

      await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.ag-row', { timeout: 30_000 });
      // Let the initial load and any early background poll stabilize on the BASELINE.
      await page.waitForTimeout(800);

      // Phase 2: switch routes to serve LTP_UP (last_price=120).
      await page.unroute('**/api/positions**');
      await page.route('**/api/positions**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_POSITIONS_LTP_UP) });
      });

      // Click RefreshButton and wait for the LTP_UP response to land.
      const refreshBtn = page.locator('[aria-label^="Refresh performance"]');
      const [posResponse] = await Promise.all([
        page.waitForResponse(r => r.url().includes('/api/positions'), { timeout: 15_000 }),
        refreshBtn.click(),
      ]);
      expect(posResponse.status()).toBe(200);

      // Poll for ltp-flash-up on pnl and day_change_val within the 600ms animation window.
      // Must be ltp-flash-up — the cascade direction is SOURCE-based (LTP tick up → all
      // derived cells in the same row get ltp-flash-up). tf-up is NOT acceptable here:
      // it would mean cascade wiring is absent and the cell is doing its own poll-diff.
      //
      // The Breakdown section (positionsAllGrid) uses pnlClsFlash() — it's the one
      // that emits ltp-flash-up. The Summary section above it uses pnlCls (no flash).
      // Find the Breakdown section by its heading text.
      const breakdownSection = page.locator('section:not(.hidden):has(h2:text("Breakdown"))');
      let pnlCls = '';
      let dayPnlCls = '';
      const deadline = Date.now() + 650;
      while (Date.now() < deadline) {
        pnlCls    = await breakdownSection.locator('.ag-cell[col-id="pnl"]').first().getAttribute('class').catch(() => '') ?? '';
        dayPnlCls = await breakdownSection.locator('.ag-cell[col-id="day_change_val"]').first().getAttribute('class').catch(() => '') ?? '';
        if (pnlCls.includes('ltp-flash-up') || pnlCls.includes('ltp-flash-down')) break;
        await page.waitForTimeout(30);
      }
      expect(pnlCls, `pnl cell should carry ltp-flash-up (cascade from LTP tick-up); got: "${pnlCls}"`).toContain('ltp-flash-up');
      expect(dayPnlCls, `day_change_val cell should carry ltp-flash-up (cascade); got: "${dayPnlCls}"`).toContain('ltp-flash-up');
    });

    test('TOTAL row never flashes ltp-flash-*', async ({ page }) => {
      test.setTimeout(90_000);

      // Phase 1: baseline
      await page.route('**/api/positions**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_POSITIONS_BASE) });
      });
      await page.route('**/api/holdings**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_HOLDINGS_BASE) });
      });
      await page.route('**/api/funds**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_FUNDS) });
      });

      await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.ag-row', { timeout: 30_000 });
      await page.waitForTimeout(800);

      // Phase 2: LTP_UP
      await page.unroute('**/api/positions**');
      await page.route('**/api/positions**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_POSITIONS_LTP_UP) });
      });

      const refreshBtn = page.locator('[aria-label^="Refresh performance"]');
      await Promise.all([
        page.waitForResponse(r => r.url().includes('/api/positions'), { timeout: 15_000 }),
        refreshBtn.click(),
      ]);

      // Sample within the flash window to ensure any cascade fires have occurred.
      // TOTAL row (pinned bottom) must NOT receive ltp-flash-* regardless.
      const breakdownSection = page.locator('section:not(.hidden):has(h2:text("Breakdown"))');
      const totalPnlCell = breakdownSection.locator(
        '.ag-floating-bottom .ag-cell[col-id="pnl"]'
      ).first();
      const cnt = await totalPnlCell.count();
      if (cnt > 0) {
        const cls = await totalPnlCell.getAttribute('class').catch(() => '');
        expect(cls ?? '').not.toContain('ltp-flash-up');
        expect(cls ?? '').not.toContain('ltp-flash-down');
      }
    });

    test('reduced-motion: ltp-flash-up has no animation', async ({ page }) => {
      test.setTimeout(30_000);
      await page.emulateMedia({ reducedMotion: 'reduce' });
      // networkidle ensures CSS link tags are fetched before evaluating animation props.
      await page.goto(`${BASE}/performance`, { waitUntil: 'networkidle' });

      const animNone = await page.evaluate(() => {
        const div = document.createElement('div');
        div.className = 'ltp-flash-up';
        document.body.appendChild(div);
        const dur  = getComputedStyle(div).animationDuration;
        const name = getComputedStyle(div).animationName;
        document.body.removeChild(div);
        return name === 'none' || dur === '0s';
      });
      expect(animNone, 'ltp-flash-up should have no animation under prefers-reduced-motion').toBe(true);
    });

    test('flash decays — ltp-flash-up clears within 1400ms', async ({ page }) => {
      test.setTimeout(90_000);

      // Phase 1: baseline
      await page.route('**/api/positions**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_POSITIONS_BASE) });
      });
      await page.route('**/api/holdings**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_HOLDINGS_BASE) });
      });
      await page.route('**/api/funds**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_FUNDS) });
      });

      await page.goto(`${BASE}/performance`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.ag-row', { timeout: 30_000 });
      await page.waitForTimeout(800);

      // Phase 2: switch to LTP_UP and trigger refresh (ltp-flash-up should fire).
      await page.unroute('**/api/positions**');
      await page.route('**/api/positions**', async (route) => {
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(FIXTURE_POSITIONS_LTP_UP) });
      });

      const refreshBtn = page.locator('[aria-label^="Refresh performance"]');
      await Promise.all([
        page.waitForResponse(r => r.url().includes('/api/positions'), { timeout: 15_000 }),
        refreshBtn.click(),
      ]);

      // Wait well beyond the 600ms animation duration + 350ms createTickFlash durationMs.
      await page.waitForTimeout(1400);
      // Scope to the Breakdown section (positionsAllGrid) — the one that CAN flash.
      const breakdownSection = page.locator('section:not(.hidden):has(h2:text("Breakdown"))');
      const pnlCell = breakdownSection.locator('.ag-cell[col-id="pnl"]').first();
      const cls = await pnlCell.getAttribute('class').catch(() => '');
      expect(cls ?? '').not.toContain('ltp-flash-up');
      expect(cls ?? '').not.toContain('ltp-flash-down');
    });
  });
}

// ── MarketPulse (/pulse) cascade tests ───────────────────────────────────────
// MarketPulse gets LTP from the SSE quoteStream, not from the positions API.
// We verify the structural wiring (pnlCellClass reads _ltpFlashUp/Down) via
// the source file static check, and the CSS class availability via the DOM.

test.describe.serial('LTP flash cascade — /pulse (static + CSS)', () => {
  test('SSOT: pnlCellClass in MarketPulse checks _ltpFlashUp/_ltpFlashDown', () => {
    // Static source check — verifies cascade wiring is in place.
    const __filename = fileURLToPath(import.meta.url);
    const __dirname  = path.dirname(__filename);
    const src = fs.readFileSync(
      path.resolve(__dirname, '../src/lib/MarketPulse.svelte'), 'utf8'
    );
    expect(src).toContain('_ltpFlashUp.has(symUpper)');
    expect(src).toContain('_ltpFlashDown.has(symUpper)');
    expect(src).toContain('ltp-flash-up');
    expect(src).toContain('ltp-flash-down');
  });

  test('SSOT: LTP paint timer refreshes derived columns when flash set is non-empty', () => {
    const __filename = fileURLToPath(import.meta.url);
    const __dirname  = path.dirname(__filename);
    const src = fs.readFileSync(
      path.resolve(__dirname, '../src/lib/MarketPulse.svelte'), 'utf8'
    );
    // The cascade refresh must include pnl and day_pnl columns.
    expect(src).toContain("'day_pnl'");
    expect(src).toContain("'pnl_pct'");
    // The hasCascade guard must be present.
    expect(src).toContain('hasCascade');
  });

  test('SSOT: PerformancePage tracks last_price in _perfFlash for LTP flash', () => {
    const __filename = fileURLToPath(import.meta.url);
    const __dirname  = path.dirname(__filename);
    const src = fs.readFileSync(
      path.resolve(__dirname, '../src/lib/PerformancePage.svelte'), 'utf8'
    );
    expect(src).toContain("_perfFlash.update(`${k}:last_price`");
    // avgVsLtpCls must apply the ltp flash class.
    expect(src).toContain("ltpCls === 'tf-up' ? 'ltp-flash-up' : 'ltp-flash-down'");
    // pnlClsFlash must read LTP direction for cascade.
    expect(src).toContain("_perfFlash.classOf(`${k}:last_price`)");
  });

  test.describe('CSS availability on /pulse page', () => {
    test('ltp-flash keyframes reachable on /pulse page', async ({ page }) => {
      test.setTimeout(60_000);
      await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });

      const keyframeFound = await page.evaluate(() => {
        for (const sheet of Array.from(document.styleSheets)) {
          try {
            for (const rule of Array.from(sheet.cssRules || [])) {
              if (rule instanceof CSSKeyframesRule &&
                  (rule.name === 'ltp-flash-up' || rule.name === 'ltp-flash-down')) {
                return true;
              }
            }
          } catch (_) { /* cross-origin */ }
        }
        return false;
      });
      expect(keyframeFound, 'ltp-flash keyframes must be in global app.css (reachable without MarketPulse)').toBe(true);
    });
  });
});
