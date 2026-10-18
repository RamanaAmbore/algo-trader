/**
 * logpanel_order_tab_layout_consistency.spec.js
 *
 * Operator report: the Order tab's own `.log-row` layout drifted from
 * every other LogPanel tab (Agent/Alerts/System/Simulator/Console), in
 * two visible ways, plus an explicit ask: "make sure orders log is
 * similar to other log format."
 *
 * Bug #1 — wrapped message text on mobile started indented after
 * time+tag instead of flush with the row's own left edge. Root cause:
 * a bespoke `.lp-order-scroll .log-row-msg { flex: 1 1 0; }` CSS
 * override (no `order`, no `flex-basis:100%`) fought the shared
 * `:global(.log-panel.log-rows .log-row-msg) { flex:1 1 100%; order:2 }`
 * rule every other tab relies on for its mobile wrap-to-full-width
 * convention. Fix (`frontend/src/lib/LogPanel.svelte`): the bespoke
 * override block is deleted outright — the Order tab's `.log-row`
 * markup already used the identical shared class names, so it now
 * falls through to the one shared layout rule every other tab uses.
 *
 * Bug #2 — a visibly larger gap between the tab bar and the first
 * order row than other tabs have. Root cause: the `{#if !_gatingMode}`
 * guard wrapped only the `{#each}` chips INSIDE `.om-bar`, so the
 * `.om-bar` div itself (padding + margin-bottom + dashed border) still
 * rendered — empty — whenever `gateByMode` was active (the default for
 * every real LogPanel mount, since both LogPanel's own `gateByMode`
 * prop and ActivityLogSurface's default to `true`). Fix: the `{#if}`
 * now wraps the whole `<div class="om-bar">`, so nothing renders when
 * there are no mode chips to show. A second contributor — `.lp-order-
 * scroll`'s own extra vertical padding stacking on top of the nested
 * `.log-panel.log-rows` div's own padding — was also tightened to
 * match the single-padding rhythm every other tab has.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *   SSOT     — asserts the Order tab's wrapped-row geometry now matches
 *              the Agent tab's (same shared `:global(.log-panel.log-
 *              rows .log-row)` CSS, not a parallel copy).
 *   Perf     — single intercepted fetch per tab, no polling wait beyond
 *              first render.
 *   Stale    — exercises the real DOM (`.lp-order-scroll .log-row`,
 *              `.om-bar`), not a speculative selector.
 *   Reusable — confirms the Order tab's `.log-row`/`.log-row-msg` reuse
 *              the SAME shared CSS class contract as every other tab,
 *              rather than drifting with a bespoke override.
 *   UX       — directly measures the two operator-visible symptoms
 *              (indentation, wasted vertical space) at the viewport
 *              sizes CLAUDE.md calls out (mobile <600px, desktop).
 *
 * Run:
 *   cd frontend && npx playwright test e2e/logpanel_order_tab_layout_consistency.spec.js
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const MOBILE_VIEWPORT = { width: 375, height: 800 };
const DESKTOP_VIEWPORT = { width: 1280, height: 900 };

const LONG_MESSAGE_FRAGMENT =
  'VERYLONGTRADINGSYMBOLNAMETHATWILLDEFINITELYWRAPACROSSMULTIPLELINESWHENRENDEREDATAMOBILEVIEWPORTWIDTHOFTHREESEVENTYFIVEPIXELS';

/** LogPanel's `_applyDateFilter` reads `order_timestamp` as an ALREADY-IST
 *  wall-clock string ("YYYY-MM-DD HH:MM:SS", matching Kite's own
 *  broker order_timestamp convention — see its own code comment) and
 *  drops any COMPLETE/CANCELLED/REJECTED/EXPIRED row whose date part
 *  isn't today's IST date. A plain `new Date().toISOString()` (UTC)
 *  silently fails this filter whenever UTC and IST disagree on the
 *  calendar date (any time after 18:30 UTC / before 00:00 UTC), which
 *  is exactly the kind of flake a naive fixture would hit. Build a
 *  real IST-local timestamp string instead. */
