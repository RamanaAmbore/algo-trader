/**
 * order_ticket_footer_and_close_consistency.spec.js
 *
 * Covers the 2026-09 Ticket-tab footer redesign + close-button color fix:
 *
 *  (a) The Ticket-tab common-action footer row (.oes-common-row) exposes
 *      exactly ONE clickable control — Submit. The basket-cart toggle
 *      (both the interactive checkbox variant and the Chain-only static
 *      badge) was removed entirely; the side label
 *      (.oes-footer-side-btn-single) is now an inert preview `<span>`
 *      with no click handler, no role, no tabindex.
 *  (b) Side selection still works end-to-end via OrderTicket's own
 *      SideToggle pills in the ticket body (.ot-side-buy / .ot-side-sell)
 *      — clicking SELL there, then Submit, posts side=SELL to
 *      /api/orders/ticket and the footer preview label mirrors the pick.
 *  (c) OptionChainTab's CE/PE buy/sell button chrome was softened
 *      (quiet at rest, full strength on hover) but the action itself —
 *      firing a quick-toast confirmation — is unchanged.
 *  (d) OrderTicket's own `.ot-close` button now uses the app-wide
 *      danger (red) close-button tokens, matching every other close
 *      button in the app (was cyan/info tokens, the only outlier).
 *
 * Run:
 *   PLAYWRIGHT_USER=rambo PLAYWRIGHT_PASS=admin1234 \
 *   npx playwright test e2e/order_ticket_footer_and_close_consistency.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(90_000);

/** Open the common-actions order ticket via the dashboard's page-header
 *  Order entry point (same pattern used by the other order_ticket_*
 *  specs) and return the modal locator. */
async function openTicketModal(page) {
  await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1_500);
  const orderBtn = page.locator('button.pha-order').first();
  if (await orderBtn.count() === 0) return null;
  await orderBtn.click({ force: true });
  const modal = page.locator('.oes-modal').first();
  await expect(modal).toBeVisible({ timeout: 8_000 });
  return modal;
}

test.describe('Ticket-tab footer — exactly one clickable control', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('(a) no basket toggle, side label is an inert span, Submit is the sole control', async ({ page }) => {
    await loginAsAdmin(page);
    const modal = await openTicketModal(page);
    if (!modal) { test.skip(true, 'no .pha-order entry point in this environment'); return; }

    // Basket-cart toggle (interactive checkbox AND the Chain-only static
    // badge) must be fully gone — not just hidden.
    const basketCount = await page.locator('.oes-common-basket-toggle-icon').count();
    expect(basketCount, 'basket-toggle icon must be removed from the Ticket-tab footer').toBe(0);

    const footerRow = page.locator('.oes-common-row').first();
    await expect(footerRow).toBeVisible({ timeout: 5_000 });

    // Count genuinely clickable descendants: buttons, role=button,
    // links, and checkboxes. Should be exactly 1 (Submit).
    const clickableCount = await footerRow.evaluate((row) => {
      const sel = 'button, [role="button"], a[href], input[type="checkbox"]';
      return row.querySelectorAll(sel).length;
    });
    expect(clickableCount, `expected exactly 1 clickable control in the footer row, found ${clickableCount}`).toBe(1);

    const submitBtn = footerRow.locator('.oes-common-submit');
    await expect(submitBtn).toHaveCount(1);

    // Side-preview label: present, but inert — a <span>, no role, no
    // tabindex, no click handler wired (Playwright can't assert "no
    // handler" directly, but tag/role/tabindex absence is the DOM-level
    // signal that it was authored as non-interactive).
    const sideLabel = page.locator('.oes-footer-side-btn-single').first();
    if (await sideLabel.count() > 0) {
      const tag = await sideLabel.evaluate((el) => el.tagName);
      expect(tag, 'side-preview label must be a <span>, not a <button>').toBe('SPAN');
      const role = await sideLabel.getAttribute('role');
      const tabindex = await sideLabel.getAttribute('tabindex');
      expect(role, 'side-preview label must have no ARIA role').toBeNull();
      expect(tabindex, 'side-preview label must have no tabindex').toBeNull();
    }
  });
});

