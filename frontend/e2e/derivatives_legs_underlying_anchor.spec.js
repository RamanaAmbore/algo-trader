/**
 * derivatives_legs_underlying_anchor.spec.js
 *
 * 2026-10-18 — operator: "legs should be in sync with it [positions]"
 * (MarketPulse's Positions grid already shows the underlying even when
 * it isn't itself a held position, via `mergeUnderlyingAnchors`). This
 * adds the same concept to the derivatives page's Legs grid: a read-only
 * row for the selected underlying's own spot, always leading its own
 * CE/PE legs (`legsUnderlyingAnchor` in +page.svelte, rendered via the
 * new `.cand-row-underlying-anchor` row, BEFORE the `{#each
 * displayedCandidates}` loop).
 *
 * Deliberately NOT merged into `displayedCandidates`/`candidatePositions`
 * — those arrays feed the TOTAL row, the checkbox/enabled-leg state, the
 * payoff curve's leg set, and the CSV export. This test's core job is
 * proving the anchor row is purely additive at the DOM level: it renders
 * first, and the TOTAL row's P&L is unaffected by its presence (computed
 * only from the two real legs below it). Its own checkbox (2026-10-18
 * follow-up, operator: "the checkbox should always enabled, protected")
 * is always-checked + disabled — visible for row-shape consistency with
 * every other leg row, but not wired into the real candidate set.
 *
 * Run:
 *   npx playwright test e2e/derivatives_legs_underlying_anchor.spec.js
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(60_000);

const UNDERLYING = 'NIFTY';
const CE_SYMBOL   = 'NIFTY26DEC25000CE';
const PE_SYMBOL   = 'NIFTY26DEC24000PE';
const EXPIRY      = '2026-12-31';
const ACCOUNT     = 'ZG0790';
const DERIV_URL    = `/admin/derivatives?u=${UNDERLYING}`;

function _row(sym, strike, optType, qty, pnl, dayPnl) {
  return {
    tradingsymbol: sym, symbol: sym, exchange: 'NFO',
    quantity: qty, opening_quantity: qty, overnight_quantity: qty,
    day_buy_quantity: 0, day_sell_quantity: 0, day_sell_value: 0,
    average_price: 200.0, last_price: 210.0, close_price: 195.0,
    pnl, day_change_val: dayPnl, unrealised_pnl: pnl, realised_pnl: 0,
    account: ACCOUNT, source: 'live',
  };
}

async function _mockEndpoints(page) {
  await page.route('**/api/positions**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        rows: [
          _row(CE_SYMBOL, 25000, 'CE', 50, 500, 50),
          _row(PE_SYMBOL, 24000, 'PE', 50, 300, 30),
        ],
        summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null,
      }),
    });
  });
  await page.route('**/api/instruments**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        cycle_date: '2026-09-29', count: 2,
        items: [
          { s: CE_SYMBOL, e: 'NFO', t: 'CE', ls: 50, ts: 0.05, u: UNDERLYING, x: EXPIRY, k: 25000 },
          { s: PE_SYMBOL, e: 'NFO', t: 'PE', ls: 50, ts: 0.05, u: UNDERLYING, x: EXPIRY, k: 24000 },
        ],
      }),
    });
  });
  await page.route('**/api/holdings**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ rows: [], summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null }),
    });
  });
  await page.route('**/api/watchlist**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ watchlists: [], symbols: [] }) });
  });
  await page.route('**/api/options/strategy-analytics**', (route) => {
    const body = route.request().postDataJSON();
    const _greeks = { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 };
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        underlying: UNDERLYING, spot: 24500, spot_prev_close: 24300,
        legs: (body?.legs ?? []).map(l => ({ ...l, greeks: _greeks })),
        payoff: [{ spot: 24000, today_value: -100, expiry_value: -100 }],
        risk: { breakevens: [24300] }, breakevens: [24300],
        pnl_at_spot: 800, exp_pnl_at_spot: 800,
        dte: 15, iv: 0.18, aggregate_greeks: _greeks,
      }),
    });
  });
  await page.route('**/api/quotes/stream', async (route) => {
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: heartbeat\ndata: {}\n\n' });
  });
}

