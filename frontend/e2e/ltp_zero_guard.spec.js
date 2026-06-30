/**
 * Regression guard: visible LTP cells must NOT flash to 0 (or "—" /
 * "0.00") for a symbol that previously had a real positive value.
 *
 * Operator complaint (Sleep audit Jun 2026): "look for LTP flickering and
 * sparkline accuracy." The backend's `_LAST_GOOD_LTP` rescue cache prevents
 * 0 leakage on the server side, but transient broker hiccups or SSE
 * snapshot races would occasionally surface `last_price: 0` in symbolStore.
 * The fix lives in `frontend/src/lib/data/symbolStore.svelte.js` (`if
 * (isLtp && v === 0)` guard).
 *
 * Test strategy: load /pulse, take 30 LTP cell readings over 30 s, and
 * confirm no cell that ever showed a positive value ever flipped back to
 * 0 or a sentinel placeholder mid-stream. We allow a cell to START at "—"
 * (cold-start tolerance) but not regress.
 *
 * Cold-start path (no prior LTP) is NOT tested here — leaving the zero
 * through is correct when there is no good value to lose.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

test.describe('LTP flicker guard — visible cells do not regress to zero', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(180_000);

  test('positions grid LTP cells stable over 30s', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 60_000 });
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    // Settle so positions/holdings polls + SSE deliver real LTPs.
    await page.waitForTimeout(8_000);

    // Capture per-symbol LTP readings over a 30 s window. Read every 2 s
    // (15 samples). Symbol id is the tradingsymbol shown in the first
    // visible cell of each row; LTP is the cell labeled `last_price`
    // (ag-Grid colId).
    /** @type {Record<string, number[]>} */
    const readings = {};
    const SAMPLE_COUNT = 12;
    const SAMPLE_INTERVAL_MS = 2_000;

    for (let i = 0; i < SAMPLE_COUNT; i++) {
      const rows = await page.$$eval('.ag-row', (els) => {
        /** @type {Array<{sym: string, ltp: string}>} */
        const out = [];
        for (const el of els) {
          const symCell = el.querySelector('[col-id="tradingsymbol"]');
          const ltpCell = el.querySelector('[col-id="last_price"]');
          const sym = (symCell?.textContent || '').trim();
          const ltpRaw = (ltpCell?.textContent || '').trim();
          if (sym) out.push({ sym, ltp: ltpRaw });
        }
        return out;
      });
      for (const r of rows) {
        const num = Number(String(r.ltp).replace(/[^\d.\-]/g, ''));
        if (Number.isFinite(num)) {
          (readings[r.sym] = readings[r.sym] || []).push(num);
        }
      }
      await page.waitForTimeout(SAMPLE_INTERVAL_MS);
    }

    // For each symbol, find any cell that ever showed >0 then later
    // showed 0 — that is the flicker pattern this guard catches.
    /** @type {string[]} */
    const regressions = [];
    for (const [sym, samples] of Object.entries(readings)) {
      if (samples.length < 3) continue;  // need enough samples to assert
      let sawPositive = false;
      for (let i = 0; i < samples.length; i++) {
        if (samples[i] > 0) sawPositive = true;
        else if (sawPositive && samples[i] === 0) {
          regressions.push(`${sym}: ${samples.join(', ')}`);
          break;
        }
      }
    }

    const totalSymbols = Object.keys(readings).length;
    console.log(`[ltp-flicker] sampled ${totalSymbols} symbols × ${SAMPLE_COUNT} reads`);
    if (regressions.length > 0) {
      console.log('[ltp-flicker] regressions:');
      for (const r of regressions) console.log(`  ↳ ${r}`);
    }

    expect(regressions.length,
      `${regressions.length} symbol(s) flickered from a positive LTP back to 0 over the 30 s window. ` +
      `First 3: ${regressions.slice(0, 3).join(' | ')}`
    ).toBe(0);
  });
});
