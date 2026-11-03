/**
 * bracket_scope_reset_pending_label_and_held_toast.spec.js
 *
 * .claude/PLAN.md — "Bracket UX — scope-reset-not-clear, paired display,
 * 'Pending' label, basket hold wiring" (2026-11-02). Covers items 2
 * (paired main+bracket display), 4 (toast wording distinction), and 6
 * (HeldOrdersCard surfaces a template-exit-held row). Item 3's ("Pending"
 * label) surfaces are covered incidentally by every test below that
 * renders a held/pending row.
 *
 * Item 1 (Chain-tab scope-reset-not-clear + flash) was REVERTED on
 * 2026-11-03 — a live incident traced Chain tab's bid/ask freezing and
 * the order-ticket modal becoming unresponsive to this feature (plus
 * the separate live-wing-badge feature from the day before); both were
 * rolled back to their last known-working state. Its test coverage was
 * removed along with the feature. Do not reintroduce without a fresh
 * root-cause investigation into why it caused the freeze.
 *
 * Items 2/4/6 ARE live-UI tests against `/orders` — no Chain UI needed,
 * reusing the mocking pattern established in
 * `order_book_chase_live_broker_merge.spec.js` (broker+algo merge via
 * shared `broker_order_id`).
 *
 * Item 2 refined same day (coordinator follow-up, after this spec's
 * first version shipped): the plain "Bracket: {name} · Pending" /
 * "triggered — TP @ {price}" text chip (`.log-chip-bracket-paired`) was
 * folded into a 3-state status pill (`.oc-bracket-pill`,
 * `pending`/`tmplhold`/`active`), mirroring ChaseCard.svelte's own
 * `.cc-mode` plain-colored-text pattern. "Tmpl Hold" is clickable and
 * calls the same `releaseHeldOrder()` HeldOrdersCard's own Release
 * button uses. The item-2 tests below were rewritten for the pill; the
 * rich template-name/TP-price detail that used to be the chip's visible
 * text now lives in the pill's `title` tooltip.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/bracket_scope_reset_pending_label_and_held_toast.spec.js \
 *     --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { priceFmt } from '../src/lib/format.js';

// ─────────────────────────────────────────────────────────────────────────
// Shared /orders mocking — items 2, 4, 6 below
// ─────────────────────────────────────────────────────────────────────────

const _NOW_ISO = '2026-09-30T04:30:00.000Z'; // same fixed "now" convention as order_book_chase_live_broker_merge.spec.js

const BRACKET_TEMPLATE = {
  id: 42, kind: 'bracket', name: 'SELL Default Bracket', slug: null,
  description: '', is_system: false, status: 'active', group: null, tags: [],
  legacy_template_id: 42,
  bracket_config: { applies_to: 'sell_option', is_default: true, tp_pct: 5, sl_pct: 3 },
};

async function mockAgents(page) {
  await page.route('**/api/agents/**', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([BRACKET_TEMPLATE]),
    });
  });
}

/**
 * @param {import('@playwright/test').Page} page
 * @param {{ algoRows: any[], brokerRows?: any[], gttRows?: any[], heldRows?: any[] }} shapes
 */
async function mockOrdersEndpoints(page, { algoRows, brokerRows = [], gttRows = [], heldRows = [] }) {
  await page.route('**/api/orders/chases/active**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
  });
  await page.route('**/api/orders/held/**', async (route) => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ held: heldRows }),
    });
  });
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    if (req.method() !== 'GET') { await route.continue(); return; }
    const url = req.url();
    if (url.includes('/orders/held/')) { await route.fallback(); return; }
    if (url.includes('/orders/chases/active')) { await route.fallback(); return; }
    if (url.includes('/orders/gtts')) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ gtts: gttRows, count: gttRows.length }),
      });
      return;
    }
    if (url.includes('/orders/algo/recent')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(algoRows) });
      return;
    }
    if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ rows: brokerRows, refreshed_at: new Date().toISOString() }),
      });
      return;
    }
    await route.continue();
  });
}

// ─────────────────────────────────────────────────────────────────────────
// Item 2 — paired main+bracket display in OrderCard.svelte
// ─────────────────────────────────────────────────────────────────────────

