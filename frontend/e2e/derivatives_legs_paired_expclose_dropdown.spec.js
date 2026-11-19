/**
 * derivatives_legs_paired_expclose_dropdown.spec.js
 *
 * 2026-11 — the derivatives Legs card's 2-option Legs/Exp-Close
 * `<AlgoTabs>` strip became a 3-option Legs/Paired/Exp Close
 * `<Select>` dropdown (see CLAUDE.md "Derivatives page: Legs/Paired/Exp
 * Close as a dropdown, new unpaired default"):
 *
 *   - 'legs'   (new default) — unpaired, account-grouped
 *     (`sortRowsByAccountOnly`): root → tier → strike ASC → CE/PE →
 *     symbol.
 *   - 'paired' — today's CE/PE auto-pair-grouped view, relabeled
 *     (`sortRowsByPairGroup`), stable within a pair group — i.e. it
 *     does NOT re-sort members of the same pair group by strike.
 *   - 'expiry' — Exp Close, unchanged.
 *
 * Fixture design (the divergence proof): two NIFTY CE legs, same
 * account, opposite sign — one long at the HIGHER strike (24000), one
 * short at the LOWER strike (22000), sent to the mocked
 * `/api/positions` endpoint in that (high-strike-first) order. The
 * frontend's own lot-waterfall auto-pairing (`lotPairSplit.js:
 * splitPairedLots`, grouped by (account, root, optType) — same-optType
 * CE-vs-CE netting, not a CE/PE straddle) matches these two into ONE
 * pair group regardless of which strike arrived first, so:
 *   - 'paired' preserves the ORIGINAL array order (both rows share one
 *     group key; stable sort keeps index order): 24000 CE, then
 *     22000 CE.
 *   - 'legs' sorts by strike ASC: 22000 CE, then 24000 CE — the EXACT
 *     REVERSE of 'paired' for this fixture.
 * A test that only checked row COUNTS (or a fixture where the two
 * orderings happen to coincide) would pass even if both modes ran the
 * identical sort — this fixture is deliberately built so they cannot.
 *
 * Five quality dimensions:
 *  1. SSOT   — asserts the real rendered DOM order, driven by the real
 *              `composeDisplayedCandidates`/`sortRowsByAccountOnly`/
 *              `sortRowsByPairGroup` functions — no reimplementation.
 *  2. Perf   — no extra polling; page's normal load + one dropdown
 *              interaction per mode switch.
 *  3. Stale  — regression guard: if the dropdown wiring regresses to
 *              mode 'legs' always calling `sortRowsByPairGroup` (or
 *              vice versa), this fixture's divergence makes the test
 *              fail loudly, not silently pass.
 *  4. Reuse  — reuses the proven `_mockCommonEndpoints` shape from
 *              derivatives_leg_coherence.spec.js (positions/instruments/
 *              holdings/watchlist/strategy-analytics/quotes-stream).
 *  5. UX     — asserts the operator-visible trigger label text and the
 *              Exp Close mode's header-disabled visual cue.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/derivatives_legs_paired_expclose_dropdown.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(60_000);

const UNDERLYING = 'NIFTY';
const ACCOUNT = 'ZG0790';
const SYM_HIGH = 'NIFTY26JUN24000CE'; // long, sent FIRST
const SYM_LOW = 'NIFTY26JUN22000CE';  // short, sent SECOND
const FUTURE_EXPIRY = '2026-12-31'; // far future — not an expiry-today edge case
const DERIV_URL = `/admin/derivatives?u=${UNDERLYING}`;

function _positionsBody(rows) {
  return { rows, summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null };
}

function _ceRow(sym, qty, strike) {
  return {
    tradingsymbol: sym, symbol: sym, exchange: 'NFO',
    quantity: qty, opening_quantity: qty, overnight_quantity: qty,
    day_buy_quantity: 0, day_sell_quantity: 0, day_sell_value: 0,
    average_price: 150.0, last_price: 160.0, close_price: 145.0,
    pnl: 750.0, day_change_val: 750.0, unrealised_pnl: 750.0, realised_pnl: 0,
    account: ACCOUNT, source: 'live', strike,
  };
}

async function _mockCommonEndpoints(page, rows) {
  await page.route('**/api/positions**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(_positionsBody(rows)) });
  });

  await page.route('**/api/instruments**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        cycle_date: '2026-09-29', count: 2,
        items: [
          { s: SYM_HIGH, e: 'NFO', t: 'CE', ls: 75, ts: 0.05, u: UNDERLYING, x: FUTURE_EXPIRY, k: 24000 },
          { s: SYM_LOW, e: 'NFO', t: 'CE', ls: 75, ts: 0.05, u: UNDERLYING, x: FUTURE_EXPIRY, k: 22000 },
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
        underlying: UNDERLYING, spot: 23000, spot_prev_close: 22900,
        legs: (body?.legs ?? []).map((l) => ({ ...l, greeks: _greeks })),
        payoff: [
          { spot: 20000, today_value: -500, expiry_value: -500 },
          { spot: 23000, today_value: 750, expiry_value: 750 },
          { spot: 26000, today_value: 2000, expiry_value: 2000 },
        ],
        risk: { breakevens: [22000] }, breakevens: [22000],
        pnl_at_spot: 750, exp_pnl_at_spot: 750,
        dte: 180, iv: 0.18, aggregate_greeks: _greeks,
      }),
    });
  });

  // Quote stream — heartbeat only, no live ticks needed for this test.
  await page.route('**/api/quotes/stream', async (route) => {
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: heartbeat\ndata: {}\n\n' });
  });
}

