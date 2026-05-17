/**
 * Signin → admin landing flow.
 *
 * Verifies the path a logged-out operator takes to land in the admin
 * site:
 *   1. Navigate to /signin
 *   2. Fill username + password, click Sign In
 *   3. Land on /dashboard (admin/designated role)
 *   4. Mode chip reads LIVE (the seeded default — paper_trading_mode
 *      AND shadow_mode both False)
 *   5. DEMO badge does NOT appear (auth store populated → isDemo false)
 *   6. Navbar shows admin-only links (Settings / Brokers / Users)
 *
 * Headless by default per durable rule.
 */

import { test, expect } from '@playwright/test';

const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test.describe.serial('Signin flow → admin landing', () => {
  test.setTimeout(90_000);

  test('Form login → /dashboard → LIVE chip, no DEMO badge, admin navbar', async ({ page }) => {
    // Start fresh — no sessionStorage, no cookies. Visit /signin
    // directly (not /dashboard, since the algo layout's auth $effect
    // would bounce us to /signin anyway).
    await page.goto('/signin');

    // The form has #s-user, #s-pass, button.btn-primary based on
    // recent fixture work. Fall back to label-based selectors for
    // robustness.
    const userInput = page.locator('#s-user, input[name="username"], input[type="text"]').first();
    const passInput = page.locator('#s-pass, input[name="password"], input[type="password"]').first();
    // Scope to the FORM submit button, not the navbar "Sign In" CTA
    // that's on the public site header. The page has two "Sign In"
    // buttons — top-right nav (which is just an anchor) and the
    // form submit. Anchor to the form's submit type explicitly.
    const submitBtn = page.locator('form button[type="submit"]').first();

    await expect(userInput).toBeVisible({ timeout: 8000 });
    await expect(passInput).toBeVisible();
    await expect(submitBtn).toBeVisible();

    // Retry on rate limit (5/min on prod).
    let landed = false;
    let lastBanner = '';
    for (const delay of [0, 8000, 20000]) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      if (await page.url().includes('/signin') === false) { landed = true; break; }

      await userInput.fill(_AUTH_USER);
      await passInput.fill(_AUTH_PASS);
      await submitBtn.click();

      try {
        await page.waitForURL(/\/(dashboard|performance|auth\/change-password)$/, { timeout: 12000 });
        landed = true;
        break;
      } catch (_) {
        lastBanner = await page.locator('.error, [role="alert"]').first().textContent().catch(() => '');
        if (!/(rate|429|too many|demo mode)/i.test(lastBanner || '')) break;
      }
    }
    expect(landed, `signin did not redirect — last banner: ${lastBanner}`).toBeTruthy();

    // For admin/designated users we should land on /dashboard.
    expect(page.url()).toMatch(/\/dashboard$/);

    // Wait for the (algo) layout to hydrate — the navbar mode chip is
    // a reliable marker.
    const modeChip = page.locator('.mode-trigger').first();
    await expect(modeChip).toBeVisible({ timeout: 10000 });

    // Allow a brief moment for /api/admin/execution/mode to land + the
    // chip to update from the optimistic fallback to the real value.
    await page.waitForTimeout(2000);

    // 1. Mode chip text should be LIVE (paper_trading_mode=false +
    //    shadow_mode=false → resolver returns 'live').
    const chipText = await modeChip.textContent();
    const dataMode = await modeChip.getAttribute('data-mode');
    console.log(`[signin_flow] mode chip text=${JSON.stringify(chipText)} data-mode=${dataMode}`);
    expect(['live', 'sim', 'replay'], `expected LIVE chip; got data-mode='${dataMode}'`)
      .toContain(dataMode);
    expect(dataMode, 'expected LIVE chip after login on prod').toBe('live');

    // 2. DEMO badge must NOT be visible (auth store is populated, so
    //    isDemo = !$authStore.user && branch === 'main' is false).
    const demoBadges = await page.locator('.algo-mode-demo, [title*="demo mode" i]').count();
    console.log(`[signin_flow] demo badges visible: ${demoBadges}`);
    expect(demoBadges, 'DEMO badge should not show for logged-in admin/designated user').toBe(0);

    // 3. Admin-only navbar links should be present (Settings, Brokers,
    //    Users). These are filtered out in demo mode via adminOnly.
    const settingsLink = page.locator('.algo-nav-btn', { hasText: /^Settings$/ }).first();
    const brokersLink  = page.locator('.algo-nav-btn', { hasText: /^Brokers$/  }).first();
    const usersLink    = page.locator('.algo-nav-btn', { hasText: /^Users$/    }).first();
    await expect(settingsLink, 'Settings nav link missing — operator still in demo?').toBeVisible({ timeout: 4000 });
    await expect(brokersLink,  'Brokers nav link missing — operator still in demo?').toBeVisible({ timeout: 4000 });
    await expect(usersLink,    'Users nav link missing — operator still in demo?').toBeVisible({ timeout: 4000 });

    console.log('[signin_flow] PASS — LIVE chip, no DEMO, admin nav visible');
  });
});
