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
    // /orders derives its default symbol from a recent-symbol store,
    // falling back to empty on a fresh browser profile with no history
    // (routes/(algo)/orders/+page.svelte) — without a symbol, every
    // knob (Type/Product/Variety/Validity/SideToggle) stays disabled
    // via OrderTicket's `_noSymbol` gate, which would make every
    // `pickType()` call below hang forever. Pick a live symbol first
    // if the ticket didn't already come pre-filled.
    const typeSelect = page.locator('[aria-label="Order type"]').first();
    if (await typeSelect.isDisabled().catch(() => false)) {
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
  }

  // ── baseline: LIMIT must not lock ────────────────────────────────────

  test('1 — LIMIT baseline: footer elements are all hittable', async ({ page }) => {
    await loginAsAdmin(page);
    await openOrders(page);

    // Verify the side-preview label and Submit are hittable (not covered
    // by an overlapping sibling) at LIMIT (the neutral state). The
    // basket-toggle icon this test used to also check was removed
    // (2026-09 Ticket-tab footer redesign) — Submit is now the sole
    // clickable control in the row.
    const footerInfo = await page.evaluate(() => {
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
        submit: hitCheck(submit),
        sideBtn: hitCheck(sideBtn),
      };
    });

    console.log('\n[LIMIT baseline]', JSON.stringify(footerInfo, null, 2));

    // The basket-toggle icon no longer exists in the DOM at all.
    const basketCount = await page.locator('.oes-common-basket-toggle-icon').count();
    expect(basketCount, 'basket-toggle icon must be fully removed').toBe(0);

    if (footerInfo.submit.found) {
      expect(footerInfo.submit.isSelf, 'LIMIT: Submit must be hittable').toBe(true);
    }
    if (footerInfo.sideBtn.found) {
      expect(footerInfo.sideBtn.isSelf, 'LIMIT: side-preview label must not be covered').toBe(true);
    }
  });

  // ── MARKET: basket toggle is gone; side label must not grow ─────────

  test('2 — MARKET: basket toggle removed, side-preview label stays at natural width', async ({ page }) => {
    await loginAsAdmin(page);
    await openOrders(page);
    await pickType(page, 'MARKET');

    // The basket-toggle icon (both the interactive checkbox and the
    // Chain-only static badge) was removed entirely in the Ticket-tab
    // footer redesign — confirm it never renders on Ticket, not even
    // for MARKET.
    const basketCount = await page.locator('.oes-common-basket-toggle-icon').count();
    expect(basketCount, 'basket-toggle icon must be fully removed').toBe(0);

    const labelInfo = await page.evaluate(() => {
      const label = document.querySelector('.oes-footer-side-btn-single');
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

    console.log('\n[MARKET side-preview label]', JSON.stringify(labelInfo, null, 2));

    expect(labelInfo.found, 'Side-preview label must be in the DOM').toBe(true);
    // Natural width is ~5.5rem = ~88px at 16px root. Anything well above
    // that means a flex-grow regression is active (the original bug's
    // class of defect — a lone footer child swallowing the row's width).
    expect(labelInfo.labelW,
      `Side-preview label width (${labelInfo.labelW}px) must be < 160px — flex-grow bug active if wider`
    ).toBeLessThan(160);
    expect(labelInfo.flexGrow,
      'Side-preview label flexGrow must be 0'
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
        hitCheck('.oes-footer-side-btn-single', 'side-btn-single'),
        hitCheck('.oes-common-submit',          'submit-btn'),
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

  // ── MARKET + side picked via the ticket body's own SideToggle ────────

  test('5 — MARKET + SideToggle: BUY/SELL pills stay hittable and drive the footer preview', async ({ page }) => {
    // 2026-09 footer redesign: the footer side label is now a static,
    // non-clickable preview (exactly ONE clickable control — Submit —
    // remains in the row). Side selection moved entirely to OrderTicket's
    // own SideToggle pills in the ticket body. This test's regression
    // intent is unchanged: picking MARKET must never leave any control
    // (the SideToggle pills, or Submit) un-hittable/"locked".
    await loginAsAdmin(page);
    await openOrders(page);
    await pickType(page, 'MARKET');

    // Wait for any pending Svelte reactive cycles from the MARKET type
    // change to settle before interacting — the original investigation's
    // hypothesis was that a reactive re-render could leave a click
    // targeting a stale/detached DOM node.
    await page.locator('.oes-footer-side-btn-single').first()
      .waitFor({ state: 'visible', timeout: 5_000 });
    await page.waitForTimeout(800);

    // Cold state: footer preview reads "Pick side" until a symbol +
    // side are both resolved.
    const sideBtn = page.locator('.oes-footer-side-btn-single').first();
    await sideBtn.scrollIntoViewIfNeeded();

    const buyPill  = page.locator('button.ot-side-buy').first();
    const sellPill = page.locator('button.ot-side-sell').first();
    await expect(buyPill).toBeVisible({ timeout: 5_000 });
    if (!(await buyPill.isEnabled().catch(() => false))) {
      test.skip(true, 'SideToggle disabled — no resolvable symbol in this environment');
      return;
    }

    // Click 1 → BUY. Hit-test the pill itself first — this is the
    // control that must never be "locked" by MARKET type selection.
    const buyBox = await buyPill.boundingBox();
    if (buyBox) {
      const hit = await page.evaluate(({ x, y }) => {
        const el = document.elementFromPoint(x, y);
        return el ? { tag: el.tagName, cls: (el.className || '').toString().substring(0, 80) } : null;
      }, { x: buyBox.x + buyBox.width / 2, y: buyBox.y + buyBox.height / 2 });
      console.log('\n[SideToggle BUY] hit at center:', hit);
    }
    await buyPill.click();
    await page.waitForTimeout(400);

    // Footer preview label mirrors the pick (onSideChange wiring).
    const afterBuy = await sideBtn.evaluate(el => ({
      text: el.textContent?.trim(),
      onBuy: el.classList.contains('on-buy'),
      cls: el.className,
    }));
    console.log('[footer preview] after BUY pill click:', afterBuy);
    expect(afterBuy.onBuy, 'Footer preview must show on-buy after BUY pill click').toBe(true);

    // Click 2 → SELL
    await expect(sellPill).toBeEnabled({ timeout: 3_000 });
    await sellPill.click();
    await page.waitForTimeout(200);
    const afterSell = await sideBtn.evaluate(el => ({
      text: el.textContent?.trim(),
      onSell: el.classList.contains('on-sell'),
    }));
    console.log('[footer preview] after SELL pill click:', afterSell);
    expect(afterSell.onSell, 'Footer preview must show on-sell after SELL pill click').toBe(true);

    // Now verify hit-test still passes AFTER side picked (margin pill may appear).
    // Submit must remain hittable — this is the ONE clickable control left
    // in the row and the actual "locked" symptom the operator reported.
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
        submit: hitCheck('.oes-common-submit'),
        sideBtn: hitCheck('.oes-footer-side-btn-single'),
        sellPill: hitCheck('.ot-side-sell'),
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
    // Pick BUY via the ticket body's own SideToggle pill so the full
    // footer (side preview + submit) is rendered.
    const buyPill = page.locator('button.ot-side-buy').first();
    if (await buyPill.isVisible({ timeout: 2_000 }).catch(() => false)
        && await buyPill.isEnabled().catch(() => false)) {
      await buyPill.click(); // → BUY
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
