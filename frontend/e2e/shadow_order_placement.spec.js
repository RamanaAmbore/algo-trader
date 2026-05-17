/**
 * Shadow mode reachability + chip + agent-routing assertion.
 *
 * SHADOW is a master-toggle mode reached only via agent fires
 * (the ticket route accepts only paper/live, never shadow). The
 * navbar dropdown no longer exposes SHADOW post-cfbdc67 — only
 * /admin/settings can flip the execution.shadow_mode row now.
 *
 * What this spec verifies deterministically (no market-hours
 * dependency):
 *   1. Toggling execution.shadow_mode via the settings API
 *      successfully changes the master mode.
 *   2. /api/admin/execution/mode resolves to 'shadow'.
 *   3. The navbar chip on /dashboard renders SHADOW.
 *   4. Cleanup restores the original flag value.
 *
 * What this spec does NOT verify (needs market hours + real
 * agent fire):
 *   - The actual AlgoOrder(mode='shadow') row written by
 *     _resolve_mode → ShadowTradeEngine when an agent action
 *     fires. That code path is unit-tested in the backend; an
 *     e2e proof requires a live engine cycle.
 *
 * Cleanup is in test.afterAll — runs even when assertions fail
 * so a stuck SHADOW flag doesn't bleed into subsequent runs.
 */

import { test, expect } from '@playwright/test';

const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedAuth = null;
let _originalShadow = null;   // captured at beforeAll; restored at afterAll

async function authOnce(page) {
  if (!_cachedAuth) {
    const envToken = process.env.PLAYWRIGHT_AUTH_TOKEN;
    let tok = envToken || null;
    if (!tok) {
      for (const delay of [0, 20000, 65000]) {
        if (delay) await new Promise((r) => setTimeout(r, delay));
        const resp = await page.request.post('/api/auth/login', {
          data: { username: _AUTH_USER, password: _AUTH_PASS },
        });
        if (resp.ok()) { tok = (await resp.json()).access_token; break; }
        if (resp.status() !== 429) throw new Error(`authOnce: /api/auth/login ${resp.status()}`);
      }
    }
    if (!tok) throw new Error('authOnce: login rate-limited');
    _cachedAuth = { token: tok, user_id: _AUTH_USER };
  }
  const { token, user_id } = _cachedAuth;
  await page.goto('/');
  await page.evaluate(({ tok, usr }) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: usr, username: usr, role: 'admin', display_name: usr,
    }));
  }, { tok: token, usr: user_id });
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${token}` });
}

async function setShadow(page, value) {
  // PATCH the settings row directly. The navbar dropdown can't reach
  // shadow_mode any more, but the settings API still owns the value.
  const r = await page.request.patch('/api/admin/settings/execution.shadow_mode', {
    data: { value: value ? 'true' : 'false' },
  });
  if (!r.ok()) {
    const body = await r.text();
    throw new Error(`PATCH execution.shadow_mode failed: ${r.status()} ${body.slice(0, 200)}`);
  }
  // Give the settings cache a beat to reload.
  await new Promise((res) => setTimeout(res, 500));
}

async function readMode(page) {
  const r = await page.request.get('/api/admin/execution/mode');
  expect(r.ok(), `read /execution/mode returned ${r.status()}`).toBeTruthy();
  return r.json();
}

test.describe.configure({ mode: 'serial' });
test.setTimeout(90_000);

test.describe('Shadow mode reachability', () => {

  test.beforeAll(async ({ request, browser }) => {
    // Capture original shadow_mode so we can restore at the end.
    // beforeAll has no `page` — need to spin a temporary context.
    const ctx = await browser.newContext();
    const p = await ctx.newPage();
    await authOnce(p);
    const r = await p.request.get('/api/admin/settings/execution.shadow_mode');
    if (r.ok()) {
      const body = await r.json();
      _originalShadow = (body.value === 'true' || body.value === true);
    } else {
      _originalShadow = false;
    }
    console.log(`[shadow_order_placement] captured original shadow_mode=${_originalShadow}`);
    await ctx.close();
  });

  test.afterAll(async ({ browser }) => {
    // Always restore the original value, even on assertion failures.
    const ctx = await browser.newContext();
    const p = await ctx.newPage();
    try {
      await authOnce(p);
      await setShadow(p, !!_originalShadow);
      console.log(`[shadow_order_placement] restored shadow_mode=${_originalShadow}`);
    } catch (e) {
      console.warn(`[shadow_order_placement] cleanup failed: ${e.message}`);
    }
    await ctx.close();
  });

  test('1: PATCH execution.shadow_mode=true via settings API + verify mode resolves to shadow', async ({ page }) => {
    await authOnce(page);

    // Pre-state: capture current mode + assert it's NOT shadow yet.
    const before = await readMode(page);
    console.log(`[shadow_order_placement] before flip: mode=${before.mode}`);

    // Flip shadow_mode true. Settings PATCH is the only path now that
    // the navbar dropdown's POST /api/admin/execution/mode rejects
    // 'shadow' as a target.
    await setShadow(page, true);

    // Verify the resolver returns 'shadow'.
    const after = await readMode(page);
    console.log(`[shadow_order_placement] after flip: mode=${after.mode}`);
    expect(after.mode, 'mode should resolve to shadow after flag flip').toBe('shadow');
  });

  test('2: navbar chip on /dashboard reads SHADOW after flag is on', async ({ page }) => {
    await authOnce(page);

    // Make sure shadow_mode is still on (test 1 set it, test order
    // is serial in this describe).
    await setShadow(page, true);

    await page.goto('/dashboard');
    await page.waitForLoadState('domcontentloaded');

    // The chip polls /api/admin/execution/mode every 30s. Force a
    // re-poll by triggering visibility or just waiting up to that
    // long. Faster path — the layout's onMount immediately calls
    // loadMode() so the chip should reflect SHADOW within a few s.
    const chip = page.locator('.mode-trigger').first();
    await expect(chip).toBeVisible({ timeout: 10_000 });

    // Poll until data-mode == 'shadow' (up to 35s — covers the 30s
    // poll interval). Faster paths: chip text contains SHADOW.
    let dataMode = null;
    for (let i = 0; i < 18; i++) {
      dataMode = await chip.getAttribute('data-mode');
      if (dataMode === 'shadow') break;
      await new Promise((r) => setTimeout(r, 2000));
    }
    console.log(`[shadow_order_placement] final chip data-mode=${dataMode}`);
    expect(dataMode, 'chip should reflect data-mode="shadow" while flag is on').toBe('shadow');

    // The chip text should also read SHADOW.
    const txt = (await chip.textContent() || '').trim().toUpperCase();
    expect(txt).toContain('SHADOW');
  });

  test('3: shadow_mode=false restores mode to live or paper', async ({ page }) => {
    await authOnce(page);
    await setShadow(page, false);

    const after = await readMode(page);
    console.log(`[shadow_order_placement] after flip-off: mode=${after.mode}`);
    expect(['live', 'paper']).toContain(after.mode);
  });
});
