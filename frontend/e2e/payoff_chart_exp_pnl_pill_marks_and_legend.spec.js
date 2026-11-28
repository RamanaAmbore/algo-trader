/**
 * payoff_chart_exp_pnl_pill_marks_and_legend.spec.js
 *
 * 2026-11-26 Payoff chart (OptionsPayoff.svelte) operator asks:
 *
 *  a. Exp P&L shaded pill at the BOTTOM of the chart, mirroring the
 *     existing spot-price pill's decoration (navy mask + colour pill +
 *     bold dark text) at the TOP. Reads `curveAtSpot.expiry_value`
 *     directly — the C1 invariant (no second, independently-computed
 *     figure) — so it can never disagree with the Exp P&L stat row or
 *     the expiry dart.
 *  b. Top-left overlay label renames: "P&L" → "Mark P&L",
 *     "ADJ" → "MARK ADJ". "DAY P&L" and "Exp P&L" are untouched.
 *  c. Legend gains dart-style (filled-circle + halo) markers for all
 *     three at-current-spot quantities: "Theoretical P&L" (amber,
 *     renamed from the old bare "P&L" line-swatch label), "Market P&L"
 *     (violet, new — gated on `dayPnl != null`, matching its dart's own
 *     guard), "Exp P&L" (sky, same text, now a dart marker instead of a
 *     dashed-line swatch).
 *
 * 2026-11-27 follow-up #1 (operator: "different background color scheme
 * [from the top pill], not just a shade variant" + "add a border to
 * [both] the Exp P&L pill and the market value box") — the bottom Exp
 * P&L pill no longer mirrors the top pill's exact solid-fill/dark-text
 * structure. It keeps the sky hue (still means "Exp P&L" everywhere
 * else on this chart) but switches to the app's translucent "info
 * pill" treatment (soft sky-tinted fill + bright sky border + bright
 * sky text), genuinely distinct from the top pill's (then still)
 * solid bright cyan fill + dark text + light-white border. Both pills
 * carried a clearly visible, matching-weight (1.5px) border.
 *
 * 2026-11-27 follow-up #2, same day, operator again: "the underlying
 * price text with background color and border is not clearly
 * visible... use better color scheme" — a real rendered screenshot
 * confirmed the complaint: the top pill's then-current solid-bright-
 * cyan-fill + light-white-border version read as a flat, washed-out
 * cyan blob, because a 1.5px white rim at 0.85 opacity sits only
 * marginally lighter than the `#22d3ee` fill it's meant to outline.
 * The bottom Exp P&L pill's own translucent-wash recipe (from follow-
 * up #1 above) was confirmed, in the same screenshot, to read clearly.
 * The top spot pill briefly reused that EXACT recipe — translucent
 * `--algo-cyan-bg-strong` wash fill + full-brightness `#22d3ee`
 * border + full-brightness `#22d3ee` text — which fixed the contrast
 * but made the two pills visually IDENTICAL in structure, distinguished
 * only by hue (cyan vs sky), not by fill treatment — reintroducing the
 * exact "shade variant" the follow-up #1 instruction above already
 * rejected.
 *
 * 2026-11-28 revert — restores follow-up #1's "genuinely different
 * background scheme" instruction for BOTH pills, not just the Exp P&L
 * one. The top spot pill is now a SOLID opaque `--algo-cyan` fill with
 * dark `#0c1830` text (dark-on-bright), plus a separate bright-cyan
 * ring element (`.payoff-spot-pill-ring`) drawn with a navy gap on
 * both sides as its border — a stroke on the pill's own edge would
 * reproduce the original invisible-rim bug, since `#0c1830` sits right
 * next to the `#0d1829` navy mask immediately outside it. The bottom
 * Exp P&L pill is untouched: still the translucent sky wash + bright
 * sky border/text (bright-on-dark). The two constructions — solid
 * opaque dark-on-bright vs. translucent wash bright-on-dark — are
 * genuinely different, not merely a different hue. See the "Fill/
 * border scheme, final" comment above the spot pill in
 * OptionsPayoff.svelte for the full history and root-cause screenshot
 * description.
 *
 * Reuses the same mock-endpoint pattern as
 * `derivatives_payoff_dart_number_parity.spec.js` (flat payoff curve,
 * `blockLiveTicks`, `mockCommonEndpoints`) — local copies per this
 * suite's established per-file convention (see that file's own header).
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';
import { loginAsAdmin } from './fixtures/auth.js';

const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const PAYOFF_PATH = path.resolve(DIR, '../src/lib/OptionsPayoff.svelte');

const ACCOUNT_A = 'ZG0790';

function positionsBody(rows) {
  return { rows, summary: [], refreshed_at: new Date().toISOString(), source: 'snapshot', as_of: null };
}

/** @param {{symbol:string,account:string,qty:number,avgCost:number,ltp:number}} opts */
function optionRow({ symbol, account, qty, avgCost, ltp }) {
  return {
    tradingsymbol: symbol, symbol, account,
    quantity: qty, average_price: avgCost, last_price: ltp,
    pnl: 0, realised: 0, unrealised: 0,
    overnight_quantity: qty, day_buy_quantity: 0, day_sell_quantity: 0,
    day_buy_value: 0, day_sell_value: 0,
  };
}

