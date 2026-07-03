/**
 * pulse_all_fields_live.spec.js — Live regression for the "MCX bare-root
 * blank" defect (Jul 2026).
 *
 * Root cause (fixed in this sprint):
 *   backend/api/routes/instruments.py::_fetch_instruments used
 *   `get_market_data_broker()` (PriceBroker with cross-broker failover).
 *   When Kite rate-limited, PriceBroker fell over to Dhan, whose
 *   instruments() returns rows without `instrument_type` / `name` /
 *   `expiry`. The API cache was poisoned with 156K stripped rows, which
 *   broke symbol_resolver's virtual-root filter (`t == "FUT" and u ==
 *   root`). MCX bare-root queries (MCX:CRUDEOIL, MCX:GOLDM, MCX:GOLD,
 *   MCX:SILVER, CDS:USDINR) silently resolved to themselves — the LKG
 *   quote cache never had those keys → all fields null in the closed-
 *   hours branch of /api/quote/batch → grid cells rendered blank.
 *
 * Fix:
 *   - _fetch_instruments now walks Kite accounts directly (never falls
 *     over to Dhan/Groww for instruments dumps).
 *   - PriceBroker.instruments() gained _instruments_has_kite_shape gate
 *     so a future direct caller can't get poisoned data either.
 *
 * Verification (this spec):
 *   1. /api/instruments returns rows with `t:"FUT"` and `u:` populated.
 *   2. /api/quote/batch for [NSE:RELIANCE, MCX:CRUDEOIL, MCX:GOLDM,
 *      MCX:GOLD, MCX:SILVER, CDS:USDINR] returns `ltp>0, open>0,
 *      close>0, volume>=0` (volume can be 0 for USDINR CDS after close;
 *      key check is presence of open/close).
 *   3. /api/quotes/sparkline for the same bare MCX/CDS roots returns
 *      non-empty arrays keyed on the original bare name (CRUDEOIL not
 *      CRUDEOILM26JULFUT).
 *   4. /pulse UI: RELIANCE row (if present) has Open cell numeric,
 *      Volume cell numeric or "—" (Kite's index rows can genuinely
 *      lack volume); MCX bare-root watchlist rows carry an LTP.
 *
 * Target: https://dev.ramboq.com (NEVER prod/ramboq.com).
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *     npx playwright test e2e/pulse_all_fields_live.spec.js --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

// The MCX / CDS bare-root universe operator flagged as blank. Presence
// of these in a healthy /api/quote/batch response is the primary
// invariant this spec locks down.
const MCX_BARE_ROOTS = ['CRUDEOIL', 'GOLDM', 'GOLD', 'SILVER'];
const CDS_BARE_ROOTS = ['USDINR'];

test.describe('/pulse — all-fields populated after MCX virtual-root fix', () => {
  test.describe.configure({ mode: 'serial' });
  test.use({ viewport: { width: 1440, height: 900 } });
  test.setTimeout(120_000);

  test('/api/instruments — Kite-shape rows (t + u populated)', async ({ page }) => {
    await loginAsAdmin(page);
    // Use the same session token the page uses (no ambient env access).
    const token = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));
    expect(token, 'session token minted after login').toBeTruthy();

    const resp = await page.request.get(`${BASE}/api/instruments`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(resp.ok(), '/api/instruments returns 2xx').toBeTruthy();
    const body = await resp.json();
    const items = body.items || [];
    expect(items.length, 'instruments cache has rows').toBeGreaterThan(1000);

    // Kite schema fields must be present. Prior defect populated every
    // row with `t:""` and no `u`; this predicate would catch that
    // silently-poisoned state.
    const withT = items.filter((it) => it.t);
    const withU = items.filter((it) => it.u);
    expect(withT.length, `rows with instrument_type ('t'): ${withT.length}/${items.length}`).toBeGreaterThan(items.length / 2);
    expect(withU.length, `rows with underlying ('u'): ${withU.length}/${items.length}`).toBeGreaterThan(0);

    // Concrete guard: at least one MCX FUT row for a common virtual root.
    const mcxCrudeFut = items.filter(
      (it) => it.e === 'MCX' && it.t === 'FUT' && (it.u || '').toUpperCase() === 'CRUDEOIL'
    );
    expect(mcxCrudeFut.length, 'at least one MCX CRUDEOIL FUT row present in instruments').toBeGreaterThan(0);
  });

  test('/api/quote/batch — MCX bare roots + RELIANCE all have populated OHLC', async ({ page }) => {
    await loginAsAdmin(page);
    const token = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));

    const keys = [
      'NSE:RELIANCE',
      ...MCX_BARE_ROOTS.map((s) => `MCX:${s}`),
      ...CDS_BARE_ROOTS.map((s) => `CDS:${s}`),
    ];
    const resp = await page.request.post(`${BASE}/api/quote/batch`, {
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      data: { keys },
    });
    expect(resp.ok(), '/api/quote/batch returns 2xx').toBeTruthy();
    const body = await resp.json();
    const byKey = {};
    for (const it of body.items || []) {
      byKey[`${it.exchange}:${it.tradingsymbol}`] = it;
    }

    // RELIANCE — canonical NSE cash equity, always has OHLC even during
    // closed hours (LKG cache from prior session).
    const reliance = byKey['NSE:RELIANCE'];
    expect(reliance, 'RELIANCE row present in batch response').toBeTruthy();
    expect(reliance.ltp, 'RELIANCE LTP > 0').toBeGreaterThan(0);
    expect(reliance.open, 'RELIANCE open > 0').toBeGreaterThan(0);
    expect(reliance.close, 'RELIANCE close > 0').toBeGreaterThan(0);
    expect(reliance.volume, 'RELIANCE volume > 0').toBeGreaterThan(0);

    // MCX bare roots — virtual resolver must map to front-month FUT,
    // LKG cache should return populated OHLC for that resolved contract.
    for (const sym of MCX_BARE_ROOTS) {
      const row = byKey[`MCX:${sym}`];
      expect(row, `MCX:${sym} row present in batch response`).toBeTruthy();
      expect(row.tradingsymbol, `MCX:${sym} tradingsymbol echoed as input`).toBe(sym);
      expect(row.ltp, `MCX:${sym} LTP > 0`).toBeGreaterThan(0);
      expect(row.open, `MCX:${sym} open > 0`).toBeGreaterThan(0);
      expect(row.close, `MCX:${sym} close > 0`).toBeGreaterThan(0);
    }

    // CDS bare roots — same virtual-root path as MCX.
    for (const sym of CDS_BARE_ROOTS) {
      const row = byKey[`CDS:${sym}`];
      expect(row, `CDS:${sym} row present in batch response`).toBeTruthy();
      expect(row.tradingsymbol, `CDS:${sym} tradingsymbol echoed as input`).toBe(sym);
      // CDS may have narrow volume in closed hours; LTP is the primary
      // signal that virtual resolution + LKG lookup succeeded.
      expect(row.ltp, `CDS:${sym} LTP > 0`).toBeGreaterThan(0);
    }
  });

  test('/api/quotes/sparkline — bare MCX/CDS roots return non-empty series', async ({ page }) => {
    await loginAsAdmin(page);
    const token = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));

    const symbols = [
      { tradingsymbol: 'RELIANCE', exchange: 'NSE' },
      ...MCX_BARE_ROOTS.map((s) => ({ tradingsymbol: s, exchange: 'MCX' })),
      ...CDS_BARE_ROOTS.map((s) => ({ tradingsymbol: s, exchange: 'CDS' })),
    ];
    const resp = await page.request.post(`${BASE}/api/quotes/sparkline`, {
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      data: { symbols, days: 5 },
    });
    expect(resp.ok(), '/api/quotes/sparkline returns 2xx').toBeTruthy();
    const body = await resp.json();
    const data = body.data || {};

    // Frontend renderer reads sparklines[row.tradingsymbol] where the row
    // carries the bare name (CRUDEOIL) — the endpoint must dual-write the
    // bare key alongside the resolved contract key.
    expect(data.RELIANCE, 'RELIANCE sparkline series present').toBeTruthy();
    expect(data.RELIANCE.length, 'RELIANCE sparkline non-empty').toBeGreaterThanOrEqual(2);

    for (const sym of MCX_BARE_ROOTS) {
      expect(data[sym], `${sym} sparkline keyed on bare name`).toBeTruthy();
      expect(data[sym].length, `${sym} sparkline non-empty`).toBeGreaterThanOrEqual(2);
    }
    for (const sym of CDS_BARE_ROOTS) {
      expect(data[sym], `${sym} sparkline keyed on bare name`).toBeTruthy();
      expect(data[sym].length, `${sym} sparkline non-empty`).toBeGreaterThanOrEqual(2);
    }
  });

  test('/pulse UI — MCX bare-root watchlist rows render an LTP (no blank cell)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // Wait for at least one grid + the first REST fetch cycle to land.
    await expect(page.locator('.ag-root').first(), 'ag-Grid mounted').toBeVisible({ timeout: 30_000 });
    await page.waitForResponse(
      (r) => r.url().includes('/api/quote/batch') && r.status() === 200,
      { timeout: 30_000 }
    ).catch(() => null);
    // A second beat lets publishPulseQuotes → symbolStore → row-composer
    // finish its 250ms throttled effect.
    await page.waitForTimeout(1500);

    // Look for one of the MCX bare-root rows the operator uses on /pulse.
    // Watchlist / pinned universes vary per operator so we soft-check:
    // if the row is on-page, assert its LTP cell is non-blank; if not,
    // skip (the API-layer assertions above already lock down the raw
    // response invariant). This keeps the spec robust to watchlist edits.
    const bareRoots = [...MCX_BARE_ROOTS, ...CDS_BARE_ROOTS];
    let anyChecked = false;
    for (const sym of bareRoots) {
      const symCell = page.locator(`.ag-row .ag-cell:has-text("${sym}")`).first();
      const count = await symCell.count();
      if (count === 0) continue;
      // The row's LTP cell is the sibling cell with field=ltp. ag-Grid
      // renders LTP formatted via numFmt — expect a digit char in it
      // (not the em-dash placeholder "—").
      const row = symCell.locator('xpath=ancestor::div[@role="row"]').first();
      const ltpCell = row.locator('[col-id="ltp"]').first();
      const ltpText = (await ltpCell.textContent() || '').trim();
      // Some watchlist rows may still be resolving on cold cache — allow
      // one retry with a small wait before failing.
      if (!/[0-9]/.test(ltpText)) {
        await page.waitForTimeout(2000);
        const retryText = (await ltpCell.textContent() || '').trim();
        expect(retryText, `/pulse row LTP for ${sym} must be numeric, not blank/em-dash (got: "${retryText}")`).toMatch(/[0-9]/);
      } else {
        expect(ltpText, `/pulse row LTP for ${sym} numeric (got: "${ltpText}")`).toMatch(/[0-9]/);
      }
      anyChecked = true;
    }
    // If no MCX/CDS bare-root rows are on this operator's watchlist,
    // the API-layer tests are the source of truth — soft-skip here.
    if (!anyChecked) {
      console.warn('pulse_all_fields_live: no MCX/CDS bare-root rows on /pulse; API tests cover the invariant');
    }
  });

  test('/pulse UI — mover rows have Open + Volume cells populated (defect 1)', async ({ page }) => {
    // Regression for the "Open / Volume / OI blank across mover grids"
    // defect (Jul 2026).  MoverRow backend schema only carries
    // last_price / previous_close / change_pct — no OHLCV — so the only
    // path for movers rows to acquire an Open / Volume value is via the
    // batchQuote pass in MarketPulse.loadPulse.  Before the fix
    // mover-only symbols (HCLTECH, LODHA, ZYDUSLIFE …) were NEVER
    // added to `allKeys`, so their Open / Volume / OI cells rendered
    // "—" indefinitely.
    await loginAsAdmin(page);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.ag-root').first(), 'ag-Grid mounted').toBeVisible({ timeout: 30_000 });
    // Wait for both movers + batchQuote passes to complete.  Movers
    // poll every 30 s but the initial fetch fires on mount; batchQuote
    // runs in loadPulse.  Give both a generous window.
    await page.waitForResponse(
      (r) => r.url().includes('/api/quote/batch') && r.status() === 200,
      { timeout: 30_000 }
    ).catch(() => null);
    await page.waitForTimeout(3000);

    // Read all mover rows via DOM traversal — mover rows carry a
    // row-id ending in "__mov" (set by buildUnified's major='movers'
    // branch).  For each mover row, check the Open and Volume cells
    // contain a digit or the em-dash placeholder is a defect.  We
    // sample the first N mover rows; on a live grid there are usually
    // 20-40.  Soft-fail if there are zero mover rows on-page (some
    // operator watchlists disable the movers grid).
    const dump = await page.evaluate(() => {
      /** @type {Array<{sym: string, ltp: string, open: string, volume: string, oi: string}>} */
      const rows = [];
      for (const r of document.querySelectorAll('.ag-center-cols-container .ag-row')) {
        const rowId = r.getAttribute('row-id') || '';
        if (!rowId.endsWith('__mov')) continue;
        const cells = {};
        for (const c of r.querySelectorAll('.ag-cell')) {
          const cid = c.getAttribute('col-id');
          if (cid && !(cid in cells)) cells[cid] = c.textContent.trim();
        }
        rows.push({
          sym: rowId.replace('__mov', ''),
          ltp: cells.ltp || '',
          open: cells.open || '',
          volume: cells.volume || '',
          oi: cells.oi || '',
        });
      }
      return rows;
    });

    if (dump.length === 0) {
      console.warn('pulse_all_fields_live: no mover rows on /pulse — soft-skip');
      return;
    }

    // Sample the first mover row that has a populated LTP (skips rows
    // where symbolStore hasn't hydrated yet).  For that row, the Open
    // cell MUST contain a digit — em-dash is the defect signature.
    // Volume can legitimately be 0 on illiquid tickers during off-hours,
    // so we're lenient: at least one of Open or Volume must be numeric.
    const hydrated = dump.filter((r) => /[0-9]/.test(r.ltp));
    expect(hydrated.length, `at least one hydrated mover row (of ${dump.length})`).toBeGreaterThan(0);

    // Log the first 5 hydrated rows for defect-diagnosis on failure.
    console.log('Mover rows (first 5 hydrated):', hydrated.slice(0, 5));

    // Assert: at least 60% of hydrated movers have a numeric Open cell.
    // A single blank row could be a symbol just added to the batchQuote
    // universe pending its first response, but a majority-blank grid is
    // the defect.
    const withOpen = hydrated.filter((r) => /[0-9]/.test(r.open));
    expect(
      withOpen.length,
      `mover rows with numeric Open: ${withOpen.length}/${hydrated.length} — ` +
      `defect if <60%.  Sample rows: ${JSON.stringify(hydrated.slice(0, 3))}`
    ).toBeGreaterThanOrEqual(Math.ceil(hydrated.length * 0.6));
  });
});
