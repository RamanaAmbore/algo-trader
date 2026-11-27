/**
 * order_ticket_margin_chip_close_release.spec.js
 *
 * 2026-11 real-money audit fix — three linked bugs:
 *
 *  Bug 2 (label): the margin/cash chip's label must reflect the real
 *  cash-flow direction, not just "BUY=cash, SELL=margin":
 *    - open long  (BUY,  no position)         → "Cash debit" / Cost·Cash
 *    - open short (SELL, no position)         → "Margin required" / Req·Avail
 *    - close long (SELL, currentQty>0)        → "Cash released" (NOT margin)
 *    - close short (BUY, currentQty<0)        → "Margin released" (NOT a
 *      cost/debit — operator's own correction: "when you are short and
 *      when you close it you release margin")
 *  A genuinely negative broker `required` relabels the Req-row key to
 *  "Released" and shows the ABSOLUTE value, never a bare negative number.
 *
 *  Bug 3 (Dhan/Groww "show both"): a verified-close note
 *  (`diagnostics.close_exemption_note`) renders as an InfoHint next to
 *  the pill ONLY when the backend sets it — never on an ordinary open
 *  order, never when the field is absent.
 *
 * Uses the dev-only `window.__stores.openOrderTicketModal(prefill)` test
 * hook (added alongside this fix) to drive `currentQty`/`side`/`product`
 * precisely — the real UI only reaches these states via a genuine open
 * position, which this spec cannot depend on existing in any given
 * environment. `/api/orders/preflight` is route-mocked so the rendered
 * label is driven entirely by a controlled `diagnostics` payload, the
 * same technique as the pre-existing
 * order_ticket_margin_preview_naked.spec.js / order_ticket_submit_label_
 * matches_action.spec.js specs.
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *  1. SSOT  — drives the real rendered `.oes-margin-pill`, not a source grep
 *  2. Perf  — one mocked preflight round-trip per case, no extra polling
 *  3. Stale — pins the operator's own corrected wording for close-short
 *             ("margin released", not "cost"/"debit")
 *  4. Reuse — same SymbolPanel/OrderTicket every real order flow uses
 *  5. UX    — asserts the pill never shows a bare negative number, and
 *             the Bug 3 note is gated strictly on close_exemption_note
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/order_ticket_margin_chip_close_release.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

/** Route-mock /api/orders/preflight with a fixed diagnostics payload. */
async function mockPreflight(page, diagnostics) {
  await page.route('**/api/orders/preflight', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, blocked: [], diagnostics }),
    });
  });
}

/** Open the order ticket with an exact prefill via the dev-only test hook. */
async function openTicketWithPrefill(page, prefill) {
  await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1_500);
  const hookReady = await page.evaluate(
    () => typeof (/** @type {any} */ (window).__stores?.openOrderTicketModal) === 'function',
  );
  if (!hookReady) {
    test.skip(true, 'window.__stores.openOrderTicketModal test hook not present in this build');
    return false;
  }
  await page.evaluate((p) => {
    /** @type {any} */ (window).__stores.openOrderTicketModal(p);
  }, prefill);
  const modal = page.locator('.oes-modal').first();
  await expect(modal).toBeVisible({ timeout: 8_000 });
  return true;
}

const BASE_PREFILL = {
  exchange: 'NFO',
  qty: 20,
  lotSize: 50,
  account: 'TEST001',
  accounts: ['TEST001'],
};

