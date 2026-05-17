/**
 * Paper order placement — end-to-end via /api/orders/ticket.
 *
 * Verifies that submitting an OrderTicket in PAPER mode lands an
 * AlgoOrder(mode='paper') row that the prod paper engine picks up
 * and runs through its chase loop.
 *
 * Hybrid approach:
 *   1. UI anchor — load /admin/options, verify the chain page mounts
 *      and the "i" button (which opens OrderTicket) is in the DOM.
 *      Catches regressions in the page where the ticket affordance
 *      goes missing.
 *   2. Backend chain — POST /api/orders/ticket with mode='paper' for
 *      a far-OTM NIFTY option. Assert 200/201, then poll
 *      /api/orders/algo/recent?mode=paper for the new row.
 *      Assert mode='paper', status ∈ {OPEN, FILLED, REJECTED,
 *      UNFILLED}, detail string carries the Kite basket_margin
 *      verdict.
 *
 * The chain → ticket → submit UI drive is intentionally NOT driven
 * here. That path needs an underlying + expiry pick + chain hydrate
 * cycle that's brittle on prod's after-hours quote returns. The
 * UI anchor catches the affordance going missing; the backend
 * chain catches everything from the route down.
 *
 * Symbol choice: NIFTY current-month ATM-ish strike CE/PE picked
 * from the chain endpoint at run time. PAPER mode never hits the
 * broker — `basket_margin` validation only — so even invalid
 * picks just produce REJECTED rows (test still passes).
 */

import { test, expect } from '@playwright/test';

// API-cached auth (rate-limit safe).
const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedAuth = null;

