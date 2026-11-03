/**
 * chase_card_bracket_hold_pending_release.spec.js
 *
 * 2026-11-03, operator: "the kill button in chase can be used to make it
 * pending and release for bracket on hold" — "if hold is on, you will
 * not see kill. you will see it as pending. when you press it it could
 * show kill. This is for bracket orders only where bracket orders can
 * be held with admin settings."
 *
 * A Bracket's exit (TP/SL/wing) can be deliberately withheld pending
 * operator release (`order_hold_gate.hold_template_exit`, gated by the
 * `hold.template_exit_released` admin setting) — the parent order stays
 * FILLED, never OPEN, so it never had a "Kill" button at all before this
 * change; nothing was there to show. `list_active_chases` now also
 * surfaces these FILLED+held rows, and ChaseCard renders a "Pending"
 * action in their actions cell instead of "Kill", calling the SAME
 * release endpoint the Held Orders card and OrderCard's "Tmpl Hold" pill
 * already call (`POST /orders/held/{id}/release`) — never a duplicated
 * release path.
 *
 * Covers:
 *  1. A held bracket row shows "Pending", not "Kill".
 *  2. Clicking "Pending" calls the release endpoint for that row's id
 *     (never the kill endpoint) and shows a success toast.
 *  3. A genuinely open, non-held chase row in the SAME list still shows
 *     "Kill" — the new branch doesn't swallow the normal case.
 *
 * Network-mocked, same precedent as chase_kill_failure_feedback.spec.js
 * (a real seeded held-bracket fixture would need a live broker fill +
 * the hold admin setting toggled, documented elsewhere in this suite as
 * impractical in CI).
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(60000);

const HELD_ROW = {
  id: 9201, account: 'T1', transaction_type: 'BUY', quantity: 50,
  symbol: 'RBQ-BRACKETHOLD', current_limit: null, initial_price: 100,
  attempts: 1, status: 'FILLED', mode: 'live', template_id: 5,
  attached_gtts_json: null, parent_order_id: null, next_attempt_at: null,
  child_order_ids: [],
  hold_json: JSON.stringify({
    category: 'template_exit', reason: 'template exits pending release',
    price_policy: 'n/a', override: null, held_at: '2026-11-03T00:00:00+00:00',
  }),
};

const OPEN_ROW = {
  id: 9202, account: 'T1', transaction_type: 'SELL', quantity: 25,
  symbol: 'RBQ-OPENCHASE', current_limit: 200, initial_price: 200,
  attempts: 2, status: 'OPEN', mode: 'live', template_id: null,
  attached_gtts_json: null, parent_order_id: null,
  next_attempt_at: Math.floor(Date.now() / 1000) + 10,
  child_order_ids: [], hold_json: null,
};

function mockChaseActive(page, rows) {
  return page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    const url = req.url();
    if (req.method() === 'GET') {
      if (url.includes('/orders/chases/active')) {
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify(rows),
        });
        return;
      }
      if (url.includes('/orders/events/recent') || url.includes('/orders/algo/recent')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
        return;
      }
      if (url.includes('/orders/gtts')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ gtts: [], count: 0 }) });
        return;
      }
      if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ rows: [], refreshed_at: new Date().toISOString() }) });
        return;
      }
    }
    await route.continue();
  });
}

test.describe('ChaseCard — bracket (template-exit) hold Pending/release', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('a held bracket row shows Pending, a normal open row still shows Kill', async ({ page }) => {
    await mockChaseActive(page, [HELD_ROW, OPEN_ROW]);

    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    const orderBtn = page.locator('.pha-order:visible').first();
    await expect(orderBtn).toBeVisible({ timeout: 15_000 });
    await orderBtn.click({ button: 'right' });

    const heldRow = page.locator('.chd-drawer .cc-row', { hasText: 'RBQ-BRACKETHOLD' });
    await expect(heldRow).toBeVisible({ timeout: 10_000 });
    await expect(heldRow.locator('.cc-pending')).toBeVisible();
    await expect(heldRow.locator('.cc-pending')).toHaveText('Pending');
    await expect(heldRow.locator('.cc-kill')).toHaveCount(0);

    const openRow = page.locator('.chd-drawer .cc-row', { hasText: 'RBQ-OPENCHASE' });
    await expect(openRow).toBeVisible();
    await expect(openRow.locator('.cc-kill')).toBeVisible();
    await expect(openRow.locator('.cc-kill')).toHaveText('Kill');
    await expect(openRow.locator('.cc-pending')).toHaveCount(0);
  });

  test('clicking Pending calls the release endpoint for that row, never kill, and shows a success toast', async ({ page }) => {
    await mockChaseActive(page, [HELD_ROW]);

    let releaseCalled = false;
    let killCalled = false;
    await page.route('**/api/orders/held/9201/release', async (route) => {
      releaseCalled = true;
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ ok: true, reason: 'template exits placed', status: 'FILLED' }),
      });
    });
    await page.route('**/api/orders/chases/9201/kill', async (route) => {
      killCalled = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
    });

    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    const orderBtn = page.locator('.pha-order:visible').first();
    await expect(orderBtn).toBeVisible({ timeout: 15_000 });
    await orderBtn.click({ button: 'right' });

    const heldRow = page.locator('.chd-drawer .cc-row', { hasText: 'RBQ-BRACKETHOLD' });
    await expect(heldRow).toBeVisible({ timeout: 10_000 });
    await heldRow.locator('.cc-pending').click();

    await expect.poll(() => releaseCalled, { timeout: 5_000 }).toBe(true);
    expect(killCalled).toBe(false);

    const toast = page.locator('.rbq-toast-msg', { hasText: /released bracket exits for #9201/i });
    await expect(toast).toBeVisible({ timeout: 5_000 });
  });
});
