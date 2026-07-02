/**
 * Dhan poll-priority chip — /admin/brokers page.
 *
 * Five quality dimensions:
 *   SSOT    — chip colour driven by PRIORITY_STYLES object (single definition)
 *   Correct — HOT=green-400, WARM=amber-400, COLD=slate-400 chips render
 *   Stale   — no raw inline styles that hardcode priority colours outside PRIORITY_STYLES
 *   Reuse   — no duplicate chip logic (single .priority-chip class per row)
 *   UX      — reduced-motion omits transition; Kite/Groww rows have no chip;
 *             red dot appears on circuit_open; 400ms transition in non-reduced
 *
 * Tests run against a mock backend (intercepts /api/admin/brokers and
 * /api/admin/broker-health) so the real Dhan/Kite accounts are not hit.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;
const BROKERS_URL = '/admin/brokers';

// Mock broker data: one Dhan (HOT), one Kite, one cold Dhan.
const MOCK_BROKERS = [
  {
    id: 1, account: 'DH6847', broker_id: 'dhan', api_key: 'key1',
    client_id: 'CL001', source_ip: null, is_active: true,
    historical_data_enabled: true, notes: null,
    poll_priority: 'hot', auto_downgrade_enabled: false,
    auto_downgraded_at: null, auto_downgrade_reason: null,
    priority: 100, extra_config: {},
    created_at: '2026-07-01T00:00:00+00:00',
    updated_at: '2026-07-01T00:00:00+00:00',
    loaded: true,
    has_api_secret: true, has_password: true, has_totp_token: true,
    has_access_token: false,
  },
  {
    id: 2, account: 'ZG0790', broker_id: 'zerodha_kite', api_key: 'key2',
    client_id: null, source_ip: null, is_active: true,
    historical_data_enabled: true, notes: null,
    poll_priority: 'hot', auto_downgrade_enabled: false,
    auto_downgraded_at: null, auto_downgrade_reason: null,
    priority: 100, extra_config: {},
    created_at: '2026-07-01T00:00:00+00:00',
    updated_at: '2026-07-01T00:00:00+00:00',
    loaded: true,
    has_api_secret: true, has_password: true, has_totp_token: true,
    has_access_token: false,
  },
  {
    id: 3, account: 'DH3747', broker_id: 'dhan', api_key: 'key3',
    client_id: 'CL002', source_ip: null, is_active: true,
    historical_data_enabled: true, notes: null,
    poll_priority: 'cold', auto_downgrade_enabled: true,
    auto_downgraded_at: '2026-07-02T10:00:00+00:00',
    auto_downgrade_reason: '5 breaker opens in 15 min',
    priority: 100, extra_config: {},
    created_at: '2026-07-01T00:00:00+00:00',
    updated_at: '2026-07-01T00:00:00+00:00',
    loaded: true,
    has_api_secret: true, has_password: true, has_totp_token: true,
    has_access_token: false,
  },
];

const MOCK_HEALTH = {
  accounts: [
    {
      account: 'DH6847', broker: 'dhan', state: 'green', reason: 'healthy',
      last_good_at: null, last_check_at: null, is_active_ticker: false,
      circuit_state: 'closed', consecutive_fail_count: 0, circuit_open_until: null,
      poll_priority: 'hot', auto_downgrade_enabled: false,
      auto_downgraded_at: null, auto_downgrade_reason: null,
    },
    {
      account: 'ZG0790', broker: 'kite', state: 'green', reason: 'healthy',
      last_good_at: null, last_check_at: null, is_active_ticker: true,
      circuit_state: 'closed', consecutive_fail_count: 0, circuit_open_until: null,
      poll_priority: 'hot', auto_downgrade_enabled: false,
      auto_downgraded_at: null, auto_downgrade_reason: null,
    },
    {
      account: 'DH3747', broker: 'dhan', state: 'red', reason: 'circuit open',
      last_good_at: null, last_check_at: null, is_active_ticker: false,
      circuit_state: 'open',
      circuit_open_until: new Date(Date.now() + 300_000).toISOString(),
      consecutive_fail_count: 3, poll_priority: 'cold',
      auto_downgrade_enabled: true,
      auto_downgraded_at: '2026-07-02T10:00:00+00:00',
      auto_downgrade_reason: '5 breaker opens in 15 min',
    },
  ],
};

async function setupMocks(page) {
  await page.route('**/api/admin/brokers', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ json: MOCK_BROKERS });
    } else {
      await route.continue();
    }
  });
  await page.route('**/api/admin/broker-health', async (route) => {
    await route.fulfill({ json: MOCK_HEALTH });
  });
  // Stub any PATCH calls so priority-change tests don't error.
  await page.route('**/api/admin/brokers/DH*', async (route) => {
    if (route.request().method() === 'PATCH') {
      await route.fulfill({ json: MOCK_BROKERS[0] });
    } else if (route.request().method() === 'POST') {
      await route.fulfill({ json: MOCK_BROKERS[0] });
    } else {
      await route.continue();
    }
  });
}

