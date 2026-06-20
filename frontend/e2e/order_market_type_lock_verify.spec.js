/**
 * Regression: MARKET order type locking all form elements on /orders.
 *
 * Operator report: "again selecting market order locks it"
 * Commits under test: cfa4d3bf (CSS selector hotfix) + 98cfffe0 (footer redesign)
 *
 * Run against prod:
 *   PLAYWRIGHT_BASE_URL=https://ramboq.com \
 *   npx playwright test e2e/order_market_type_lock_verify.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe.configure({ mode: 'serial' });
test.setTimeout(90_000);

test.describe('/orders — MARKET type lock regression (cfa4d3bf + 98cfffe0)', () => {

  // ── helpers ──────────────────────────────────────────────────────────

  /** Open the Type dropdown and click a specific option. */
  async function pickType(page, label) {
    // Custom Select (rbq-select) — trigger has aria-label="Order type"
    // Scope to the rbq-select container holding the Type trigger so
    // we don't confuse it with Product / Exchange / other rbq-selects.
    const typeSelect = page.locator('[aria-label="Order type"]').locator('..').locator('..');
    const trigger = page.locator('[aria-label="Order type"]').first();
    await expect(trigger).toBeVisible({ timeout: 8_000 });
    await trigger.click();
    // Wait for the panel to open inside the Type select container
    const panel = page.locator('[aria-label="Order type"]').locator('xpath=following-sibling::*').first();
    // Simpler: just wait for ANY rbq-select-option-label with the right text to appear
    const labelSpan = page
      .locator('.rbq-select-option-label')
      .filter({ hasText: label })
      .first();
    await expect(labelSpan).toBeVisible({ timeout: 4_000 });
    await labelSpan.click();
    // After clicking an option, press Escape to ensure any lingering panel closes
    await page.keyboard.press('Escape');
    await page.waitForTimeout(350); // let Svelte re-render and panel fully close
  }

  /** Navigate to /orders with a hard reload so no CSS is cached. */
  async function openOrders(page) {
    await page.goto('/orders');
    await page.waitForLoadState('networkidle');
    // The Order Entry card must be visible
    await page.locator('.bucket-card-entry').first().waitFor({ state: 'visible', timeout: 15_000 });
    // Ticket tab should be active by default (first tab in the strip)
    // If there's a tab selector, click the Ticket tab explicitly.
    const ticketTab = page.locator('[role="tab"]').filter({ hasText: /ticket/i }).first();
    if (await ticketTab.isVisible({ timeout: 1_500 }).catch(() => false)) {
      await ticketTab.click();
      await page.waitForTimeout(200);
    }
  }

  // ── baseline: LIMIT must not lock ────────────────────────────────────

  test('1 — LIMIT baseline: footer elements are all hittable', async ({ page }) => {
    await loginAsAdmin(page);
    await openOrders(page);

    // Verify basket icon and submit are hittable at LIMIT (the neutral state)
    const footerInfo = await page.evaluate(() => {
      const basket = document.querySelector('.oes-common-basket-toggle-icon');
      const submit = document.querySelector('.oes-common-submit');
      const sideBtn = document.querySelector('.oes-footer-side-btn-single');

      function clsStr(el) {
        if (!el) return '';
        const c = el.className;
        return (typeof c === 'string' ? c : (c?.baseVal ?? '')).substring(0, 60);
      }
      function hitCheck(el) {
        if (!el) return { found: false };
        const r = el.getBoundingClientRect();
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        const hit = document.elementFromPoint(cx, cy);
        return {
          found: true,
          w: Math.round(r.width),
          h: Math.round(r.height),
          isSelf: el === hit || el.contains(hit),
          hitCls: clsStr(hit),
        };
      }

      return {
        basket: hitCheck(basket),
        submit: hitCheck(submit),
        sideBtn: hitCheck(sideBtn),
      };
    });

    console.log('\n[LIMIT baseline]', JSON.stringify(footerInfo, null, 2));

    if (footerInfo.basket.found) {
      expect(footerInfo.basket.isSelf, 'LIMIT: basket icon must be hittable').toBe(true);
      expect(footerInfo.basket.w, 'LIMIT: basket icon must not be wider than 50px').toBeLessThan(50);
    }
    if (footerInfo.submit.found) {
      expect(footerInfo.submit.isSelf, 'LIMIT: Submit must be hittable').toBe(true);
    }
  });

  // ── MARKET: basket label must not grow ───────────────────────────────

  test('2 — MARKET: basket toggle icon stays at natural width (~30px)', async ({ page }) => {
    await loginAsAdmin(page);
    await openOrders(page);
    await pickType(page, 'MARKET');

    const basketInfo = await page.evaluate(() => {
      const label = document.querySelector('.oes-common-basket-toggle-icon');
      if (!label) return { found: false };
      const r = label.getBoundingClientRect();
      const cs = getComputedStyle(label);
      const row = label.closest('.oes-common-row');
      const children = row
        ? [...row.children].map(el => ({
            tag: el.tagName,
            cls: el.className.substring(0, 70),
            w: Math.round(el.getBoundingClientRect().width),
            flexGrow: getComputedStyle(el).flexGrow,
          }))
        : [];
      return {
        found: true,
        labelW: Math.round(r.width),
        labelH: Math.round(r.height),
        flexGrow: cs.flexGrow,
        flexShrink: cs.flexShrink,
        rowW: row ? Math.round(row.getBoundingClientRect().width) : 0,
        children,
      };
    });

    console.log('\n[MARKET basket label]', JSON.stringify(basketInfo, null, 2));

    expect(basketInfo.found, 'Basket toggle must be in the DOM').toBe(true);
    // Natural width is 1.9rem = ~30px at 16px root. Allow up to 50px for
    // any root-font rounding. Anything above 80px means flex-grow is active.
    expect(basketInfo.labelW,
      `Basket label width (${basketInfo.labelW}px) must be < 80px — flex-grow bug active if wider`
    ).toBeLessThan(80);
    // flex-grow must be 0 (flex-shrink: 0 is asserted by the rule in SymbolPanel)
    expect(basketInfo.flexGrow,
      'Basket label flexGrow must be 0 after cfa4d3bf fix'
    ).toBe('0');
  });

  // ── MARKET: hit-test every footer element ────────────────────────────

  test('3 — MARKET: every footer element is clickable (hit-test)', async ({ page }) => {
    await loginAsAdmin(page);
    await openOrders(page);
    await pickType(page, 'MARKET');

    const hits = await page.evaluate(() => {
      function _clsStr(el) {
        if (!el) return '';
        const c = el.className;
        return (typeof c === 'string' ? c : (c?.baseVal ?? '')).substring(0, 80);
      }
      function hitCheck(selector, label) {
        const el = document.querySelector(selector);
        if (!el) return { label, found: false };
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return { label, found: true, zero: true };
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        const hit = document.elementFromPoint(cx, cy);
        return {
          label,
          found: true,
          zero: false,
          w: Math.round(r.width),
          h: Math.round(r.height),
          isSelf: el === hit || el.contains(hit),
          hitTag: hit?.tagName ?? 'none',
          hitCls: _clsStr(hit),
        };
      }

      return [
        hitCheck('.oes-common-basket-toggle-icon', 'basket-icon'),
        hitCheck('.oes-footer-side-btn-single',    'side-btn-single'),
        hitCheck('.oes-common-submit',              'submit-btn'),
      ];
    });

    console.log('\n[MARKET hit-test]', JSON.stringify(hits, null, 2));

    for (const h of hits) {
      if (!h.found || h.zero) {
        // Not in DOM yet (needs a symbol) — acceptable, warn
        console.warn(`[MARKET hit-test] ${h.label} not found or zero-size — skip`);
        continue;
      }
      expect(h.isSelf,
        `${h.label}: click intercepted by <${h.hitTag}>.${h.hitCls}`
      ).toBe(true);
    }
  });

  // ── MARKET: cold-prompt must NOT overflow the action cluster ─────────

  test('4 — MARKET with no side: cold-prompt must not cover action buttons', async ({ page }) => {
    await loginAsAdmin(page);
    await openOrders(page);
    await pickType(page, 'MARKET');
    // Do NOT pick a side — cold-prompt should be visible

    const coldOverflow = await page.evaluate(() => {
      const prompt = document.querySelector('.oes-cold-prompt');
      const submit = document.querySelector('.oes-common-submit');
      const sideBtn = document.querySelector('.oes-footer-side-btn-single');
      if (!prompt || !submit) return { promptFound: !!prompt, submitFound: !!submit };

      const pr = prompt.getBoundingClientRect();
      const sr = submit.getBoundingClientRect();
      const sdr = sideBtn?.getBoundingClientRect() ?? null;

      // Check if prompt's right edge overlaps submit's left edge
      const overlapSubmit = pr.right > sr.left;
      const overlapSide = sdr ? pr.right > sdr.left : false;

      return {
        promptFound: true,
        promptLeft: Math.round(pr.left),
        promptRight: Math.round(pr.right),
        promptW: Math.round(pr.width),
        submitLeft: Math.round(sr.left),
        sideLeft: sdr ? Math.round(sdr.left) : null,
        overlapSubmit,
        overlapSide,
      };
    });

    console.log('\n[MARKET cold-prompt overflow]', JSON.stringify(coldOverflow, null, 2));

    if (coldOverflow.promptFound && coldOverflow.submitFound !== false) {
      expect(coldOverflow.overlapSubmit,
        `Cold-prompt right edge (${coldOverflow.promptRight}) must not overlap Submit left (${coldOverflow.submitLeft})`
      ).toBe(false);
    }
  });

  // ── MARKET + side picked: side-btn cycles correctly ──────────────────

  test('5 — MARKET + side-btn single: cycles cold→BUY→SELL on click', async ({ page }) => {
    await loginAsAdmin(page);
    await openOrders(page);
    await pickType(page, 'MARKET');

    // Wait for any pending Svelte reactive cycles from MARKET type change to settle
    // Root cause investigation: picking MARKET triggers Svelte reactivity that
    // may temporarily detach/re-create the footer button, leaving a stale
    // DOM reference. Waiting for the side button to be stable before clicking.
    await page.locator('.oes-footer-side-btn-single').first()
      .waitFor({ state: 'visible', timeout: 5_000 });
    // Extra settle time for Svelte 5 reactive effects triggered by MARKET type change
    await page.waitForTimeout(800);

    // Cold state: "Pick side"
    const sideBtn = page.locator('.oes-footer-side-btn-single').first();
    await sideBtn.scrollIntoViewIfNeeded();
    const coldText = await sideBtn.textContent();
    expect(coldText?.trim(), 'Side btn cold state must read "Pick side"').toBe('Pick side');
    expect(await sideBtn.evaluate(el => el.classList.contains('on-none'))).toBe(true);

    // Click 1 → BUY
    // BUG INVESTIGATION NOTE: After picking MARKET, the side button's onclick
    // handler (Svelte 5 event delegation) does not fire even though:
    //   - the button is visible and hit-test passes (isSelf: true in test 3)
    //   - no pointer-events: none anywhere in the ancestry
    //   - the click event itself fires (confirmed via addEventListener capture)
    //   - btn.onclick is null (Svelte 5 uses addEventListener, not property)
    // Hypothesis: MARKET type change triggers Svelte reactive update that
    // detaches/re-creates the footer block; click lands on the old node.
    // This IS the operator's reported "selecting market order locks it" bug.
    await sideBtn.click();
    await page.waitForTimeout(600);
    const afterBuy = await page.locator('.oes-footer-side-btn-single').first().evaluate(el => ({
      text: el.textContent?.trim(),
      onBuy: el.classList.contains('on-buy'),
      cls: el.className,
    }));
    console.log('\n[side-btn] after click 1:', afterBuy);
    expect(afterBuy.onBuy, 'Side btn must have on-buy class after first click').toBe(true);

    // Click 2 → SELL
    await page.locator('.oes-footer-side-btn-single').first().click();
    await page.waitForTimeout(200);
    const afterSell = await page.locator('.oes-footer-side-btn-single').first().evaluate(el => ({
      text: el.textContent?.trim(),
      onSell: el.classList.contains('on-sell'),
    }));
    console.log('[side-btn] after click 2:', afterSell);
    expect(afterSell.onSell, 'Side btn must have on-sell class after second click').toBe(true);

    // Now verify hit-test still passes AFTER side picked (margin pill may appear)
    const hitAfterSide = await page.evaluate(() => {
      function _cs2(el) {
        if (!el) return '';
        const c = el.className;
        return (typeof c === 'string' ? c : (c?.baseVal ?? '')).substring(0, 80);
      }
      function hitCheck(selector) {
        const el = document.querySelector(selector);
        if (!el) return { found: false };
        const r = el.getBoundingClientRect();
        if (r.width === 0) return { found: true, zero: true };
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return { found: true, isSelf: el === hit || el.contains(hit),
                 hitCls: _cs2(hit), w: Math.round(r.width) };
      }
      return {
        basket: hitCheck('.oes-common-basket-toggle-icon'),
        submit: hitCheck('.oes-common-submit'),
        sideBtn: hitCheck('.oes-footer-side-btn-single'),
      };
    });

    console.log('[MARKET+side hit-test]', JSON.stringify(hitAfterSide, null, 2));

    for (const [k, h] of Object.entries(hitAfterSide)) {
      if (!h.found || h.zero) continue;
      expect(h.isSelf,
        `${k}: still intercepted after side pick — hitCls=${h.hitCls}`
      ).toBe(true);
    }
  });

  // ── MARKET: form dropdowns remain interactive ─────────────────────────

  test('6 — MARKET: Type/Product/Exchange dropdowns have pointer-events != none', async ({ page }) => {
    await loginAsAdmin(page);
    await openOrders(page);
    await pickType(page, 'MARKET');

    const pe = await page.evaluate(() => {
      const get = (sel) => {
        const el = document.querySelector(sel);
        return el ? getComputedStyle(el).pointerEvents : 'not-found';
      };
      return {
        type:     get('[aria-label="Order type"]'),
        product:  get('[aria-label="Product"]'),
        exchange: get('[aria-label="Exchange"]'),
        variety:  get('[aria-label="Variety"]'),
      };
    });

    console.log('\n[MARKET pointer-events]', JSON.stringify(pe, null, 2));

    for (const [k, v] of Object.entries(pe)) {
      if (v === 'not-found') continue; // element may not be mounted
      expect(v, `${k} dropdown must not have pointer-events:none after MARKET`).not.toBe('none');
    }
  });

  // ── screenshot for visual confirmation ───────────────────────────────

  test('7 — MARKET screenshot: visual confirmation of footer layout', async ({ page }) => {
    await loginAsAdmin(page);
    await openOrders(page);
    await pickType(page, 'MARKET');
    // Pick BUY so the full footer (basket + side=BUY + submit) is rendered
    const sideBtn = page.locator('.oes-footer-side-btn-single').first();
    if (await sideBtn.isVisible({ timeout: 2_000 }).catch(() => false)) {
      await sideBtn.click(); // → BUY
      await page.waitForTimeout(300);
    }

    const card = page.locator('.bucket-card-entry').first();
    await card.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: 'test-results/market_lock_verify.png',
      clip: await card.boundingBox() ?? undefined,
    });
    console.log('[market-lock-verify] screenshot → test-results/market_lock_verify.png');
  });
});