/** Returns the Legs grid's data-row symbols, in rendered DOM order. */
async function _rowSymbols(page) {
  const texts = await page.locator('.cand-row:not(.cand-row-total) .cand-sym').allTextContents();
  return texts.map((t) => t.replace(/\s+/g, ' ').trim());
}

test.describe('Derivatives Legs card — Legs/Paired/Exp Close dropdown', () => {
  test('switching the dropdown shows the correct row order/state for each of the 3 modes', async ({ page }) => {
    await loginAsAdmin(page);
    const rows = [_ceRow(SYM_HIGH, 75, 24000), _ceRow(SYM_LOW, -75, 22000)];
    await _mockCommonEndpoints(page, rows);

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const trigger = page.locator('#opt-legs-view');
    await expect(trigger).toBeVisible({ timeout: 20_000 });

    // Wait for both legs to actually render before asserting order.
    await expect(page.locator('.cand-row:not(.cand-row-total)')).toHaveCount(2, { timeout: 20_000 });

    // Underlying-anchor row (`legsUnderlyingAnchor`, +page.svelte) — its
    // own guard was widened from `legsTab !== 'legs'` to `legsTab ===
    // 'expiry'` as part of this same change (previously a confirmed
    // regression: the old guard would have hidden the anchor on the new
    // 'paired' view, today's actual main/default-looking view). Asserted
    // across all 3 modes below, alongside each mode's own row-order check.
    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');

    // ── Mode 1: 'legs' (default) — unpaired, strike ASC ────────────────
    await expect(trigger).toHaveText(/^Legs/);
    await expect(anchor, "underlying anchor must render on the 'legs' view").toBeVisible({ timeout: 10_000 });
    const legsOrder = await _rowSymbols(page);
    expect(legsOrder.some((s) => s.includes('22000'))).toBe(true);
    expect(legsOrder.some((s) => s.includes('24000'))).toBe(true);
    const legsIdx22000 = legsOrder.findIndex((s) => s.includes('22000'));
    const legsIdx24000 = legsOrder.findIndex((s) => s.includes('24000'));
    expect(legsIdx22000, "'legs' mode: 22000 strike must sort BEFORE 24000 (strike ASC)")
      .toBeLessThan(legsIdx24000);

    // ── Mode 2: 'paired' — CE/PE auto-paired, preserves original array
    //    order within the shared pair group (24000 sent first) ─────────
    await trigger.click();
    await page.getByRole('option', { name: /^Paired/ }).click();
    await expect(trigger).toHaveText(/^Paired/);
    // Regression guard: the OLD guard (`legsTab !== 'legs'`) would have
    // returned null here, since legsTab is now 'paired', not 'legs'.
    await expect(anchor, "underlying anchor must ALSO render on the 'paired' view (confirmed regression if hidden)")
      .toBeVisible({ timeout: 10_000 });
    const pairedOrder = await _rowSymbols(page);
    const pairedIdx22000 = pairedOrder.findIndex((s) => s.includes('22000'));
    const pairedIdx24000 = pairedOrder.findIndex((s) => s.includes('24000'));
    expect(pairedIdx24000, "'paired' mode: 24000 (sent first) must sort BEFORE 22000 — the exact reverse of 'legs'")
      .toBeLessThan(pairedIdx22000);

    // The two modes must disagree on this fixture — proves 'legs' and
    // 'paired' are genuinely different sort functions, not aliases.
    expect(legsOrder).not.toEqual(pairedOrder);

    // ── Mode 3: 'expiry' (Exp Close) — header sort becomes inert, and
    //    the underlying anchor (a Legs/Paired-only concept — Exp Close
    //    has its own expiry-band grouping) must NOT render ───────────
    await trigger.click();
    await page.getByRole('option', { name: /^Exp Close/ }).click();
    await expect(trigger).toHaveText(/^Exp Close/);
    await expect(anchor, "underlying anchor must NOT render on the 'expiry' (Exp Close) view").toHaveCount(0);
    const symbolHeader = page.locator('.cand-headrow [role="columnheader"]', { hasText: 'Symbol' }).first();
    await expect(symbolHeader).toHaveClass(/cand-hdr-disabled/);

    // ── Switch back to 'legs' — confirms the dropdown round-trips ──────
    await trigger.click();
    await page.getByRole('option', { name: /^Legs/ }).click();
    await expect(trigger).toHaveText(/^Legs/);
    const legsOrderAgain = await _rowSymbols(page);
    expect(legsOrderAgain).toEqual(legsOrder);
    await expect(anchor).toBeVisible({ timeout: 10_000 });

    // Mobile-viewport guard (2026-11, live-measured regression at this
    // project's own 360px mobile-portrait viewport): the dropdown
    // trigger's fixed-ish width must never push `.ch-middle` into
    // horizontal overflow, which would clip the caret — the only visual
    // cue this control is a dropdown, not static text. A no-op on wider
    // viewports (plenty of headroom there).
    const middleOverflow = await page.locator('.opt-legs-card .ch-middle')
      .evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(middleOverflow, '.ch-middle must not overflow horizontally because of the view-picker trigger')
      .toBeLessThanOrEqual(0);
  });
});