test.describe('Ticket-tab side selection — end to end via SideToggle', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('(b) picking SELL via the ticket body SideToggle posts transaction_type=SELL', async ({ page }) => {
    /** @type {any} */
    let postedBody = null;
    await page.route('**/api/orders/ticket', async (route) => {
      if (route.request().method() === 'POST') {
        try { postedBody = route.request().postDataJSON(); } catch { /* ignore */ }
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ order_id: 'MOCK-FOOTER-SELL', mode: 'paper', status: 'OPEN' }),
      });
    });
    // Engine-idle guard — OrderTicket.submit() refuses with "Engine is
    // idle" when the executionMode store reads 'idle', which it does by
    // default on dev/localhost until the layout's loadMode() poll
    // confirms otherwise (routes/(algo)/+layout.svelte, GET
    // /api/admin/execution/mode via fetchExecutionMode()).
    await page.route('**/api/admin/execution/mode*', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ mode: 'paper', allowed_modes: ['paper', 'live', 'shadow', 'sim', 'replay'], branch: 'dev' }),
      });
    });
    // Margin preflight (POST /api/orders/preflight) — mocked so a real
    // broker-side margin-check failure in this test environment can't
    // block/slow the submit path this test is actually exercising.
    await page.route('**/api/orders/preflight', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ok: true, blocked: [],
          diagnostics: { basket_margin_used: 1000, available_margin: 100000, margin_shortfall: 0 },
        }),
      });
    });

    await loginAsAdmin(page);
    const modal = await openTicketModal(page);
    if (!modal) { test.skip(true, 'no .pha-order entry point in this environment'); return; }

    // Symbol picker uses SymbolSearchInput's own `.ssi-input` — select a
    // real suggestion row rather than blind-pressing Enter so the
    // resolved-symbol state (which gates SideToggle) reliably lands.
    const symInput = page.locator('.ssi-input').first();
    if (await symInput.count() === 0) { test.skip(true, 'no symbol input in this ticket layout'); return; }
    await symInput.fill('RELIANCE');
    // Pick the plain equity row specifically (exact "RELIANCE" symbol
    // text) — a bare prefix match can otherwise land the FIRST result,
    // which is often a derivative contract (e.g. RELIANCE26SEPFUT) and
    // routes through a different qty/margin/confirm flow than the
    // simple equity path this test exercises.
    const eqRow = page.locator('.ssi-row').filter({ has: page.locator('.ssi-row-sym', { hasText: /^RELIANCE$/ }) }).first();
    if (await eqRow.waitFor({ state: 'visible', timeout: 6_000 }).then(() => true).catch(() => false)) {
      await eqRow.click();
    } else {
      const anyRow = page.locator('.ssi-row').first();
      if (await anyRow.count() > 0) await anyRow.click();
      else await page.keyboard.press('Enter').catch(() => {});
    }
    await page.waitForTimeout(600);

    const sellPill = page.locator('button.ot-side-sell').first();
    await expect(sellPill).toBeVisible({ timeout: 5_000 });
    if (!(await sellPill.isEnabled().catch(() => false))) {
      test.skip(true, 'SideToggle still disabled — symbol did not resolve in this environment');
      return;
    }
    await sellPill.click();

    // Wait for the depth-ladder poll to auto-fill the limit price before
    // submitting — this exact race (submit fires while `_price` is
    // still '') is the historical "CRUDEOIL SELL never fires" defect
    // (see crudeoil_add_sell.spec.js); clicking Submit before the price
    // lands trips OrderTicket's silent `if (validationErr) return;`
    // guard with "Limit price required", masking the actual side-
    // selection behaviour this test verifies.
    const priceInput = page.locator('.ot-price-cell .ot-input').first();
    if (await priceInput.count() > 0) {
      await expect(async () => {
        const v = await priceInput.inputValue();
        expect(v.trim().length).toBeGreaterThan(0);
      }).toPass({ timeout: 8_000 });
    } else {
      await page.waitForTimeout(1_000);
    }

    // Footer preview label mirrors the pick (onSideChange wiring) —
    // confirms the whole onSideChange → _modalSide → side-prop chain.
    const sideBtn = page.locator('.oes-footer-side-btn-single').first();
    if (await sideBtn.count() > 0) {
      await expect(sideBtn).toHaveClass(/on-sell/, { timeout: 2_000 });
      await expect(sideBtn).toContainText('SELL');
    }

    const submitBtn = page.locator('.oes-common-submit').first();
    await expect(submitBtn).toBeVisible({ timeout: 5_000 });
    if (!(await submitBtn.isEnabled().catch(() => false))) {
      test.skip(true, 'submit button not enabled — ticket form incomplete in this environment');
      return;
    }
    await submitBtn.click();
    await page.waitForTimeout(1_500);

    expect(postedBody, 'no POST to /api/orders/ticket fired').not.toBeNull();
    expect(postedBody?.side, 'posted side mismatch').toBe('SELL');
  });
});

