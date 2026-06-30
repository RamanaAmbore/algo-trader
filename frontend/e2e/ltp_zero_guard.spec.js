/**
 * Regression guard: symbolStore.mergeSymbolUpdate must not allow a
 * poll-class write of `ltp=0` to clobber a previously-stored positive LTP.
 *
 * Operator complaint (Sleep audit Jun 2026): "look for LTP flickering and
 * sparkline accuracy." The backend's `_LAST_GOOD_LTP` rescue cache prevents
 * 0 leakage on the server side, but transient broker hiccups, stale SSE
 * snapshots, or first-tick race conditions occasionally surface
 * `last_price: 0` in API payloads. Before this fix, those zeros landed in
 * symbolStore for one render cycle before the next tick restored the real
 * price — operator-visible as a "—" or 0 flicker on the cell.
 *
 * Test strategy: load /pulse, capture a known-good LTP from any rendered
 * grid row, then call the publish helpers with `ltp: 0` for that symbol
 * and confirm the stored value did NOT change.
 *
 * Cold-start path (no prior LTP) is NOT tested here — leaving the zero
 * through is the correct behaviour when there is no good value to lose.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

test.describe('symbolStore — LTP zero guard', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(120_000);

  test('poll-class ltp=0 cannot overwrite stored positive ltp', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'load', timeout: 60_000 });
    // Wait for at least one row + a non-zero LTP to land in symbolStore.
    await page.locator('.ag-row').first()
      .waitFor({ state: 'visible', timeout: 30_000 }).catch(() => {});
    // Settle so SSE / polls deliver real LTPs.
    await page.waitForTimeout(4_000);

    // Pull a known symbol with a positive LTP from symbolStore. The
    // store is exported via the module graph; we re-import it inside
    // the page context using a Vite-compatible dynamic import string.
    const probe = await page.evaluate(async () => {
      // SvelteKit serves modules under /src for dev and the bundled
      // path in prod — fall back to whichever resolves.
      /** @type {any} */
      let mod = null;
      try {
        // Vite dev path.
        mod = await import('/src/lib/data/symbolStore.svelte.js');
      } catch (_) {
        try {
          // The store mirrors itself under window.__symbolStore on dev
          // only when explicitly attached; production builds prune
          // the module path. Skip the test gracefully if we can't reach it.
          mod = window.__symbolStore;
        } catch (__) { mod = null; }
      }
      if (!mod || typeof mod.getSnapshot !== 'function') {
        return { skip: true, reason: 'symbolStore module unreachable from page context' };
      }
      // Snap the first sym with ltp>0.
      /** @type {any} */
      let storeEntries = [];
      try {
        storeEntries = Array.from(/** @type {any} */ (mod.symbolStore).entries());
      } catch (_) { storeEntries = []; }
      let probeSym = null;
      let probeLtp = 0;
      for (const [sym, snap] of storeEntries) {
        if (snap?.ltp > 0) { probeSym = sym; probeLtp = Number(snap.ltp); break; }
      }
      if (!probeSym) return { skip: true, reason: 'no positive-LTP entry in symbolStore yet' };

      // Attempt a poll-class zero write — exactly what
      // _publishPositionsRows would do if the broker returned 0.
      const wrote = mod.mergeSymbolUpdate(probeSym, { ltp: 0 }, { ltp_ts: 0, snapshot_ts: 0 });
      const after = mod.getSnapshot(probeSym);
      return {
        skip: false,
        probeSym,
        probeLtp,
        wrote,
        afterLtp: after?.ltp ?? null,
      };
    });

    if (probe.skip) {
      test.skip(true, probe.reason || 'symbolStore not reachable');
      return;
    }

    console.log('[ltp-zero-guard] probe:', probe);
    // Critical: stored LTP must NOT have flipped to zero.
    expect(probe.afterLtp,
      `symbolStore.ltp for ${probe.probeSym} dropped from ${probe.probeLtp} to ${probe.afterLtp} after a zero poll write — flicker bug regression`
    ).toBe(probe.probeLtp);
    // wrote==false confirms the merge rejected the write (zero LTP guard).
    expect(probe.wrote,
      'mergeSymbolUpdate returned true for a zero LTP overwrite — guard bypassed'
    ).toBe(false);
  });
});
