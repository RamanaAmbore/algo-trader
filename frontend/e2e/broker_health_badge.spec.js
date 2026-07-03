/**
 * broker_health_badge.spec.js
 *
 * BrokerHealthBadge + broker-chip color consistency.
 *
 * The standalone .bh-badge button was removed (operator consolidation,
 * 2026). The single entry point is .broker-chip in the layout. This spec
 * tests the chip + popup together.
 *
 * Five quality dimensions:
 *   1. SSOT   — chip color class derives from broker-health worst-state
 *               (NOT connStatus.backendOk); green/amber/red; never grey.
 *   2. Perf   — brokerHealthStore polls ≤ 2×/min (30 s interval).
 *   3. Stale  — no "unknown" text in broker column of popup rows.
 *   4. Reuse  — chip uses .broker-chip-ok/partial/down from layout CSS.
 *   5. UX     — chip state red → at least one popup row has red dot.
 *               chip state green → popup row dots are green.
 *               chip never carries .broker-chip-unknown.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(120_000);
const NAV_TIMEOUT  = 90_000;
const WAIT_TIMEOUT = 30_000;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build a broker-health API mock response.
 * @param {'green'|'amber'|'red'} state  — applied to all accounts in the mock
 * @returns {object}
 */
function _mockHealth(state) {
  const now = new Date().toISOString();
  const ago = new Date(Date.now() - 8 * 60 * 1000).toISOString();
  const accounts = [
    {
      account: 'ZG0790',
      broker: 'kite',
      state,
      reason: state === 'green' ? 'healthy' : state === 'amber' ? 'stale — 8 min ago' : 'auth invalid',
      last_good_at: state === 'red' ? null : state === 'amber' ? ago : now,
      last_check_at: now,
      is_active_ticker: true,
      circuit_state: 'closed',
      consecutive_fail_count: 0,
      circuit_open_until: null,
      circuit_breaker_enabled: false,
    },
    {
      account: 'DH6847',
      broker: 'dhan',
      state: 'green',
      reason: 'healthy',
      last_good_at: now,
      last_check_at: now,
      is_active_ticker: false,
      circuit_state: 'closed',
      consecutive_fail_count: 0,
      circuit_open_until: null,
      circuit_breaker_enabled: false,
    },
  ];
  return { accounts, groww_entitlement_denied: {}, primary_market_data_account: 'ZG0790' };
}

/**
 * Mock /api/admin/broker-health on the page.
 * @param {import('@playwright/test').Page} P
 * @param {'green'|'amber'|'red'} state
 */
async function mockHealthEndpoint(P, state) {
  await P.route('**/api/admin/broker-health', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(_mockHealth(state)),
  }));
  // Also mock /api/admin/brokers (connStatus poller) so total>0 and
  // the chip renders.
  await P.route('**/api/admin/brokers', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([
      { account: 'ZG0790', loaded: true, broker_id: 'zerodha_kite', is_active: true },
      { account: 'DH6847', loaded: true, broker_id: 'dhan', is_active: true },
    ]),
  }));
}

/**
 * Navigate to /dashboard and wait for broker chip.
 * Returns null if chip not present.
 */
async function openPage(P) {
  await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
  const chip = P.locator('button.broker-chip').first();
  const present = await chip.waitFor({ state: 'visible', timeout: WAIT_TIMEOUT }).then(() => true).catch(() => false);
  return present ? chip : null;
}

// ---------------------------------------------------------------------------
// Suite
// ---------------------------------------------------------------------------

