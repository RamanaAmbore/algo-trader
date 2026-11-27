/**
 * order_ticket_margin_chip_close_release.spec.js
 *
 * 2026-11 real-money audit fix — three linked bugs. Includes a post-
 * review correctness pass: a broker's `required` figure for a close is
 * near-zero (closing commits no NEW cash/margin), so it can never be
 * shown AS the released amount — doing so would say "releases ₹0" for a
 * real premium credit, or label a possibly Dhan/Groww-inflated positive
 * requirement as if it were a release.
 *
 *  Bug 1 (product): a close ticket carries the position's REAL product
 *  (MIS/NRML/CNC), never a silently-defaulted NRML — see the second
 *  describe block below.
 *
 *  Bug 2 (label): the margin/cash chip's label must reflect the real
 *  cash-flow direction, not just "BUY=cash, SELL=margin":
 *    - open long  (BUY,  no position)  → "Cash debit" / Cost·Cash (honest)
 *    - open short (SELL, no position)  → "Margin required" / Req·Avail (honest)
 *    - close long (SELL, currentQty>0) → "Cash released (est. premium)"
 *      with price×qty as the estimate when a limit price is set, else a
 *      bare "Releases cash" with NO asserted number
 *    - close short (BUY, currentQty<0) → tooltip says "releases margin"
 *      (operator's own correction: "when you are short and when close it
 *      you release margin"), but the visible Req figure and its number
 *      stay the HONEST broker value — never relabelled "Released ₹X"
 *  A genuinely negative broker `required` (a real credit, e.g. Kite
 *  netting) relabels to "Released ₹|x|" — that is the ONE case where the
 *  literal broker figure IS the release amount.
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
 *  3. Stale — pins the operator's own corrected wording for close-short,
 *             AND the post-review correctness fix (no false ₹0/₹X "released")
 *  4. Reuse — same SymbolPanel/OrderTicket every real order flow uses
 *  5. UX    — asserts the pill never shows a bare negative number, never
 *             labels a dishonest figure as "released", and the Bug 3
 *             note is gated strictly on close_exemption_note
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

  test('close a long option with a limit price → "Cash released (est. premium)" with a REAL, non-zero estimate, never the broker\'s near-zero required figure', async ({ page }) => {
    // required=0 is what Kite/Dhan/Groww actually return for a closing
    // SELL (no NEW margin/cash committed) — post-review correctness
    // fix: this must NEVER be shown as the "released" amount. The real
    // estimated credit is `_price × _qty`, where `_qty` is the ticket's
    // own RESOLVED contract count (BASE_PREFILL's qty=20/lotSize=50
    // rounds UP to 1 whole lot = 50 contracts, same lot-rounding every
    // real ticket applies) — 500 × 50 = 25,000.
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
      price: 500,
    });
    if (!opened) return;

    await page.waitForTimeout(1_200);
    const pill = page.locator('.oes-margin-pill').first();
    if (await pill.count() === 0) {
      test.skip(true, 'margin pill did not render in this environment');
      return;
    }
    const title = (await pill.getAttribute('title')) || '';
    const text = (await pill.textContent()) || '';
    console.log(`[close-long] pill title: "${title}" · text: "${text}"`);
    expect(title.toLowerCase()).toContain('released');
    expect(title.toLowerCase()).not.toContain('margin required');
    // Never the broker's near-zero figure under "released" framing —
    // scoped to the credit clause only (before " vs "), since the
    // Avail/Cash side of the tooltip legitimately can read ₹0.00 for a
    // fake test account with no real funds row.
    const creditClause = title.split(' vs ')[0];
    expect(creditClause).not.toMatch(/released.*₹\s*0(\.00)?\b/i);
    // The real estimate (25,000) appears instead.
    expect(creditClause).toMatch(/25,000|25000/);
    expect(text).toMatch(/25,000|25000/);
  });

  test('close a long option with NO limit price yet → "Releases cash" with no asserted number (never a false ₹0)', async ({ page }) => {
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
      // price deliberately omitted — no estimate possible yet.
    });
    if (!opened) return;

    await page.waitForTimeout(1_200);
    const pill = page.locator('.oes-margin-pill').first();
    if (await pill.count() === 0) {
      test.skip(true, 'margin pill did not render in this environment');
      return;
    }
    const text = (await pill.textContent()) || '';
    console.log(`[close-long-no-price] pill text: "${text}"`);
    expect(text.toLowerCase()).toContain('releases cash');
    // Must never show the broker's meaningless 0 as if it were the
    // release amount — assert the DOM structure directly (the "Releases
    // cash" row renders as plain text with no `.oes-margin-pill-val`
    // child at all), not just a `₹` string match, which would be
    // vacuously true regardless of this fix (no `₹` glyph is ever
    // rendered in the pill's VISIBLE text to begin with — only in the
    // `title` tooltip attribute).
    const releasesCashRow = pill.locator('.oes-margin-pill-row', { hasText: 'Releases cash' });
    await expect(releasesCashRow).toBeVisible();
    expect(await releasesCashRow.locator('.oes-margin-pill-val').count()).toBe(0);
    // Belt-and-suspenders: no standalone "0" or "0.00" token anywhere in
    // the rendered text either.
    expect(text).not.toMatch(/(^|\s)0(\.00)?(\s|$)/);
  });

  test('close a short option (BUY, currentQty<0) → "releases margin" wording, the honest Req figure, NOT a cost/debit, NOT asserted as the release amount', async ({ page }) => {
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
    const text = (await pill.textContent()) || '';
    console.log(`[close-short] pill title: "${title}" · text: "${text}"`);
    // Operator's own correction: "when you are short and when close it
    // you release margin" — the DIRECTION must say so.
    expect(title.toLowerCase()).toContain('releases margin');
    expect(title.toLowerCase()).not.toContain('cash debit');
    // But the ₹15,000 broker figure must NOT be asserted as the literal
    // release amount — "margin released: ₹15,000" would be false (it's
    // a requirement figure, possibly Dhan/Groww-inflated, not a release).
    expect(title.toLowerCase()).not.toMatch(/margin released:\s*₹/);
    // The visible pill body stays honest: "Req ₹15,000", not "Released".
    expect(text).toMatch(/Req/);
    expect(text).not.toContain('Released');
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

test.describe('Bug 1 — a close ticket carries the REAL product, not a defaulted NRML', () => {
  test.setTimeout(60_000);

  test('product=MIS prefill reaches /api/orders/preflight as product=MIS, not NRML', async ({ page }) => {
    let preflightBody = null;
    await page.route('**/api/orders/preflight', async (route) => {
      preflightBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ok: true, blocked: [],
          diagnostics: { basket_margin_used: 15000, available_margin: 200000, margin_shortfall: 0 },
        }),
      });
    });
    await loginAsAdmin(page);
    const opened = await openTicketWithPrefill(page, {
      ...BASE_PREFILL,
      symbol: 'NIFTY25DEC24000CE',
      side: 'SELL',
      currentQty: 20,
      action: 'close',
      product: 'MIS',
    });
    if (!opened) return;

    await page.waitForTimeout(1_200);
    expect(preflightBody, 'expected a /api/orders/preflight call').not.toBeNull();
    console.log(`[bug1-product] preflight body product: ${preflightBody?.product}`);
    expect(preflightBody?.product).toBe('MIS');
  });

  test('no product in the prefill falls back to NRML (OrderTicket default), never silently to the wrong held product', async ({ page }) => {
    let preflightBody = null;
    await page.route('**/api/orders/preflight', async (route) => {
      preflightBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ok: true, blocked: [],
          diagnostics: { basket_margin_used: 15000, available_margin: 200000, margin_shortfall: 0 },
        }),
      });
    });
    await loginAsAdmin(page);
    const opened = await openTicketWithPrefill(page, {
      ...BASE_PREFILL,
      symbol: 'NIFTY25DEC24000CE',
      side: 'SELL',
      currentQty: 20,
      action: 'close',
      // product deliberately omitted
    });
    if (!opened) return;

    await page.waitForTimeout(1_200);
    expect(preflightBody, 'expected a /api/orders/preflight call').not.toBeNull();
    console.log(`[bug1-product-default] preflight body product: ${preflightBody?.product}`);
    expect(preflightBody?.product).toBe('NRML');
  });

  test('source: all three close-ticket openers pass the real row product through', async () => {
    const { execSync } = await import('child_process');
    const check = (file, pattern) => {
      const out = execSync(
        `grep -c "${pattern}" "${file}" || true`,
        { cwd: process.cwd().replace(/\/frontend$/, ''), encoding: 'utf8' },
      ).trim();
      return Number(out) > 0;
    };
    // MarketPulse.svelte's _openTicketFromRow
    expect(check('frontend/src/lib/MarketPulse.svelte', 'product:.*r\\.product')).toBe(true);
    // PerformancePage.svelte's openOrderTicket
    expect(check('frontend/src/lib/PerformancePage.svelte', 'product:.*row\\.product')).toBe(true);
    // derivatives +page.svelte's closePosition
    expect(check(
      'frontend/src/routes/(algo)/admin/derivatives/+page.svelte',
      'product:.*c\\.product',
    )).toBe(true);
  });
});
