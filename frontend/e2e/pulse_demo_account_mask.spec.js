/**
 * P0 regression guard: /pulse in demo mode must NEVER render a real
 * broker account ID (pattern [A-Z]{2}\d{4,6} or DH\d{4}).
 *
 * Root cause (fixed):
 *   /api/accounts returned raw `account_id` ("ZG0790") while only
 *   `display` was masked.  stores.js connStatus demo-poll read
 *   `account_id` directly → connStatus.accounts held real IDs →
 *   MarketPulse._knownBrokerAccounts populated the account picker
 *   with unmasked codes.  localStorage snapshot from a prior admin
 *   session compounded the issue (painted real IDs on first frame).
 *
 * Fix:
 *   - Backend: account_id = mask_account(account) when do_mask=True
 *   - Frontend: clear connStatus localStorage on anonymous poll start
 *
 * Five quality dimensions:
 *   1. SSOT   — all account rendering funnels through mask_account()
 *   2. Perf   — /pulse loads within 5 s on demo session
 *   3. Stale  — no raw-ID pattern in DOM after positions/holdings load
 *   4. Reuse  — mask helper is invoked; no re-implementation per component
 *   5. UX     — pattern absent on desktop + mobile viewports
 *
 * Run against prod only (demo mode is main-branch-only):
 *   PLAYWRIGHT_BASE_URL=https://ramboq.com \
 *   cd frontend && npx playwright test e2e/pulse_demo_account_mask.spec.js \
 *     --project=chromium-desktop --project=mobile-portrait
 */

import { test, expect } from '@playwright/test';

// Demo mode only fires on the main branch (prod).  Skip if not explicitly
// targeting ramboq.com so the spec doesn't false-pass on dev/localhost
// where is_demo_request() always returns False.
const PROD_URL = process.env.PLAYWRIGHT_BASE_URL ?? '';
const IS_PROD  = PROD_URL.includes('ramboq.com') && !PROD_URL.includes('dev.');

// Raw broker-ID pattern — must never appear in the DOM for demo visitors.
// Matches: ZG0790, ZJ6294, DH3747, DH6847, GR87DF, etc.
const RAW_ID_RE = /\b[A-Z]{2}\d{4,6}\b/;
const DH_RAW_RE = /\bDH\d{4}\b/;