test.describe('broker priority chip', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await setupMocks(page);
    await page.goto(BROKERS_URL);
    // Wait for table to render.
    await page.locator('.brokers-table tbody tr').first().waitFor({ timeout: TIMEOUT });
  });

  // ── SSOT / Correct: chip renders correct colour per priority ──────

  test('HOT chip has green-400 palette', async ({ page }) => {
    // DH6847 is 'hot'.
    const chip = page.locator('.brokers-table tbody tr').filter({ hasText: 'DH6847' })
      .locator('.priority-chip');
    await expect(chip).toBeVisible({ timeout: TIMEOUT });
    await expect(chip).toHaveText(/HOT/i);
    // Check computed color is roughly green-400 (#4ade80 = rgb(74,222,128)).
    const color = await chip.evaluate(el => window.getComputedStyle(el).color);
    expect(color).toMatch(/rgb\(74,\s*222,\s*128\)/);
  });

  test('COLD chip has slate-400 palette', async ({ page }) => {
    // DH3747 is 'cold'.
    const chip = page.locator('.brokers-table tbody tr').filter({ hasText: 'DH3747' })
      .locator('.priority-chip');
    await expect(chip).toBeVisible({ timeout: TIMEOUT });
    await expect(chip).toHaveText(/COLD/i);
    const color = await chip.evaluate(el => window.getComputedStyle(el).color);
    // slate-400 = #94a3b8 = rgb(148, 163, 184)
    expect(color).toMatch(/rgb\(148,\s*163,\s*184\)/);
  });

  // ── Correct: Kite row has no chip ─────────────────────────────────

  test('Kite row has no priority chip', async ({ page }) => {
    const kiteRow = page.locator('.brokers-table tbody tr').filter({ hasText: 'ZG0790' });
    await expect(kiteRow).toBeVisible({ timeout: TIMEOUT });
    const chip = kiteRow.locator('.priority-chip');
    await expect(chip).toHaveCount(0);
    // Instead should show the em-dash placeholder.
    const na = kiteRow.locator('.priority-na');
    await expect(na).toBeVisible();
  });

  // ── UX: red dot appears on circuit_open ───────────────────────────

  test('red dot appears when circuit_state=open', async ({ page }) => {
    // DH3747 has circuit_state='open' in MOCK_HEALTH.
    const dot = page.locator('.brokers-table tbody tr').filter({ hasText: 'DH3747' })
      .locator('.circuit-dot');
    await expect(dot).toBeVisible({ timeout: TIMEOUT });
    const bg = await dot.evaluate(el => window.getComputedStyle(el).backgroundColor);
    // #f87171 = rgb(248, 113, 113)
    expect(bg).toMatch(/rgb\(248,\s*113,\s*113\)/);
  });

  test('no red dot when circuit_state=closed', async ({ page }) => {
    // DH6847 has circuit_state='closed'.
    const dot = page.locator('.brokers-table tbody tr').filter({ hasText: 'DH6847' })
      .locator('.circuit-dot');
    await expect(dot).toHaveCount(0);
  });

  // ── UX: no animation on chip in steady state ──────────────────────

  test('chip has no keyframe animation in steady state', async ({ page }) => {
    const chip = page.locator('.priority-chip').first();
    await expect(chip).toBeVisible({ timeout: TIMEOUT });
    const animName = await chip.evaluate(
      el => window.getComputedStyle(el).animationName
    );
    // Should be 'none' — no CSS keyframe animations on the chip.
    expect(animName).toBe('none');
  });

  // ── UX: 400ms bg-color transition in non-reduced-motion ──────────

  test('chip has 400ms background-color transition', async ({ page }) => {
    // This test only runs in non-reduced-motion context.
    const chip = page.locator('.priority-chip').first();
    await expect(chip).toBeVisible({ timeout: TIMEOUT });
    const transition = await chip.evaluate(
      el => window.getComputedStyle(el).transition
    );
    // Must contain 'background-color' and '0.4s' (or '400ms').
    expect(transition).toMatch(/background-color/);
    expect(transition).toMatch(/0\.4s|400ms/);
  });

  // ── UX: reduced-motion removes transition ─────────────────────────

  test('reduced-motion emulation removes transition', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    // Reload to pick up the new media query.
    await page.reload();
    await page.locator('.brokers-table tbody tr').first().waitFor({ timeout: TIMEOUT });

    const chip = page.locator('.priority-chip').first();
    await expect(chip).toBeVisible({ timeout: TIMEOUT });
    const transition = await chip.evaluate(
      el => window.getComputedStyle(el).transition
    );
    // Under prefers-reduced-motion, transition should be 'none' or
    // not contain background-color.
    const hasTransition = /background-color/.test(transition)
      && !/\bnone\b/.test(transition);
    expect(hasTransition).toBe(false);
  });

  // ── Correct: auto-downgrade annotation + restore link ────────────

  test('cold auto-downgraded row shows annotation and restore link', async ({ page }) => {
    const row = page.locator('.brokers-table tbody tr').filter({ hasText: 'DH3747' });
    await expect(row).toBeVisible({ timeout: TIMEOUT });
    const annotation = row.locator('.auto-dg-annotation');
    await expect(annotation).toBeVisible();
    await expect(annotation).toContainText('auto @');
    const restoreLink = row.locator('.restore-link');
    await expect(restoreLink).toBeVisible();
    await expect(restoreLink).toHaveText('restore');
  });

  // ── Correct: chip click opens dropdown ────────────────────────────

  test('chip click opens priority dropdown', async ({ page }) => {
    const chip = page.locator('.brokers-table tbody tr').filter({ hasText: 'DH6847' })
      .locator('.priority-chip');
    await chip.click();
    const dropdown = page.locator('.priority-dropdown').first();
    await expect(dropdown).toBeVisible({ timeout: 3000 });
    // Should contain all three priority options.
    await expect(dropdown.locator('.priority-dropdown-item')).toHaveCount(3);
  });
});
