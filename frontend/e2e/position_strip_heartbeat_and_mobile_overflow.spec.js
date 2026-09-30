/**
 * position_strip_heartbeat_and_mobile_overflow.spec.js
 *
 * Two PositionStrip (operator name: "NavStrip") defect fixes, 2026-09-30:
 *
 *  #1 Heartbeat animation (.ps-heartbeat / @keyframes ps-heartbeat-pulse)
 *     never fired live. Root cause was NOT in PositionStrip.svelte's own
 *     reactive wiring (verified correct via static analysis): it was in
 *     marketDataStores.svelte.js's `_tickBookPollers()` — the book-poller
 *     cadence gate used `_holdingsSnapshotAt` (set whenever /api/holdings
 *     served a daily_book snapshot, which happens for the ENTIRE
 *     15:30-23:30 IST window because the holdings route gates on NSE
 *     hours only) as a proxy for "market fully closed". During that
 *     ~8h/day window — NSE closed, MCX still trading — the poller
 *     silently collapsed from 5s to 30min cadence, starving
 *     bookPollerTick / _pollCycleStamp and, with it, the heartbeat pulse
 *     (plus live positions/margin/cash refresh generally). Fixed by
 *     gating on `isNseOpen() || isMcxOpen()` directly instead of the
 *     NSE-only holdings snapshot flag.
 *
 *  #2 Mobile: PositionStrip content overflowing a narrow viewport was
 *     shifting/hiding the whole page instead of scrolling within the
 *     strip's own `overflow-x: auto` box. `.ps-strip`'s box model was
 *     already correctly constrained (fixed + left:0/right:0, no
 *     min-width anywhere including the mobile @media overrides) so it
 *     cannot itself grow past the viewport — the symptom is a touch
 *     scroll-chaining artifact: once the strip's own horizontal scroll
 *     hits its edge, a continuing swipe hands momentum to the document,
 *     which has no overflow-x guard anywhere in app.css. Fixed via
 *     `overscroll-behavior-x: contain` scoped to `.ps-strip`.
 *
 * Five quality dimensions:
 *  1. SSOT    — heartbeat cadence now derives from the same isNseOpen/
 *               isMcxOpen gate every other market-hours check in the app
 *               uses, instead of a holdings-route side-effect.
 *  2. Perf    — no change to poll frequency outside the previously-
 *               broken 15:30-23:30 window; still throttles fully-closed
 *               hours to 30 min as designed.
 *  3. Stale   — asserts the CSS keyframe animation actually engages
 *               (getAnimations()), not just a text/class snapshot.
 *  4. Reuse   — reuses the loginAsAdmin + page.route mock fixture
 *               pattern established in closed_hours_day_change.spec.js
 *               and ltp_flash_cascade.spec.js.
 *  5. UX      — verifies BOTH the strip's own internal scroll still
 *               works AND the page never moves — the fix must not trade
 *               one defect for the other.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30_000;

// ── Shared fixtures ─────────────────────────────────────────────────────────

async function mockMarketOpen(page) {
  await page.route('**/api/market/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        nse_open: true,
        mcx_open: false,
        any_open: true,
        is_holiday: false,
      }),
    })
  );
}

/** NSE closed / MCX still open — the exact window (15:30-23:30 IST daily)
 *  that triggered the poller-cadence regression: the holdings route
 *  gates on NSE hours only, so it serves a daily_book snapshot (`as_of`
 *  set) throughout this whole window even though the overall market
 *  (MCX) is still live. */
async function mockMcxOnlyOpen(page) {
  await page.route('**/api/market/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        nse_open: false,
        mcx_open: true,
        any_open: true,
        is_holiday: false,
      }),
    })
  );
}

/** Large-notional, multi-account book — big enough that the compact
 *  aggregate values (₹-crore scale) push the four P/M/C/H pills past a
 *  412px viewport, so the mobile spec's overflow scenario isn't vacuous. */
