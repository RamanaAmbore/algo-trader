/**
 * order_ticket_currentqty_from_watchlist.spec.js
 *
 * Regression pin for the 2026-11 6D-audit defect: OrderTicket's
 * `currentQty` prop (the real held quantity of the traded symbol) was only
 * populated when a ticket was opened straight from a position row. Tickets
 * opened from other entry points (watchlist, chart, bare header Order
 * button, Chain → Ticket handoff) left `currentQty` at 0 even when the
 * symbol was genuinely held. A SELL that closes a held long was then
 * classified as "opening a new short", so the margin/cash chip read
 * "Margin required" instead of "Releases cash".
 *
 * The fix makes OrderTicket resolve `currentQty` from the live positions
 * store, independent of the entry point. This spec pins that behaviour
 * end to end:
 *
 *  Case 1 (the broken path, REPRO): a ticket opened with a watchlist-shaped
 *    prefill (no `currentQty` key at all) on a symbol that is held long in
 *    the mocked positions feed. The chip must read the close-long label.
 *    Expected to FAIL against pre-fix code ("Margin required") and PASS
 *    once the fix lands.
 *
 *  Case 2 (control, position-row path): the same symbol/side opened the
 *    old, correct way, with the real `currentQty` the position-row opener
 *    (MarketPulse `_openTicketFromRow`) passes. Must PASS before and after
 *    the fix. Proves both paths now agree on the same label.
 *
 * Prefill shape note: the real MarketPulse `_openTicketGeneric` (watchlist
 * context-menu "Place order") passes `currentQty: posQty`, derived from the
 * row's `src.p` flag. The bug manifests when that flag is absent, so Case 1
 * models the no-`currentQty` prefill from the bug report. It is a
 * watchlist-shaped prefill driven through the `window.__stores` hook, not a
 * click on the real watchlist UI.
 *
 * Label source: `marginChipMode()` in src/lib/order/orderTicketSubmit.js
 * returns 'cash_released' for a closing SELL on an option (kind CE/PE).
 * The pill renders "Releases cash" when no limit price is set (the
 * price-less branch asserted in order_ticket_margin_chip_close_release.spec.js).
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/order_ticket_currentqty_from_watchlist.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const ACCOUNT = 'TEST001';
// Live-expiry NFO call. `kind` is derived from the symbol suffix (/CE$/),
// so the expiry only needs to be a plausible, unexpired contract.
const SYMBOL = 'NIFTY26DEC24000CE';
const LOT_SIZE = 50;

/** Held long: 50 contracts (one lot) of SYMBOL in ACCOUNT, NRML. */
const HELD_LONG_ROW = {
  account: ACCOUNT,
  tradingsymbol: SYMBOL,
  exchange: 'NFO',
  product: 'NRML',
  quantity: LOT_SIZE,
  qty: LOT_SIZE,
  average_price: 120,
  last_price: 125,
  pnl: 250,
};

/** Route-mock the positions feed so the held long is the only position. */
async function mockHeldLong(page) {
  await page.route('**/api/positions/**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        rows: [HELD_LONG_ROW],
        refreshed_at: new Date().toISOString(),
        as_of: null,
        stale_accounts: [],
      }),
    });
  });
}

/** Route-mock preflight so the chip renders from a controlled payload. */
async function mockPreflight(page) {
  await page.route('**/api/orders/preflight', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        blocked: [],
        diagnostics: { basket_margin_used: 0, available_margin: 200000, margin_shortfall: 0 },
      }),
    });
  });
}

/**
 * Load dashboard, force the positions store to fetch the mocked feed, then
 * open the ticket through the dev-only hook with the given prefill.
 * Returns false (test skipped) only when the hooks are absent from the build.
 */