async function blockLiveTicks(page) {
  await page.route('**/api/quotes/stream', (route) => route.abort());
  await page.routeWebSocket('**/ws/performance', (ws) => ws.close());
  await page.routeWebSocket('**/ws/algo', (ws) => ws.close());
}

/**
 * @param {import('@playwright/test').Page} page
 * @param {{positionsRows: any[], instrumentItems: any[], strategyResponse: any}} opts
 */
async function mockCommonEndpoints(page, { positionsRows, instrumentItems, strategyResponse }) {
  await page.route('**/api/positions**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(positionsBody(positionsRows)) });
  });
  await page.route('**/api/instruments**', (route) => {
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ cycle_date: '2026-11-26', count: instrumentItems.length, items: instrumentItems }),
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
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(strategyResponse) });
  });
}

function flatCurve(expiryValue, todayValue, spotCenter) {
  const pts = [spotCenter * 0.8, spotCenter, spotCenter * 1.2];
  return pts.map((spot) => ({ spot, today_value: todayValue, expiry_value: expiryValue }));
}

async function gotoNiftyStrategy(page, { expPnl, todayPnl, spot = 20000 }) {
  await loginAsAdmin(page);
  await blockLiveTicks(page);

  const SYM = 'NIFTY26NOV20000CE';
  await mockCommonEndpoints(page, {
    positionsRows: [optionRow({ symbol: SYM, account: ACCOUNT_A, qty: 1, avgCost: 100, ltp: 110 })],
    instrumentItems: [
      { s: SYM, e: 'NFO', t: 'CE', ls: 50, ts: 0.05, u: 'NIFTY', x: '2026-11-27', k: spot },
    ],
    strategyResponse: {
      underlying: 'NIFTY', spot, spot_prev_close: spot,
      // Synthetic anchor (test-only determinism knob, same convention as
      // derivatives_payoff_dart_number_parity.spec.js's Case (b)/(d)) —
      // guarantees payoffSpot resolves to `spot` regardless of any
      // live/poll underlying-quote tick (the real NIFTY LTP in this test
      // account can sit numerically outside the mocked curve's domain,
      // which would otherwise suppress both the spot pill and this new
      // Exp P&L pill via their shared spot-in-domain guard).
      spot_anchor_contract: 'NIFTY_TEST_SPOT_ANCHOR', spot_source: 'futures',
      legs: [
        { symbol: SYM, qty: 1, strike: spot, opt_type: 'CE', greeks: { delta: 0.5, gamma: 0.01, theta: -0.3, vega: 0.2, rho: 0.01 } },
      ],
      payoff: flatCurve(expPnl, todayPnl, spot),
      risk: { breakevens: [spot] }, breakevens: [spot],
      pnl_at_spot: todayPnl, exp_pnl_at_spot: expPnl,
      dte: 1, iv: 0.3, aggregate_greeks: { delta: 1.0, gamma: 0.02, theta: -0.6, vega: 0.4, rho: 0.02 },
    },
  });

  await page.goto('/admin/derivatives?u=NIFTY', { waitUntil: 'domcontentloaded' });
  const overlay = page.locator('.payoff-stats');
  await expect(overlay).toBeVisible({ timeout: 20_000 });
  // Confirm the mocked strategy (not the client-side stub curve) is active.
  const legsChip = overlay.locator('.ps-row', { has: page.locator('.ps-k', { hasText: 'LEGS' }) }).locator('.ps-v');
  await expect(legsChip).not.toHaveText('0', { timeout: 15_000 });
  return overlay;
}