test.describe('Derivatives Legs grid — underlying-anchor row (sync with Positions)', () => {
  test('the underlying-anchor row renders first, before the real CE/PE legs', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const legsGrid = page.locator('.cand-grid');
    await expect(legsGrid).toBeVisible({ timeout: 15_000 });
    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');
    await expect(anchor).toBeVisible({ timeout: 15_000 });

    // The anchor must be the FIRST row in the body — before any real leg.
    const firstChild = page.locator('.cand-body-rows > div').first();
    await expect(firstChild).toHaveClass(/cand-row-underlying-anchor/);

    // Symbol cell shows the underlying, not a specific option contract.
    await expect(anchor.locator('.cand-anchor-label')).toContainText('NIFTY');
  });

  test('the anchor row\'s checkbox is always checked and disabled ("protected" — 2026-10-18 follow-up)', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');
    await expect(anchor).toBeVisible({ timeout: 15_000 });
    const cb = anchor.locator('input[type="checkbox"]');
    await expect(cb).toHaveCount(1);
    await expect(cb).toBeChecked();
    await expect(cb).toBeDisabled();
  });

  test('TOTAL P&L reflects only the two real legs (500 + 300 = 800), unaffected by the anchor row', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const totalRow = page.locator('.cand-row.cand-row-total');
    await expect(totalRow).toBeVisible({ timeout: 15_000 });
    // P&L TOTAL cell is the 12th span (index 11) per the grid's fixed
    // column order — matches the existing TOTAL-row convention used by
    // derivatives_total_expclose.spec.js.
    const pnlCell = totalRow.locator('> span').nth(11);
    await expect(pnlCell).toContainText('800');
  });

  test('a real CE/PE leg row\'s checkbox is enabled and toggleable (negative control — only the anchor\'s is protected)', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const realRow = page.locator('.cand-body-rows > .cand-row:not(.cand-row-underlying-anchor):not(.cand-row-total)').first();
    await expect(realRow).toBeVisible({ timeout: 15_000 });
    const cb = realRow.locator('input[type="checkbox"]');
    await expect(cb).toHaveCount(1);
    await expect(cb).toBeEnabled();
  });

  test('the anchor row\'s St column shows "X" (2026-10-18, operator: "make st as x, with similar coding like o")', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');
    await expect(anchor).toBeVisible({ timeout: 15_000 });
    const stCell = anchor.locator('.cand-state-cell');
    await expect(stCell).toHaveCount(1);
    await expect(stCell).toHaveText('X');
  });

  test('the anchor row\'s symbol cell has a neutral baseline divider, like a real leg\'s own symbol-cell divider', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');
    await expect(anchor).toBeVisible({ timeout: 15_000 });
    const label = anchor.locator('.cand-anchor-label');
    const boxShadow = await label.evaluate((el) => getComputedStyle(el).boxShadow);
    expect(boxShadow).not.toBe('none');
  });

  test('the anchor row\'s Chg% cell has the same right-edge divider every real leg\'s Chg% cell has (2026-10-18 follow-up, operator: "after chg% there is a vertical bar for other rows. do the same for gold")', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');
    await expect(anchor).toBeVisible({ timeout: 15_000 });
    const chgCell = anchor.locator('.cand-anchor-chg-sep');
    await expect(chgCell).toHaveCount(1);
    const boxShadow = await chgCell.evaluate((el) => getComputedStyle(el).boxShadow);
    expect(boxShadow).not.toBe('none');
  });

  test('the anchor row\'s text is vertically centered, same as a real leg row (2026-10-18 follow-up, operator: "gold text is not vertically centered" — the anchor row\'s span cells were missing display:flex/align-items:center that real rows already have)', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');
    await expect(anchor).toBeVisible({ timeout: 15_000 });

    const info = await anchor.evaluate((row) => {
      const rowBox = row.getBoundingClientRect();
      const label = row.querySelector('.cand-anchor-label');
      const cs = getComputedStyle(label);
      const textNode = Array.from(label.childNodes).find((n) => n.nodeType === 3 && n.textContent.trim());
      const r = document.createRange();
      r.selectNodeContents(textNode);
      const textBox = r.getBoundingClientRect();
      return {
        display: cs.display,
        alignItems: cs.alignItems,
        topGap: textBox.top - rowBox.top,
        bottomGap: rowBox.bottom - textBox.bottom,
      };
    });
    expect(info.display).toBe('flex');
    expect(info.alignItems).toBe('center');
    // Symmetric top/bottom gap is the actual proof of centering, not just
    // the CSS properties being present — small sub-pixel/font-metric slack
    // is fine, a one-sided gap (the pre-fix bug) is not.
    expect(Math.abs(info.topGap - info.bottomGap)).toBeLessThanOrEqual(3);
  });

  test('the anchor row\'s background uses the SAME --c-underlying-bg token Positions\' own row-und treatment uses (2026-10-18 follow-up)', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');
    await expect(anchor).toBeVisible({ timeout: 15_000 });
    const [rowBg, rootVar] = await Promise.all([
      anchor.evaluate((el) => getComputedStyle(el).backgroundColor),
      page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--c-underlying-bg').trim()),
    ]);
    // --c-underlying-bg is an rgba() string; the row's computed background
    // must resolve to that same color (not the old flat gray). Compare
    // parsed numeric channels, not raw text — the browser may normalise
    // trailing zeros (e.g. "0.10" -> "0.1") differently than the source.
    const parse = (s) => (s.match(/[\d.]+/g) || []).map(Number);
    expect(parse(rowBg)).toEqual(parse(rootVar));
  });

  test('the anchor row\'s rendered height equals a real leg row\'s height (2026-10-18, item 3 fix — anchor was taller before min-height parity)', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');
    const realRow = page.locator('.cand-body-rows > .cand-row:not(.cand-row-underlying-anchor):not(.cand-row-total)').first();
    await expect(anchor).toBeVisible({ timeout: 15_000 });
    await expect(realRow).toBeVisible({ timeout: 15_000 });

    const [anchorBox, realBox] = await Promise.all([anchor.boundingBox(), realRow.boundingBox()]);
    expect(anchorBox).not.toBeNull();
    expect(realBox).not.toBeNull();
    expect(Math.abs(anchorBox.height - realBox.height)).toBeLessThanOrEqual(1);
  });

  test('the anchor row\'s and TOTAL row\'s text is the same font-size/family as a real leg row (2026-10-19 fix — both were stuck at --fs-sm/--font-numeric, visibly smaller than real leg rows at --fs-lg/monospace)', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const anchorLabel = page.locator('.cand-body-rows > .cand-row-underlying-anchor .cand-anchor-label');
    const realLabel = page.locator('.cand-body-rows > .cand-row:not(.cand-row-underlying-anchor):not(.cand-row-total) .cand-sym').first();
    const totalCell = page.locator('.cand-row.cand-row-total > span').first();
    await expect(anchorLabel).toBeVisible({ timeout: 15_000 });
    await expect(realLabel).toBeVisible({ timeout: 15_000 });
    await expect(totalCell).toBeVisible({ timeout: 15_000 });

    const [anchorCs, realCs, totalCs] = await Promise.all([
      anchorLabel.evaluate((el) => ({ size: getComputedStyle(el).fontSize, family: getComputedStyle(el).fontFamily })),
      realLabel.evaluate((el) => ({ size: getComputedStyle(el).fontSize, family: getComputedStyle(el).fontFamily })),
      totalCell.evaluate((el) => ({ size: getComputedStyle(el).fontSize, family: getComputedStyle(el).fontFamily })),
    ]);
    expect(anchorCs.size).toBe(realCs.size);
    expect(anchorCs.family).toBe(realCs.family);
    expect(totalCs.size).toBe(realCs.size);
    expect(totalCs.family).toBe(realCs.family);
  });

  test('the anchor row\'s tf-cell and a real leg row\'s tf-cell flash at the SAME alpha (2026-10-19 consolidation fix — anchor/Snapshot used to flash at 0.22, real leg rows at 0.13; both now read the single global app.css rule)', async ({ page }) => {
    await loginAsAdmin(page);
    await _mockEndpoints(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const anchorCell = page.locator('.cand-body-rows > .cand-row-underlying-anchor span.tf-cell').first();
    const realCell   = page.locator('.cand-body-rows > .cand-row:not(.cand-row-underlying-anchor):not(.cand-row-total) span.tf-cell').first();
    await expect(anchorCell).toBeVisible({ timeout: 15_000 });
    await expect(realCell).toBeVisible({ timeout: 15_000 });

    // Inject tf-up, pause the real CSS animation at its start frame (peak
    // alpha), read backgroundColor, then clean up. Doing this on BOTH
    // cells proves there is no page-scoped shadow rule (e.g. the deleted
    // `.byund-row > .tf-up` / `.tf-cell.tf-up` at 0.22) still overriding
    // the global `.tf-up` (now also 0.22) for one of the two but not the
    // other.
    async function peakBg(locator) {
      return locator.evaluate((el) => {
        el.classList.add('tf-up');
        const anim = el.getAnimations().find((a) => a.animationName?.startsWith('tf-pnl-up'));
        let bg;
        if (anim) {
          anim.pause();
          anim.currentTime = 0;
          bg = getComputedStyle(el).backgroundColor;
        } else {
          bg = getComputedStyle(el).backgroundColor;
        }
        el.classList.remove('tf-up');
        return bg;
      });
    }

    const [anchorBg, realBg] = await Promise.all([peakBg(anchorCell), peakBg(realCell)]);
    expect(anchorBg, 'anchor row tf-cell background').not.toMatch(/rgba\(0, 0, 0, 0\)|transparent/);
    expect(anchorBg).toBe(realBg);
  });

  test('the right-edge bar mechanism is wired to price direction, not a flat color (source check — live direction depends on real market data)', async () => {
    const { readFileSync } = await import('fs');
    const path = await import('path');
    const dir = path.resolve(import.meta.dirname ?? new URL('.', import.meta.url).pathname, '..');
    const src = readFileSync(
      path.join(dir, 'src/routes/(algo)/admin/derivatives/+page.svelte'),
      'utf8'
    );
    expect(src).toMatch(/cand-anchor-up[\s\S]{0,80}rgba\(74,\s*222,\s*128/);
    expect(src).toMatch(/cand-anchor-down[\s\S]{0,80}rgba\(248,\s*113,\s*113/);
    // The up/down class is driven by the anchor's own pct, not qty —
    // confirms this isn't accidentally reusing the long/short mechanism.
    expect(src).toMatch(/_anchorDir\s*=\s*legsUnderlyingAnchor\.pct\s*>\s*0/);
  });
});