async function openTicketWithPrefill(page, prefill) {
  await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

  // The (algo) layout installs window.__stores in onMount, which runs after
  // domcontentloaded. Wait for it rather than checking once and skipping.
  const hooksReady = await page
    .waitForFunction(
      () =>
        typeof (/** @type {any} */ (window).__stores?.openOrderTicketModal) === 'function' &&
        typeof (/** @type {any} */ (window).__stores?.positionsStore?.load) === 'function',
      null,
      { timeout: 15_000 },
    )
    .then(() => true)
    .catch(() => false);
  if (!hooksReady) {
    test.skip(true, 'window.__stores test hooks not present in this build');
    return false;
  }

  // Force the positions store to pull the mocked held-long feed.
  const positionsResponse = page.waitForResponse((r) => r.url().includes('/api/positions/'));
  await page.evaluate(() =>
    /** @type {any} */ (window).__stores.positionsStore.load({ force: true }),
  );
  await positionsResponse;

  await page.evaluate((p) => {
    /** @type {any} */ (window).__stores.openOrderTicketModal(p);
  }, prefill);

  const modal = page.locator('.oes-modal').first();
  await expect(modal).toBeVisible({ timeout: 8_000 });
  return true;
}

/**
 * Assert the ticket knows it is closing a held long.
 *
 * Primary signal: the SELL side button label. OrderTicket derives it from
 * `currentQty` (sideLabels, OrderTicket.svelte ~L352): "CLOSE/SELL" when a
 * long is held, plain "SELL" when currentQty is 0. This renders regardless
 * of market hours.
 *
 * Secondary signal: the margin/cash chip. It renders only once a preflight
 * result lands, which may not happen with the market closed. When present,
 * it must read the cash-released label and never "Margin required".
 */
async function expectClosingLongLabel(page, caseName) {
  const sellBtn = page.locator('.oes-modal .ot-side-btn:visible', { hasText: 'SELL' }).first();
  await expect(sellBtn, `${caseName}: SELL side button must render`).toBeVisible({ timeout: 8_000 });
  const sideText = ((await sellBtn.textContent()) || '').trim();
  console.log(`[${caseName}] side button: "${sideText}"`);
  expect(sideText, `${caseName}: SELL must read as a CLOSE of the held long`).toContain('CLOSE/SELL');

  const pill = page.locator('.oes-margin-pill').first();
  if (await pill.count()) {
    const title = (await pill.getAttribute('title')) || '';
    const text = (await pill.textContent()) || '';
    console.log(`[${caseName}] margin pill title: "${title}" · text: "${text}"`);
    expect(text.toLowerCase(), `${caseName}: pill must not read margin-required`).not.toContain('margin required');
    expect(text.toLowerCase(), `${caseName}: pill text`).toContain('releases cash');
  } else {
    console.log(`[${caseName}] margin pill not rendered (no preflight result in this environment); side label is the assertion`);
  }
}

test.describe('OrderTicket currentQty resolves from the held position, regardless of entry point', () => {
  test.setTimeout(90_000);

  test('watchlist-shaped open (no currentQty in prefill) of a held long via SELL → "Releases cash", not "Margin required"', async ({ page }) => {
    await mockHeldLong(page);
    await mockPreflight(page);
    await loginAsAdmin(page);

    // Watchlist-shaped prefill: symbol/side/qty/account only. No
    // `currentQty` key, so the ticket must resolve the held quantity itself.
    const opened = await openTicketWithPrefill(page, {
      symbol: SYMBOL,
      exchange: 'NFO',
      side: 'SELL',
      qty: LOT_SIZE,
      lotSize: LOT_SIZE,
      account: ACCOUNT,
      accounts: [ACCOUNT],
      action: 'open',
    });
    if (!opened) return;

    await expectClosingLongLabel(page, 'watchlist-shaped');
  });

  test('position-row open of the same held long via SELL (control) → "Releases cash"', async ({ page }) => {
    await mockHeldLong(page);
    await mockPreflight(page);
    await loginAsAdmin(page);

    // Control: the real currentQty the position-row opener passes
    // (+50 long), plus action 'close', as MarketPulse _openTicketFromRow does.
    const opened = await openTicketWithPrefill(page, {
      symbol: SYMBOL,
      exchange: 'NFO',
      side: 'SELL',
      qty: LOT_SIZE,
      lotSize: LOT_SIZE,
      account: ACCOUNT,
      accounts: [ACCOUNT],
      currentQty: LOT_SIZE,
      action: 'close',
    });
    if (!opened) return;

    await expectClosingLongLabel(page, 'position-row-control');
  });
});