function bigPositionsFixture() {
  const accounts = ['ZG0790', 'ZJ6294', 'ZQ1122'];
  const rows = accounts.map((account, i) => ({
    account, tradingsymbol: `NIFTY26JUN${25000 + i * 100}CE`, exchange: 'NFO',
    product: 'NRML', quantity: 50 * (i + 1), average_price: 180.5,
    last_price: 210.25 + i, close_price: 190.0,
    pnl: 148_375 * (i + 1) * (i % 2 === 0 ? 1 : -1),
    pnl_percentage: 16.5, day_change_val: 98_512.75 * (i + 1),
    day_change_percentage: 5.4, unrealised: 148_375, realised: 0,
  }));
  const summary = accounts.map((account, i) => ({
    account, pnl: 148_375 * (i + 1), day_change_val: 98_512.75 * (i + 1),
    day_change_percentage: 5.4,
  }));
  summary.push({
    account: 'TOTAL',
    pnl: rows.reduce((s, r) => s + r.pnl, 0),
    day_change_val: rows.reduce((s, r) => s + r.day_change_val, 0),
    day_change_percentage: 5.4,
  });
  return { rows, summary, refreshed_at: new Date().toISOString(), source: 'live' };
}

function bigHoldingsFixture() {
  const accounts = ['ZG0790', 'ZJ6294'];
  const rows = accounts.map((account, i) => ({
    account, tradingsymbol: i === 0 ? 'RELIANCE' : 'TCS', exchange: 'NSE',
    product: 'CNC', quantity: 100 * (i + 1), average_price: 2800,
    last_price: 2950 + i * 10, close_price: 2900, pnl: 15_000 * (i + 1),
    pnl_percentage: 5.36, day_change_val: 5_000 * (i + 1),
    day_change_percentage: 1.72, cur_val: 295_000 * (i + 1), inv_val: 280_000 * (i + 1),
  }));
  const summary = accounts.map((account, i) => ({
    account, pnl: 15_000 * (i + 1), day_change_val: 5_000 * (i + 1),
    day_change_percentage: 1.72, pnl_percentage: 5.36,
    cur_val: 295_000 * (i + 1), inv_val: 280_000 * (i + 1),
  }));
  return { rows, summary, refreshed_at: new Date().toISOString(), source: 'live' };
}

function bigFundsFixture() {
  const rows = [
    { account: 'ZG0790', avail_margin: 1_245_000, used_margin: 340_500, cash: 980_000, live_cash: 912_400, collateral: 150_000 },
    { account: 'ZJ6294', avail_margin: 2_310_750, used_margin: 512_300, cash: 1_450_000, live_cash: 1_390_200, collateral: 220_000 },
    { account: 'ZQ1122', avail_margin: 645_200, used_margin: 98_100, cash: 512_000, live_cash: 498_600, collateral: 0 },
    {
      account: 'TOTAL',
      avail_margin: 1_245_000 + 2_310_750 + 645_200,
      used_margin: 340_500 + 512_300 + 98_100,
      cash: 980_000 + 1_450_000 + 512_000,
      live_cash: 912_400 + 1_390_200 + 498_600,
      collateral: 370_000,
    },
  ];
  return { rows, refreshed_at: new Date().toISOString() };
}

/** @param {import('@playwright/test').Page} page
 *  @param {{ holdingsAsOf?: string|null }} [opts] holdingsAsOf mimics the
 *    real backend's closed_hours_or_broker snapshot path (holdings.py
 *    gates on NSE hours only) — set whenever the holdings response is a
 *    daily_book snapshot rather than a live broker fetch. */
async function mockBook(page, opts = {}) {
  const positions = bigPositionsFixture();
  const holdings = bigHoldingsFixture();
  const funds = bigFundsFixture();
  if (opts.holdingsAsOf) holdings.as_of = opts.holdingsAsOf;
  await page.route('**/api/positions**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(positions) })
  );
  await page.route('**/api/holdings**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(holdings) })
  );
  await page.route('**/api/funds**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(funds) })
  );
}

// ── #1 Heartbeat ─────────────────────────────────────────────────────────────

