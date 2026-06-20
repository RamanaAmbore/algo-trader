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
  await trigger.click();
  const opt = page.locator('[role="option"]').filter({ hasText: new RegExp(`^${typeValue}$`) }).first();
  if (await opt.isVisible({ timeout: 2000 })) {
    await opt.click();
    return;
  }
  await page.locator('.sel-option, .sel-item').filter({ hasText: new RegExp(`^${typeValue}$`) }).first().click();
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

  test('2: basket toggle label must not expand beyond its natural 1.9rem width', async ({ page }) => {
    // Regression guard for the root cause:
    // `.oes-common-row > *:first-child { flex: 1 1 auto }` must NOT apply
    // to .oes-common-basket-toggle-icon when the info slot is empty.
    // Fix: add `flex-shrink: 0` to the basket label OR scope the
    // first-child rule to named info-slot classes only.
    await authOnce(page);
    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');
    await page.locator('.bucket-card-entry').first().waitFor({ timeout: 12_000 });

    // Pick a side first (so cold-prompt hides), then pick MARKET
    // (so margin may not appear). This is the exact condition that triggers
    // the basket label to become first-child with flex-grow.

    // Pick MARKET type
    await pickOrderType(page, 'MARKET');
    await page.waitForTimeout(400);

    // Pick BUY side in the footer
    const footerBuy = page.locator('.oes-footer-side-btn-buy').first();
    if (await footerBuy.isVisible()) {
      await footerBuy.click();
      await page.waitForTimeout(300);
    }

    // Now inspect basket label width
    const basketInfo = await page.evaluate(() => {
      const label = document.querySelector('.oes-common-basket-toggle-icon');
      if (!label) return { found: false };
      const rect  = label.getBoundingClientRect();
      const cs    = getComputedStyle(label);
      // Walk the common-row to see all direct children widths
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

    console.log('\n===== Basket label + row children =====\n', JSON.stringify(basketInfo, null, 2));

    if (!basketInfo.found) {
      console.log('Basket label not found — may need a symbol first');
      return;
    }

    // The basket icon should be at most its natural width (1.9rem ≈ 30px at 16px root)
    // plus a small tolerance. If it's expanded to > 80px, the flex-grow bug is active.
    expect(basketInfo.labelWidth,
      'Basket toggle label must not expand beyond natural width (flex-grow bug)'
    ).toBeLessThan(80);
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