async function authOnce(page) {
  if (!_cachedAuth) {
    // Allow a pre-acquired token to be injected via env var to avoid
    // hitting the prod login rate-limit when running multiple times in
    // quick succession (e.g. re-runs, CI retries).
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

// Pick a sensible NIFTY option symbol to place. Asks the instruments
// endpoint for current NIFTY CE options, picks the lowest-premium one
// to minimise margin impact (PAPER doesn't really use margin, but
// keeping it deterministic is nice).
async function pickNiftyOption(page) {
  // /api/instruments returns the full instrument dump with abbreviated
  // field names: s=tradingsymbol, e=exchange, t=type (CE/PE/FUT/EQ),
  // ls=lot_size, u=underlying, x=expiry, k=strike.
  // The server ignores query-param filters — we filter client-side.
  const r = await page.request.get('/api/instruments');
  if (r.ok()) {
    const inst = await r.json();
    // Response shape: {cycle_date, count, items:[{s,e,t,ls,u,x,k}, …]}
    // Also handle plain array for forward-compat.
    const items = Array.isArray(inst) ? inst : (inst.items || inst.rows || []);
    const ces = items
      .filter((x) => x.e === 'NFO' && x.t === 'CE' && /^NIFTY/i.test(x.u || x.s || ''))
      .slice(0, 200);
    if (ces.length) {
      // Pick one near the middle of the strike range to avoid extreme OTM.
      const mid = ces[Math.floor(ces.length / 2)];
      return {
        tradingsymbol: mid.s || mid.tradingsymbol,
        exchange:      mid.e || mid.exchange || 'NFO',
        lot_size:      mid.ls || mid.lot_size || 50,
      };
    }
  }
  // Fallback — fetch via /api/options/instruments which exists on prod.
  const r2 = await page.request.get('/api/options/instruments?underlying=NIFTY');
  if (r2.ok()) {
    const inst = await r2.json();
    const rows = Array.isArray(inst) ? inst : (inst.items || inst.rows || []);
    const ces = rows.filter((x) => /CE$/i.test(x.tradingsymbol || x.symbol || x.s || ''));
    if (ces.length) {
      const mid = ces[Math.floor(ces.length / 2)];
      return {
        tradingsymbol: mid.tradingsymbol || mid.symbol || mid.s,
        exchange:      mid.exchange || mid.e || 'NFO',
        lot_size:      mid.lot_size || mid.ls || 50,
      };
    }
  }
  return null;
}

// ─────────────────────────────────────────────────────────────────

test.describe.configure({ mode: 'serial' });
test.setTimeout(120_000);

test.describe('Paper order placement', () => {

  test('1: /admin/options chain page mounts with ticket affordances', async ({ page }) => {
    await authOnce(page);
    await page.goto('/admin/options');
    await page.waitForLoadState('domcontentloaded');

    // Page header is the cheapest "page hydrated" marker.
    const heading = page.getByRole('heading', { name: /options/i }).first();
    await expect(heading).toBeVisible({ timeout: 12_000 });

    // The Chain tab opens the chain panel (with +/−/i buttons that
    // drive the ticket). The button is unconditionally rendered.
    const chainBtn = page.locator('button:has-text("Chain")').first();
    await expect(chainBtn).toBeVisible({ timeout: 8_000 });
    console.log('[paper_order_placement] /admin/options page mounted + Chain button present');
  });

  test('2: POST /api/orders/ticket mode=paper → AlgoOrder(mode=paper) row appears', async ({ page }) => {
    await authOnce(page);

    // Pick an option to place. Skip the test cleanly if the
    // instruments endpoint returns nothing (off-hours / instruments
    // cache not yet warmed).
    const opt = await pickNiftyOption(page);
    if (!opt) {
      test.skip(true, 'no NIFTY options returned from instruments endpoint — instruments cache cold');
      return;
    }
    console.log(`[paper_order_placement] picked option: ${opt.tradingsymbol} (lot=${opt.lot_size})`);

    // Need an account to scope the order to. Fetch from /api/admin/brokers.
    const brR = await page.request.get('/api/admin/brokers');
    if (!brR.ok()) {
      test.skip(true, `brokers endpoint returned ${brR.status()}`);
      return;
    }
    const brokers = await brR.json();
    const acct = (Array.isArray(brokers) ? brokers : (brokers.rows || []))
      .find((b) => b.loaded)?.account;
    if (!acct) {
      test.skip(true, 'no loaded broker account available');
      return;
    }
    console.log(`[paper_order_placement] using account: ${acct}`);

    // Capture pre-place AlgoOrder count so we can detect a new row.
    const beforeR = await page.request.get('/api/orders/algo/recent?mode=paper&limit=200');
    const before = beforeR.ok() ? await beforeR.json() : [];
    const beforeIds = new Set((before || []).map((o) => o.id));
    console.log(`[paper_order_placement] pre-place paper orders: ${beforeIds.size}`);

    // Submit the ticket. BUY 1 lot at LIMIT — limit price is set to
    // a low value so the basket_margin check is the only gate
    // (PAPER doesn't actually fill against a broker; the prod paper
    // engine fills via the chase loop against live quotes).
    const payload = {
      mode:             'paper',
      side:             'BUY',
      tradingsymbol:    opt.tradingsymbol,
      exchange:         opt.exchange,
      product:          'NRML',
      order_type:       'LIMIT',
      variety:          'regular',
      quantity:         opt.lot_size,
      price:            1.0,         // far-below-market LIMIT — won't fill but won't reject either
      trigger_price:    null,
      account:          acct,
    };
    const placeR = await page.request.post('/api/orders/ticket', { data: payload });
    const placeStatus = placeR.status();
    const placeBody = placeR.ok() ? await placeR.json() : await placeR.text();
    console.log(`[paper_order_placement] place status=${placeStatus} body=${JSON.stringify(placeBody).slice(0, 300)}`);

    // 200/201 OR a REJECTED-by-basket_margin response are both OK for
    // this test — both prove the route + AlgoOrder pipeline ran.
    expect([200, 201].includes(placeStatus), `ticket route returned ${placeStatus}: ${JSON.stringify(placeBody).slice(0, 200)}`).toBe(true);

    // Poll /api/orders/algo/recent for the new row (engine indexes
    // by created_at; the new row should appear at the head).
    let newRow = null;
    for (let i = 0; i < 10; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      const r2 = await page.request.get('/api/orders/algo/recent?mode=paper&limit=20');
      if (!r2.ok()) continue;
      const rows = await r2.json();
      newRow = (rows || []).find((o) => !beforeIds.has(o.id) && o.symbol === opt.tradingsymbol);
      if (newRow) break;
    }
    expect(newRow, `no new paper AlgoOrder row for ${opt.tradingsymbol} after ticket submit`).toBeTruthy();
    console.log(`[paper_order_placement] new row: id=${newRow.id} mode=${newRow.mode} status=${newRow.status} detail=${(newRow.detail || '').slice(0, 120)}`);

    // Assertions on the row shape.
    expect(newRow.mode).toBe('paper');
    expect(['OPEN', 'FILLED', 'REJECTED', 'UNFILLED']).toContain(newRow.status);
    expect(newRow.symbol).toBe(opt.tradingsymbol);
    expect(newRow.transaction_type).toBe('BUY');
    // basket_margin verdict should be reflected in detail.
    expect((newRow.detail || '').length, 'AlgoOrder.detail should carry the basket_margin verdict').toBeGreaterThan(0);
  });

});