/** Observe `.ps-strip`'s class attribute for `windowMs`, recording every
 *  ps-heartbeat toggle plus whether the real CSS animation was engaged
 *  (Svelte hashes the keyframe name, hence the `.includes()` match). */
async function observeHeartbeat(page, windowMs) {
  return page.evaluate((ms) => new Promise((resolve) => {
    const el = document.querySelector('.ps-strip');
    /** @type {Array<{ t: number, has: boolean, animName: string|null }>} */
    const record = [];
    const mo = new MutationObserver(() => {
      const has = el.classList.contains('ps-heartbeat');
      let animName = null;
      if (has) {
        const anims = el.getAnimations();
        const hb = anims.find((a) => (a.animationName || '').includes('ps-heartbeat-pulse'));
        animName = hb ? hb.animationName : null;
      }
      record.push({ t: performance.now(), has, animName });
    });
    mo.observe(el, { attributes: true, attributeFilter: ['class'] });
    setTimeout(() => { mo.disconnect(); resolve(record); }, ms);
  }), windowMs);
}

test.describe('PositionStrip heartbeat — fires during market-open hours', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('.ps-heartbeat applies with the ps-heartbeat-pulse animation, ~300ms window', async ({ page }) => {
    await loginAsAdmin(page);
    await mockMarketOpen(page);
    await mockBook(page);

    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    // Observe the strip's class attribute for up to ~13s — the book
    // poller ticks every 5s in market-open mode (post-fix), so this
    // window covers at least two cycles even with mount-timing jitter.
    const observed = await observeHeartbeat(page, 13_000);

    // 1. The class actually applied at least once.
    const onEvents = observed.filter((r) => r.has);
    expect(onEvents.length, `no .ps-heartbeat toggle observed in 13s: ${JSON.stringify(observed)}`).toBeGreaterThan(0);

    // 2. While applied, the real CSS animation (ps-heartbeat-pulse,
    //    Svelte-hashed name) was actually engaged — not just the class
    //    string with no visual effect.
    expect(onEvents.some((r) => r.animName), `class applied but no ps-heartbeat-pulse animation found: ${JSON.stringify(onEvents)}`).toBe(true);

    // 3. The class clears again within roughly the 300ms pulse window
    //    (generous tolerance for CI scheduling jitter).
    const firstOnIdx = observed.findIndex((r) => r.has);
    const nextOff = observed.slice(firstOnIdx + 1).find((r) => !r.has);
    expect(nextOff, 'class never cleared after applying').toBeTruthy();
    const duration = nextOff.t - observed[firstOnIdx].t;
    expect(duration).toBeGreaterThan(100);
    expect(duration).toBeLessThan(1200);
  });

  // Regression test for the actual root cause (see file header). The
  // generic "market open" test above passes even on the pre-fix code,
  // because it never exercises the specific NSE-closed/MCX-open +
  // holdings-snapshot combination that collapsed the poller to 30min —
  // this test reproduces that exact combination directly.
  test('NSE closed + MCX open + holdings snapshot — poller stays at 5s cadence, not 30min', async ({ page }) => {
    await loginAsAdmin(page);
    await mockMcxOnlyOpen(page);
    // holdingsAsOf set — mirrors the real backend's closed_hours_or_broker
    // NSE-only gate serving a daily_book snapshot for holdings even
    // though MCX (and thus the overall market) is still open.
    await mockBook(page, { holdingsAsOf: new Date().toISOString() });

    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    // With the pre-fix 30min-cadence bug, bookPollerTick fires once at
    // mount and then not again for 30 minutes, so .ps-heartbeat would
    // toggle on/off exactly ONCE within this 12s window. The fixed 5s
    // cadence must produce at least two toggles.
    const observed = await observeHeartbeat(page, 12_000);
    const onCount = observed.filter((r) => r.has).length;
    expect(onCount, `expected ≥2 heartbeat cycles at live 5s cadence, saw ${onCount}: ${JSON.stringify(observed)}`).toBeGreaterThanOrEqual(2);
  });
});

