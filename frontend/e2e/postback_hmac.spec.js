/**
 * Kite postback HMAC validation — Wave A security check.
 *
 * POST /api/orders/postback now validates the incoming `checksum`
 * field against HMAC-SHA256(order_id + order_timestamp + api_secret)
 * for every loaded broker account. Mismatched / missing checksum →
 * 401 with detail referencing the signature failure.
 *
 * Pure API test — no browser interaction. Skipped on localhost
 * unless PLAYWRIGHT_BASE_URL is set, since local dev environments
 * typically have no broker accounts loaded and the postback route
 * has its own short-circuit shape there.
 */

import { test, expect } from '@playwright/test';

test.describe('Postback HMAC validation', () => {
  test.skip(
    !process.env.PLAYWRIGHT_BASE_URL,
    'PLAYWRIGHT_BASE_URL not set — postback HMAC requires a real broker-loaded server',
  );

  test('invalid checksum returns 401', async ({ page }) => {
    const r = await page.request.post('/api/orders/postback', {
      data: {
        order_id: 'TEST123',
        order_timestamp: '2026-05-08 09:15:00',
        checksum: 'deadbeef-not-a-real-signature',
        user_id: 'ZG0790',
        status: 'COMPLETE',
        tradingsymbol: 'NIFTY26MAY22000PE',
        transaction_type: 'SELL',
        quantity: 50,
        average_price: 100.5,
      },
    });
    expect(r.status(), `expected 401, got ${r.status()}`).toBe(401);
    const body = await r.json().catch(() => ({}));
    const detail = String(body?.detail || body?.message || '').toLowerCase();
    expect(detail).toMatch(/signature|checksum/);
  });

  test('missing checksum returns 401', async ({ page }) => {
    const r = await page.request.post('/api/orders/postback', {
      data: {
        order_id: 'TEST456',
        order_timestamp: '2026-05-08 09:15:00',
        // checksum omitted entirely
        user_id: 'ZG0790',
        status: 'COMPLETE',
        tradingsymbol: 'NIFTY26MAY22000PE',
        transaction_type: 'SELL',
        quantity: 50,
        average_price: 100.5,
      },
    });
    expect(r.status(), `expected 401, got ${r.status()}`).toBe(401);
  });
});