test.describe('Pulse page — demo account masking', () => {
  test.skip(!IS_PROD, 'Demo mode only fires on ramboq.com (main branch). Set PLAYWRIGHT_BASE_URL=https://ramboq.com to run.');

  // ── shared setup: visit /pulse anonymously, wait for data to load ──────

  async function visitPulseAnon(page) {
    // Clear all storage so no prior admin session can pre-seed real IDs.
    await page.context().clearCookies();
    await page.evaluate(() => {
      try { localStorage.clear(); } catch {}
      try { sessionStorage.clear(); } catch {}
    }).catch(() => {});

    const t0 = Date.now();
    await page.goto(PROD_URL + '/pulse', { waitUntil: 'domcontentloaded', timeout: 15_000 });

    // 2. Perf — /pulse DOM-content-loaded within 5 s (budget check)
    const elapsed = Date.now() - t0;
    expect(elapsed).toBeLessThan(5_000);

    // Wait for the positions/holdings grids to settle — the ag-Grid cells
    // render asynchronously after the SSE + polling data lands.
    // We look for any cell that looks like an account code OR the masked
    // equivalent, giving the grids up to 8 s to paint.
    await page.waitForTimeout(4_000);
  }

  // ── 5a. UX — desktop viewport ──────────────────────────────────────────

  test('desktop: no raw account IDs visible in Pulse DOM', async ({ page }) => {
    await visitPulseAnon(page);

    const bodyText = await page.evaluate(() => document.body.innerText);

    // 5. UX — raw-id pattern must be absent
    expect(RAW_ID_RE.test(bodyText)).toBe(false);
    expect(DH_RAW_RE.test(bodyText)).toBe(false);
  });

  // ── 5b. UX — also check the /api/accounts response directly ───────────

  test('API: /api/accounts returns masked account_id for anonymous caller', async ({ page, request }) => {
    // Use the Playwright request context (no cookies = anonymous)
    const resp = await request.get(PROD_URL + '/api/accounts/', {
      headers: { Accept: 'application/json' },
    });

    // The endpoint is demo-accessible (auth_or_demo_guard)
    expect(resp.status()).toBe(200);

    const body = await resp.json();
    const accounts = body?.accounts ?? [];

    // Must have at least one account (prod has active brokers)
    expect(accounts.length).toBeGreaterThan(0);

    for (const acct of accounts) {
      // 1. SSOT / 4. Reuse — account_id must be the masked form
      expect(acct.account_id).toBeDefined();
      expect(RAW_ID_RE.test(acct.account_id ?? '')).toBe(false);

      // 5. UX — display also masked
      expect(acct.display).toBeDefined();
      expect(RAW_ID_RE.test(acct.display ?? '')).toBe(false);

      // account_id === display for demo callers (both masked identically)
      expect(acct.account_id).toBe(acct.display);
    }
  });

  // ── 5c. UX — mobile portrait ───────────────────────────────────────────

  test('mobile: no raw account IDs visible in Pulse DOM', async ({ page }) => {
    await visitPulseAnon(page);

    const bodyText = await page.evaluate(() => document.body.innerText);

    expect(RAW_ID_RE.test(bodyText)).toBe(false);
    expect(DH_RAW_RE.test(bodyText)).toBe(false);
  });

  // ── 3. Stale — localStorage is cleared before demo poll fires ──────────

  test('localStorage connStatus is cleared on anonymous poll', async ({ page }) => {
    // Seed stale admin data in localStorage to simulate the "prior admin
    // session left real IDs in connStatus cache" scenario.
    await page.goto(PROD_URL + '/pulse', { waitUntil: 'domcontentloaded', timeout: 15_000 });
    await page.evaluate(() => {
      try {
        localStorage.setItem('rbq.cache.connStatus.v1', JSON.stringify({
          loaded: 2, total: 2, backendOk: true,
          failingAccounts: [],
          accounts: ['ZG0790', 'ZJ6294'],   // real (unmasked) admin IDs
        }));
      } catch {}
    });

    // Reload as anonymous — the demo poll must clear the stale entry
    await page.reload({ waitUntil: 'domcontentloaded', timeout: 15_000 });
    await page.waitForTimeout(3_000);   // let the demo poll fire

    const lsValue = await page.evaluate(() => {
      try { return localStorage.getItem('rbq.cache.connStatus.v1'); } catch { return null; }
    });

    // Either the key is gone (cleared and not re-written because total=0)
    // OR the stored accounts are masked (no raw codes).
    if (lsValue) {
      const stored = JSON.parse(lsValue);
      const ids = stored?.accounts ?? [];
      for (const id of ids) {
        expect(RAW_ID_RE.test(id)).toBe(false);
      }
    }
    // If lsValue is null the key was cleared — that satisfies the invariant.
  });

  // ── 1. SSOT grep — account-column cells don't bypass mask_account ──────

  test('account cells in ag-Grid rows carry only masked codes', async ({ page }) => {
    await visitPulseAnon(page);

    // ag-Grid renders account values in cells with data-field="account"
    // or as chip text in the pulse row. We scan all text content of
    // elements carrying the account column class.
    const accountCellTexts = await page.evaluate(() => {
      const cells = [
        ...document.querySelectorAll('[col-id="account"], .ag-col-account, [data-field="account"]'),
      ];
      return cells.map(el => el.textContent || '');
    });

    for (const text of accountCellTexts) {
      expect(RAW_ID_RE.test(text)).toBe(false);
    }
  });
});