// ── #2 Mobile overflow ───────────────────────────────────────────────────────

test.describe('PositionStrip mobile — internal scroll only, no page-level shift', () => {
  test.use({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });

  test('overflowing content scrolls within .ps-strip; document never overflows horizontally', async ({ page }) => {
    await loginAsAdmin(page);
    await mockMarketOpen(page);
    await mockBook(page);

    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.ps-strip');
    await expect(strip).toBeVisible({ timeout: TIMEOUT });
    // Wait for the pills to actually paint big-notional values (not the
    // ₹0 placeholder) so the overflow scenario is real, not vacuous.
    await expect(strip.locator('.ps-agg-v').first()).not.toHaveText('₹0', { timeout: TIMEOUT });

    // At 412px the mobile @media rules (`@media (max-width: 640px)` /
    // `380px` in PositionStrip.svelte) already shrink font-size enough
    // that this fixture's compact (aggCompact — K/L/C suffix, ≤7 chars)
    // values alone measure ~382px, comfortably under the ~404px client
    // area — i.e. the strip's own responsive sizing is already tuned
    // well for typical portfolio magnitudes at this exact viewport. To
    // reliably exercise the overflow/scroll-chaining CODE PATH under
    // test (independent of the exact numeric magnitude that happens to
    // trip it for a given operator/account/zoom level — a real,
    // reachable state e.g. under iOS "Larger Text" accessibility zoom,
    // or simply more/longer account labels than this fixture models),
    // widen the value cells slightly via injected CSS. This does not
    // touch `.ps-strip`'s own box model (still fixed + left:0/right:0,
    // no min-width) — only forces its CHILDREN wide enough to need the
    // strip's existing overflow-x:auto to actually engage.
    await page.addStyleTag({ content: '.ps-strip .ps-agg-v { min-width: 46px; }' });

    // Precondition: the strip genuinely needs to scroll internally at
    // this viewport — otherwise the rest of this test is vacuous.
    const pre = await page.evaluate(() => {
      const el = document.querySelector('.ps-strip');
      return {
        stripScrollWidth: el.scrollWidth,
        stripClientWidth: el.clientWidth,
        docScrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
        scrollX: window.scrollX,
      };
    });
    expect(pre.stripScrollWidth, 'strip content does not overflow — test fixture too small').toBeGreaterThan(pre.stripClientWidth);
    // No page-level horizontal overflow BEFORE any gesture. Assert
    // against the fixed 412 viewport width, not window.innerWidth,
    // which can itself have already grown if the bug is present.
    expect(pre.docScrollWidth).toBeLessThanOrEqual(412 + 1);
    expect(pre.scrollX).toBe(0);

    // Simulate a horizontal swipe gesture ON the strip via CDP (real
    // touch-scroll-chaining path — a plain `el.scrollLeft = x` write
    // would not reproduce the browser's momentum/rubber-band behavior).
    const box = await strip.boundingBox();
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Input.synthesizeScrollGesture', {
      x: box.x + box.width / 2,
      y: box.y + box.height / 2,
      xDistance: -250, // negative = swipe content leftward (scroll right)
      yDistance: 0,
      gestureSourceType: 'touch',
      speed: 800,
    });
    // Let momentum/animation settle.
    await page.waitForTimeout(500);

    const post = await page.evaluate(() => {
      const el = document.querySelector('.ps-strip');
      return {
        stripScrollLeft: el.scrollLeft,
        docScrollWidth: document.documentElement.scrollWidth,
        scrollX: window.scrollX,
      };
    });

    // The strip's OWN internal scroll moved...
    expect(post.stripScrollLeft, 'strip did not scroll internally on swipe').toBeGreaterThan(0);
    // ...but the document/page itself never moved or grew wider.
    expect(post.scrollX, 'the whole page shifted right on strip swipe').toBe(0);
    expect(post.docScrollWidth).toBeLessThanOrEqual(412 + 1);
  });
});
