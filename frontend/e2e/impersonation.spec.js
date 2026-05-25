/**
 * Impersonation feature — support session flow.
 *
 * Run against dev only:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *     npx playwright test e2e/impersonation.spec.js \
 *     --project=chromium-desktop --workers=1
 *
 * Users on dev:
 *   rambo        — admin
 *   ambore       — designated
 *   Subramanian  — admin
 *
 * No partner-role user exists on dev, so:
 *   - rambo (admin) visiting /admin/users → NO 'View as' buttons
 *     (admin can only impersonate partners; none exist)
 *   - ambore (designated) visiting /admin/users → 'View as' on rambo +
 *     Subramanian rows (not on own row)
 *
 * Permission ladder:
 *   designated → can impersonate anyone
 *   admin      → can impersonate partners only
 *   partner    → 403
 *
 * Tests 5 and 6 hit the API directly (rambo credentials only; no
 * dependency on ambore password being available).
 *
 * Tests 2-4 require ambore credentials via PLAYWRIGHT_IMP_PASS env var
 * (or PLAYWRIGHT_USER=ambore + PLAYWRIGHT_PASS=<pass>). When absent
 * they self-skip with an explanatory message.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

// Credentials.
const RAMBO_USER  = 'rambo';
const RAMBO_PASS  = process.env.PLAYWRIGHT_PASS || 'admin1234';

// ambore credentials — optional; tests skip when absent.
const AMBORE_USER = 'ambore';
// Accept PLAYWRIGHT_IMP_PASS as an explicit override; fall back to the
// default PLAYWRIGHT_PASS only when PLAYWRIGHT_USER is 'ambore'.
const AMBORE_PASS = process.env.PLAYWRIGHT_IMP_PASS
  || (process.env.PLAYWRIGHT_USER === 'ambore' ? process.env.PLAYWRIGHT_PASS : null);

const CAN_TEST_AMBORE = !!AMBORE_PASS;

const TIMEOUT = 30_000;

// ─── helpers ──────────────────────────────────────────────────────────────

async function signInAs(page, user, pass) {
  let lastError = '';
  for (const delay of [0, 3000, 8000]) {
    if (delay) await new Promise((r) => setTimeout(r, delay));

    await page.goto('/signin', { waitUntil: 'domcontentloaded' });
    await page.locator('input[name="username"], input#username, input#s-user').first().fill(user);
    await page.locator('input[name="password"], input#password, input#s-pass').first().fill(pass);
    await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();

    try {
      await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
      for (let i = 0; i < 10; i++) {
        const has = await page.evaluate(() => !!sessionStorage.getItem('ramboq_token'));
        if (has) break;
        await new Promise((r) => setTimeout(r, 300));
      }
      const tok = await page.evaluate(() => sessionStorage.getItem('ramboq_token'));
      if (!tok) throw new Error('no token after redirect');
      await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
      return tok;
    } catch (_) {
      lastError = await page.locator('.pub-banner-error, .error, [role="alert"]')
        .first().textContent({ timeout: 2000 }).catch(() => '') || `no redirect after ${user}`;
      if (!/(rate|429|too many|demo mode|feature unavailable)/i.test(lastError)) break;
    }
  }
  throw new Error(`signInAs(${user}) failed: ${lastError}`);
}

async function getToken(page) {
  return page.evaluate(() => sessionStorage.getItem('ramboq_token'));
}

// ─── Group A: rambo-only (API + no-partner check) ─────────────────────────
// These run unconditionally — no ambore credentials required.

test.describe('Impersonation — rambo-only checks', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(90_000);

  test('1 · admin (rambo) on /admin/users: no View-as buttons (no partners on dev)', async ({ page }) => {
    await loginAsAdmin(page, { user: RAMBO_USER, pass: RAMBO_PASS });
    await page.goto('/admin/users', { waitUntil: 'domcontentloaded' });

    await page.locator('table tbody tr, .user-row, .ag-row').first()
      .waitFor({ state: 'attached', timeout: TIMEOUT }).catch(() => {});
    await page.waitForLoadState('networkidle').catch(() => {});

    const viewAsBtns = page.locator('button:has-text("View as"), a:has-text("View as"), [data-action="impersonate"]');
    const count = await viewAsBtns.count();
    console.log(`[imp] test1: View-as button count for rambo = ${count}`);

    expect(count, 'admin (rambo) should see no View-as buttons when no partners exist').toBe(0);
  });

  test('5 · API: admin (rambo) → admin (Subramanian) returns 403', async ({ page }) => {
    await loginAsAdmin(page, { user: RAMBO_USER, pass: RAMBO_PASS });

    const token = await getToken(page);
    expect(token, 'rambo token required').toBeTruthy();

    const resp = await page.request.post('/api/auth/impersonate/Subramanian', {
      headers: { Authorization: `Bearer ${token}` },
    });
    console.log(`[imp] test5: admin→admin status = ${resp.status()}`);

    expect(resp.status(), 'admin impersonating admin should be 403').toBe(403);

    const body = await resp.json().catch(() => ({}));
    const detail = body?.detail || '';
    console.log(`[imp] test5: detail = "${detail}"`);
    expect(detail, 'error detail should mention partners').toMatch(/partner/i);
  });

  test('6 · API: self-impersonation (rambo → rambo) returns 422', async ({ page }) => {
    await loginAsAdmin(page, { user: RAMBO_USER, pass: RAMBO_PASS });

    const token = await getToken(page);
    expect(token, 'rambo token required').toBeTruthy();

    const resp = await page.request.post('/api/auth/impersonate/rambo', {
      headers: { Authorization: `Bearer ${token}` },
    });
    console.log(`[imp] test6: self-impersonation status = ${resp.status()}`);

    expect(resp.status(), 'self-impersonation should return 422').toBe(422);

    const body = await resp.json().catch(() => ({}));
    const detail = body?.detail || '';
    console.log(`[imp] test6: detail = "${detail}"`);
    expect(detail, 'error detail should mention "yourself"').toMatch(/yourself/i);
  });
});

// ─── Group B: ambore (designated) flows ───────────────────────────────────
// Serial within the group; skip as a block when ambore password is absent.

test.describe('Impersonation — designated (ambore) flows', () => {
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(120_000);

  test('2 · designated (ambore) on /admin/users: View-as on other users, not own row', async ({ page }) => {
    if (!CAN_TEST_AMBORE) {
      test.skip(true, 'PLAYWRIGHT_IMP_PASS not set — skip; supply PLAYWRIGHT_IMP_PASS=<ambore-pass>');
    }

    await signInAs(page, AMBORE_USER, AMBORE_PASS);
    await page.goto('/admin/users', { waitUntil: 'domcontentloaded' });

    await page.locator('table tbody tr, .user-row, .ag-row').first()
      .waitFor({ state: 'attached', timeout: TIMEOUT }).catch(() => {});
    await page.waitForLoadState('networkidle').catch(() => {});

    const viewAsBtns = page.locator('button:has-text("View as"), a:has-text("View as"), [data-action="impersonate"]');
    const count = await viewAsBtns.count();
    console.log(`[imp] test2: View-as button count for ambore = ${count}`);

    expect(count, 'designated (ambore) should see View-as buttons for other users').toBeGreaterThanOrEqual(2);

    // Own row must have no View-as.
    const ownRow = page.locator('tr, .user-row, .ag-row').filter({ hasText: /\bambore\b/i }).first();
    if (await ownRow.count() > 0) {
      const ownBtn = ownRow.locator('button:has-text("View as"), a:has-text("View as"), [data-action="impersonate"]');
      await expect(ownBtn, "ambore's own row must not have a View-as button").toHaveCount(0);
    }

    // Screenshot (b) — users table with View-as buttons visible.
    await page.screenshot({ path: 'test-results/imp-users-viewas.png', fullPage: false });
    console.log('[imp] test2: screenshot → test-results/imp-users-viewas.png');
  });

  test('3 · ambore clicks View-as rambo: yellow banner + End button visible', async ({ page }) => {
    if (!CAN_TEST_AMBORE) {
      test.skip(true, 'PLAYWRIGHT_IMP_PASS not set — skip');
    }

    await signInAs(page, AMBORE_USER, AMBORE_PASS);
    await page.goto('/admin/users', { waitUntil: 'domcontentloaded' });

    await page.locator('table tbody tr, .user-row, .ag-row').first()
      .waitFor({ state: 'attached', timeout: TIMEOUT }).catch(() => {});
    await page.waitForLoadState('networkidle').catch(() => {});

    // Click 'View as' on rambo's row.
    const ramboRow = page.locator('tr, .user-row, .ag-row').filter({ hasText: /\brambo\b/i }).first();
    const viewAsBtn = ramboRow.locator('button:has-text("View as"), a:has-text("View as"), [data-action="impersonate"]').first();
    await expect(viewAsBtn, 'View-as button for rambo not found').toBeVisible({ timeout: TIMEOUT });

    await viewAsBtn.click();

    // The page navigates after successful impersonation.
    await page.waitForURL(/\/(pulse|dashboard|performance|admin)/, { timeout: TIMEOUT });

    // Yellow banner must be present.
    const banner = page.locator(
      '.imp-banner, .impersonation-banner, [data-testid="imp-banner"], ' +
      '.support-session-banner, [class*="imp"][class*="banner"]'
    ).first();
    // Fallback: text-content match.
    const bannerFallback = page.locator(
      ':text("Support session"), :text("viewing as rambo"), :text("viewing as"), :text("Viewing as")'
    ).first();

    let bannerEl = banner;
    if (await banner.count() === 0) bannerEl = bannerFallback;

    await expect(bannerEl, 'impersonation banner must be visible').toBeVisible({ timeout: TIMEOUT });

    const bannerText = await bannerEl.textContent();
    console.log(`[imp] test3: banner = "${bannerText?.trim()}"`);
    expect(bannerText, 'banner should name the target (rambo)').toMatch(/rambo/i);
    expect(bannerText, 'banner should name the actor (ambore)').toMatch(/ambore/i);

    // End session button.
    const endBtn = page.locator('button:has-text("End session"), button:has-text("End"), a:has-text("End session")').first();
    await expect(endBtn, '"End session" button must be visible').toBeVisible({ timeout: TIMEOUT });

    // sessionStorage should have original token stashed.
    const origToken = await page.evaluate(() => sessionStorage.getItem('ramboq_orig_token'));
    console.log(`[imp] test3: ramboq_orig_token present = ${!!origToken}`);
    expect(origToken, 'ramboq_orig_token should be set during impersonation').toBeTruthy();

    // Screenshot (a) — active impersonation banner.
    await page.screenshot({ path: 'test-results/imp-banner-active.png', fullPage: false });
    console.log('[imp] test3: screenshot → test-results/imp-banner-active.png');
  });

  test('4 · End session: banner gone, back to /admin/users, ambore token restored', async ({ page }) => {
    if (!CAN_TEST_AMBORE) {
      test.skip(true, 'PLAYWRIGHT_IMP_PASS not set — skip');
    }

    const origToken = await signInAs(page, AMBORE_USER, AMBORE_PASS);
    await page.goto('/admin/users', { waitUntil: 'domcontentloaded' });

    await page.locator('table tbody tr, .user-row, .ag-row').first()
      .waitFor({ state: 'attached', timeout: TIMEOUT }).catch(() => {});
    await page.waitForLoadState('networkidle').catch(() => {});

    const ramboRow = page.locator('tr, .user-row, .ag-row').filter({ hasText: /\brambo\b/i }).first();
    const viewAsBtn = ramboRow.locator('button:has-text("View as"), a:has-text("View as"), [data-action="impersonate"]').first();
    await expect(viewAsBtn).toBeVisible({ timeout: TIMEOUT });
    await viewAsBtn.click();

    await page.waitForURL(/\/(pulse|dashboard|performance|admin)/, { timeout: TIMEOUT });

    // Confirm session started.
    const endBtn = page.locator('button:has-text("End session"), button:has-text("End"), a:has-text("End session")').first();
    await expect(endBtn, '"End session" must exist before clicking').toBeVisible({ timeout: TIMEOUT });

    // Confirm stash.
    const stashedOrig = await page.evaluate(() => sessionStorage.getItem('ramboq_orig_token'));
    expect(stashedOrig, 'ramboq_orig_token must be stashed').toBeTruthy();

    // Click End session and expect the stop-impersonate API to be called.
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/auth/stop-impersonate') && r.status() === 200,
        { timeout: TIMEOUT }
      ),
      endBtn.click(),
    ]);

    // Should navigate back to /admin/users.
    await page.waitForURL(/\/admin\/users/, { timeout: TIMEOUT });

    // Banner must be gone.
    const impBanner = page.locator('.imp-banner, .impersonation-banner, [data-testid="imp-banner"], .support-session-banner');
    const textBanner = page.locator(':text("Support session"), :text("viewing as")');
    const remainingCount = await impBanner.count() + await textBanner.count();
    console.log(`[imp] test4: remaining banner count = ${remainingCount}`);
    expect(remainingCount, 'impersonation banner should be gone after End session').toBe(0);

    // sessionStorage: orig_token must be cleared.
    const afterOrig = await page.evaluate(() => sessionStorage.getItem('ramboq_orig_token'));
    expect(afterOrig, 'ramboq_orig_token must be cleared').toBeNull();

    // Active token should be restored to ambore's original.
    const tokenAfter = await getToken(page);
    expect(tokenAfter, 'active token should be restored to ambore original').toBe(origToken);
  });
});
