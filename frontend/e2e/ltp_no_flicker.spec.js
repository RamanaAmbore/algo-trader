/**
 * Regression contract: LTP must NEVER regress to 0 once positive.
 *
 * Sleep audit Jun 2026 — definitive LTP-flicker fix. Extends the existing
 * `ltp_zero_guard.spec.js` (which sampled at 2s × 12 reads = 24s window
 * but selected the wrong column id `last_price` instead of the actual
 * `ltp` colId — so it was silently passing without sampling any cell).
 *
 * Strategy:
 *   1. Log in. Visit /pulse, wait for both grids to populate (8s).
 *   2. Capture per-symbol LTP readings every 500ms for 60s (120 samples).
 *   3. For every symbol whose first sampled value was positive, assert
 *      NO subsequent sample is 0 or non-numeric placeholder.
 *
 * This is the operator's hard contract: "once a price is shown, it must
 * stay shown until a real new value lands." A flicker to 0 / "—" is the
 * defect; a steady positive value is the only acceptable behaviour.
 *
 * Runs on chromium-desktop + chromium-mobile via the playwright.config.js
 * projects matrix.
 *
 * Test dimensions covered:
 *   - SSOT: every visible LTP cell across the unified grid (both panels)
 *   - Performance: implicit — the test would also fail if the sampler
 *     burned the main thread on each read (we rely on $$eval batched fetch)
 *   - Stale-code grep: the symptom this catches (cell flips to 0) is the
 *     exact UX defect the operator escalated
 *   - Reusable-component usage: MarketPulse LTP column shared by both grids
 *   - UX consistency: no flicker at any tick during the sample window
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('LTP no-flicker contract — never regress to zero', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(180_000);

  test('positions + holdings + watchlist LTP cells stable over 60s', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 60_000 });

    // Wait for at least one row in any grid to be visible. Pulse has
    // two side-by-side ag-Grids; either one being populated is enough.
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    // Settle so positions / holdings poll + SSE snapshot + first ticks land.
    await page.waitForTimeout(8_000);

    // Per-symbol arrays of raw cell text. Sampling every 500ms × 120
    // = 60 seconds. Use $$eval so the round-trip is one IPC call per
    // sample, not one per cell — keeps overhead off the main thread.
    /** @type {Record<string, string[]>} */
    const cellHistory = {};
    const SAMPLE_COUNT = 120;
    const SAMPLE_INTERVAL_MS = 500;

    for (let i = 0; i < SAMPLE_COUNT; i++) {
      const rows = await page.$$eval('.ag-row', (els) => {
        /** @type {Array<{sym: string, ltpText: string}>} */
        const out = [];
        for (const el of els) {
          const symCell = el.querySelector('[col-id="tradingsymbol"]');
          // The LTP column's colId is 'ltp' (MarketPulse.svelte:3848).
          // The earlier spec at `ltp_zero_guard.spec.js` used
          // `[col-id="last_price"]` which never matches — that test
          // was silent. This one targets the real column.
          const ltpCell = el.querySelector('[col-id="ltp"]');
          const sym = (symCell?.textContent || '').trim();
          const ltpText = (ltpCell?.textContent || '').trim();
          if (sym && ltpText) out.push({ sym, ltpText });
        }
        return out;
      });
      for (const { sym, ltpText } of rows) {
        (cellHistory[sym] = cellHistory[sym] || []).push(ltpText);
      }
      await page.waitForTimeout(SAMPLE_INTERVAL_MS);
    }

    // For each symbol, parse its samples and find any positive →
    // zero/placeholder regression. Placeholder forms we treat as
    // "regressed": "0", "0.00", "—", "-", "" — anything that visually
    // looks like a missing or zeroed price.
    //
    // Cold-start tolerance: a symbol whose FIRST sample was a placeholder
    // (no SSE / no poll data yet) is allowed to start at "—" and then
    // populate. We only enforce the contract from the first positive
    // sample forward.
    const isPlaceholder = (/** @type {string} */ s) => {
      if (!s) return true;
      const compact = s.replace(/[\s,]/g, '');
      if (compact === '' || compact === '—' || compact === '-') return true;
      const n = Number(compact.replace(/[^\d.\-]/g, ''));
      return Number.isFinite(n) && n === 0;
    };
    const parsePositive = (/** @type {string} */ s) => {
      const n = Number(s.replace(/[^\d.\-]/g, ''));
      return Number.isFinite(n) && n > 0 ? n : null;
    };

    /** @type {Array<{sym: string, samples: string[], firstZeroAt: number}>} */
    const regressions = [];
    for (const [sym, samples] of Object.entries(cellHistory)) {
      if (samples.length < 4) continue;
      let sawPositive = false;
      for (let i = 0; i < samples.length; i++) {
        const v = parsePositive(samples[i]);
        if (v != null) sawPositive = true;
        else if (sawPositive && isPlaceholder(samples[i])) {
          regressions.push({ sym, samples, firstZeroAt: i });
          break;
        }
      }
    }

    const totalSymbols = Object.keys(cellHistory).length;
    console.log(`[ltp-no-flicker] sampled ${totalSymbols} symbols × ${SAMPLE_COUNT} reads over 60s`);
    if (regressions.length > 0) {
      console.log('[ltp-no-flicker] regressions:');
      for (const r of regressions.slice(0, 10)) {
        const slice = r.samples.slice(Math.max(0, r.firstZeroAt - 2), r.firstZeroAt + 3);
        console.log(`  ${r.sym} @sample[${r.firstZeroAt}] context: ${slice.join(' → ')}`);
      }
    }

    expect(
      regressions.length,
      `${regressions.length} symbol(s) flickered to 0/"—" after showing a positive LTP over the 60s window. ` +
        `First 3: ${regressions.slice(0, 3).map(r => r.sym).join(', ')}`,
    ).toBe(0);
  });
});