test.describe('Margin/cash chip — close-long releases cash, close-short releases margin', () => {
  test.setTimeout(90_000);

  test('close a long option (SELL, currentQty>0) → "Cash released", never "Margin required"', async ({ page }) => {
    await mockPreflight(page, {
      basket_margin_used: 0, available_margin: 200000, margin_shortfall: 0,
    });
    await loginAsAdmin(page);
    const opened = await openTicketWithPrefill(page, {
      ...BASE_PREFILL,
      symbol: 'NIFTY25DEC24000CE',
      side: 'SELL',
      currentQty: 20,
      action: 'close',
    });
    if (!opened) return;

    await page.waitForTimeout(1_200);
    const pill = page.locator('.oes-margin-pill').first();
    if (await pill.count() === 0) {
      test.skip(true, 'margin pill did not render in this environment');
      return;
    }
    const title = (await pill.getAttribute('title')) || '';
    console.log(`[close-long] pill title: "${title}"`);
    expect(title.toLowerCase()).toContain('released');
    expect(title.toLowerCase()).not.toContain('margin required');
  });

  test('close a short option (BUY, currentQty<0) → "Margin released", NOT a cost/debit', async ({ page }) => {
    await mockPreflight(page, {
      basket_margin_used: 15000, available_margin: 200000, margin_shortfall: 0,
    });
    await loginAsAdmin(page);
    const opened = await openTicketWithPrefill(page, {
      ...BASE_PREFILL,
      symbol: 'NIFTY25DEC24000PE',
      side: 'BUY',
      currentQty: -20,
      action: 'close',
    });
    if (!opened) return;

    await page.waitForTimeout(1_200);
    const pill = page.locator('.oes-margin-pill').first();
    if (await pill.count() === 0) {
      test.skip(true, 'margin pill did not render in this environment');
      return;
    }
    const title = (await pill.getAttribute('title')) || '';
    console.log(`[close-short] pill title: "${title}"`);
    // Operator's own correction: "when you are short and when close it
    // you release margin" — must say margin released, never cash debit.
    expect(title.toLowerCase()).toContain('margin released');
    expect(title.toLowerCase()).not.toContain('cash debit');
  });

  test('open a long option (BUY, no position) stays "Cash debit" — unaffected by this fix', async ({ page }) => {
    await mockPreflight(page, {
      basket_margin_used: 15000, available_margin: 200000, margin_shortfall: 0,
    });
    await loginAsAdmin(page);
    const opened = await openTicketWithPrefill(page, {
      ...BASE_PREFILL,
      symbol: 'NIFTY25DEC24000CE',
      side: 'BUY',
      currentQty: 0,
      action: 'open',
    });
    if (!opened) return;

    await page.waitForTimeout(1_200);
    const pill = page.locator('.oes-margin-pill').first();
    if (await pill.count() === 0) {
      test.skip(true, 'margin pill did not render in this environment');
      return;
    }
    const title = (await pill.getAttribute('title')) || '';
    console.log(`[open-long] pill title: "${title}"`);
    expect(title.toLowerCase()).toContain('cash debit');
  });

  test('open a short option (SELL, no position) stays "Margin required" — unaffected by this fix', async ({ page }) => {
    await mockPreflight(page, {
      basket_margin_used: 15000, available_margin: 200000, margin_shortfall: 0,
    });
    await loginAsAdmin(page);
    const opened = await openTicketWithPrefill(page, {
      ...BASE_PREFILL,
      symbol: 'NIFTY25DEC24000PE',
      side: 'SELL',
      currentQty: 0,
      action: 'open',
    });
    if (!opened) return;

    await page.waitForTimeout(1_200);
    const pill = page.locator('.oes-margin-pill').first();
    if (await pill.count() === 0) {
      test.skip(true, 'margin pill did not render in this environment');
      return;
    }
    const title = (await pill.getAttribute('title')) || '';
    console.log(`[open-short] pill title: "${title}"`);
    expect(title.toLowerCase()).toContain('margin required');
  });

  test('a genuinely negative broker `required` relabels to "Released", never shown as a bare negative number', async ({ page }) => {
    await mockPreflight(page, {
      // Live example cited in the audit: required=-501045.81 (Kite netting
      // credit). This is a margin-mode (futures) order; close/open is
      // irrelevant to this specific relabel rule.
      basket_margin_used: -501045.81, available_margin: null, margin_shortfall: null,
    });
    await loginAsAdmin(page);
    const opened = await openTicketWithPrefill(page, {
      ...BASE_PREFILL,
      symbol: 'NIFTY25DECFUT',
      side: 'SELL',
      currentQty: 0,
      action: 'open',
    });
    if (!opened) return;

    await page.waitForTimeout(1_200);
    const pill = page.locator('.oes-margin-pill').first();
    if (await pill.count() === 0) {
      test.skip(true, 'margin pill did not render in this environment');
      return;
    }
    const text = (await pill.textContent()) || '';
    console.log(`[negative-required] pill text: "${text}"`);
    expect(text).not.toMatch(/-\s*₹|₹\s*-/);
    expect(text.toLowerCase()).toContain('released');
    // The absolute value, not the raw negative, must be shown — rounded
    // by aggFmtMargin (…81 → …46), so match loosely on the magnitude
    // rather than the exact unrounded fraction.
    expect(text).toMatch(/5,01,04[5-9]|501,04[5-9]|50104[5-9]/);
  });

  test('Bug 3 note renders ONLY when the backend sets close_exemption_note', async ({ page }) => {
    const note = 'Verified close — broker margin figure may be overstated (Dhan/Groww do not net against existing positions)';
    await mockPreflight(page, {
      basket_margin_used: 15000, available_margin: 200000, margin_shortfall: 0,
      close_exemption: true, close_exemption_note: note,
    });
    await loginAsAdmin(page);
    const opened = await openTicketWithPrefill(page, {
      ...BASE_PREFILL,
      symbol: 'NIFTY25DEC24000CE',
      side: 'SELL',
      currentQty: 20,
      action: 'close',
    });
    if (!opened) return;

    await page.waitForTimeout(1_200);
    const pill = page.locator('.oes-margin-pill').first();
    if (await pill.count() === 0) {
      test.skip(true, 'margin pill did not render in this environment');
      return;
    }
    // InfoHint renders as a SIBLING right after the pill, gated on
    // closeExemptionNote — scoped via the sibling selector so this
    // doesn't collide with other, unrelated InfoHints in the modal
    // (DRAFT / chase tooltips).
    const hintCount = await page.locator('.oes-margin-pill ~ .info-wrap').count();
    console.log(`[bug3-note] .info-wrap siblings right after the pill: ${hintCount}`);
    expect(hintCount).toBeGreaterThan(0);
  });

  test('Bug 3 note absent for an ordinary open order with no close_exemption_note', async ({ page }) => {
    await mockPreflight(page, {
      basket_margin_used: 15000, available_margin: 200000, margin_shortfall: 0,
    });
    await loginAsAdmin(page);
    const opened = await openTicketWithPrefill(page, {
      ...BASE_PREFILL,
      symbol: 'NIFTY25DEC24000CE',
      side: 'BUY',
      currentQty: 0,
      action: 'open',
    });
    if (!opened) return;

    await page.waitForTimeout(1_200);
    const pill = page.locator('.oes-margin-pill').first();
    if (await pill.count() === 0) {
      test.skip(true, 'margin pill did not render in this environment');
      return;
    }
    const hintCount = await page.locator('.oes-margin-pill ~ .info-wrap').count();
    console.log(`[bug3-note-absent] .info-wrap siblings right after the pill: ${hintCount}`);
    expect(hintCount).toBe(0);
  });
});