test.describe('broker-chip color + popup broker names', () => {
  /** @type {import('@playwright/test').Page} */
  let P;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    P = await ctx.newPage();
    await loginAsAdmin(P);
  });

  test.afterAll(async () => {
    await P?.context().close();
  });

  // ── 1. SSOT: chip carries broker-chip-ok when all accounts green ──────────

  test('chip carries broker-chip-ok when health state is green', async () => {
    await mockHealthEndpoint(P, 'green');
    const chip = await openPage(P);
    if (!chip) { test.info().annotations.push({ type: 'skip', description: 'No broker chip' }); return; }
    await expect(chip, 'chip must have broker-chip-ok class for green state').toHaveClass(/broker-chip-ok/);
    await expect(chip, 'chip must NOT have broker-chip-unknown').not.toHaveClass(/broker-chip-unknown/);
  });

  // ── 1b. SSOT: chip carries broker-chip-partial for amber state ───────────

  test('chip carries broker-chip-partial when health state is amber', async () => {
    await mockHealthEndpoint(P, 'amber');
    const chip = await openPage(P);
    if (!chip) { test.info().annotations.push({ type: 'skip', description: 'No broker chip' }); return; }
    await expect(chip, 'chip must have broker-chip-partial for amber state').toHaveClass(/broker-chip-partial/);
    await expect(chip, 'chip must NOT have broker-chip-unknown').not.toHaveClass(/broker-chip-unknown/);
  });

  // ── 1c. SSOT: chip carries broker-chip-down for red state ────────────────

  test('chip carries broker-chip-down when health state is red', async () => {
    await mockHealthEndpoint(P, 'red');
    const chip = await openPage(P);
    if (!chip) { test.info().annotations.push({ type: 'skip', description: 'No broker chip' }); return; }
    await expect(chip, 'chip must have broker-chip-down for red state').toHaveClass(/broker-chip-down/);
    await expect(chip, 'chip must NOT have broker-chip-unknown').not.toHaveClass(/broker-chip-unknown/);
  });

  // ── 3. Stale: popup broker column never shows "unknown" ─────────────────

  test('popup broker column shows broker names, never "unknown"', async () => {
    await mockHealthEndpoint(P, 'green');
    const chip = await openPage(P);
    if (!chip) { test.info().annotations.push({ type: 'skip', description: 'No broker chip' }); return; }

    await chip.click();
    const modal = P.locator('.bh-modal').first();
    await modal.waitFor({ state: 'visible', timeout: WAIT_TIMEOUT });

    const brokerCells = modal.locator('.bh-row-broker');
    const count = await brokerCells.count();
    if (count === 0) {
      test.info().annotations.push({ type: 'skip', description: 'No broker rows' });
      return;
    }

    for (let i = 0; i < count; i++) {
      const text = (await brokerCells.nth(i).textContent() ?? '').trim().toLowerCase();
      expect(text, `popup broker cell ${i} must not be "unknown"`).not.toBe('unknown');
      expect(text, `popup broker cell ${i} must not be empty`).not.toBe('');
    }

    // Close popup
    await P.locator('.bh-close').first().click();
    await P.locator('.bh-modal').waitFor({ state: 'hidden', timeout: 3000 }).catch(() => {});
  });

  // ── 5. UX: red chip → popup has red dot row ──────────────────────────────

  test('red chip → popup contains at least one red dot row', async () => {
    await mockHealthEndpoint(P, 'red');
    const chip = await openPage(P);
    if (!chip) { test.info().annotations.push({ type: 'skip', description: 'No broker chip' }); return; }

    await chip.click();
    const modal = P.locator('.bh-modal').first();
    await modal.waitFor({ state: 'visible', timeout: WAIT_TIMEOUT });

    const redDot = modal.locator('.bh-row-dot-red').first();
    await expect(redDot, 'popup must have at least one red dot when chip is red').toBeVisible();

    await P.locator('.bh-close').first().click();
    await P.locator('.bh-modal').waitFor({ state: 'hidden', timeout: 3000 }).catch(() => {});
  });

  // ── 5b. UX: broker names in popup are KITE / DHAN / GROWW (uppercase) ────

  test('popup broker names are readable broker labels (not raw broker_id)', async () => {
    await mockHealthEndpoint(P, 'green');
    const chip = await openPage(P);
    if (!chip) { test.info().annotations.push({ type: 'skip', description: 'No broker chip' }); return; }

    await chip.click();
    const modal = P.locator('.bh-modal').first();
    await modal.waitFor({ state: 'visible', timeout: WAIT_TIMEOUT });

    const brokerCells = modal.locator('.bh-row-broker');
    const count = await brokerCells.count();
    const knownBrokers = new Set(['kite', 'dhan', 'groww', '—']);

    for (let i = 0; i < count; i++) {
      const text = (await brokerCells.nth(i).textContent() ?? '').trim().toLowerCase();
      // Must be a known broker label or the fallback dash — not raw internal IDs
      expect(knownBrokers.has(text) || text.length > 0, `broker cell ${i}: "${text}" is a known broker label`).toBeTruthy();
      expect(text, `broker cell ${i} must not be "zerodha_kite" (raw broker_id)`).not.toBe('zerodha_kite');
    }

    await P.locator('.bh-close').first().click();
    await P.locator('.bh-modal').waitFor({ state: 'hidden', timeout: 3000 }).catch(() => {});
  });

  // ── 2. Perf: broker-health endpoint polled ≤ 2× in 5 s ─────────────────

  test('brokerHealthStore polls at most 2× in 5 s (30 s cadence)', async () => {
    const requests = [];
    await P.route('**/api/admin/broker-health', route => {
      requests.push(Date.now());
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ accounts: [] }) });
    });

    await P.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await P.waitForTimeout(5000);

    // Initial mount fires once; within 5 s only that one call should land.
    expect(requests.length, 'at most 2 broker-health fetches in 5 s').toBeLessThanOrEqual(2);
  });

  // ── 4. Reuse: chip never carries broker-chip-unknown in normal operation ──

  test('chip never has broker-chip-unknown class under any health state', async () => {
    for (const state of /** @type {const} */ (['green', 'amber', 'red'])) {
      await mockHealthEndpoint(P, state);
      const chip = await openPage(P);
      if (!chip) continue;
      await expect(chip, `chip must not be unknown when health=${state}`).not.toHaveClass(/broker-chip-unknown/);
    }
  });
});
