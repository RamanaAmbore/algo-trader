// dropdown_panel_overflow_fix.spec.js
//
// Regression coverage for the CardHeader `.ch-left` / `.ch-middle`
// vertical-clipping defect (2026-10): both zones set `overflow-x: auto`
// for mobile horizontal scroll, and per the CSS Overflow spec, once one
// axis is non-`visible` the OTHER axis computes to `auto` too — even
// when left unset (`.ch-left`) or explicitly set to `visible`
// (`.ch-middle`, which had an EXPLICIT `overflow-y: visible` that the
// spec makes ineffective once `overflow-x: auto` is also present on the
// same element). Any `position: absolute` dropdown panel mounted inside
// one of those zones (MultiSelect / Select, via AccountMultiSelect /
// ActivityAccountSelect) was clipped to zero VISIBLE pixels the moment
// it opened, while `getBoundingClientRect()` still reported a full-size
// box and Playwright's `toBeVisible()` false-passed — the box exists,
// it just isn't painted where the browser says it is. That is exactly
// why every assertion below reads real screen pixels via
// `document.elementFromPoint()` instead of `toBeVisible()`.
//
// Fix: MultiSelect.svelte / Select.svelte's dropdown panel now renders
// `position: fixed` with JS-computed left/top/width (`floatingPanel.js`,
// same strategy as InfoHint.svelte's popup mode) instead of
// `position: absolute`, so it can never be clipped by an ancestor's
// overflow or stacking context — the panel stays in the DOM in the same
// place; only its CSS positioning scheme changed.
//
// Covers:
//   1. MarketPulse Positions card account filter (CardHeader `.ch-left`)
//      at a narrow mobile viewport.
//   2. The Activity surface's inline account filter — the SAME
//      ActivityAccountSelect component, mounted via LogPanel's `.ch-middle`
//      slot rather than `.ch-left` (the "dormant" sibling bug per the
//      investigation). Live mount: `/console` (`hideInlineAccountFilter=
//      false`) — ActivityLogModal / `/activity` / the Dashboard Activity
//      card currently leave that prop at its default (hidden), so they
//      don't exercise this exact code path today; that's a separate,
//      pre-existing wiring gap, not something this fix touches.
//   3. Regression spot-check: a Select + MultiSelect pair that live
//      OUTSIDE any CardHeader zone (derivatives Underlying picker) still
//      position directly under their trigger — proves the switch from
//      absolute → fixed didn't break the common, non-clipped case.

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

/**
 * Reads real painted-pixel visibility for an already-open floating
 * panel, using `document.elementFromPoint()` at its center and near its
 * bottom edge — the same method used to originally confirm this bug
 * (NOT `toBeVisible()`, which false-passes: the clipped box still has a
 * non-zero bounding rect, it just isn't painted there).
 *
 * @param {import('@playwright/test').Page} page
 * @param {string} panelSelector
 * @param {string} triggerSelector
 */
async function readPanelPixelState(page, panelSelector, triggerSelector) {
  return page.evaluate(({ panelSelector, triggerSelector }) => {
    const panel = document.querySelector(panelSelector);
    const trigger = document.querySelector(triggerSelector);
    if (!panel || !trigger) {
      return { error: `missing panel=${!!panel} trigger=${!!trigger}` };
    }
    const pr = panel.getBoundingClientRect();
    const tr = trigger.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const cx = pr.left + pr.width / 2;
    const cy = pr.top + pr.height / 2;
    // 2px inset from the bottom edge — the exact edge pixel can land on
    // a border/antialiasing seam and return the panel's own border
    // element's parent by sub-pixel luck even when clipped; 2px inside
    // is still well within the panel's content box.
    const nearBottomY = Math.min(pr.top + pr.height - 2, vh - 1);
    const elAtCenter = document.elementFromPoint(cx, Math.min(cy, vh - 1));
    const elAtNearBottom = document.elementFromPoint(cx, nearBottomY);
    return {
      panelRect: { top: pr.top, left: pr.left, width: pr.width, height: pr.height, bottom: pr.bottom, right: pr.right },
      triggerRect: { top: tr.top, bottom: tr.bottom, left: tr.left, width: tr.width },
      viewport: { w: vw, h: vh },
      centerInsidePanel: !!elAtCenter && panel.contains(elAtCenter),
      nearBottomInsidePanel: !!elAtNearBottom && panel.contains(elAtNearBottom),
    };
  }, { panelSelector, triggerSelector });
}

