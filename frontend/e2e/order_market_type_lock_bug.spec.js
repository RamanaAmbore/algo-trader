// Bug investigation: MARKET order type locks all form elements
// Root cause identified via static analysis of commit 30379583:
//
// `.oes-common-row > *:first-child { flex: 1 1 auto }` applies to the
// basket-toggle <label> when the info slot (sticky/notice/margin/cold-prompt)
// renders nothing — which happens when MARKET is selected AND a side is set
// (cold-prompt condition `!_modalSide` becomes false) AND no margin/notice is
// showing yet. The label expands to fill the row's full width, covering the
// BUY/SELL footer buttons and Submit button — all clicks go to the label's
// hidden checkbox, making the footer appear "locked".
//
// Run against prod:
//   PLAYWRIGHT_BASE_URL=https://ramboq.com \
//   PLAYWRIGHT_AUTH_TOKEN=<your-jwt> \
//   npx playwright test e2e/order_market_type_lock_bug.spec.js --project=chromium-desktop --workers=1
//
// Or locally with the dev server (no PLAYWRIGHT_BASE_URL):
//   PLAYWRIGHT_AUTH_TOKEN=<local-jwt> npx playwright test e2e/order_market_type_lock_bug.spec.js

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
        if (resp.status() !== 429) throw new Error(`authOnce: /api/auth/login ${resp.status()} — set PLAYWRIGHT_AUTH_TOKEN`);
      }
    }
    if (!tok) throw new Error('authOnce: login rate-limited — set PLAYWRIGHT_AUTH_TOKEN');
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

async function pickOrderType(page, typeValue) {
  const trigger = page.locator('[aria-label="Order type"]').first();
  await expect(trigger).toBeVisible({ timeout: 10_000 });
  // /orders derives its default symbol from a recent-symbol store,
  // falling back to empty on a fresh browser profile with no history
  // (routes/(algo)/orders/+page.svelte) — without a symbol, every knob
  // (Type/Product/Variety/Validity/SideToggle) stays disabled via
  // OrderTicket's `_noSymbol` gate, which would hang the click below
  // forever. Pick a live symbol first if the ticket isn't pre-filled.
  if (await trigger.isDisabled().catch(() => false)) {
    const symInput = page.locator('.ssi-input').first();
    if (await symInput.count() > 0) {
      await symInput.fill('RELIANCE');
      const symRow = page.locator('.ssi-row').first();
      if (await symRow.waitFor({ state: 'visible', timeout: 6_000 }).then(() => true).catch(() => false)) {
        await symRow.click();
      } else {
        await page.keyboard.press('Enter').catch(() => {});
      }
      await page.waitForTimeout(500);
    }
  }
  await trigger.click();
  // Primary: the rbq-select option-label class (matches this suite's
  // canonical sibling, order_market_type_lock_verify.spec.js:pickType).
  const labelSpan = page.locator('.rbq-select-option-label').filter({ hasText: new RegExp(`^${typeValue}$`) }).first();
  if (await labelSpan.isVisible({ timeout: 3_000 }).catch(() => false)) {
    await labelSpan.click();
    return;
  }
  // Fallback: generic ARIA role, in case the option markup varies by
  // Select variant.
  const opt = page.locator('[role="option"]').filter({ hasText: new RegExp(`^${typeValue}$`) }).first();
  await opt.click();
}

test.describe.configure({ mode: 'serial' });
test.setTimeout(90_000);

