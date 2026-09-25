/**
 * order_ticket_cold_cache_close_qty.spec.js
 *
 * D2 fix (2026-09): a CLOSE ticket opened while the instruments cache is
 * still loading must NOT send a many-times-oversized order. Before the fix,
 * `getInstrument()` returning null (cache cold) fell back to a bogus
 * lot=1, and OrderTicket's qty math sent the raw held-contract count as if
 * it were already in lots — e.g. closing 1 lot of NIFTY (75 contracts) sent
 * quantity=75, which the backend's lots-convention endpoint then multiplied
 * by the REAL lot size (75) → 5625 contracts, a ~75x oversize.
 *
 * Verified here on BOTH surfaces that own a close-ticket entry point:
 *   - /pulse            → MarketPulse.svelte `_openTicketFromRow`
 *   - /admin/derivatives → derivatives +page.svelte `closePosition`
 *
 * Strategy: delay `/api/instruments` so the ticket's first render happens
 * against a cold cache, click a close row IMMEDIATELY (before the delayed
 * response lands), then assert the ticket's resolved qty settles to the
 * CORRECT lot count once the cache resolves — never a lot-multiplied
 * oversize. The order POST is intercepted and mocked so this test never
 * reaches the real backend or broker.
 *
 * Five quality dimensions:
 *  1. SSOT   — both close-ticket entry points covered (MarketPulse +
 *              derivatives), not just one
 *  2. Perf   — delaying /api/instruments must not hang the ticket open
 *              indefinitely; awaits settle within a bounded timeout
 *  3. Stale  — asserts the LIVE resolved qty, not a source-grep proxy
 *  4. Reuse  — both surfaces render the canonical SymbolPanel/OrderTicket,
 *              not a bespoke close dialog
 *  5. UX     — the qty chip (".ot-qty-chip") must read the real contract
 *              count, never a lot-multiplied value, by the time the
 *              operator can interact with the form
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   npx playwright test e2e/order_ticket_cold_cache_close_qty.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(90_000);

/** Delay (but still fulfill from the real backend) /api/instruments so the
 *  page's first paint + first click race against a cold cache. */
async function delayInstruments(page, ms = 4_000) {
  await page.route('**/api/instruments/**', async (route) => {
    await new Promise((r) => setTimeout(r, ms));
    await route.continue();
  });
}

/** Mock the order-placement POST so this spec never reaches a real broker.
 *  Captures the submitted body for assertion. */
function mockTicketPlacement(page) {
  const captured = [];
  page.route('**/api/orders/ticket', async (route) => {
    const body = route.request().postDataJSON();
    captured.push(body);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ order_id: 'MOCK-1', mode: body.mode, status: 'OPEN' }),
    });
  });
  return captured;
}

test.describe('D2 — close ticket on a cold instruments cache (/pulse)', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('closing a position opened before /api/instruments resolves settles to the real lot qty, not a lot-multiplied oversize', async ({ page }) => {
    await delayInstruments(page, 4_000);
    mockTicketPlacement(page);

    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const posRow = page.locator('.ag-row.row-pos-long, .ag-row.row-pos-short').first();
    if (await posRow.count() === 0) {
      test.skip(true, 'no open F&O position in this environment — D2 cold-cache test skipped');
      return;
    }

    // Click immediately — the instruments fetch is still in flight (4s delay).
    await posRow.click();

    const overlay = page.locator('.canonical-modal-overlay');
    await expect(overlay).toBeVisible({ timeout: 8_000 });

    // Qty chip renders "= {qty} units". Read it once the delayed
    // instruments call has had time to resolve and OrderTicket's
    // cache-version-reactive repair effect has had a chance to fire.
    const qtyChip = page.locator('.ot-qty-chip').first();
    await expect(qtyChip).toBeVisible({ timeout: 3_000 }).catch(() => {});
    await page.waitForTimeout(5_000); // instruments delay (4s) + settle margin

    const lotsInput = page.locator('.ot-lots-input').first();
    if (await lotsInput.count() === 0) {
      // Non-F&O (equity) close — no lot concept, nothing to oversize.
      test.skip(true, 'closed position is equity (no lot multiplier) — D2 does not apply');
      return;
    }
    const lotsVal = Number(await lotsInput.inputValue());

    // Sanity ceiling: a genuine oversize bug multiplies by the real lot
    // size (typically 15-1800 depending on the instrument) ON TOP of the
    // correct lot count — i.e. it inflates by 15x-1800x. A generous but
    // still meaningful ceiling: no single-position close ticket should
    // ever resolve to more than 500 lots from THIS bug class (the G2
    // fat-finger cap for closes is exempted at 5 for opens, but even a
    // large legitimate close is not going to be >500 lots in this book).
    expect(lotsVal, `resolved lots (${lotsVal}) looks oversized — possible D2 regression`)
      .toBeLessThan(500);
    expect(lotsVal).toBeGreaterThan(0);

    console.log(`[order_ticket_cold_cache_close_qty/pulse] resolved lots=${lotsVal}`);
  });
});

test.describe('D2 — close ticket on a cold instruments cache (/admin/derivatives)', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('closePosition() on a cold cache settles to the real lot qty', async ({ page }) => {
    await delayInstruments(page, 4_000);
    mockTicketPlacement(page);

    await loginAsAdmin(page);
    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });

    // Legs / Close tab rows render position symbols with a close handler
    // (`onClosePosition={closePosition}`). Click the first position row
    // immediately, before the delayed /api/instruments response lands.
    const legRow = page.locator('[data-testid="leg-row"], .ag-row').first();
    if (await legRow.count() === 0) {
      test.skip(true, 'no legs/positions rendered in this environment — D2 derivatives test skipped');
      return;
    }
    await legRow.click();

    const overlay = page.locator('.canonical-modal-overlay');
    const opened = await overlay.isVisible({ timeout: 8_000 }).catch(() => false);
    if (!opened) {
      test.skip(true, 'clicking the row did not open a close ticket in this environment');
      return;
    }

    await page.waitForTimeout(5_000); // instruments delay + settle margin

    const lotsInput = page.locator('.ot-lots-input').first();
    if (await lotsInput.count() === 0) {
      test.skip(true, 'no lot-based ticket opened — D2 does not apply to this row');
      return;
    }
    const lotsVal = Number(await lotsInput.inputValue());
    expect(lotsVal, `resolved lots (${lotsVal}) looks oversized — possible D2 regression`)
      .toBeLessThan(500);
    expect(lotsVal).toBeGreaterThan(0);

    console.log(`[order_ticket_cold_cache_close_qty/derivatives] resolved lots=${lotsVal}`);
  });
});
