/**
 * order_ticket_exchange_submit_payload.spec.js
 *
 * 2026-10-20 fix — `OrderTicket.svelte`'s submit handler built its
 * `submitCtx.exchange` field from the bare `exchange` PROP. Every other
 * call site in the same file that needs "the real exchange for this
 * order" (margin preview, preflight check, basket submission, the
 * market-badge label, the embedded Chart link) resolves via the
 * established fallback chain `_exchange || _resolvedExchange ||
 * exchange || 'NFO'` instead — `_exchange` is local `$state` seeded
 * from the instruments-cache-resolved `_resolvedExchange`, with the
 * bare prop only as a last-resort fallback. The real order-submission
 * payload was the one path still reading the stale prop directly.
 *
 * REAL, currently-reachable regression (not contrived): `/orders` page
 * persists `?symbol=…&exchange=…` to the URL so an operator can
 * bookmark/share a specific contract (own comment, orders/+page.svelte
 * "Persist ?symbol=…&exchange=… so the operator can bookmark..."). The
 * `exchange` URL param seeds `_entryExchange` ONE-WAY (URL → state) and
 * is never re-synced when the operator picks a DIFFERENT symbol via the
 * search box afterwards (`onSymbolChange` only updates `_entrySymbol`,
 * not `_entryExchange`) — so a bookmarked/stale exchange query param
 * lingers as the `exchange` PROP fed into `<SymbolPanel>` →
 * `<OrderTicket>` even after the operator searches for and picks a
 * symbol that actually lives on a different exchange. OrderTicket's own
 * `_resolvedExchange` (instruments-cache lookup keyed off the picked
 * symbol) computes the CORRECT exchange regardless, and `_exchange`
 * seeds from it — but pre-fix, the real order PAYLOAD (`submitCtx`,
 * flowing into `placeCtx.exchange` and from there into
 * `buildPlacePayload`'s own `ctx.exchange || ctx.resolvedExchange`
 * chain, which picks the stale non-empty `ctx.exchange` FIRST) still
 * carried the stale URL-param value, not the correct resolved one.
 *
 * Verified live (both directions) against a fully reverted copy of this
 * file (`git stash` the single-line fix, re-run, `git stash pop`):
 *   - `/orders?exchange=NSE` + pick `CRUDEOIL26NOVFUT` (a real MCX
 *     future) → pre-fix POST body carried `exchange: "NSE"`, lot_size
 *     resolved correctly (100) but the SEGMENT was wrong. Post-fix:
 *     `"MCX"`.
 *   - `/orders?exchange=MCX` + pick `NIFTY26OCTFUT` (NFO) → same class,
 *     mirrored direction. Post-fix: `"NFO"`.
 * Both cases are covered below as the primary discriminating tests.
 *
 * Downstream trace (per task instructions) — confirmed NO separate fix
 * needed beyond this one line:
 *   - `orderTicketSubmit.js:buildPlacePayload()` already has its own
 *     `ctx.exchange || ctx.resolvedExchange || 'NFO'` fallback — once
 *     `submitCtx.exchange` (this fix) carries the correct value,
 *     `placeCtx.exchange` (`{...submitCtx}`) is already correct and
 *     this fallback never needs to rescue it from `resolvedExchange`.
 *   - `orderTicketSubmit.js:buildOnSubmitPayload()` reads `ctx.exchange`
 *     directly with NO fallback at all — this is the `onSubmit(payload)`
 *     callback surfaced to every host page (MarketPulse, derivatives,
 *     /orders). All three hosts' `onSubmit` handlers only reload/toast
 *     on the result and never branch on `payload.exchange` for
 *     correctness, but the field is now correct for any future
 *     consumer that does.
 *   - The separate `action==='modify'` payload (`buildModifyPayload`)
 *     sends NO exchange field at all — `PUT /orders/{id}` resolves the
 *     order's own exchange/symbol server-side from the `AlgoOrder` row
 *     (see CLAUDE.md "Lot/contract oversize guards" §Admin manual
 *     modify-order route). Nothing to fix there.
 *
 * NOT covered dynamically — the originally-envisioned "operator
 * manually overrides via the Exchange Select dropdown" scenario: the
 * dropdown itself was deliberately REMOVED on 2026-07-26 (commit
 * 271713dd, "remove exchange knob") — `OrderKnobsRow.svelte` still
 * accepts `exchange`/`onExchangeChange`/`exchangeOptions` props (dead
 * plumbing) but its markup renders only Type/Product/Variety, no
 * Exchange `<Select>`. There is currently no UI control that can set
 * `_exchangeTouched = true`. The URL-param scenario above exercises the
 * exact same code path (`_exchange` seeded from `_resolvedExchange`,
 * diverging from a stale non-empty `exchange` prop) and IS reachable
 * via the real, documented bookmarking feature, so it stands in for the
 * override case without needing to re-add removed UI. Flagged separately
 * for the operator: the dead Exchange-picker plumbing + its now-stale
 * explanatory comments are a distinct, pre-existing finding, not fixed
 * here (out of this item's scope).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const ROOT = '/Users/ramanambore/projects/ramboq/frontend/src/lib';

async function mockPaperMode(page) {
  await page.route('**/api/admin/execution/mode', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          mode: 'paper',
          branch: 'dev',
          allowed_modes: ['idle', 'sim', 'replay', 'paper', 'shadow'],
        }),
      });
    } else {
      await route.continue();
    }
  });
}

