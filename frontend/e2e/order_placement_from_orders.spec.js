/**
 * Order placement from /orders page — end-to-end paper-mode submission.
 *
 * /orders hosts the live Kite order list + a command bar that
 * parses "buy NIFTY..." and opens the OrderTicket pre-filled.
 * Per-row Edit / Cancel / Repeat actions also open the ticket.
 *
 * Hybrid approach (same as previous order_placement specs):
 *   1. UI anchor — load /orders, verify the page mounts (the
 *      <h1 class="page-title-chip"> with text "Orders" is the
 *      cheapest reliable marker).
 *   2. Backend chain — POST /api/orders/ticket mode='paper'
 *      against a NIFTY CE option. Assert new AlgoOrder(mode='paper')
 *      row appears.
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

async function pickNiftyOption(page) {
  const r = await page.request.get('/api/instruments');
  if (!r.ok()) return null;
  const inst = await r.json();
  const items = inst.items || [];
  const ces = items.filter((x) => x.t === 'CE' && x.u === 'NIFTY');
  if (!ces.length) return null;
  const mid = ces[Math.floor(ces.length / 2)];
  return { tradingsymbol: mid.s, exchange: mid.e || 'NFO', lot_size: mid.ls || 50 };
}

test.describe.configure({ mode: 'serial' });
test.setTimeout(120_000);

test.describe('Order placement from /orders', () => {

  test('1: /orders page mounts with Orders title chip', async ({ page }) => {
    await authOnce(page);
    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    const title = page.locator('.page-title-chip', { hasText: /^Orders$/ }).first();
    await expect(title).toBeVisible({ timeout: 12_000 });
    console.log('[order_placement_from_orders] /orders page mounted');
  });

  test('2: POST /api/orders/ticket mode=paper → AlgoOrder(mode=paper) row', async ({ page }) => {
    await authOnce(page);

    const opt = await pickNiftyOption(page);
    if (!opt) { test.skip(true, 'no NIFTY options'); return; }

    const brR = await page.request.get('/api/admin/brokers');
    if (!brR.ok()) { test.skip(true, `brokers ${brR.status()}`); return; }
    const brokers = await brR.json();
    const acct = (Array.isArray(brokers) ? brokers : (brokers.rows || []))
      .find((b) => b.loaded)?.account;
    if (!acct) { test.skip(true, 'no loaded broker'); return; }

    const beforeR = await page.request.get('/api/orders/algo/recent?mode=paper&limit=200');
    const beforeIds = new Set(((beforeR.ok() ? await beforeR.json() : []) || []).map((o) => o.id));

    const payload = {
      mode: 'paper', side: 'BUY',
      tradingsymbol: opt.tradingsymbol, exchange: opt.exchange,
      product: 'NRML', order_type: 'LIMIT', variety: 'regular',
      // v2 API (2026-07-08): qty is LOTS for F&O — 1 lot.
      quantity: 1, lot_size_hint: opt.lot_size,
      price: 1.0, trigger_price: null, account: acct,
    };
    const placeR = await page.request.post('/api/orders/ticket', { data: payload });
    expect([200, 201].includes(placeR.status()),
      `ticket route returned ${placeR.status()}`).toBe(true);

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
    console.log(`[order_placement_from_orders] new row id=${newRow.id} status=${newRow.status}`);
  });
});