test.describe('OrderCard — 3-state Bracket status pill (item 2, refined)', () => {
  test.setTimeout(60_000);

  test('pending: template_id set, not yet attached -> pill reads "Pending", tooltip names the template', async ({ page }) => {
    await loginAsAdmin(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockAgents(page);
    await mockOrdersEndpoints(page, {
      algoRows: [{
        id: 901, account: 'ZG0790', symbol: 'RBQ-PENDBRK', exchange: 'NFO',
        transaction_type: 'SELL', quantity: 75, initial_price: 250, attempts: 0,
        status: 'OPEN', engine: 'live', mode: 'live', detail: null,
        created_at: '2026-09-30T04:20:00', template_id: 42, attached_gtts_json: null,
        broker_order_id: null,
      }],
    });

    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    const card = page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-PENDBRK' });
    await expect(card).toHaveCount(1);
    const pill = card.locator('.oc-bracket-pill');
    await expect(pill).toHaveClass(/oc-bracket-pill-pending/);
    await expect(pill).toHaveText('Pending');
    await expect(pill).not.toHaveClass(/oc-bracket-pill-clickable/);
    await expect(pill).toHaveAttribute('title', /SELL Default Bracket.*armed/);
  });

  test('active: attached_gtts_json has a TP entry matched to a live broker GTT -> pill reads "Active", tooltip shows TP price', async ({ page }) => {
    await loginAsAdmin(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockAgents(page);

    const brokerOrderId = 'B7001';
    await mockOrdersEndpoints(page, {
      algoRows: [{
        id: 902, account: 'ZG0790', symbol: 'RBQ-TRIGBRK', exchange: 'NFO',
        transaction_type: 'SELL', quantity: 75, initial_price: 250, fill_price: 251,
        attempts: 1, status: 'FILLED', engine: 'live', mode: 'live', detail: null,
        created_at: '2026-09-30T04:20:00',
        template_id: 42,
        attached_gtts_json: JSON.stringify([{ kind: 'gtt', label: 'TP', id: 'GTT555' }]),
        broker_order_id: brokerOrderId,
      }],
      brokerRows: [{
        order_id: brokerOrderId, account: 'ZG0790', exchange: 'NFO', tradingsymbol: 'RBQ-TRIGBRK',
        transaction_type: 'SELL', quantity: 75, pending_quantity: 0, filled_quantity: 75,
        price: 250, trigger_price: 0, average_price: 251, status: 'COMPLETE',
        order_type: 'LIMIT', product: 'MIS', variety: 'regular',
        order_timestamp: '2026-09-30 09:15:00',
      }],
      gttRows: [{
        gtt_id: 'GTT555', status: 'active', trigger_values: [262.5],
        tradingsymbol: 'RBQ-TRIGBRK', account: 'ZG0790', created_at: '2026-09-30 09:16:00',
      }],
    });

    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    const card = page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-TRIGBRK' });
    await expect(card).toHaveCount(1);
    const pill = card.locator('.oc-bracket-pill');
    await expect(pill).toHaveClass(/oc-bracket-pill-active/);
    await expect(pill).toHaveText('Active');
    await expect(pill).not.toHaveClass(/oc-bracket-pill-clickable/);
    const expectedPrice = priceFmt(262.5);
    await expect(pill).toHaveAttribute('title', new RegExp(`triggered.*TP @ ${expectedPrice.replace(/[.,]/g, '\\$&')}`));
  });

  test('tmplhold: FILLED + no attach + template_exit hold -> pill reads "Tmpl Hold", clickable, and click calls the SAME release endpoint HeldOrdersCard uses', async ({ page }) => {
    await loginAsAdmin(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockAgents(page);

    const holdJson = JSON.stringify({
      category: 'template_exit', reason: 'template exits held until released',
      price_policy: 'n/a', override: null, held_at: '2026-09-30T04:20:00+00:00',
    });
    let releaseCalledForId = null;
    await mockOrdersEndpoints(page, {
      algoRows: [{
        id: 905, account: 'ZG0790', symbol: 'RBQ-PILLHOLD', exchange: 'NFO',
        transaction_type: 'SELL', quantity: 75, initial_price: 250, fill_price: 251,
        attempts: 1, status: 'FILLED', engine: 'live', mode: 'live', detail: null,
        created_at: '2026-09-30T04:20:00',
        template_id: 42, attached_gtts_json: null, hold_json: holdJson,
        broker_order_id: null,
      }],
    });
    await page.route('**/api/orders/held/905/release', async (route) => {
      releaseCalledForId = 905;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
    });

    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });
    await page.waitForTimeout(600);

    const card = page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-PILLHOLD' });
    await expect(card).toHaveCount(1);
    const pill = card.locator('.oc-bracket-pill');
    await expect(pill).toHaveClass(/oc-bracket-pill-tmplhold/);
    await expect(pill).toHaveText('Tmpl Hold');
    await expect(pill).toHaveClass(/oc-bracket-pill-clickable/);
    await expect(pill).toHaveAttribute('role', 'button');
    await expect(pill).toHaveAttribute('title', /held pending release.*Click to release/);

    await pill.click();
    await expect.poll(() => releaseCalledForId).toBe(905);
    await expect(page.locator('.rbq-toast-msg', { hasText: /Released bracket exit for #905/ }))
      .toBeVisible({ timeout: 5_000 });
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Item 4 — toast wording distinguishes held-pending-release from a
// genuine attach failure
// ─────────────────────────────────────────────────────────────────────────

test.describe('Order Book — "held pending release" toast vs. "did not attach" (item 4)', () => {
  test.setTimeout(60_000);

  const HOLD_JSON = JSON.stringify({
    category: 'template_exit', reason: 'template exits held until released',
    price_policy: 'n/a', override: null, held_at: '2026-09-30T04:20:00+00:00',
  });

  test('an algo-tracked, live, FILLED, template-exit-held row toasts "Bracket pending — release from Held Orders", never "did not attach"', async ({ page }) => {
    await loginAsAdmin(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockAgents(page);

    // Algo-only row (no broker_order_id / no matching broker row) — a
    // broker-matched row's `status`/`mode` are NOT in OrderBook.svelte's
    // merge passthrough list (only attempts/current_limit/next_attempt_at/
    // agent_origin/template_id/attached_gtts_json/hold_json are), so a
    // merged row keeps the BROKER's own status vocabulary ('COMPLETE',
    // never 'FILLED') — the documented, pre-existing, out-of-scope
    // "OrderBook FILLED-status chip gap" noted in templateAttachToast.js's
    // own module header. This test exercises the toast logic's FILLED
    // gate directly, on a row shape (algo-only) that genuinely reaches it
    // live — the merge-passthrough fix itself is covered separately below.
    await mockOrdersEndpoints(page, {
      algoRows: [{
        id: 903, account: 'ZG0790', symbol: 'RBQ-HELDBRK', exchange: 'NFO',
        transaction_type: 'SELL', quantity: 75, initial_price: 250, fill_price: 251,
        attempts: 1, status: 'FILLED', engine: 'live', mode: 'live', detail: null,
        created_at: '2026-09-30T04:20:00',
        template_id: 42, attached_gtts_json: null, hold_json: HOLD_JSON,
        broker_order_id: null,
      }],
    });

    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });

    await expect(page.locator('.rbq-toast-msg', { hasText: /Bracket pending — release from Held Orders/ }))
      .toBeVisible({ timeout: 5_000 });
    await expect(page.locator('.rbq-toast-msg', { hasText: /did not attach/i })).toHaveCount(0);

    // The OrderCard badge for this row reads "Pending", not "HELD" (item 3).
    const card = page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-HELDBRK' });
    await expect(card.locator('.held-badge')).toHaveText('Pending');
  });

  test('merge-passthrough: hold_json survives the broker+algo merge so a broker-matched row still shows the "Pending" badge', async ({ page }) => {
    await loginAsAdmin(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockAgents(page);

    const brokerOrderId = 'B7003';
    await mockOrdersEndpoints(page, {
      algoRows: [{
        id: 904, account: 'ZG0790', symbol: 'RBQ-HELDMERGE', exchange: 'NFO',
        transaction_type: 'SELL', quantity: 75, initial_price: 250, fill_price: 251,
        attempts: 1, status: 'FILLED', engine: 'live', mode: 'live', detail: null,
        created_at: '2026-09-30T04:20:00',
        template_id: 42, attached_gtts_json: null, hold_json: HOLD_JSON,
        broker_order_id: brokerOrderId,
      }],
      brokerRows: [{
        order_id: brokerOrderId, account: 'ZG0790', exchange: 'NFO', tradingsymbol: 'RBQ-HELDMERGE',
        transaction_type: 'SELL', quantity: 75, pending_quantity: 0, filled_quantity: 75,
        price: 250, trigger_price: 0, average_price: 251, status: 'COMPLETE',
        order_type: 'LIMIT', product: 'MIS', variety: 'regular',
        order_timestamp: '2026-09-30 09:15:00',
      }],
    });

    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.ob-status-bar', { timeout: 15_000 });

    // This row renders under the broker row's OWN status (COMPLETE), so
    // it shows up on the Filled chip (OrderBook's COMPLETE/FILLED chip
    // predicate — unrelated pre-existing behaviour, unaffected here).
    const card = page.locator('.oc-book-grid .order-card', { hasText: 'RBQ-HELDMERGE' });
    await expect(card).toHaveCount(1);
    // hold_json rode along onto the broker-origin row via the new
    // passthrough fields — proves the OrderBook.svelte/LogPanel.svelte
    // merge fix itself, independent of the toast's own FILLED gate above.
    await expect(card.locator('.held-badge')).toHaveText('Pending');
    // The 3-state Bracket pill degrades to 'pending', NOT 'tmplhold', on
    // a broker-merged row — `isHeldPendingReleaseState()` requires
    // `mode === 'live'` and `status === 'FILLED'`, neither of which
    // survives the merge (a broker row carries no `mode` field and its
    // own status vocabulary is 'COMPLETE', never 'FILLED' — the same
    // documented, pre-existing "OrderBook FILLED-status chip gap" noted
    // in templateAttachToast.js's module header, see item 4's own test
    // above). The pill still renders correctly (never crashes, never
    // shows a wrong/stale state) — it just can't resolve the more
    // specific 'tmplhold' state for this row shape.
    const pill = card.locator('.oc-bracket-pill');
    await expect(pill).toHaveClass(/oc-bracket-pill-pending/);
    await expect(pill).toHaveText('Pending');
  });
});

// ─────────────────────────────────────────────────────────────────────────
// Item 6 — HeldOrdersCard surfaces a template-exit-held row (parent
// status='FILLED', hold_json set) and its Release button works
// ─────────────────────────────────────────────────────────────────────────

test.describe('HeldOrdersCard — template-exit-held row (item 6)', () => {
  test.setTimeout(60_000);

  test('a status=FILLED row with hold_json carrying category=template_exit renders and releases correctly', async ({ page }) => {
    await loginAsAdmin(page);
    await page.clock.setFixedTime(new Date(_NOW_ISO));
    await mockAgents(page);

    const holdJson = JSON.stringify({
      category: 'template_exit', reason: 'template exits held until released',
      price_policy: 'n/a', override: null, held_at: '2026-09-30T04:20:00+00:00',
    });

    let releaseCalled = false;
    await mockOrdersEndpoints(page, {
      algoRows: [],
      heldRows: [{
        id: 777, account: 'Z####90', symbol: 'RBQ-CARDHELD', exchange: 'NFO',
        side: 'SELL', qty: 75, product: 'MIS', hold: holdJson,
      }],
    });
    await page.route('**/api/orders/held/777/release', async (route) => {
      releaseCalled = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
    });

    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.held-card', { timeout: 15_000 });

    const row = page.locator('.held-row', { hasText: 'RBQ-CARDHELD' });
    await expect(row).toHaveCount(1);
    // The row's own hold-category label is unaffected by item 3 ("Bracket
    // exit", a distinct, more specific word — never the literal "Held").
    await expect(row.locator('.held-kind')).toHaveText('Bracket exit');

    await row.locator('.held-release').click();
    await expect.poll(() => releaseCalled).toBe(true);
  });
});
