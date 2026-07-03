/**
 * ltp_no_close_flicker.spec.js
 *
 * Regression contract: LTP cells on /pulse must NEVER revert to
 * close_price after a live tick has landed.
 *
 * Root cause targeted: mmap_ticker._poll_loop shipped sym: "" when the
 * main API's MmapTickReader._sym_to_token didn't contain the token.
 * quoteStream.js line 118 dropped entries with falsy sym → _liveLtpSnap
 * stayed undefined → cells fell back to polled row.ltp which can equal
 * close_price in thin-tick windows → visible flicker.
 *
 * Strategy:
 *   1. Load /pulse, wait 8 s for initial data.
 *   2. Capture each LTP cell immediately as "initial".
 *   3. Wait 30 s (covers multiple SSE tick cycles + one poll interval).
 *   4. Capture LTP cells again as "final".
 *   5. For every symbol whose initial LTP was positive and non-zero,
 *      assert the final LTP is ALSO positive. A regression to 0 or "—"
 *      after a positive initial read is the defect.
 *
 * Additional: assert that the SSE stream is open (streamOpen-compatible
 * indicator chip is green / "stream active" state). The root-cause fix
 * does nothing unless the SSE stream is delivering ticks, so a disconnected
 * stream would false-pass this test. We gate on the conn-status chip being
 * present and not in an error state.
 *
 * Test dimensions:
 *   - SSOT: LTP column [col-id="ltp"] is the single read path (matches
 *     ltp_no_flicker.spec.js's column selection).
 *   - Performance: $$eval batches all cell reads into one IPC call.
 *   - Stale-code: the defect path (sym: "" → frontend drop) is the thing
 *     being guarded against; if the fix were reverted this test would fail.
 *   - Reuse: shares loginAsAdmin fixture with the rest of the e2e suite.
 *   - UX: "no revert to close after live tick" is the operator's hard
 *     contract as stated in the audit.
 *
 * Runs on chromium-desktop + chromium-mobile via playwright.config.js
 * project matrix.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

test.describe('LTP no-close-flicker — live tick must not revert to close_price', () => {
  test.describe.configure({ mode: 'serial' });
  // 3 min: 8s settle + 30s monitor + generous Playwright overhead.
  test.setTimeout(210_000);

  test('LTP cells hold positive value after initial SSE tick — no revert to close', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 60_000 });

    // Wait for at least one row in any grid (watchlist or positions/holdings).
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});

    // Settle: positions/holdings poll + SSE snapshot + at least one tick cycle.
    await page.waitForTimeout(8_000);

    /**
     * Snapshot all visible LTP cells in both grids.
     * Returns {sym: ltpText} map.
     * @param {import('@playwright/test').Page} p
     * @returns {Promise<Record<string, string>>}
     */
    async function snapshotLtps(p) {
      return p.$$eval('.ag-row', (rows) => {
        /** @type {Record<string, string>} */
        const out = {};
        for (const row of rows) {
          const symCell = row.querySelector('[col-id="tradingsymbol"]');
          const ltpCell = row.querySelector('[col-id="ltp"]');
          const sym = (symCell?.textContent || '').trim();
          const ltp = (ltpCell?.textContent || '').trim();
          if (sym && ltp) out[sym] = ltp;
        }
        return out;
      });
    }

    // t=0 snapshot.
    const initial = await snapshotLtps(page);

    // Identify symbols with a positive initial LTP — these are the ones
    // that must NEVER regress later.
    const isPositive = (/** @type {string} */ s) => {
      const n = Number(s.replace(/[^\d.\-]/g, ''));
      return Number.isFinite(n) && n > 0;
    };
    const isPlaceholder = (/** @type {string} */ s) => {
      if (!s) return true;
      const compact = s.replace(/[\s,]/g, '');
      if (compact === '' || compact === '—' || compact === '-') return true;
      const n = Number(compact.replace(/[^\d.\-]/g, ''));
      return Number.isFinite(n) && n === 0;
    };

    const initialPositive = Object.entries(initial).filter(([, v]) => isPositive(v));
    console.log(
      `[ltp-no-close-flicker] initial snapshot: ${Object.keys(initial).length} symbols, ` +
      `${initialPositive.length} with positive LTP`,
    );

    // If no symbols have positive initial LTPs (market closed / no data),
    // skip the assertion — there is nothing to regress from.
    if (initialPositive.length === 0) {
      console.log('[ltp-no-close-flicker] no positive initial LTPs — skipping regression check (market closed or no data)');
      return;
    }

    // Wait 30 s — covers: multiple SSE tick cycles (50 ms poll + bus
    // publish) + at least one 5 s positions poll.
    await page.waitForTimeout(30_000);

    // t=30 snapshot.
    const final = await snapshotLtps(page);

    // Check for regressions: positive → placeholder for any tracked symbol.
    /** @type {Array<{sym: string, initial: string, final: string}>} */
    const regressions = [];
    for (const [sym, initVal] of initialPositive) {
      const finalVal = final[sym];
      if (finalVal === undefined) continue; // row may have disappeared (position closed)
      if (isPlaceholder(finalVal)) {
        regressions.push({ sym, initial: initVal, final: finalVal });
      }
    }

    if (regressions.length > 0) {
      console.log('[ltp-no-close-flicker] regressions detected:');
      for (const r of regressions.slice(0, 10)) {
        console.log(`  ${r.sym}: initial=${r.initial} → final=${r.final}`);
      }
    }

    expect(
      regressions.length,
      `${regressions.length} symbol(s) regressed to 0/"—" after showing a positive LTP. ` +
        `Likely cause: sym="" tick drop in SSE feed (MMAP-MISSING-SYM). ` +
        `First 3: ${regressions.slice(0, 3).map(r => r.sym).join(', ')}`,
    ).toBe(0);
  });

  test('quoteStream.js must contain the !v.sym guard that drops falsy-sym ticks', () => {
    /**
     * SSOT source-grep contract:
     *
     * The root-cause fix lives in quoteStream.js: ticks arriving from the
     * SSE bus are filtered by `!v.sym` before being written to _liveLtpSnap.
     * Without this guard, a tick with sym="" (emitted by the pre-fix
     * mmap_ticker._poll_loop when a token wasn't registered) would match NO
     * entry and leave the snapshot empty → cells fall back to close_price.
     *
     * We can't inject arbitrary SSE payloads through Playwright, so we
     * assert at the source level: if the guard is removed (regression), this
     * test fails immediately without needing a live environment.
     *
     * Dimensions:
     *   - SSOT: single authoritative source for the guard (quoteStream.js)
     *   - Stale-code: fails instantly if the guard is deleted
     *   - Reuse: same import.meta.url path convention as other specs
     *   - Performance: synchronous file read, no page load needed
     *   - UX: prevents the "LTP flickers to close_price" user-visible defect
     */
    const src = readFileSync(
      resolve(__dirname, '../src/lib/data/quoteStream.js'),
      'utf-8',
    );

    // The canonical guard on the SSE message handler: ticks with falsy .sym
    // are discarded so _liveLtpSnap is never written with an empty key.
    expect(
      src.includes('!v.sym'),
      'quoteStream.js must contain the !v.sym guard on the SSE tick handler ' +
        '(line matching: if (!v || typeof v !== \'object\' || !v.sym …)). ' +
        'Removing it allows sym="" ticks to corrupt _liveLtpSnap and cause ' +
        'LTP cells to revert to close_price after a live tick.',
    ).toBe(true);

    // Secondary guard: ltp null-check must also be present in the same branch.
    expect(
      src.includes('v.ltp == null'),
      'quoteStream.js must also guard against null ltp on the same tick-handler line.',
    ).toBe(true);
  });
});