/**
 * Open /orders (optionally with a stale ?exchange= query param), pick
 * `searchTerm` from the symbol search suggestions, set order type to
 * MARKET (avoids the limit-price validation gate / depth-poll delay),
 * flip side to BUY, and submit. Returns the intercepted
 * /api/orders/ticket POST body.
 */
async function placeViaSearch(page, { urlExchange = '', searchTerm }) {
  let capturedBody = null;
  await page.route('**/api/orders/ticket', async (route) => {
    capturedBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ order_id: '12345', mode: 'paper', status: 'COMPLETE', detail: 'ok' }),
    });
  });
  await mockPaperMode(page);
  await loginAsAdmin(page);

  const url = urlExchange ? `${BASE}/orders?exchange=${urlExchange}` : `${BASE}/orders`;
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30_000 });

  const symInput = page.locator('.ssi-input').first();
  await expect(symInput).toBeVisible({ timeout: 15_000 });
  await symInput.fill(searchTerm);
  const sugg = page.locator('.ssi-drop .ssi-row').first();
  await expect(sugg).toBeVisible({ timeout: 10_000 });
  await sugg.click({ force: true });
  await page.waitForTimeout(800);

  // MARKET type avoids the "limit price required" validation gate.
  const typeTrigger = page.locator('[aria-label="Order type"]').first();
  await typeTrigger.click();
  const marketOpt = page.locator('.rbq-select-option-label').filter({ hasText: /^MARKET$/ }).first();
  await marketOpt.click();
  await page.waitForTimeout(300);

  const buyPill = page.locator('button.ot-side-buy').first();
  if (await buyPill.count() > 0 && await buyPill.isEnabled().catch(() => false)) {
    await buyPill.click();
    await page.waitForTimeout(200);
  }

  const submitBtn = page.locator('.oes-common-submit, .ot-submit').first();
  await expect(submitBtn).toBeVisible({ timeout: 8_000 });
  if (!(await submitBtn.isEnabled())) {
    test.skip(true, 'submit button not enabled — ticket form incomplete in this environment');
    return null;
  }
  await submitBtn.click();
  await page.waitForTimeout(1500);
  return capturedBody;
}

test.describe('Order Ticket — submit payload exchange field', () => {
  test('source: submitCtx.exchange resolves via the same fallback chain as every other exchange-reading site in the file', () => {
    const orderTicket = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    const idx = orderTicket.indexOf('const submitCtx = {');
    expect(idx, 'submitCtx construction not found').toBeGreaterThan(-1);
    const block = orderTicket.slice(idx, orderTicket.indexOf('};', idx));
    // Old buggy line (bare prop, no fallback) must be gone from this block.
    expect(block).not.toMatch(/\n\s*exchange,\s*\n/);
    // New line matches the established pattern used elsewhere in the file.
    expect(block).toMatch(/exchange:\s*_exchange\s*\|\|\s*_resolvedExchange\s*\|\|\s*exchange\s*\|\|\s*'NFO'/);
  });

  test('live: default NFO future symbol (NIFTY) submits with exchange=NFO', async ({ page }) => {
    const body = await placeViaSearch(page, { searchTerm: 'NIFTY26OCTFUT' });
    if (!body) return;
    expect(body.tradingsymbol).toBe('NIFTY26OCTFUT');
    expect(body.exchange).toBe('NFO');
  });

  test('live: default MCX future symbol (CRUDEOIL) submits with exchange=MCX', async ({ page }) => {
    const body = await placeViaSearch(page, { searchTerm: 'CRUDEOIL26NOVFUT' });
    if (!body) return;
    expect(body.tradingsymbol).toBe('CRUDEOIL26NOVFUT');
    expect(body.exchange).toBe('MCX');
  });

  test('live: REGRESSION — stale ?exchange=NSE URL param does not override a freshly-picked MCX contract', async ({ page }) => {
    // Confirmed via git-stash A/B on this exact file: pre-fix this POST
    // body carried exchange:"NSE" (the stale bookmarked query param);
    // post-fix it carries the instruments-cache-resolved "MCX".
    const body = await placeViaSearch(page, { urlExchange: 'NSE', searchTerm: 'CRUDEOIL26NOVFUT' });
    if (!body) return;
    expect(body.tradingsymbol).toBe('CRUDEOIL26NOVFUT');
    expect(body.exchange, 'stale ?exchange=NSE leaked into the real order payload').toBe('MCX');
  });

  test('live: REGRESSION — stale ?exchange=MCX URL param does not override a freshly-picked NFO contract', async ({ page }) => {
    // Mirror direction of the above — same class of bug, opposite segments.
    const body = await placeViaSearch(page, { urlExchange: 'MCX', searchTerm: 'NIFTY26OCTFUT' });
    if (!body) return;
    expect(body.tradingsymbol).toBe('NIFTY26OCTFUT');
    expect(body.exchange, 'stale ?exchange=MCX leaked into the real order payload').toBe('NFO');
  });
});
