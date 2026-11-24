// Audit finding #19 (2026-11-24): `kindOf()`/`kindClassOf()` in
// HeldOrdersCard.svelte used to fall through to the 'Expiry close' label
// for ANY category that wasn't 'agent_order' or 'agent_close' — including
// a genuinely unrecognized/missing one, actively mislabeling it as a
// real, known kind. Fixed to fall through to a neutral 'Pending' label
// (held-kind-pending CSS class) instead. This is the dedicated
// live-browser fixture-rendering spec the finding's own text asked for —
// the sibling held_orders_card_styling.spec.js covers the same mechanism
// at source-grep level only, never mounting the card with real data.
//
// Mocking pattern reused verbatim from
// cardheader_sweep_round2.spec.js's HeldOrdersCard describe block.

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

async function mountWithRows(page, rows) {
  await loginAsAdmin(page);
  await page.route('**/api/orders/held/**', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ held: rows }),
  }));
  await page.route('**/api/orders/chases/active**', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify([]),
  }));
  await page.goto('/orders', { waitUntil: 'domcontentloaded' });
}

test.describe('HeldOrdersCard — kind label fixtures (audit finding #19)', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('expiry_close category renders "Expiry close" with the amber kind class', async ({ page }) => {
    await mountWithRows(page, [{
      id: 'held-exp', side: 'SELL', qty: 1, symbol: 'RBQ-EXP', account: 'T1',
      hold: JSON.stringify({ category: 'expiry_close' }),
    }]);
    const row = page.locator('.held-row', { hasText: 'RBQ-EXP' });
    await expect(row).toBeVisible({ timeout: 20_000 });
    const kind = row.locator('.held-kind');
    await expect(kind).toHaveText('Expiry close');
    await expect(kind).toHaveClass(/held-kind-expiry/);
  });

  test('agent_close category renders "Profit-target close" with the green kind class', async ({ page }) => {
    await mountWithRows(page, [{
      id: 'held-ac', side: 'BUY', qty: 2, symbol: 'RBQ-AC', account: 'T2',
      hold: JSON.stringify({ category: 'agent_close' }),
    }]);
    const row = page.locator('.held-row', { hasText: 'RBQ-AC' });
    await expect(row).toBeVisible({ timeout: 20_000 });
    const kind = row.locator('.held-kind');
    await expect(kind).toHaveText('Profit-target close');
    await expect(kind).toHaveClass(/held-kind-agent-close/);
  });

  test('agent_order category renders "Repeated rejection" with the orange kind class', async ({ page }) => {
    await mountWithRows(page, [{
      id: 'held-ao', side: 'SELL', qty: 3, symbol: 'RBQ-AO', account: 'T3',
      hold: JSON.stringify({ category: 'agent_order' }),
    }]);
    const row = page.locator('.held-row', { hasText: 'RBQ-AO' });
    await expect(row).toBeVisible({ timeout: 20_000 });
    const kind = row.locator('.held-kind');
    await expect(kind).toHaveText('Repeated rejection');
    await expect(kind).toHaveClass(/held-kind-rejection/);
  });

  test('a genuinely unrecognized category renders the neutral "Pending" label, never "Expiry close"', async ({ page }) => {
    await mountWithRows(page, [{
      id: 'held-unk', side: 'BUY', qty: 4, symbol: 'RBQ-UNK', account: 'T4',
      hold: JSON.stringify({ category: 'some_future_category_this_card_does_not_know' }),
    }]);
    const row = page.locator('.held-row', { hasText: 'RBQ-UNK' });
    await expect(row).toBeVisible({ timeout: 20_000 });
    const kind = row.locator('.held-kind');
    await expect(kind).toHaveText('Pending');
    await expect(kind).not.toHaveText('Expiry close');
    await expect(kind).toHaveClass(/held-kind-pending/);
  });

  test('a malformed/unparseable hold field renders the neutral "Pending" label, never "Expiry close"', async ({ page }) => {
    await mountWithRows(page, [{
      id: 'held-bad', side: 'SELL', qty: 5, symbol: 'RBQ-BAD', account: 'T5',
      hold: 'not valid json {{{',
    }]);
    const row = page.locator('.held-row', { hasText: 'RBQ-BAD' });
    await expect(row).toBeVisible({ timeout: 20_000 });
    const kind = row.locator('.held-kind');
    await expect(kind).toHaveText('Pending');
    await expect(kind).not.toHaveText('Expiry close');
    await expect(kind).toHaveClass(/held-kind-pending/);
  });

  test('a missing hold field renders the neutral "Pending" label, never "Expiry close"', async ({ page }) => {
    await mountWithRows(page, [{
      id: 'held-nohold', side: 'BUY', qty: 6, symbol: 'RBQ-NOHOLD', account: 'T6',
    }]);
    const row = page.locator('.held-row', { hasText: 'RBQ-NOHOLD' });
    await expect(row).toBeVisible({ timeout: 20_000 });
    const kind = row.locator('.held-kind');
    await expect(kind).toHaveText('Pending');
    await expect(kind).not.toHaveText('Expiry close');
    await expect(kind).toHaveClass(/held-kind-pending/);
  });

  test('mixed fixture — each row resolves its own distinct label independently', async ({ page }) => {
    await mountWithRows(page, [
      { id: 'm1', side: 'SELL', qty: 1, symbol: 'RBQ-M1', account: 'T1',
        hold: JSON.stringify({ category: 'expiry_close' }) },
      { id: 'm2', side: 'BUY', qty: 2, symbol: 'RBQ-M2', account: 'T2',
        hold: JSON.stringify({ category: 'agent_close' }) },
      { id: 'm3', side: 'SELL', qty: 3, symbol: 'RBQ-M3', account: 'T3',
        hold: JSON.stringify({ category: 'unknown_thing' }) },
    ]);
    await expect(page.locator('.held-row', { hasText: 'RBQ-M1' }).locator('.held-kind'))
      .toHaveText('Expiry close', { timeout: 20_000 });
    await expect(page.locator('.held-row', { hasText: 'RBQ-M2' }).locator('.held-kind'))
      .toHaveText('Profit-target close');
    await expect(page.locator('.held-row', { hasText: 'RBQ-M3' }).locator('.held-kind'))
      .toHaveText('Pending');
  });
});
