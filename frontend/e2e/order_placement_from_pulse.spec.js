/**
 * Order placement from /pulse — end-to-end paper-mode submission.
 *
 * /pulse hosts the MarketPulse component. Order entry on this page
 * is driven by a row click → openTicket() → OrderTicket modal.
 * Alternative path: right-click a row → context menu → "Open ticket".
 *
 * Hybrid approach (same as paper_order_placement.spec.js):
 *   1. UI anchor — load /pulse, verify the MarketPulse grid mounts
 *      (.ag-theme-algo container is the cheapest marker). Catches
 *      regressions where the page entry vanishes.
 *   2. Backend chain — POST /api/orders/ticket with mode='paper'
 *      against a position-relevant NIFTY option (matching what a
 *      real /pulse row submission would do). Assert new
 *      AlgoOrder(mode='paper') row.
 *
 * Why not full UI drive? Driving an ag-grid row click is brittle
 * (virtual scrolling, dynamic data hydration). The anchor catches
 * the page-level affordance; the backend chain proves the order
 * pipeline.
 */

import { test, expect } from '@playwright/test';

const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedAuth = null;

async function authOnce(page) {
  if (!_cachedAuth) {
    const envToken = process.env.PLAYWRIGHT_AUTH_TOKEN;
    let tok = envToken || null;
    if (!tok) {
      for (const delay of [0, 20000, 65000]) {
        if (delay) await new Promise((r) => setTimeout(r, delay));
        const resp = await page.request.post('/api/auth/login', {
          data: { username: _AUTH_USER, password: _AUTH_PASS },
        });
        if (resp.ok()) { tok = (await resp.json()).access_token; break; }
        if (resp.status() !== 429) throw new Error(`authOnce: /api/auth/login ${resp.status()}`);
      }
    }
    if (!tok) throw new Error('authOnce: login rate-limited');
    _cachedAuth = { token: tok, user_id: _AUTH_USER };
  }
  const { token, user_id } = _cachedAuth;
  await page.goto('/');
  await page.evaluate(({ tok, usr }) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: usr, username: usr, role: 'admin', display_name: usr,
    }));
  }, { tok: token, usr: user_id });
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${token}` });
}

// Pick a NIFTY CE option to place. Reads /api/instruments which
// returns {items:[{s, e, t, ls, u, …}]} with abbreviated fields.
async function pickNiftyOption(page) {
  const r = await page.request.get('/api/instruments');
  if (!r.ok()) return null;
  const inst = await r.json();
  const items = inst.items || [];
  const ces = items.filter((x) => x.t === 'CE' && x.u === 'NIFTY');
  if (!ces.length) return null;
  const mid = ces[Math.floor(ces.length / 2)];
  return {
    tradingsymbol: mid.s,
    exchange:      mid.e || 'NFO',
    lot_size:      mid.ls || 50,
  };
}

test.describe.configure({ mode: 'serial' });
test.setTimeout(120_000);

test.describe('Order placement from /pulse', () => {

  test('1: /pulse page mounts with MarketPulse grid', async ({ page }) => {
    await authOnce(page);
    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    // ag-theme-algo container is the visible marker for the
    // MarketPulse grid mounting. It renders for both positions
    // and holdings views.
    const grid = page.locator('.ag-theme-algo').first();
    await expect(grid).toBeVisible({ timeout: 12_000 });
    console.log('[order_placement_from_pulse] /pulse grid mounted');
  });

  test('2: POST /api/orders/ticket mode=paper → AlgoOrder(mode=paper) row', async ({ page }) => {
    await authOnce(page);

    const opt = await pickNiftyOption(page);
    if (!opt) {
      test.skip(true, 'no NIFTY options in instruments — cache cold');
      return;
    }

    const brR = await page.request.get('/api/admin/brokers');
    if (!brR.ok()) { test.skip(true, `brokers endpoint ${brR.status()}`); return; }
    const brokers = await brR.json();
    const acct = (Array.isArray(brokers) ? brokers : (brokers.rows || []))
      .find((b) => b.loaded)?.account;
    if (!acct) { test.skip(true, 'no loaded broker account'); return; }

    const beforeR = await page.request.get('/api/orders/algo/recent?mode=paper&limit=200');
    const before = beforeR.ok() ? await beforeR.json() : [];
    const beforeIds = new Set((before || []).map((o) => o.id));

    // Submit BUY 1 lot at a far-below-market LIMIT — won't fill,
    // won't reject, just registers with the prod paper engine.
    const payload = {
      mode: 'paper', side: 'BUY',
      tradingsymbol: opt.tradingsymbol, exchange: opt.exchange,
      product: 'NRML', order_type: 'LIMIT', variety: 'regular',
      quantity: opt.lot_size, price: 1.0, trigger_price: null,
      account: acct,
    };
    const placeR = await page.request.post('/api/orders/ticket', { data: payload });
    const placeBody = placeR.ok() ? await placeR.json() : await placeR.text();
    console.log(`[order_placement_from_pulse] place status=${placeR.status()} body=${JSON.stringify(placeBody).slice(0, 200)}`);
    expect([200, 201].includes(placeR.status())).toBe(true);

    let newRow = null;
    for (let i = 0; i < 10; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      const r2 = await page.request.get('/api/orders/algo/recent?mode=paper&limit=20');
      if (!r2.ok()) continue;
      const rows = await r2.json();
      newRow = (rows || []).find((o) => !beforeIds.has(o.id) && o.symbol === opt.tradingsymbol);
      if (newRow) break;
    }
    expect(newRow, `no new paper AlgoOrder for ${opt.tradingsymbol}`).toBeTruthy();
    expect(newRow.mode).toBe('paper');
    expect(['OPEN', 'FILLED', 'REJECTED', 'UNFILLED']).toContain(newRow.status);
    expect(newRow.symbol).toBe(opt.tradingsymbol);
    expect(newRow.transaction_type).toBe('BUY');
    console.log(`[order_placement_from_pulse] new row id=${newRow.id} status=${newRow.status}`);
  });
});
