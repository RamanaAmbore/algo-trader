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

  /**
   * 2026-11 operator correction — reverses an earlier explicit decision
   * (made when the 2-option AlgoTabs strip became this 3-option dropdown)
   * to KEEP the St (pairing-state) badge visible on all 3 views, including
   * the new unpaired 'legs' default. The St column represents AUTO-PAIRED/
   * ORPHAN pairing state (positionStateBadge.js) — the whole point of the
   * new 'legs' view is to show positions WITHOUT CE/PE pairing, so showing
   * a pairing-state column there is meaningless. St is now:
   *   - 'legs'   — REMOVED from layout entirely: no header cell, no
   *                per-row cell, no underlying-anchor cell, no TOTAL-row
   *                filler cell, and the matching 28px grid-template-columns
   *                track is dropped too (not just blanked — the other
   *                columns reflow left to fill the gap).
   *   - 'paired' — UNCHANGED, today's existing behavior: header + per-row
   *                badges fully render (matches the OLD pre-dropdown
   *                "Legs" tab, byte-for-byte).
   *   - 'expiry' — UNCHANGED, today's existing (2026-10-17) behavior: the
   *                header cell and the 28px track both stay in the DOM,
   *                but each row's own `.cand-state-cell` content is blank
   *                (no badge, no background/color/border) — Exp Close has
   *                its OWN identity system (`.expiry-id-chip`, C1/C2/N1/N2)
   *                instead. This test documents, not changes, that behavior.
   */
  test('St column: removed from layout on legs, present on paired, present-but-empty on expiry', async ({ page }) => {
    await loginAsAdmin(page);
    const rows = [_ceRow(SYM_HIGH, 75, 24000), _ceRow(SYM_LOW, -75, 22000)];
    await _mockCommonEndpoints(page, rows);

    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });

    const trigger = page.locator('#opt-legs-view');
    await expect(trigger).toBeVisible({ timeout: 20_000 });
    await expect(page.locator('.cand-row:not(.cand-row-total)')).toHaveCount(2, { timeout: 20_000 });

    const stHeader = page.locator('.cand-headrow .cand-state-hdr');
    const anchor = page.locator('.cand-body-rows > .cand-row-underlying-anchor');
    const firstRealRow = page.locator('.cand-row:not(.cand-row-total)').first();
    const totalRow = page.locator('.cand-row.cand-row-total');
    const grid = page.locator('.cand-grid');

    /** Computed `grid-template-columns` track count (one length per track). */
    async function trackCount() {
      const gtc = await grid.evaluate((el) => getComputedStyle(el).gridTemplateColumns);
      return gtc.trim().split(/\s+/).filter(Boolean).length;
    }

    /** Symbol-column cell x-position agrees across header/data-row/TOTAL —
     *  proves the removed St track didn't leave the other columns
     *  misaligned relative to each other (the exact failure mode a
     *  track-count mismatch between `.cand-grid` and a row's own subgrid
     *  child count would produce). */
    async function symbolColumnAligned() {
      const [hdrBox, rowBox, totalBox] = await Promise.all([
        page.locator('.cand-headrow [role="columnheader"]', { hasText: 'Symbol' }).first().boundingBox(),
        firstRealRow.locator('.cand-sym').boundingBox(),
        totalRow.locator('.cand-total-label').boundingBox(),
      ]);
      expect(hdrBox, 'Symbol header box').not.toBeNull();
      expect(rowBox, 'Symbol data-row box').not.toBeNull();
      expect(totalBox, 'TOTAL label box').not.toBeNull();
      expect(Math.abs(hdrBox.x - rowBox.x), `header.x=${hdrBox.x} row.x=${rowBox.x}`).toBeLessThan(1.5);
      expect(Math.abs(hdrBox.x - totalBox.x), `header.x=${hdrBox.x} total.x=${totalBox.x}`).toBeLessThan(1.5);
    }

    // ── 'legs' (default) — St removed entirely, 22 tracks, columns
    //    reflow with no gap ─────────────────────────────────────────────
    await expect(trigger).toHaveText(/^Legs/);
    await expect(stHeader, "St header must not exist on 'legs'").toHaveCount(0);
    await expect(firstRealRow.locator('.cand-state-cell'), "a leg row's St cell must not exist on 'legs'").toHaveCount(0);
    await expect(anchor.locator('.cand-state-cell'), "the underlying-anchor's St cell must not exist on 'legs'").toHaveCount(0);
    // TOTAL row's state-track filler `<span></span>` is indistinguishable
    // from other unlabelled filler spans by class — assert via the total
    // child-span COUNT instead (22 on 'legs', one fewer than 'paired'/
    // 'expiry' below), which is the direct DOM-child analogue of the
    // subgrid-track-count check for this one row.
    const legsTotalSpanCount = await totalRow.locator('> span').count();
    expect(legsTotalSpanCount, "TOTAL row span count on 'legs'").toBe(22);
    expect(await trackCount(), "'.cand-grid' track count on 'legs'").toBe(22);
    await symbolColumnAligned();

    // ── 'paired' — St fully present (byte-identical to the OLD "Legs") ──
    await trigger.click();
    await page.getByRole('option', { name: /^Paired/ }).click();
    await expect(trigger).toHaveText(/^Paired/);
    await expect(stHeader, "St header must exist on 'paired'").toHaveCount(1);
    await expect(firstRealRow.locator('.cand-state-badge-btn'), "a leg row's St badge must render on 'paired'")
      .toHaveCount(1);
    await expect(anchor.locator('.cand-state-badge-btn[data-pos-state="anchor"]'),
      "the underlying-anchor's St badge must render on 'paired'").toHaveCount(1);
    const pairedTotalSpanCount = await totalRow.locator('> span').count();
    expect(pairedTotalSpanCount, "TOTAL row span count on 'paired'").toBe(23);
    expect(await trackCount(), "'.cand-grid' track count on 'paired'").toBe(23);
    await symbolColumnAligned();

    // ── 'expiry' (Exp Close) — St header + track stay, cell content is
    //    blank (2026-10-17 pre-existing decision, unchanged by this fix);
    //    the underlying-anchor row never renders here (pre-existing) ────
    await trigger.click();
    await page.getByRole('option', { name: /^Exp Close/ }).click();
    await expect(trigger).toHaveText(/^Exp Close/);
    await expect(stHeader, "St header must still exist (present-but-inert) on 'expiry'").toHaveCount(1);
    expect(await trackCount(), "'.cand-grid' track count on 'expiry'").toBe(23);
    const expiryRow = page.locator('.cand-row:not(.cand-row-total)').first();
    const expiryCount = await expiryRow.count();
    if (expiryCount > 0) {
      const expiryStCell = expiryRow.locator('.cand-state-cell');
      await expect(expiryStCell, "Exp Close row's St cell must still exist in the DOM").toHaveCount(1);
      await expect(expiryStCell.locator('.cand-state-badge-btn'),
        "Exp Close row's St cell must render NO badge (2026-10-17 decision)").toHaveCount(0);
    }
  });
});
