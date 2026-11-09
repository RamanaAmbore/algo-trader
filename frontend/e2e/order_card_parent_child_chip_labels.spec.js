/**
 * order_card_parent_child_chip_labels.spec.js
 *
 * 2026-11 (Track 3, T3.4, Bracket/Template/GTT removal) — OrderCard.svelte
 * used to render its auto-attached-children chip (`order.child_order_ids`)
 * with the key text `tp:`/`tps:` (singular/plural), inherited from the
 * deleted Bracket wing-chip's wording during the Bracket-removal rewrite.
 * That collides with the PRE-EXISTING, unrelated `target_pct` chip
 * (`order.target_pct != null` → `tp:+30.0%`), which is a genuinely
 * different concept (this order's OWN auto-target percentage, not a
 * reference to another order). Both chips can render on the SAME card —
 * a parent order can carry its own `target_pct` AND already have spawned
 * a take-profit child (`child_order_ids`) — so two `tp:`-keyed chips with
 * unrelated meanings could appear side by side on one row.
 *
 * Fix: the children chip's key is `child:`/`children:` (singular/plural),
 * matching the terminology OrderTimelineDrawer.svelte's own linked-orders
 * chip strip already uses ("Parent: #300" / "Child: #302" —
 * order_timeline_per_order_view.spec.js) and advisor-reviewed to avoid
 * the collision.
 *
 * Reuses the `brokerRow` / `mockOrderBookPage` fixture pattern from
 * order_timeline_per_order_view.spec.js.
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

function brokerRow(overrides = {}) {
  return {
    order_id: '401', tradingsymbol: 'NIFTY26JUN25000CE', exchange: 'NFO',
    transaction_type: 'BUY', quantity: 50, status: 'OPEN',
    order_timestamp: '2026-01-01 09:00:00', price: 100, account: 'T1',
    variety: 'regular', product: 'MIS',
    ...overrides,
  };
}

async function mockOrderBookPage(page, { rows }) {
  await page.route('**/api/orders/**', async (route) => {
    const req = route.request();
    const url = req.url();
    const method = req.method();
    if (method === 'GET') {
      if (url.includes('/orders/events/recent')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
        return;
      }
      if (url.includes('/orders/algo/recent')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
        return;
      }
      if (url.includes('/orders/gtts')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ gtts: [], count: 0 }) });
        return;
      }
      if (/\/api\/orders\/?(\?.*)?$/.test(url)) {
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({ rows, refreshed_at: new Date().toISOString() }),
        });
        return;
      }
      await route.continue();
      return;
    }
    await route.continue();
  });
}

test.describe('OrderCard — parent/target_pct/child chip labels never collide (2026-11, T3.4)', () => {
  test('a row with BOTH target_pct and child_order_ids renders two distinct, unambiguous chips', async ({ page }) => {
    const row = brokerRow({
      target_pct: 0.3,
      child_order_ids: [402],
    });
    await loginAsAdmin(page);
    await mockOrderBookPage(page, { rows: [row] });

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');

    const card = page.locator('.order-card').filter({ hasText: /NIFTY.*25000.*CE/ });
    await expect(card).toBeVisible({ timeout: 15_000 });

    // The order's own auto-target-percentage chip.
    const tpChip = card.locator('.log-chip-tp');
    await expect(tpChip).toBeVisible();
    await expect(tpChip).toContainText('tp:+30.0%');

    // The auto-attached-children chip — must say "child:", never "tp:".
    const childChip = card.locator('.log-chip-child');
    await expect(childChip).toBeVisible();
    await expect(childChip).toContainText('child:');
    await expect(childChip).toContainText('#402');
    await expect(childChip).not.toContainText('tp:');
  });

  test('plural children chip reads "children:", never "tps:"', async ({ page }) => {
    const row = brokerRow({
      child_order_ids: [402, 403],
    });
    await loginAsAdmin(page);
    await mockOrderBookPage(page, { rows: [row] });

    await page.goto('/orders');
    await page.waitForLoadState('domcontentloaded');

    const card = page.locator('.order-card').filter({ hasText: /NIFTY.*25000.*CE/ });
    await expect(card).toBeVisible({ timeout: 15_000 });

    const childChip = card.locator('.log-chip-child');
    await expect(childChip).toBeVisible();
    await expect(childChip).toContainText('children:');
    await expect(childChip).not.toContainText('tps:');
  });
});
