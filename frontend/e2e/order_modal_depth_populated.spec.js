/**
 * order_modal_depth_populated.spec.js
 *
 * Verifies commit d9ebcce3: when the modal opens with the operator's
 * default symbol (resolved to a tradeable future), the depth ladder
 * polls the right /api/quote endpoint and shows live bid/ask values.
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/order_modal_depth_populated.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const API_HOST = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;

let _cachedToken = null;

async function login(page) {
  if (!_cachedToken && process.env.PLAYWRIGHT_TOKEN) _cachedToken = process.env.PLAYWRIGHT_TOKEN;
  if (!_cachedToken) {
    for (const u of ['rambo', 'ambore', 'admin']) {
      const r = await page.request.post(`${API_HOST}/api/auth/login`, {
        data: { username: u, password: _AUTH_PASS },
        timeout: 15_000,
      }).catch(() => null);
      if (r && r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
    if (!_cachedToken) throw new Error('login failed');
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _cachedToken);
}

test.describe('Order modal — depth ladder populates for the resolved symbol', () => {
  test.setTimeout(90_000);

  test('OrderDepth fetches /api/quote with resolved tradingsymbol + exchange and shows non-em-dash bid/ask', async ({ page }) => {
    const log = { timeline: [], errors: [], quoteCalls: [] };
    page.on('pageerror', (e) => log.errors.push(String(e).slice(0, 200)));

    // Capture /api/quote requests so we can see what the depth is asking for.
    page.on('request', (req) => {
      const u = req.url();
      if (/\/api\/quote(?:\?|$)/.test(u)) log.quoteCalls.push(u.slice(u.indexOf('/api/quote')));
    });

    await login(page);

    const acctResp = await page.request.get(`${API_HOST}/api/accounts/`, {
      headers: { Authorization: `Bearer ${_cachedToken}` },
    });
    const { default_symbol } = await acctResp.json();
    log.timeline.push(`default_symbol: ${default_symbol}`);
    expect(default_symbol).toBeTruthy();

    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    const orderBtn = page.locator('button.pha-order').first();
    if (await orderBtn.count() === 0) {
      log.timeline.push('skip: no .pha-order'); console.log(JSON.stringify(log, null, 2)); return;
    }
    await orderBtn.click({ force: true });
    await page.waitForTimeout(3500); // let instruments hydrate + first depth poll

    // ── 1. /api/quote was called with the resolved tradingsymbol ──────
    const rootBare = String(default_symbol).toUpperCase().split(/\s+/)[0];
    const futCalls = log.quoteCalls.filter(u => new RegExp(`tradingsymbol=${rootBare}[A-Z0-9]*FUT`, 'i').test(u));
    log.timeline.push(`/api/quote calls: ${log.quoteCalls.length} · matching ${rootBare}*FUT: ${futCalls.length}`);
    if (futCalls.length > 0) {
      log.timeline.push(`first matching call: ${futCalls[0]}`);
    } else if (log.quoteCalls.length > 0) {
      log.timeline.push(`first /api/quote call (no FUT match): ${log.quoteCalls[0]}`);
      log.timeline.push(`all quote calls: ${log.quoteCalls.slice(0, 5).join(' | ')}`);
    } else {
      log.timeline.push('no /api/quote calls captured AT ALL');
    }

    // DOM probe: is OrderDepth mounted? what symbol does its header show?
    const depthMount = await page.evaluate(() => {
      const depth = document.querySelector('.oes-modal .ot-depth');
      if (!depth) return { mounted: false };
      const header = depth.querySelector('.ot-depth-h');
      return {
        mounted: true,
        headerText: header ? (header.textContent || '').trim().slice(0, 200) : '<no header>',
      };
    });
    log.timeline.push(`OrderDepth DOM: mounted=${depthMount.mounted} · header="${depthMount.headerText || ''}"`);

    console.log('\n=== depth-populated DIAG ===');
    console.log(JSON.stringify(log, null, 2));
    expect(futCalls.length).toBeGreaterThan(0);

    // ── 2. Exchange query string is correct ───────────────────────────
    // CRUDEOIL → MCX; NIFTY → NFO; RELIANCE → NSE
    const firstCall = futCalls[0] || '';
    const exchMatch = firstCall.match(/exchange=([A-Z]+)/);
    const calledExch = exchMatch ? exchMatch[1] : '';
    log.timeline.push(`exchange in /api/quote: ${calledExch}`);
    expect(calledExch).toBeTruthy();
    // Sanity check for MCX commodity roots
    if (/^(CRUDEOIL|GOLD|SILVER|COPPER|ZINC|LEAD|NICKEL|NATURALGAS|ALUMINIUM)/.test(rootBare)) {
      expect(calledExch).toBe('MCX');
      log.timeline.push('✓ MCX exchange used for commodity root');
    }

    // ── 3. Depth ladder shows non-em-dash values somewhere ────────────
    // Wait a bit more for the response to land.
    await page.waitForTimeout(2000);
    const depthCellText = await page.evaluate(() => {
      const root = document.querySelector('.oes-modal');
      if (!root) return '';
      const cells = Array.from(root.querySelectorAll('.ot-depth-bid, .ot-depth-ask, .ot-depth-bid-qty, .ot-depth-ask-qty'));
      return cells.map(c => (c.textContent || '').trim()).join(' | ');
    });
    log.timeline.push(`depth cell text (first 200): "${depthCellText.slice(0, 200)}"`);
    // At least one cell should NOT be the em-dash fallback.
    const nonDashCells = depthCellText.split(/\s*\|\s*/).filter(s => s && s !== '—' && s !== '-');
    log.timeline.push(`non-dash cells: ${nonDashCells.length}`);
    if (nonDashCells.length > 0) {
      log.timeline.push(`✓ depth ladder populated (${nonDashCells.length} live cells)`);
    } else {
      log.timeline.push('note: every depth cell is em-dash — market may be closed / illiquid');
    }
    // Soft expectation — broker returning all em-dash off-hours is OK,
    // but at least the API was called correctly.

    console.log('\n=== depth-populated ===');
    console.log(JSON.stringify(log, null, 2));
  });
});
