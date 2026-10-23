/**
 * chase_kill_failure_feedback.spec.js
 *
 * 2026-10-23 Kill-lifecycle fix (.claude/PLAN.md Wave 1 cluster 1A, bug #2).
 *
 * Backend `POST /api/orders/chases/{id}/kill` used to always return
 * `ok: true`, even when the broker cancel genuinely failed (returning
 * `err: "broker cancel failed: …"` alongside it) or the row was already
 * terminal. `ChaseCard.svelte:_kill()` treated any `ok: true` as success —
 * it removed the row from `_chases` locally and never surfaced any
 * failure feedback. Because the card's own visibility gate
 * (`{#if !headless && (_chases.length || ...)}`) collapses to nothing
 * once the last chase row is removed, this ALSO made the component's own
 * `_err` banner (set from a genuine failure) invisible — it had no card
 * left to render inside.
 *
 * This spec mocks exactly that old, buggy response shape
 * (`ok: true` + a non-null `err`) to prove the frontend defends against
 * it even if a future backend regression reintroduces it (operator
 * instruction: "treat ok && err as a failure too, as defense in
 * depth"), plus the NEW `pending: true` shape the backend now returns
 * when chase is still actively resolving the row.
 *
 * Both tests are expected to FAIL against the pre-fix ChaseCard.svelte
 * (confirmed via a red run before the fix landed) and pass after.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(60000);

function mockChaseActive(page, chaseRow) {
  return page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    const url = req.url();
    if (req.method() === 'GET') {
      if (url.includes('/orders/chases/active')) {
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify([chaseRow]),
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

test.describe('Chase Kill failure feedback', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('a genuine broker-cancel failure (old ok:true/err-set shape) keeps the row and shows a sticky toast, never silently vanishes', async ({ page }) => {
    const nowSec = Math.floor(Date.now() / 1000);
    const chaseRow = {
      id: 9101, account: 'T1', transaction_type: 'BUY', quantity: 50,
      symbol: 'RBQ-KILLFAIL1', current_limit: 102, initial_price: 100,
      attempts: 2, status: 'OPEN', next_attempt_at: nowSec + 12,
      mode: 'live', child_order_ids: [],
    };
    await mockChaseActive(page, chaseRow);

    await page.route('**/api/orders/chases/9101/kill', async (route) => {
      // The OLD, buggy backend response shape — ok:true alongside a
      // genuine failure message. The frontend must defend against this
      // shape regardless of what the backend currently sends.
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ ok: true, err: 'broker cancel failed: order does not exist' }),
      });
    });

    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    // 2026-10-23 relocation — the standalone `.chase-chip` is gone; the
    // chase drawer is now reached via long-press/right-click on the
    // Order button (see order_header_chase_gesture.spec.js).
    const orderBtn = page.locator('.pha-order:visible').first();
    await expect(orderBtn).toBeVisible({ timeout: 15_000 });
    await orderBtn.click({ button: 'right' });

    const row = page.locator('.chd-drawer .cc-row', { hasText: 'RBQ-KILLFAIL1' });
    await expect(row).toBeVisible({ timeout: 10_000 });

    await row.locator('.cc-kill').click();

    // The row must still be there IMMEDIATELY after the click — checked
    // with a short timeout, well inside ChaseCard's own 3s poll
    // interval, so this can't be masked by the next poll simply
    // re-fetching the (unconditionally non-empty, in this mock) active
    // list and papering over an optimistic removal. A failed kill (even
    // one the backend mislabels ok:true) must never remove it, not even
    // for one render.
    await expect(row).toBeVisible({ timeout: 1_000 });

    // A sticky error toast must report the real failure text — this
    // survives ChaseCard's own 3s poll clearing `_err`, unlike the
    // transient in-card banner alone.
    const toast = page.locator('.rbq-toast-msg', { hasText: /broker cancel failed/i });
    await expect(toast).toBeVisible({ timeout: 5_000 });
  });

  test('a pending (chase still cancelling its replacement) response keeps the row and warns, without claiming success', async ({ page }) => {
    const nowSec = Math.floor(Date.now() / 1000);
    const chaseRow = {
      id: 9102, account: 'T1', transaction_type: 'SELL', quantity: 25,
      symbol: 'RBQ-KILLPEND1', current_limit: 98, initial_price: 100,
      attempts: 4, status: 'OPEN', next_attempt_at: nowSec + 8,
      mode: 'live', child_order_ids: [],
    };
    await mockChaseActive(page, chaseRow);

    await page.route('**/api/orders/chases/9102/kill', async (route) => {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          ok: false, pending: true, status: 'OPEN',
          err: 'chase is still cancelling the replacement order — retry shortly',
        }),
      });
    });

    await page.goto('/pulse');
    await page.waitForLoadState('domcontentloaded');

    const orderBtn = page.locator('.pha-order:visible').first();
    await expect(orderBtn).toBeVisible({ timeout: 15_000 });
    await orderBtn.click({ button: 'right' });

    const row = page.locator('.chd-drawer .cc-row', { hasText: 'RBQ-KILLPEND1' });
    await expect(row).toBeVisible({ timeout: 10_000 });

    await row.locator('.cc-kill').click();

    // Not removed — nothing succeeded yet. Short timeout, same reasoning
    // as the sibling test above.
    await expect(row).toBeVisible({ timeout: 1_000 });

    const toast = page.locator('.rbq-toast-msg', { hasText: /still cancelling/i });
    await expect(toast).toBeVisible({ timeout: 5_000 });
  });
});
