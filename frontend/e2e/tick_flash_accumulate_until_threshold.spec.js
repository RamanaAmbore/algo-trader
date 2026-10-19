/**
 * tick_flash_accumulate_until_threshold.spec.js
 *
 * Covers the 2026-10-19 "GOLD never flashes" fix: several consecutive
 * small-percentage ticks that individually stay under the configured
 * `ltpFlashPct` threshold (default 0.1%) must still flash once their
 * CUMULATIVE move since the last visible flash crosses the threshold —
 * comparing only against the immediately-preceding raw tick (the old bug)
 * lets a GOLD-sized instrument (MCX, ~₹150,000/unit, ₹1 tick size — a
 * single tick is ~0.0007%, ~140x below the gate) never flash at all.
 *
 * Drives real LTP writes through `window.__stores.mergeSymbolUpdate` (dev
 * hook, (algo)/+layout.svelte) — the SAME write path real SSE ticks use
 * (symbolStore._mergeSymbolWrite → tickBus.emit) — rather than the bare
 * `window.__stores.tickBus.emit()` hook other specs use, because this fix
 * lives specifically in MarketPulse's tickBus subscriber reading the
 * symbol's live LTP from symbolStore (see MarketPulse.svelte onMount).
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT     — drives the real production write path (mergeSymbolUpdate),
 *                not a bypass, so the assertion exercises the actual fix.
 *  2. Perf     — single synthetic symbol, no grid-wide scan beyond the one
 *                row under test.
 *  3. Stale    — confirms intermediate sub-threshold writes do NOT flash
 *                (would catch a regression back to "flash on every tick").
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
    // clears the threshold once the subscription is live, regardless of
    // how many attempts were silently dropped before it came up.
    val = val * 1.05;
    await page.waitForTimeout(150);
  }
  return false;
}

test.describe('Accumulate-until-threshold — GOLD-sized small ticks', () => {
  test.setTimeout(120_000);

  test('several sub-threshold ticks accumulate; only the cumulative-crossing tick flashes', async ({ page }) => {
    await loginAsAdmin(page);
    await mockPositions(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const row = page.locator(`[row-id="${SYM}::${ACCT}__pos"]`)
      .filter({ has: page.locator('[col-id="ltp"]') }).first();
    const ltpCell = row.locator('[col-id="ltp"]');
    await expect(ltpCell).toBeVisible({ timeout: 60_000 });

    // Warm up until MarketPulse's real tickBus subscription proves itself
    // live via an observed flash. Confirms the negative checks below mean
    // something.
    const warmedUp = await warmupUntilFlash(page, ltpCell, SYM, 150000);
    expect(warmedUp, 'MarketPulse tickBus subscription never came up — real backend round-trip too slow in this environment').toBe(true);

    // Let the warm-up flash fully clear (300ms clear timer in MarketPulse).
    await expect(ltpCell).not.toHaveClass(/tf-up/, { timeout: 2_000 });
    await page.waitForTimeout(300);

    // Read back the real baseline MarketPulse just locked in (the warm-up
    // flash's own value) so the percentage math below is exact regardless
    // of how many warm-up retries were needed.
    const baseline = await readLtp(page, SYM);
    expect(baseline).toBeGreaterThan(0);

    // Step 1: +0.05% from baseline — individually AND cumulatively below
    // the default 0.1% threshold. Must NOT flash.
    await writeLtp(page, SYM, baseline * 1.0005);
    await page.waitForTimeout(300);
    await expect(ltpCell).not.toHaveClass(/tf-up/);
    await expect(ltpCell).not.toHaveClass(/tf-down/);

    // Step 2: a further small move — cumulative from the UNCHANGED baseline
    // is now +0.09%, still below 0.1%. This is the critical regression
    // check: tick-to-tick-only comparison (the old bug) would compare this
    // write against step 1's value (a ~0.04% delta) and never catch up;
    // the fix compares against the still-fixed `baseline`, correctly
    // staying just under threshold. Must NOT flash yet.
    await writeLtp(page, SYM, baseline * 1.0009);
    await page.waitForTimeout(300);
    await expect(ltpCell).not.toHaveClass(/tf-up/);
    await expect(ltpCell).not.toHaveClass(/tf-down/);

    // Step 3: cumulative from baseline is now +0.15% — crosses the 0.1%
    // threshold. Under the OLD tick-to-tick-only bug, this write's delta
    // from step 2 alone is only ~0.06% (also sub-threshold) and would
    // NEVER flash — exactly the GOLD defect. The fix compares against the
    // fixed `baseline` (unmoved since steps 1–2 never flashed), correctly
    // firing here.
    await writeLtp(page, SYM, baseline * 1.0015);
    await expect(ltpCell).toHaveClass(/tf-up/, { timeout: 2_000 });
  });
});