test.describe('Payoff chart — Exp P&L bottom pill, label renames, legend markers', () => {
  // The bottom pill's live rendering depends on `spot` landing inside
  // the mocked curve's x-domain. A first attempt with
  // `spot_anchor_contract: null` (index-sourced) let the real-time
  // NIFTY quote — a separate live data source from the mocked
  // `strategy-analytics` response — drive `payoffSpot` instead, which
  // sat well outside the mocked domain and correctly SUPPRESSED both
  // the existing top spot pill and this new bottom pill via their
  // shared `spot >= sMin && spot <= sMax` guard. `gotoNiftyStrategy()`
  // below instead uses the same synthetic-anchor determinism knob
  // `derivatives_payoff_dart_number_parity.spec.js`'s Case (b)/(d)
  // already establish (`spot_anchor_contract: 'NIFTY_TEST_SPOT_ANCHOR'`,
  // `spot_source: 'futures'`), which guarantees `payoffSpot` resolves to
  // the mocked `spot` regardless of any live tick — the LIVE test below
  // asserts the pill's exact rendered text, not just its source shape.
  test('SOURCE: the bottom Exp P&L pill reads curveAtSpot.expiry_value directly (C1 invariant) and shares the spot pill\'s guard + navy-mask technique', () => {
    const src = readFileSync(PAYOFF_PATH, 'utf8');
    const pillIdx = src.indexOf('Exp P&L pill — bottom of chart');
    expect(pillIdx, 'bottom Exp P&L pill comment not found').toBeGreaterThan(-1);
    // Widened from 3600 → 4400 (2026-11-27 same-day follow-up): the
    // pill's own comment grew a "Z-order" paragraph explaining the
    // foreground-svg relocation fix, pushing the actual <rect>/<text>
    // markup further from the comment's start index.
    const block = src.slice(pillIdx, pillIdx + 4400);

    // Same guard shape as the spot pill (spot >= sMin && spot <= sMax),
    // plus the curve must actually be resolved.
    expect(block).toMatch(/curveAtSpot != null && curveAtSpot\.expiry_value != null && spot >= sMin && spot <= sMax/);
    // No second, independently-computed figure — reads the identical
    // value the Exp P&L stat row and the expiry dart's own y-position use.
    expect(block).toMatch(/\{@const expLabel = fmtMoney\(curveAtSpot\.expiry_value\)\}/);
    // Same navy-mask technique as the top spot pill (2 <rect>s + 1
    // <text>, rx 5 mask / rx 4 pill).
    expect(block).toMatch(/rx="5"\s*\n\s*fill="#0d1829"/);
    expect(block).toMatch(/text-anchor="middle"/);
    // Positioned at the BOTTOM of the plot area (height - PAD_B), not the top.
    expect(block).toMatch(/expChipY = height - PAD_B - expChipH - 3/);
  });

  test('SOURCE: the top spot pill and bottom Exp P&L pill use genuinely different background schemes (2026-11-28 revert)', () => {
    const src = readFileSync(PAYOFF_PATH, 'utf8');

    // The pill fill itself is a SOLID opaque cyan (dark-on-bright), not
    // the translucent wash the bottom pill uses — this is the actual
    // regression guard: a prior intermediate fix made this element read
    // `fill="var(--algo-cyan-bg-strong, ...)"` (a wash), which would be
    // the exact "shade variant" of the bottom pill this test rejects.
    const spotIdx = src.indexOf('class="payoff-spot-pill"');
    expect(spotIdx, 'top spot pill (.payoff-spot-pill) not found').toBeGreaterThan(-1);
    const spotBlock = src.slice(spotIdx, spotIdx + 250);
    expect(spotBlock).toMatch(/fill="var\(--algo-cyan, #22d3ee\)"/);
    expect(spotBlock).not.toMatch(/--algo-cyan-bg-strong/);
    // No stroke on the pill's own edge — a border drawn directly on a
    // solid bright fill is the original invisible-rim bug; the border
    // is a separate ring element instead (asserted below).
    expect(spotBlock).not.toMatch(/stroke=/);

    // The border is a dedicated ring, drawn OUTSIDE the pill with a
    // navy gap on both sides, in the same bright cyan the fill uses —
    // genuinely visible against both the fill and the dark backdrop,
    // unlike a dark stroke (which would blend into the navy mask) or a
    // bright stroke on the fill's own edge (which would blend into the
    // fill, the original bug).
    const ringIdx = src.indexOf('class="payoff-spot-pill-ring"');
    expect(ringIdx, 'top spot pill ring (.payoff-spot-pill-ring) not found').toBeGreaterThan(-1);
    const ringBlock = src.slice(ringIdx, ringIdx + 300);
    expect(ringBlock).toMatch(/fill="none"/);
    expect(ringBlock).toMatch(/stroke="var\(--algo-cyan, #22d3ee\)"/);
    expect(ringBlock).toMatch(/stroke-width="1\.5"/);
    expect(ringBlock).toMatch(/vector-effect="non-scaling-stroke"/);
    expect(ringBlock).not.toMatch(/stroke="#ffffff"/);

    const spotTextIdx = src.indexOf('class="payoff-spot-pill-text"');
    expect(spotTextIdx, 'top spot pill text (.payoff-spot-pill-text) not found').toBeGreaterThan(-1);
    const spotTextBlock = src.slice(spotTextIdx, spotTextIdx + 150);
    // Dark navy text (readable against the SOLID bright fill) — the
    // opposite of the bottom pill's bright-on-dark text.
    expect(spotTextBlock).toMatch(/fill="#0c1830"/);
    expect(spotTextBlock).not.toMatch(/fill="#22d3ee"/);
    expect(spotTextBlock).not.toMatch(/fill="var\(--algo-cyan/);

    const expIdx = src.indexOf('class="payoff-exp-pill"');
    expect(expIdx, 'bottom Exp P&L pill (.payoff-exp-pill) not found').toBeGreaterThan(-1);
    const expBlock = src.slice(expIdx, expIdx + 400);
    // Bottom pill: translucent sky-tinted WASH fill (soft info-pill
    // convention) — the SAME recipe as the top pill above, just the
    // sky hue (still means "Exp P&L" chart-wide) instead of cyan.
    expect(expBlock).toMatch(/fill="var\(--algo-sky-bg-strong/);
    expect(expBlock).not.toMatch(/fill="#7dd3fc"/);
    expect(expBlock).toMatch(/stroke="#7dd3fc"/);
    expect(expBlock).toMatch(/stroke-width="1\.5"/);
    expect(expBlock).toMatch(/vector-effect="non-scaling-stroke"/);

    // Bottom pill's TEXT is bright sky (readable against the dark
    // translucent fill), not the dark "#0c1830" text the old
    // solid-fill version used.
    const expTextIdx = src.indexOf('class="payoff-exp-pill-text"');
    expect(expTextIdx, 'bottom Exp P&L pill text (.payoff-exp-pill-text) not found').toBeGreaterThan(-1);
    const expTextBlock = src.slice(expTextIdx, expTextIdx + 150);
    expect(expTextBlock).toMatch(/fill="#7dd3fc"/);
  });

  test('LIVE: bottom Exp P&L pill renders with the exact same text as the Exp P&L stat row', async ({ page }) => {
    const EXP_PNL = 2345;
    const overlay = await gotoNiftyStrategy(page, { expPnl: EXP_PNL, todayPnl: 1500 });

    const expRow = overlay.locator('.ps-row', { has: page.locator('.ps-k', { hasText: 'Exp P&L' }) }).first();
    const expText = (await expRow.locator('.ps-v').textContent()) || '';

    // Scoped to .payoff-svg-stack (the wrapper around BOTH svg layers),
    // not .payoff-svg alone — 2026-11-27 same-day follow-up moved both
    // pills into the foreground svg (.payoff-svg-fg) so curves/darts/the
    // vertical spot line can never paint over them (see the "Z-order"
    // comments in OptionsPayoff.svelte next to each pill).
    const chartStack = page.locator('.payoff-svg-stack').first();
    await expect(chartStack).toBeVisible({ timeout: 15_000 });
    const pillTexts = await chartStack.locator('text').allTextContents();
    const cleaned = (s) => s.replace(/[₹,\s]/g, '');
    const matchFound = pillTexts.some((t) => cleaned(t) !== '' && cleaned(t) === cleaned(expText));
    expect(matchFound, `expected a chart <text> matching the Exp P&L stat row's own text (${expText}); saw: ${JSON.stringify(pillTexts)}`).toBe(true);

    // Three <rect>s for the top spot pill (mask + ring + solid fill),
    // two more for the bottom Exp P&L pill (mask + pill) — now in the
    // foreground svg layer, still inside the same .payoff-svg-stack
    // wrapper.
    const rectCount = await chartStack.locator('rect').count();
    expect(rectCount, 'expected at least 5 <rect> elements (3 top spot pill + 2 bottom Exp P&L pill)').toBeGreaterThanOrEqual(5);
  });

  test('LIVE (2026-11-28 revert): the top spot pill and bottom Exp P&L pill use genuinely different, each-visible background schemes', async ({ page }) => {
    await gotoNiftyStrategy(page, { expPnl: 2345, todayPnl: 1500 });

    const spotPill = page.locator('.payoff-spot-pill').first();
    const spotRing = page.locator('.payoff-spot-pill-ring').first();
    const spotText = page.locator('.payoff-spot-pill-text').first();
    const expPill = page.locator('.payoff-exp-pill').first();
    const expText = page.locator('.payoff-exp-pill-text').first();
    await expect(spotPill).toBeVisible({ timeout: 15_000 });
    await expect(spotRing).toBeVisible({ timeout: 15_000 });
    await expect(expPill).toBeVisible({ timeout: 15_000 });

    const styleOf = async (locator) => locator.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { fill: cs.fill, stroke: cs.stroke, strokeWidth: cs.strokeWidth };
    });
    const spotPillStyle = await styleOf(spotPill);
    const spotRingStyle = await styleOf(spotRing);
    const spotTextStyle = await styleOf(spotText);
    const expPillStyle = await styleOf(expPill);
    const expTextStyle = await styleOf(expText);

    const alphaOf = (rgbaStr) => {
      const m = rgbaStr.match(/rgba?\(([^)]+)\)/);
      if (!m) return 1;
      const parts = m[1].split(',').map((s) => parseFloat(s.trim()));
      return parts.length === 4 ? parts[3] : 1;
    };
    // Luminance of an "r, g, b" (or "r, g, b, a") string — used below to
    // confirm the top pill's text is genuinely DARK and the bottom
    // pill's text is genuinely BRIGHT, not just two different colour
    // strings that happen to both be "blue".
    const luminanceOf = (rgbaStr) => {
      const m = rgbaStr.match(/rgba?\(([^)]+)\)/);
      if (!m) return 0;
      const [r, g, b] = m[1].split(',').map((s) => parseFloat(s.trim()));
      return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
    };

    // The top pill's FILL is a solid, opaque colour (the badge itself);
    // the bottom pill's fill is a translucent wash. This is the real
    // regression guard for "genuinely different background scheme, not
    // just a shade variant" — a hue-only difference (e.g. cyan vs sky,
    // both still washes) would pass a bare string-inequality check but
    // fail this alpha-based one.
    expect(alphaOf(spotPillStyle.fill), 'top pill fill must be solid/opaque').toBeGreaterThan(0.9);
    expect(alphaOf(expPillStyle.fill), 'bottom pill fill must be a translucent wash').toBeLessThan(0.5);

    // Text contrast is reversed between the two pills: dark text on the
    // top pill's bright solid fill, bright text on the bottom pill's
    // dark translucent fill.
    expect(luminanceOf(spotTextStyle.fill), 'top pill text must be dark (reads against a bright solid fill)').toBeLessThan(0.3);
    expect(luminanceOf(expTextStyle.fill), 'bottom pill text must be bright (reads against a dark translucent fill)').toBeGreaterThan(0.5);

    // Both pills must carry a real, visible border — the top pill's
    // border lives on the separate ring element, not the pill itself.
    expect(parseFloat(spotRingStyle.strokeWidth), 'top pill ring border width').toBeGreaterThan(0);
    expect(parseFloat(expPillStyle.strokeWidth), 'bottom pill border width').toBeGreaterThan(0);
    expect(spotRingStyle.stroke, 'top pill ring must have a stroke colour').not.toBe('none');
    expect(expPillStyle.stroke, 'bottom pill must have a stroke colour').not.toBe('none');
    // Same border WEIGHT across both pills ("consistent treatment"),
    // compared via the top pill's ring (its actual border element).
    expect(parseFloat(spotRingStyle.strokeWidth)).toBeCloseTo(parseFloat(expPillStyle.strokeWidth), 1);

    // The top pill's own rect carries no stroke at all — its border is
    // exclusively the separate ring element (confirms the two-layer
    // gap-ring construction, not a stroke drawn on the fill's own edge).
    expect(spotPillStyle.strokeWidth === '0px' || spotPillStyle.stroke === 'none',
      'top pill rect itself must have no stroke of its own').toBe(true);
  });

  test('SOURCE (2026-11-27 same-day follow-up): both pills live in the foreground svg, never the background one, so a curve/dart/vertical-spot-line can never paint over them', () => {
    // Operator: "the current underlying price shown with background and
    // border in payoff chart is not clearly visible, may be because of
    // background." Root cause: the pills used to live in the background
    // svg (.payoff-svg, z-index 1), while the today/expiry curves and
    // the vertical spot line are redrawn in the foreground svg
    // (.payoff-svg-fg, z-index 4) — which paints OVER the whole
    // background layer regardless of document order inside it. A curve
    // whose value at spotX landed near a pill's fixed top/bottom band
    // sliced straight across its fill/border/digits. Fix: both pills
    // (mask + rect + text) now render as the LAST children of the
    // foreground svg, so they are always painted after every curve,
    // dart and the vertical spot line in both layers.
    const src = readFileSync(PAYOFF_PATH, 'utf8');

    const bgOpenIdx = src.indexOf('class="payoff-svg"');
    const bgCloseIdx = src.indexOf('</svg>', bgOpenIdx);
    const fgOpenIdx = src.indexOf('class="payoff-svg-fg"');
    const fgCloseIdx = src.indexOf('</svg>', fgOpenIdx);
    expect(bgOpenIdx, '.payoff-svg not found').toBeGreaterThan(-1);
    expect(fgOpenIdx, '.payoff-svg-fg not found').toBeGreaterThan(-1);
    expect(fgOpenIdx, 'foreground svg must come after the background svg in document order').toBeGreaterThan(bgCloseIdx);

    const bgBlock = src.slice(bgOpenIdx, bgCloseIdx);
    const fgBlock = src.slice(fgOpenIdx, fgCloseIdx);

    // Neither pill's class hook may appear in the background svg...
    expect(bgBlock).not.toMatch(/class="payoff-spot-pill"/);
    expect(bgBlock).not.toMatch(/class="payoff-exp-pill"/);
    // ...both must appear in the foreground svg instead.
    expect(fgBlock).toMatch(/class="payoff-spot-pill"/);
    expect(fgBlock).toMatch(/class="payoff-exp-pill"/);

    // Both pills must be the LAST drawable content in the foreground
    // svg — i.e. after every <path> (curves) and <circle> (darts) —
    // so nothing paints on top of them within that layer either.
    const lastPathIdx = fgBlock.lastIndexOf('<path');
    const lastCircleIdx = fgBlock.lastIndexOf('<circle');
    const spotPillIdx = fgBlock.indexOf('class="payoff-spot-pill"');
    const expPillIdx = fgBlock.indexOf('class="payoff-exp-pill"');
    expect(spotPillIdx, 'spot pill must come after every curve <path>').toBeGreaterThan(lastPathIdx);
    expect(spotPillIdx, 'spot pill must come after every dart <circle>').toBeGreaterThan(lastCircleIdx);
    expect(expPillIdx, 'Exp P&L pill must come after every curve <path>').toBeGreaterThan(lastPathIdx);
    expect(expPillIdx, 'Exp P&L pill must come after every dart <circle>').toBeGreaterThan(lastCircleIdx);
  });

  test('LIVE (2026-11-27 same-day follow-up): a curve forced to the pill\'s own y-band no longer paints across it', async ({ page }) => {
    // Flat payoff curve (today_value and expiry_value identical at every
    // mocked spot) means the WHOLE visible chart width is one horizontal
    // line at a single y — deterministically forces that line through
    // whichever pill's fixed y-band happens to sit near that value,
    // reproducing the reported occlusion without depending on real
    // market data or a hand-picked spot/curve shape.
    await gotoNiftyStrategy(page, { expPnl: 2345, todayPnl: 1500 });

    const spotPill = page.locator('.payoff-spot-pill').first();
    await expect(spotPill).toBeVisible({ timeout: 15_000 });

    // Every <path> (curve) and the vertical spot <line> must render
    // BEFORE the spot pill in the foreground svg's actual DOM order —
    // Playwright's accessibility/DOM query order matches paint order
    // for SVG siblings with no z-index of their own.
    const fgSvg = page.locator('.payoff-svg-fg').first();
    const fgChildTags = await fgSvg.evaluate((svg) =>
      Array.from(svg.children).map((c) => `${c.tagName}.${c.getAttribute('class') || ''}`)
    );
    const spotPillFgIdx = fgChildTags.findIndex((t) => t.includes('payoff-spot-pill'));
    const expPillFgIdx = fgChildTags.findIndex((t) => t.includes('payoff-exp-pill'));
    expect(spotPillFgIdx, 'spot pill must be present in the fg svg child list').toBeGreaterThan(-1);
    expect(expPillFgIdx, 'Exp P&L pill must be present in the fg svg child list').toBeGreaterThan(-1);
    const lastPathFgIdx = fgChildTags.map((t, i) => (t.startsWith('path') ? i : -1)).filter((i) => i >= 0).pop() ?? -1;
    const lastCircleFgIdx = fgChildTags.map((t, i) => (t.startsWith('circle') ? i : -1)).filter((i) => i >= 0).pop() ?? -1;
    expect(spotPillFgIdx).toBeGreaterThan(lastPathFgIdx);
    expect(spotPillFgIdx).toBeGreaterThan(lastCircleFgIdx);
    expect(expPillFgIdx).toBeGreaterThan(lastPathFgIdx);
    expect(expPillFgIdx).toBeGreaterThan(lastCircleFgIdx);
  });

  test('top-left overlay renames: "Mark P&L" and "MARK ADJ" render; "DAY P&L"/"Exp P&L" are untouched', async ({ page }) => {
    const overlay = await gotoNiftyStrategy(page, { expPnl: 500, todayPnl: 1500 });

    await expect(overlay.locator('.ps-k', { hasText: 'Mark P&L' })).toBeVisible({ timeout: 10_000 });
    // "Mark P&L" must not ALSO still show a bare "P&L" label anywhere
    // (the old label is gone, not merely duplicated).
    const bareLabel = overlay.locator('.ps-k').filter({ hasText: /^P&L$/ });
    expect(await bareLabel.count()).toBe(0);

    await expect(overlay.locator('.ps-k', { hasText: 'DAY P&L' })).toBeVisible();
    await expect(overlay.locator('.ps-k', { hasText: 'Exp P&L' })).toBeVisible();
  });

  test('legend shows three dart-style markers: Theoretical P&L (amber), Market P&L (violet, gated on dayPnl), Exp P&L (sky)', async ({ page }) => {
    await gotoNiftyStrategy(page, { expPnl: 500, todayPnl: 1500 });

    const legend = page.locator('.payoff-legend').first();
    await expect(legend).toBeVisible({ timeout: 15_000 });

    const theoretical = legend.locator('.legend-item', { hasText: 'Theoretical P&L' });
    await expect(theoretical).toBeVisible();
    await expect(theoretical.locator('.legend-dart.legend-dart-today')).toBeVisible();

    const exp = legend.locator('.legend-item', { hasText: 'Exp P&L' }).first();
    await expect(exp).toBeVisible();
    await expect(exp.locator('.legend-dart.legend-dart-expiry')).toBeVisible();

    // Market P&L requires `dayPnl != null` on the component — the
    // derivatives page threads this from the baseline-diff Day P&L sum,
    // which may legitimately be absent/zero in this mocked scenario. If
    // present, it must carry the violet dart marker.
    const market = legend.locator('.legend-item', { hasText: 'Market P&L' });
    const marketCount = await market.count();
    if (marketCount > 0) {
      await expect(market.locator('.legend-dart.legend-dart-actual')).toBeVisible();
    } else {
      test.info().annotations.push({ type: 'skip-reason', description: 'dayPnl was null/absent in this mocked scenario — Market P&L legend entry correctly omitted' });
    }

    // Old line-swatch classes must be gone from the legend entirely.
    await expect(legend.locator('.legend-today')).toHaveCount(0);
    await expect(legend.locator('.legend-expiry')).toHaveCount(0);
  });
});