test.describe('Dropdown panel overflow fix — fixed positioning escapes CardHeader clipping', () => {
  test.describe.configure({ mode: 'serial' });

  test('MarketPulse Positions account filter renders real visible pixels at narrow viewport', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    const triggerSel = 'button.rbq-multi-trigger[aria-label="Filter Positions by broker account"]';
    const trigger = page.locator(triggerSel);

    // availableAccounts seeds from the positions/holdings poll (plus the
    // broker registry) — the first poll cycle takes several seconds
    // after navigation, so give it real headroom before concluding the
    // picker genuinely isn't there.
    await trigger.waitFor({ state: 'attached', timeout: 25_000 }).catch(() => {});
    const count = await trigger.count();
    if (count === 0) {
      // accountPicker's availableAccounts union needs either positions/
      // holdings rows or a configured broker registry with >0 accounts
      // (see MarketPulse.svelte's accountPicker seeding block). A
      // single-account env is valid behaviour — the picker legitimately
      // doesn't render — note it and skip rather than false-failing.
      console.log('[SKIP] Positions account filter trigger not found — single-account env?');
      test.skip();
      return;
    }

    await expect(trigger, 'Positions account filter trigger visible').toBeVisible({ timeout: 20_000 });
    await trigger.scrollIntoViewIfNeeded();
    await trigger.click();

    const panel = page.locator('.rbq-multi-panel');
    await expect(panel, 'dropdown panel mounts in DOM').toHaveCount(1, { timeout: 5_000 });

    const state = await readPanelPixelState(page, '.rbq-multi-panel', triggerSel);
    expect(state.error, `readPanelPixelState error: ${state.error}`).toBeUndefined();

    console.log('[pulse positions filter]', JSON.stringify(state));

    // The core regression check: real painted pixels at the panel's own
    // center AND near its bottom edge resolve to an element INSIDE the
    // panel — not the grid/page behind it. Before the fix this failed
    // (resolved to whatever sat behind the clipped box) even though
    // `toBeVisible()` on the same locator reported true.
    expect(state.centerInsidePanel, 'panel center pixel belongs to the panel').toBe(true);
    expect(state.nearBottomInsidePanel, 'panel near-bottom pixel belongs to the panel').toBe(true);

    // Panel must have real on-screen size and sit within the viewport —
    // not collapsed to 0×0 and not pushed off-screen.
    expect(state.panelRect.height).toBeGreaterThan(20);
    expect(state.panelRect.width).toBeGreaterThan(20);
    expect(state.panelRect.top).toBeGreaterThanOrEqual(0);
    expect(state.panelRect.bottom).toBeLessThanOrEqual(state.viewport.h + 1);

    // Placement sanity: panel opens directly below the trigger (±2px of
    // the documented 4px gap) when there's room — true at the top of a
    // freshly-loaded page.
    expect(Math.abs(state.panelRect.top - (state.triggerRect.bottom + 4))).toBeLessThanOrEqual(3);

    await page.screenshot({ path: 'test-results/dropdown-overflow-fix-pulse-positions.png' });
  });

  test('Activity surface inline account filter (CardHeader .ch-middle) renders real visible pixels', async ({ page }) => {
    // Spot-check the "dormant" sibling instance of the same bug:
    // ActivityAccountSelect rendered via LogPanel's OWN inline slot in
    // CardHeader's `middle()` snippet (`.ch-middle`, LogPanel.svelte:1656)
    // rather than AccountMultiSelect's usual `left()` slot host
    // (`.ch-left`, covered by the MarketPulse test above). `.ch-middle`
    // had an EXPLICIT `overflow-y: visible` that the CSS Overflow spec
    // makes ineffective once `overflow-x: auto` is also set on the same
    // element — so this is a distinct confirmation, not a duplicate of
    // the first test.
    //
    // Mount point: `/console` is the one live page that opts into the
    // inline filter (`hideInlineAccountFilter={false}` — see
    // ActivityLogSurface.svelte's own usage doc). ActivityLogModal,
    // `/activity`, and the Dashboard Activity card currently all leave
    // this prop at its default (`true`, hidden) per `grep -rn
    // hideInlineAccountFilter`, so they don't exercise this code path
    // today — that's a separate, pre-existing wiring gap, not something
    // this fix changes.
    await page.setViewportSize({ width: 390, height: 844 });
    await loginAsAdmin(page);
    await page.goto(`${BASE}/console`, { waitUntil: 'domcontentloaded' });

    // Default tab is 'terminal' (no account filter there) — switch to
    // Orders, which is one of the four tabs `_showAccountFilter` allows.
    const ordersTab = page.locator('[role="tab"]:has-text("Orders")').first();
    await expect(ordersTab, 'Orders tab visible').toBeVisible({ timeout: 15_000 });
    await ordersTab.click();

    const triggerSel = '.rbq-multi-trigger';
    const trigger = page.locator(triggerSel).first();

    // _availableAccounts seeds from LogPanel's own order-rows poll —
    // give it real headroom, same as the MarketPulse picker above.
    await trigger.waitFor({ state: 'attached', timeout: 20_000 }).catch(() => {});
    const count = await trigger.count();
    if (count === 0) {
      console.log('[SKIP] inline ActivityAccountSelect trigger not found — single-account env or no order history');
      test.skip();
      return;
    }

    await expect(trigger, 'inline activity account filter trigger visible').toBeVisible({ timeout: 10_000 });
    await trigger.scrollIntoViewIfNeeded();
    await trigger.click();

    const panel = page.locator('.rbq-multi-panel');
    await expect(panel, 'dropdown panel mounts in DOM').toHaveCount(1, { timeout: 5_000 });

    const state = await readPanelPixelState(page, '.rbq-multi-panel', triggerSel);
    expect(state.error, `readPanelPixelState error: ${state.error}`).toBeUndefined();

    console.log('[console inline activity account filter]', JSON.stringify(state));

    expect(state.centerInsidePanel, 'panel center pixel belongs to the panel').toBe(true);
    expect(state.nearBottomInsidePanel, 'panel near-bottom pixel belongs to the panel').toBe(true);
    expect(state.panelRect.height).toBeGreaterThan(20);
    expect(state.panelRect.bottom).toBeLessThanOrEqual(state.viewport.h + 1);

    await page.screenshot({ path: 'test-results/dropdown-overflow-fix-console-activity.png' });
  });

  test('regression: derivatives Underlying Select + account MultiSelect still position under trigger (no clipping ancestor)', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await loginAsAdmin(page);
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });

    // Select.svelte — Underlying picker, lives in the page body, not
    // inside any CardHeader zone. `id` is applied directly to the
    // trigger `<button class="rbq-select-trigger">` itself (not an
    // ancestor wrapper), so `#opt-und` IS the trigger.
    const selTrigger = page.locator('#opt-und');
    await expect(selTrigger, 'Underlying Select trigger visible').toBeVisible({ timeout: 20_000 });
    await selTrigger.click();

    const selPanel = page.locator('.rbq-select-panel');
    await expect(selPanel, 'Select dropdown panel mounts').toHaveCount(1, { timeout: 5_000 });

    const selState = await readPanelPixelState(page, '.rbq-select-panel', '#opt-und');
    expect(selState.error, `readPanelPixelState error: ${selState.error}`).toBeUndefined();
    console.log('[derivatives underlying select]', JSON.stringify(selState));

    expect(selState.centerInsidePanel, 'Select panel center pixel belongs to the panel').toBe(true);
    // Unchanged-behaviour check: still opens directly below the trigger.
    expect(Math.abs(selState.panelRect.top - (selState.triggerRect.bottom + 4))).toBeLessThanOrEqual(3);
    // Width still matches the trigger (JS now sets this explicitly where
    // CSS `left:0;right:0` used to do it implicitly under `absolute`).
    expect(Math.abs(selState.panelRect.width - selState.triggerRect.width)).toBeLessThanOrEqual(2);

    await page.keyboard.press('Escape');

    // MultiSelect.svelte — account filter, same page, also outside any
    // CardHeader zone. Same id-on-trigger-itself shape as Select.svelte.
    const msTrigger = page.locator('#opt-acct');
    await expect(msTrigger, 'account MultiSelect trigger visible').toBeVisible({ timeout: 10_000 });
    await msTrigger.click();

    const msPanel = page.locator('.rbq-multi-panel');
    await expect(msPanel.first(), 'MultiSelect dropdown panel mounts').toHaveCount(1, { timeout: 5_000 });

    const msState = await readPanelPixelState(page, '.rbq-multi-panel', '#opt-acct');
    expect(msState.error, `readPanelPixelState error: ${msState.error}`).toBeUndefined();
    console.log('[derivatives account multiselect]', JSON.stringify(msState));

    expect(msState.centerInsidePanel, 'MultiSelect panel center pixel belongs to the panel').toBe(true);
    expect(Math.abs(msState.panelRect.top - (msState.triggerRect.bottom + 4))).toBeLessThanOrEqual(3);
  });
});
