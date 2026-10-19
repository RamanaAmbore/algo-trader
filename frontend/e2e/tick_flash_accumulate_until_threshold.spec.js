/**
 * tick_flash_accumulate_until_threshold.spec.js
 *
 * Covers the 2026-10-19 "flash on any real change" redesign. The prior
 * version of this spec asserted an "accumulate until the cumulative move
 * crosses a percentage threshold" fix for GOLD-sized instruments (MCX,
 * ~₹150,000/unit, ₹1 tick size — a single tick is ~0.0007% there).
 * Operator decision superseded that model entirely: "instead of looking
 * for a threshold for change, show flash [when] the number changes" — no
 * percentage-threshold gating anywhere, no accumulation bookkeeping
 * needed. This spec now asserts the NEW behaviour: a single GOLD-sized
 * tick (far below the OLD 0.1% gate) flashes immediately, with no need
 * to accumulate several ticks first.
 *
 * Drives real LTP writes through `window.__stores.mergeSymbolUpdate` (dev
 * hook, (algo)/+layout.svelte) — the SAME write path real SSE ticks use
 * (symbolStore._mergeSymbolWrite → tickBus.emit) — rather than the bare
 * `window.__stores.tickBus.emit()` hook other specs use, because the
 * fix lives in MarketPulse's tickBus subscriber reading real LTP writes.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT     — drives the real production write path (mergeSymbolUpdate),
 *                not a bypass, so the assertion exercises the actual fix.
 *  2. Perf     — single synthetic symbol, no grid-wide scan beyond the one
 *                row under test.
 *  3. Stale    — confirms a single sub-0.1%-magnitude tick (the exact
 *                GOLD-sized move that used to need accumulation) flashes
 *                on its own — would catch a regression back to any
 *                percentage-threshold gate, cumulative or not.
 *  4. Reusable — follows the same mock-positions + retry-until-flash
 *                pattern as flash_fixes_2026_10_batch.spec.js (Item 9).
 *  5. UX       — the LTP cell gets tf-up, matching the app's existing
 *                flash-class convention (app.css .tf-up/.tf-down).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const ACCT = 'ZG0790';
const SYM = 'GOLDTEST01'; // synthetic — doesn't collide with any real holding
const POSITIONS_RE = /\/api\/positions\/(\?.*)?$/;

const ROW = {
  tradingsymbol: SYM, symbol: SYM, exchange: 'MCX',
  account: ACCT, product: 'NRML', quantity: 1, average_price: 150000, last_price: 150000,
  pnl: 0, realised: 0, unrealised: 0, day_pnl: 0, prev_settlement_pnl: 0,
  prev_close: 150000, close_price: 150000,
  pair_group_key: null, is_orphan: false, has_gtt: false,
};

async function mockPositions(page) {
  await page.route(POSITIONS_RE, async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    let body = { rows: [] };
    try {
      const real = await route.fetch();
      body = { ...(await real.json()) };
    } catch (_) { /* backend unreachable — minimal envelope below */ }
    body.rows = [ROW];
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

/** Write a real LTP value through the production write path. */
async function writeLtp(page, sym, ltp) {
  await page.evaluate(
    ([s, v]) => {
      /** @type {any} */
      const stores = window.__stores;
      if (!stores?.mergeSymbolUpdate) throw new Error('window.__stores.mergeSymbolUpdate not found — is DEV flag set?');
      stores.mergeSymbolUpdate(s, { ltp: v }, { ltp_ts: Date.now() });
    },
    [sym, ltp],
  );
}

/** Read back the symbol's current live LTP from symbolStore. */
async function readLtp(page, sym) {
  return page.evaluate(
    (s) => {
      /** @type {any} */
      const stores = window.__stores;
      return stores?.getSnapshot?.(s)?.ltp ?? null;
    },
    sym,
  );
}

/**
 * Retry-write large jumps (same pattern as flash_fixes_2026_10_batch.spec.js's
 * emitUntilClassMatches) until the target locator flashes tf-up, or maxMs
 * elapses. Needed because MarketPulse's tickBus subscription comes up only
 * after a real backend round-trip (loadPulse) completes — a single write
 * too early is silently missed (no subscriber registered yet).
 */
async function warmupUntilFlash(page, ltpCell, sym, startLtp, maxMs = 45_000) {
  const start = Date.now();
  let val = startLtp;
  while (Date.now() - start < maxMs) {
    await writeLtp(page, sym, val);
    await page.waitForTimeout(150);
    const matched = await ltpCell.evaluate(
      (el) => /tf-up/.test(el.className),
    ).catch(() => false);
    if (matched) return true;
    // Large (+5%) jump each retry so a genuinely fresh write always
    // flashes once the subscription is live, regardless of how many
    // attempts were silently dropped before it came up.
    val = val * 1.05;
    await page.waitForTimeout(150);
  }
  return false;
}

test.describe('Flash on any real change — GOLD-sized small ticks', () => {
  test.setTimeout(120_000);

  test('a single GOLD-sized tick (~0.0007%, far below the old threshold) flashes immediately, no accumulation needed', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const row = page.locator(`[row-id="${SYM}::${ACCT}__pos"]`)
      .filter({ has: page.locator('[col-id="ltp"]') }).first();
    const ltpCell = row.locator('[col-id="ltp"]');
    await expect(ltpCell).toBeVisible({ timeout: 60_000 });

    // Warm up until MarketPulse's real tickBus subscription proves itself
    // live via an observed flash. Confirms the checks below mean something.
    const warmedUp = await warmupUntilFlash(page, ltpCell, SYM, 150000);
    expect(warmedUp, 'MarketPulse tickBus subscription never came up — real backend round-trip too slow in this environment').toBe(true);

    // Let the warm-up flash fully clear (300ms clear timer in MarketPulse).
    await expect(ltpCell).not.toHaveClass(/tf-up/, { timeout: 2_000 });
    await page.waitForTimeout(300);

    // Read back the real baseline MarketPulse just locked in (the warm-up
    // flash's own value) so the single-tick math below is exact regardless
    // of how many warm-up retries were needed.
    const baseline = await readLtp(page, SYM);
    expect(baseline).toBeGreaterThan(0);

    // A single GOLD-sized tick: +₹1 on a ~₹150,000 instrument is ~0.0007%,
    // ~140x below the OLD default 0.1% gate — and well below even a single
    // accumulation step the old fix needed. With threshold gating removed
    // entirely, this ONE tiny real change must flash on its own.
    await writeLtp(page, SYM, baseline + 1);
    await expect(ltpCell).toHaveClass(/tf-up/, { timeout: 2_000 });

    // Let it clear, then confirm a second, equally tiny move in the other
    // direction flashes tf-down just as immediately — no residual
    // baseline/accumulation state left over from the first tick.
    await expect(ltpCell).not.toHaveClass(/tf-up/, { timeout: 2_000 });
    await page.waitForTimeout(300);
    const afterFirst = await readLtp(page, SYM);
    await writeLtp(page, SYM, afterFirst - 1);
    await expect(ltpCell).toHaveClass(/tf-down/, { timeout: 2_000 });
  });
});