function istOrderTimestamp() {
  const d = new Date(Date.now() + 5.5 * 60 * 60 * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
         `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

/** Mocks the /admin/execution/mode poll the (algo) layout fires on
 *  mount — boot mode stays 'idle' (the localhost default) otherwise,
 *  which would zero out mode-gated rows before the layout bug could
 *  even be exercised. Pass `mode: null` to leave _gatingMode falsy
 *  (exercises the om-bar-visible path); any real mode string makes
 *  _gatingMode truthy (exercises the om-bar-hidden path — the default
 *  for every real mount). */
async function mockExecutionMode(page, mode = 'paper') {
  await page.route('**/api/admin/execution/mode', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ mode, allowed_modes: ['paper', 'live', 'shadow', 'sim', 'replay'], branch: 'dev' }),
    })
  );
}

async function mockOrdersFeed(page, { mode = 'paper' } = {}) {
  await mockExecutionMode(page, mode);
  const rows = [{
    order_id: 'ORD9001',
    tradingsymbol: LONG_MESSAGE_FRAGMENT,
    exchange: 'NFO',
    transaction_type: 'BUY',
    quantity: 50,
    status: 'COMPLETE',
    order_timestamp: istOrderTimestamp(),
    average_price: 210.25,
    mode,
  }];
  await page.route('**/api/orders/', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows }) })
  );
  await page.route('**/api/orders/algo/recent**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) })
  );
  await page.route('**/api/orders/events/recent**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) })
  );
}

async function mockAgentEvents(page, { mode = 'paper' } = {}) {
  await mockExecutionMode(page, mode);
  const NOW = new Date().toISOString();
  const rows = [{
    id: 1, agent_id: 10, event_type: 'action_success', trigger_condition: null,
    detail: JSON.stringify({
      account: 'ZG0790',
      message: LONG_MESSAGE_FRAGMENT + ' ' + LONG_MESSAGE_FRAGMENT,
    }),
    timestamp: NOW, sim_mode: false,
  }];
  await page.route('**/api/agents/events/recent*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(rows) })
  );
  await page.route('**/api/simulator/events/recent*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) })
  );
}

/** Returns { rowLeft, wrapped, secondLineX } for the first `.log-row`'s
 *  `.log-row-msg` span inside the given container selector. `wrapped`
 *  is false if the message rendered on a single visual line (nothing
 *  to assert an indentation delta on).
 *
 *  Uses a `Range` over the element's contents (not
 *  `element.getClientRects()`) — `.log-row-msg` is a flex item with a
 *  blockified computed `display`, and `getClientRects()` on a BLOCK
 *  box returns exactly one rect for the whole box regardless of how
 *  many lines the text wraps to internally (confirmed empirically: a
 *  131-char message that visually wraps to 4 lines still reported a
 *  single clientRect spanning all of them). A `Range`'s
 *  `getClientRects()` fragments per rendered LINE BOX of its inline
 *  content instead, which is what actually reveals wrap geometry —
 *  and works whether the content is plain text (Order tab's `{evt.
 *  message}`) or a mix of text + chip `<span>`s (Agent tab's `@html`
 *  detail-chip rendering). */
async function firstRowWrapGeometry(page, containerSel) {
  return page.evaluate((sel) => {
    const container = document.querySelector(sel);
    if (!container) return null;
    const row = container.querySelector('.log-row');
    const msg = row?.querySelector('.log-row-msg');
    if (!row || !msg) return null;
    const rowRect = row.getBoundingClientRect();
    const range = document.createRange();
    range.selectNodeContents(msg);
    const rects = Array.from(range.getClientRects());
    if (rects.length < 2) return { rowLeft: rowRect.left, wrapped: false, secondLineX: null };
    return { rowLeft: rowRect.left, wrapped: true, secondLineX: rects[1].x };
  }, containerSel);
}

test.describe('LogPanel Order tab — layout matches the shared row template (2026-10-18)', () => {
  test('mobile (375px): Order-tab wrapped message starts flush with row left edge, same as Agent tab', async ({ page }) => {
    await page.setViewportSize(MOBILE_VIEWPORT);
    await loginAsAdmin(page);
    await mockOrdersFeed(page);
    await page.goto('/activity?tab=order', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.lp-order-scroll .log-row').first()).toBeVisible({ timeout: 10_000 });

    const orderGeo = await firstRowWrapGeometry(page, '.lp-order-scroll');
    expect(orderGeo, 'Order tab row/msg not found').not.toBeNull();
    expect(orderGeo.wrapped, 'fixture message did not wrap at 375px — strengthen fixture').toBe(true);
    expect(
      Math.abs(orderGeo.secondLineX - orderGeo.rowLeft),
      `Order tab wrapped line indented ${orderGeo.secondLineX}px vs row left edge ${orderGeo.rowLeft}px`
    ).toBeLessThanOrEqual(3);

    // Direct before/after comparison — same assertion against the Agent tab,
    // which was never affected by the bespoke Order-tab CSS override.
    await mockAgentEvents(page);
    await page.goto('/activity?tab=agent', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.log-panel.log-rows .log-row').first()).toBeVisible({ timeout: 10_000 });

    const agentGeo = await firstRowWrapGeometry(page, '.log-panel.log-rows');
    expect(agentGeo, 'Agent tab row/msg not found').not.toBeNull();
    expect(agentGeo.wrapped, 'agent fixture message did not wrap at 375px — strengthen fixture').toBe(true);
    expect(
      Math.abs(agentGeo.secondLineX - agentGeo.rowLeft),
      `Agent tab wrapped line indented ${agentGeo.secondLineX}px vs row left edge ${agentGeo.rowLeft}px`
    ).toBeLessThanOrEqual(3);
  });

  test('desktop (1280px): gap above the first row is not meaningfully larger on Order tab than Agent tab', async ({ page }) => {
    await page.setViewportSize(DESKTOP_VIEWPORT);
    await loginAsAdmin(page);

    // gateByMode active (mode='paper') is the DEFAULT real-world state —
    // the om-bar mode-chip strip is hidden, which is exactly the case
    // that used to leave an empty, padded `.om-bar` div behind.
    await mockOrdersFeed(page, { mode: 'paper' });
    await page.goto('/activity?tab=order', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.lp-order-scroll .log-row').first()).toBeVisible({ timeout: 10_000 });

    // The tab bar (CardHeader + AlgoTabs strip) sits at a fixed position
    // at the top of the SAME `/activity` page layout regardless of which
    // tab is active, so the first row's own absolute `.y` is directly
    // comparable across tabs without needing a separate reference
    // element — simpler and avoids a second, possibly-racy boundingBox
    // lookup on a wrapper div.
    const orderBox = await page.locator('.lp-order-scroll .log-row').first().boundingBox();

    await mockAgentEvents(page, { mode: 'paper' });
    await page.goto('/activity?tab=agent', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.log-panel.log-rows .log-row').first()).toBeVisible({ timeout: 10_000 });

    const agentBox = await page.locator('.log-panel.log-rows .log-row').first().boundingBox();

    expect(
      Math.abs(orderBox.y - agentBox.y),
      `Order tab first-row y=${orderBox.y}px vs Agent tab first-row y=${agentBox.y}px — should be close`
    ).toBeLessThanOrEqual(6);
  });

  test('.om-bar does not render (zero footprint) when gateByMode is active and there are no mode chips', async ({ page }) => {
    await page.setViewportSize(DESKTOP_VIEWPORT);
    await loginAsAdmin(page);
    await mockOrdersFeed(page, { mode: 'paper' });
    await page.goto('/activity?tab=order', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.lp-order-scroll .log-row').first()).toBeVisible({ timeout: 10_000 });

    // _gatingMode is truthy ('paper') in this fixture, so the mode-chip
    // strip is suppressed — .om-bar itself must not be in the DOM at all
    // (not just empty/hidden) per the fix.
    await expect(page.locator('.om-bar')).toHaveCount(0);
  });
});