test.describe('OptionChainTab — CE/PE buttons still fire after chrome softening', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('(c) clicking a chain buy pill confirms via toast (or a visible error, not silent no-op)', async ({ page }) => {
    await loginAsAdmin(page);
    const modal = await openTicketModal(page);
    if (!modal) { test.skip(true, 'no .pha-order entry point in this environment'); return; }

    // Seed a liquid F&O underlying via the shared header symbol picker
    // (SymbolSearchInput), then switch to the Chain tab — same
    // underlying carries across tabs (_localSymbol is header-level,
    // shared by both Ticket and Chain).
    const symInput = page.locator('.ssi-input').first();
    if (await symInput.count() === 0) { test.skip(true, 'no symbol input in this ticket layout'); return; }
    await symInput.fill('NIFTY');
    const symRow = page.locator('.ssi-row').first();
    if (await symRow.waitFor({ state: 'visible', timeout: 6_000 }).then(() => true).catch(() => false)) {
      await symRow.click();
    } else {
      await page.keyboard.press('Enter').catch(() => {});
    }
    await page.waitForTimeout(600);

    // Scoped to the modal's own tab strip (.oes-tabs) — a bare
    // `[role="tab"]` matches page-wide, including unrelated Dashboard
    // card tabs (NAV/Intraday/Performance/etc).
    // AlgoTabs renders each label with a trailing space in textContent
    // (badge-slot layout) — anchor the regex loosely so it doesn't
    // accidentally match "Chart" via a bare substring test either.
    const chainTab = page.locator('.oes-modal .oes-tabs [role="tab"]').filter({ hasText: /^Chain\s*$/i }).first();
    if (await chainTab.count() === 0) { test.skip(true, 'no Chain tab rendered in this ticket layout'); return; }
    if (await chainTab.isDisabled().catch(() => false)) {
      test.skip(true, 'Chain tab disabled — no F&O for this underlying in this environment');
      return;
    }
    await chainTab.click();

    const buyBtn = page.locator('button.chain-btn.chain-btn-buy').first();
    try {
      await buyBtn.waitFor({ state: 'visible', timeout: 20_000 });
    } catch {
      test.skip(true, 'chain picker rendered but no contracts loaded (Kite outage / illiquid underlying)');
      return;
    }
    // Buttons disable per-row until their own live quote lands ("No
    // quote — price unknown") — give the quote poll a real window
    // before deciding the environment can't exercise this (e.g. market
    // closed with no last-quote fallback for this contract).
    const enabled = await buyBtn.isEnabled().catch(() => false)
      || await expect(buyBtn).toBeEnabled({ timeout: 15_000 }).then(() => true).catch(() => false);
    if (!enabled) {
      test.skip(true, 'no live quote for this contract in this environment (market closed / no last price)');
      return;
    }

    // Softened-chrome regression check: border must still carry the
    // buy-side tint at rest (not literally invisible/transparent).
    const restBorder = await buyBtn.evaluate((el) => getComputedStyle(el).borderColor);
    expect(restBorder, 'chain buy button must still have a visible border at rest').not.toBe('rgba(0, 0, 0, 0)');

    await buyBtn.click();

    // Either a quick-toast confirmation or a visible basket error banner
    // must appear — never a silent no-op (the account-race regression
    // this exact button previously guarded against).
    const toastOrErr = page.locator('.chain-quick-toast, .chain-basket-err').first();
    await expect(toastOrErr).toBeVisible({ timeout: 5_000 });
  });
});

test.describe('.ot-close — matches the app-wide danger (red) close-button convention', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('(d) .ot-close computed background is the danger token, not cyan', async ({ page }) => {
    await loginAsAdmin(page);
    const modal = await openTicketModal(page);
    if (!modal) { test.skip(true, 'no .pha-order entry point in this environment'); return; }

    // .ot-close only renders when OrderTicket is mounted with
    // standalone=true; SymbolPanel (the app's sole mount point) always
    // passes standalone=false, so .ot-close never actually paints live.
    // Use the same synthetic-element + copied-scope-class technique as
    // order_ticket_visual_consistency.spec.js's CE/PE `.on` check to
    // resolve the scoped rule without needing a live standalone mount.
    const result = await page.evaluate(() => {
      const ticketModalEl = document.querySelector('.ot-modal');
      const oesCloseEl = document.querySelector('.oes-close');
      if (!ticketModalEl || !oesCloseEl) return null;
      const scopeClass = Array.from(ticketModalEl.classList).find((c) => /^s-/.test(c));
      if (!scopeClass) return null;

      const btn = document.createElement('button');
      btn.className = `ot-close ${scopeClass}`;
      ticketModalEl.appendChild(btn);

      const otStyle = getComputedStyle(btn);
      const oesStyle = getComputedStyle(oesCloseEl);
      const out = {
        otBg: otStyle.backgroundColor,
        oesBg: oesStyle.backgroundColor,
        otColor: otStyle.color,
      };
      btn.remove();
      return out;
    });

    expect(result, 'could not resolve .ot-close / .oes-close computed styles').not.toBeNull();
    // Cyan/info token is rgba(34, 211, 238, ...) — must no longer match.
    expect(result?.otBg, '.ot-close must not use the cyan/info background').not.toMatch(/34,\s*211,\s*238/);
    // Must match the same danger-token background every other close
    // button in the app uses (SymbolPanel's own .oes-close is the
    // canonical sibling rendered in this exact modal).
    expect(result?.otBg, '.ot-close background must match .oes-close (danger token)').toBe(result?.oesBg);
  });
});
