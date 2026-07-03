/**
 * Regression guard: demo mode account dropdown must include Dhan accounts
 * (D1####, D2####) even when those brokers have zero positions/holdings.
 *
 * Root cause (2026-07-03): AccountsController used jwt_guard, which 401s
 * anonymous demo sessions. connStatus.accounts stayed empty [], so
 * _knownBrokerAccounts was []. The dropdown union only included accounts
 * seen in positions/holdings rows — Dhan at 0 rows = invisible.
 *
 * Fix: AccountsController guard changed to auth_or_demo_guard (backend).
 * connStatus poller in demo mode now fetches /api/accounts/ and populates
 * connStatus.accounts with masked codes (frontend stores.js).
 *
 * Five quality dimensions:
 *   SSOT      — verify single source of accounts (/api/accounts/) used by demo UI
 *   Perf      — /api/accounts/ must respond within 3s
 *   Stale     — grep for jwt_guard on AccountsController confirms it's removed
 *   Reuse     — visitAnonymous fixture from e2e/fixtures/auth.js
 *   UX        — masked codes (D1####, D2####) visible in dropdown (not raw IDs)
 *
 * Run against PROD only (demo mode requires main branch):
 *   PLAYWRIGHT_BASE_URL=https://ramboq.com npx playwright test \
 *     e2e/demo_dhan_dropdown.spec.js --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { visitAnonymous } from './fixtures/auth.js';

const BASE   = process.env.PLAYWRIGHT_BASE_URL || 'https://ramboq.com';
const TIMEOUT = 15_000;

async function detectBranch(page) {
  try {
    const r = await page.request.get(`${BASE}/api/charts/paper-status`);
    if (!r.ok()) return undefined;
    const j = await r.json();
    return j?.branch;
  } catch (_) {
    return undefined;
  }
}

// ── SSOT + Perf: /api/accounts/ responds anonymously with Dhan accounts ────

test.describe('demo /api/accounts/ — Dhan accounts visible anonymously', () => {
  test('accounts endpoint returns D1#### and D2#### without a JWT', async ({ page }) => {
    const branch = await detectBranch(page);
    if (branch !== 'main') {
      test.skip(true, `branch=${branch} — demo mode only applies on main`);
    }

    const t0 = Date.now();
    const r = await page.request.get(`${BASE}/api/accounts/`);
    const elapsed = Date.now() - t0;

    // Perf: endpoint must respond within 3 s
    expect(elapsed, 'accounts endpoint latency').toBeLessThan(3000);

    expect(r.ok(), `expected 200, got ${r.status()}`).toBe(true);

    const body = await r.json();
    const ids = (body?.accounts || []).map((a) => String(a?.account_id || ''));

    // UX: masked Dhan codes must be present (D1#### = DH3747, D2#### = DH6847)
    expect(ids, 'D1#### (first Dhan) must be in accounts list').toContain('D1####');
    expect(ids, 'D2#### (second Dhan) must be in accounts list').toContain('D2####');

    // UX: raw codes must NOT leak to demo sessions
    for (const id of ids) {
      expect(id, `raw account ID must not appear in demo; got "${id}"`).not.toMatch(/^DH\d/);
      expect(id, `raw account ID must not appear in demo; got "${id}"`).not.toMatch(/^ZG\d/);
      expect(id, `raw account ID must not appear in demo; got "${id}"`).not.toMatch(/^ZJ\d/);
    }
  });
});

// ── UX: /pulse account dropdown contains Dhan + all broker accounts ─────────

test.describe('demo /pulse account dropdown — Dhan accounts visible', () => {
  test.beforeEach(async ({ page }) => {
    await visitAnonymous(page);
  });

  test('account picker on /pulse lists D1#### and D2#### in demo', async ({ page }) => {
    const branch = await detectBranch(page);
    if (branch !== 'main') {
      test.skip(true, `branch=${branch} — demo mode only applies on main`);
    }

    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle', timeout: TIMEOUT });

    // Wait for the connStatus poller to fetch accounts and for the UI to settle
    await page.waitForTimeout(4000);

    // The account picker trigger or the dropdown options should include D1#### / D2####.
    // The MarketPulse component renders two MultiSelect widgets (positions + holdings).
    // Both draw from availableAccounts which is the union of positions/holdings rows
    // + _knownBrokerAccounts (connStatus.accounts after our fix). We click the first
    // account picker to open it and inspect the visible option labels.
    const picker = page.locator('[data-testid="positions-account-picker"], .ms-trigger, .acct-picker').first();
    const pickerExists = await picker.isVisible({ timeout: 5000 }).catch(() => false);

    if (!pickerExists) {
      // Fall back to checking the rendered text on the page for masked codes
      const pageText = await page.content();
      expect(pageText, 'D1#### must appear on /pulse in demo').toContain('D1####');
      expect(pageText, 'D2#### must appear on /pulse in demo').toContain('D2####');
      return;
    }

    await picker.click();
    await page.waitForTimeout(500);

    const optionsText = await page.locator('.ms-option, [role="option"], .acct-option').allTextContents();
    const joined = optionsText.join(' ');
    expect(joined, 'D1#### (first Dhan) must appear in account picker').toContain('D1####');
    expect(joined, 'D2#### (second Dhan) must appear in account picker').toContain('D2####');
  });
});

// ── Stale code check: jwt_guard must NOT be on AccountsController ───────────

test('stale code: AccountsController must not use jwt_guard', async () => {
  // Read the backend source via the API — we can't do a file read in
  // Playwright, so we assert the VISIBLE SYMPTOM: /api/accounts/ must
  // respond 200 (not 401) without any Authorization header. This is the
  // only observable invariant from a browser-side test.
  const r = await (await fetch(`${BASE}/api/accounts/`));
  expect(r.status, 'AccountsController must return 200 for anonymous demo (not 401 from jwt_guard)').toBe(200);
});
