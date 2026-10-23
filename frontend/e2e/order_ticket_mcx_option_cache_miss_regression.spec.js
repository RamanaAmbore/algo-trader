/**
 * order_ticket_mcx_option_cache_miss_regression.spec.js
 *
 * 2026-10-23 — live-blocking incident: operator tried to place a real SELL
 * order on an MCX GOLD option ("GOLD 30OCT26 1,42,000 CE") and the ticket
 * showed the GENERIC "Market closed — order will queue" banner — not
 * "MCX closed" — while `/api/market/status` confirmed MCX was genuinely
 * open (`mcx_open: true, nse_open: false`). The generic (non-MCX) wording
 * is the tell: `_modalNotice` (SymbolPanel.svelte) only prints the generic
 * text when the resolved exchange is NOT 'MCX'/'NCO', so the ticket's own
 * exchange resolution had silently landed on something else.
 *
 * Root cause: a 2026-10-22 fix (see
 * order_ticket_market_badge_and_error_toast.spec.js's "live incident
 * regression" test) added an instruments-cache-miss rescue to
 * `exchangeOptions` (OrderTicket.svelte) — but ONLY for the bare-root FUT
 * case (`kind === 'FUT'`). An already-resolved CE/PE contract that misses
 * `listExchangesForSymbol()` (e.g. the specific option row hasn't loaded
 * into the instruments cache yet, or a draft-promotion flow opened the
 * ticket before the cache warmed) fell straight through to the generic
 * kind-fallback list for options — `['NFO', 'BFO']` — which has NO MCX/
 * commodity entry at all. The auto-snap effect that seeds `_exchange`
 * (OrderTicket.svelte ~1205-1227) always picks `exchangeOptions[0]`, so
 * the ticket silently locked onto NFO. Because NSE/NFO was closed while
 * MCX was open, `_mktOpenForExchange('NFO')` returned false and
 * `_modalNotice` rendered the GENERIC closed-market banner — exactly the
 * reported symptom — even though the real contract trades on MCX, which
 * was open.
 *
 * Fix: `exchangeOptions`' cache-miss branch now also rescues CE/PE by
 * resolving the contract's root (regex-stripped leading letters, e.g.
 * "GOLD" from "GOLD26OCT142000CE") and checking whether that root has a
 * listed future — an option always trades on the same exchange as its
 * underlying future. `_resolvedExchange` got the same rescue as defense
 * in depth. Two draft-promotion call sites (MarketPulse.svelte
 * `_openTicketFromDraftRow`, derivatives `+page.svelte` `executeDraft`)
 * that used to pass a hardcoded `'NFO'` exchange HINT on their own cache
 * miss were also hardened to attempt the same root-future rescue before
 * falling back to the hint — not the primary driver (OrderTicket's own
 * resolution overrides the hint via the fixes above regardless), but an
 * independent improvement so the hint is never actively wrong.
 *
 * This test reproduces the cache-miss precondition directly via a mocked
 * `/api/instruments` response that includes a GOLD future on MCX but
 * deliberately OMITS the specific GOLD option contract row — exactly the
 * "option row hasn't loaded / was purged / never listed in this response"
 * shape that defeats `listExchangesForSymbol()` and `getInstrument()`
 * while a root-future rescue can still succeed. The ticket is opened via
 * the bookmarkable `/orders?symbol=&exchange=` URL params with an
 * explicit wrong `exchange=NFO` hint — mirroring exactly what the
 * draft-promotion call sites used to send on their own cache miss.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

const GOLD_CE_SYMBOL = 'GOLD26OCT142000CE';
const GOLD_FUT_SYMBOL = 'GOLD26NOVFUT';
// Far-future expiry so findNearestFuture's `r.x > today` check always
// resolves this row as the front-month, regardless of when the suite runs.
const FAR_FUTURE_EXPIRY = '2099-11-25';

async function _mockMarketStatusMcxOpenNseClosed(page) {
  await page.route('**/api/market/status', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      nse_open: false,
      mcx_open: true,
      any_open: true,
      is_holiday: false,
      checked_at: new Date().toISOString(),
    }),
  }));
}

// Instruments payload that has the GOLD root's FUTURE on MCX, but
// deliberately NO row for the specific CE contract — the cache-miss
// precondition this regression test targets.
async function _mockInstrumentsOptionMiss(page) {
  await page.route('**/api/instruments**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      cycle_date: '2026-10-23',
      count: 1,
      items: [
        { s: GOLD_FUT_SYMBOL, e: 'MCX', t: 'FUT', ls: 100, ts: 1, u: 'GOLD', x: FAR_FUTURE_EXPIRY },
      ],
    }),
  }));
}

test.describe('OrderTicket — MCX option instruments-cache-miss exchange resolution (2026-10-23 regression)', () => {
  test('source: exchangeOptions cache-miss branch rescues CE/PE via root future, not just FUT', () => {
    const orderTicket = readFileSync(
      '/Users/ramanambore/projects/ramboq/frontend/src/lib/order/OrderTicket.svelte',
      'utf-8',
    );
    const idx = orderTicket.indexOf('const exchangeOptions = $derived.by');
    expect(idx).toBeGreaterThan(-1);
    const block = orderTicket.slice(idx, idx + 4000);
    expect(block).toMatch(/kind === 'CE' \|\| kind === 'PE'/);
    // Must resolve via a root extraction + findNearestFuture, not just
    // repeat the bare-root `findNearestFuture(sym)` call (which only
    // works when `sym` itself IS the bare root, not a suffixed contract).
    expect(block).toContain("sym.match(/^([A-Z]+)/)");
  });

  test('live incident regression: a GOLD option that misses the instruments cache must resolve to MCX, not the generic NFO/NSE closed banner', async ({ page }) => {
    await _mockMarketStatusMcxOpenNseClosed(page);
    await _mockInstrumentsOptionMiss(page);
    await loginAsAdmin(page);

    // Mirrors exactly what the draft-promotion call sites used to send on
    // their own cache miss: the real symbol, with a WRONG exchange hint.
    await page.goto(
      `${BASE}/orders?symbol=${GOLD_CE_SYMBOL}&exchange=NFO`,
      { waitUntil: 'domcontentloaded', timeout: 30_000 },
    );

    // Give the (mocked, fast) instruments fetch + the ticket's reactive
    // exchange resync effect time to settle.
    await page.waitForTimeout(1_500);

    const notice = page.locator('.oes-notice').first();
    if (await notice.count() > 0) {
      const text = (await notice.textContent()) || '';
      // The core regression: must NEVER show the generic wording while
      // MCX is open — that wording only ever prints when the ticket
      // believes the resolved exchange is NOT MCX/NCO.
      expect(text).not.toContain('Market closed');
      // If a notice is showing for some unrelated reason, it must not be
      // the market-hours one at all, since MCX (the real exchange) is open.
      expect(text).not.toContain('order will queue');
    }
  });
});
