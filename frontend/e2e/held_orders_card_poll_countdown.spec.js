// Audit finding #6 (frontend half, 2026-11-24): HeldOrdersCard.svelte
// loaded once on mount and never again — an operator had to reload the
// page to see a newly-staged hold or notice one had auto-released. Added
// a 30s visibleInterval poll (same cadence BrokerHealthBadge.svelte
// already uses) and an auto-release countdown, fed by the new
// `auto_release_at`/`status` fields on GET /api/orders/held/.
//
// Mocking pattern reused from cardheader_sweep_round2.spec.js's
// HeldOrdersCard describe block / held_orders_card_kind_labels.spec.js.

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('HeldOrdersCard — poll + auto-release countdown (audit finding #6)', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('shows a live "auto-releases in Xm Ys" countdown fed by auto_release_at', async ({ page }) => {
    await loginAsAdmin(page);
    const autoReleaseAt = new Date(Date.now() + 5 * 60_000).toISOString();
    await page.route('**/api/orders/held/**', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ held: [{
        id: 'held-cd', side: 'SELL', qty: 1, symbol: 'RBQ-CD', account: 'T1',
        status: 'HELD',
        hold: JSON.stringify({ category: 'agent_close', auto_release_at: autoReleaseAt }),
        auto_release_at: autoReleaseAt,
      }] }),
    }));
    await page.route('**/api/orders/chases/active**', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify([]),
    }));
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    const row = page.locator('.held-row', { hasText: 'RBQ-CD' });
    await expect(row).toBeVisible({ timeout: 20_000 });
    const countdown = row.locator('.held-countdown');
    await expect(countdown).toContainText('auto-releases in 4m', { timeout: 10_000 });

    // Live-ticks: wait a couple of real seconds and confirm the number
    // actually decreased, proving the 1s clock is running, not a static
    // render of the initial value.
    const first = await countdown.textContent();
    await page.waitForTimeout(2200);
    const second = await countdown.textContent();
    expect(second).not.toBe(first);
  });

  test('a row with no auto_release_at (e.g. a repeated-rejection hold) shows no countdown', async ({ page }) => {
    await loginAsAdmin(page);
    await page.route('**/api/orders/held/**', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ held: [{
        id: 'held-nocd', side: 'BUY', qty: 2, symbol: 'RBQ-NOCD', account: 'T2',
        status: 'HELD',
        hold: JSON.stringify({ category: 'agent_order' }),
        auto_release_at: null,
      }] }),
    }));
    await page.route('**/api/orders/chases/active**', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify([]),
    }));
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    const row = page.locator('.held-row', { hasText: 'RBQ-NOCD' });
    await expect(row).toBeVisible({ timeout: 20_000 });
    await expect(row.locator('.held-countdown')).toHaveCount(0);
  });

  test('source wires load() to a 30s visibleInterval poll, matching BrokerHealthBadge\'s own cadence', async () => {
    const { readFileSync } = await import('fs');
    const src = readFileSync(
      new URL('../src/lib/HeldOrdersCard.svelte', import.meta.url).pathname, 'utf8'
    );
    expect(src).toMatch(/import \{ visibleInterval \} from '\$lib\/stores'/);
    expect(src).toMatch(/stopInterval = visibleInterval\(load, 30_000\)/);
    expect(src).toMatch(/if \(stopInterval\) stopInterval\(\);/);
  });

  test('a newly-staged row appears via the real 30s poll, with no page reload', async ({ page }) => {
    // Extends past the default 30s test timeout since this deliberately
    // waits out one real interval tick — the actual behavior the
    // finding asks for ("an operator had to manually reload the page to
    // see a newly-staged hold").
    test.setTimeout(60_000);
    await loginAsAdmin(page);
    let callCount = 0;
    await page.route('**/api/orders/held/**', (route) => {
      callCount += 1;
      const held = callCount === 1
        ? []
        : [{
            id: 'held-late', side: 'SELL', qty: 3, symbol: 'RBQ-LATE', account: 'T3',
            status: 'HELD', hold: JSON.stringify({ category: 'expiry_close' }),
            auto_release_at: null,
          }];
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ held }) });
    });
    await page.route('**/api/orders/chases/active**', (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify([]),
    }));
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    // First poll (on mount) returns nothing — card renders nothing
    // (a {#if rows.length} section). Poll for callCount rather than a
    // fixed sleep, since the page has other components that may delay
    // this one's onMount firing.
    await expect.poll(() => callCount, { timeout: 10_000 }).toBeGreaterThanOrEqual(1);
    await expect(page.locator('.held-row', { hasText: 'RBQ-LATE' })).toHaveCount(0);

    // The row appears on its own once the real 30s interval fires a
    // second GET — no reload, no user action.
    await expect(page.locator('.held-row', { hasText: 'RBQ-LATE' }))
      .toBeVisible({ timeout: 35_000 });
    expect(callCount).toBeGreaterThanOrEqual(2);
  });
});