test.describe('OrderTicket — MARKET type locking bug (commit 30379583)', () => {
  test('1: /orders page mounts with Order Entry card', async ({ page }) => {
    await authOnce(page);
    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    const entryCard = page.locator('.bucket-card-entry').first();
    await expect(entryCard).toBeVisible({ timeout: 12_000 });
  });

  test('2: footer side-preview label must not expand beyond its natural width', async ({ page }) => {
    // Regression guard for the root cause:
    // `.oes-common-row > *:first-child { flex: 1 1 auto }` must NOT apply
    // to a footer action-row child when the info slot is empty.
    //
    // The original basket-toggle <label> this test targeted was removed
    // (2026-09, Ticket-tab footer redesign — see SymbolPanel.svelte;
    // exactly ONE clickable control, Submit, remains in the row). The
    // regression class of bug (a footer child expanding via flex-grow
    // and swallowing clicks meant for its siblings) is still worth
    // guarding — now on the side-preview label, the row's other
    // first-child candidate.
    await authOnce(page);
    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.locator('.bucket-card-entry').first().waitFor({ timeout: 12_000 });

    // Pick MARKET type — the exact condition that historically triggered
    // the flex-grow bug (no margin/notice content in the info slot).
    await pickOrderType(page, 'MARKET');
    await page.waitForTimeout(400);

    // Pick BUY via OrderTicket's own SideToggle pill (the footer label
    // is now a static, non-clickable preview).
    const buyPill = page.locator('button.ot-side-buy').first();
    if (await buyPill.isVisible().catch(() => false) && await buyPill.isEnabled().catch(() => false)) {
      await buyPill.click();
      await page.waitForTimeout(300);
    }

    // Now inspect the side-preview label's width.
    const labelInfo = await page.evaluate(() => {
      const label = document.querySelector('.oes-footer-side-btn-single');
      if (!label) return { found: false };
      const rect  = label.getBoundingClientRect();
      const cs    = getComputedStyle(label);
      const row = label.closest('.oes-common-row');
      const children = row ? [...row.children].map(el => ({
        cls: el.className.substring(0, 60),
        w: el.getBoundingClientRect().width,
        pe: getComputedStyle(el).pointerEvents,
        flex: getComputedStyle(el).flex,
      })) : [];
      return {
        found: true,
        labelWidth: rect.width,
        labelHeight: rect.height,
        labelFlex: cs.flex,
        labelFlexGrow: cs.flexGrow,
        rowWidth: row?.getBoundingClientRect().width ?? 0,
        children,
      };
    });

    console.log('\n===== Side-preview label + row children =====\n', JSON.stringify(labelInfo, null, 2));

    if (!labelInfo.found) {
      console.log('Side-preview label not found — may need a symbol first');
      return;
    }

    // The label should be at most its natural width (~5.5rem ≈ 88px at
    // 16px root) plus tolerance. A much wider value means the flex-grow
    // bug is active again.
    expect(labelInfo.labelWidth,
      'Side-preview label must not expand beyond natural width (flex-grow bug)'
    ).toBeLessThan(160);
    expect(labelInfo.labelFlexGrow,
      'Side-preview label flexGrow must be 0'
    ).toBe('0');
  });

  test('3: footer BUY/SELL buttons must be hittable after MARKET selection', async ({ page }) => {
    await authOnce(page);
    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.locator('.bucket-card-entry').first().waitFor({ timeout: 12_000 });

    // Set MARKET
    await pickOrderType(page, 'MARKET');
    await page.waitForTimeout(400);

    // The footer BUY button must be clickable (not intercepted by basket label)
    const footerBuy = page.locator('.oes-footer-side-btn-buy').first();
    await expect(footerBuy).toBeVisible({ timeout: 5_000 });

    // Get element at center of the BUY button — if basket label is expanded,
    // elementFromPoint will return the label or one of its children, not the button.
    const hitResult = await page.evaluate(() => {
      const btn = document.querySelector('.oes-footer-side-btn-buy');
      if (!btn) return { found: false };
      const rect = btn.getBoundingClientRect();
      const cx = rect.left + rect.width / 2;
      const cy = rect.top  + rect.height / 2;
      const el = document.elementFromPoint(cx, cy);
      return {
        found: true,
        btnTag: btn.tagName,
        btnCls: btn.className,
        hitTag: el?.tagName ?? 'none',
        hitCls: (el?.className ?? '').substring(0, 80),
        hitId:  el?.id ?? '',
        isSelf: el === btn || btn.contains(el),
      };
    });

    console.log('\n===== Hit test — BUY footer button =====\n', JSON.stringify(hitResult, null, 2));

    if (!hitResult.found) {
      console.log('Footer BUY button not in DOM — may need a symbol');
      return;
    }

    expect(hitResult.isSelf,
      `Element at BUY button center should be the button itself, not: ${hitResult.hitTag}.${hitResult.hitCls}`
    ).toBe(true);
  });

  test('4: form elements inside order ticket are NOT locked after picking MARKET', async ({ page }) => {
    await authOnce(page);
    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.locator('.bucket-card-entry').first().waitFor({ timeout: 12_000 });

    await pickOrderType(page, 'MARKET');
    await page.waitForTimeout(500);

    const results = await page.evaluate(() => {
      const get = (s) => document.querySelector(s);
      const cs  = (el) => el ? getComputedStyle(el) : null;
      return {
        buyDisabled:        get('.ot-side-buy')?.disabled ?? 'n/a',
        sellDisabled:       get('.ot-side-sell')?.disabled ?? 'n/a',
        lotsInputDisabled:  get('.ot-lots-input')?.disabled ?? 'n/a',
        typeSelectPE:       cs(get('[aria-label="Order type"]'))?.pointerEvents ?? 'n/a',
        productPE:          cs(get('[aria-label="Product"]'))?.pointerEvents ?? 'n/a',
        exchangePE:         cs(get('[aria-label="Exchange"]'))?.pointerEvents ?? 'n/a',
        varietyPE:          cs(get('[aria-label="Variety"]'))?.pointerEvents ?? 'n/a',
        validityPE:         cs(get('[aria-label="Validity"]'))?.pointerEvents ?? 'n/a',
        // price input should be HIDDEN (not disabled) for MARKET
        priceInputInDOM:    (() => {
          const inputs = [...document.querySelectorAll('.ot-lots-price-row .ot-input.ot-num')]
            .filter(el => !el.classList.contains('ot-lots-input') && el.id !== 'ot-lots');
          return inputs.length > 0;
        })(),
      };
    });

    console.log('\n===== Form element states after MARKET =====\n', JSON.stringify(results, null, 2));

    expect(results.typeSelectPE, 'Type dropdown must be interactive').not.toBe('none');
    expect(results.productPE,   'Product dropdown must be interactive').not.toBe('none');
    expect(results.exchangePE,  'Exchange dropdown must be interactive').not.toBe('none');
    expect(results.varietyPE,   'Variety dropdown must be interactive').not.toBe('none');
    expect(results.validityPE,  'Validity dropdown must be interactive').not.toBe('none');
    // price input should be hidden (showLimit=false for MARKET)
    expect(results.priceInputInDOM, 'Price input should be absent from DOM for MARKET').toBe(false);
    if (results.buyDisabled !== 'n/a') {
      expect(results.buyDisabled, 'BUY button in form must not be disabled').toBe(false);
    }
    if (results.sellDisabled !== 'n/a') {
      expect(results.sellDisabled, 'SELL button in form must not be disabled').toBe(false);
    }
  });

  test('5: screenshot — MARKET state for visual confirmation', async ({ page }) => {
    await authOnce(page);
    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.locator('.bucket-card-entry').first().waitFor({ timeout: 12_000 });

    await pickOrderType(page, 'MARKET');
    await page.waitForTimeout(600);

    await page.locator('.bucket-card-entry').first().scrollIntoViewIfNeeded();
    await page.screenshot({ path: 'test-results/market_order_type_state.png', fullPage: false });
    console.log('[market-lock] screenshot → test-results/market_order_type_state.png');
  });
});
