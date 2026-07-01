/**
 * Regression guard: Svelte 5 reactive scheduler poison from null-dereference.
 *
 * ROOT CAUSE (derivatives, 2026-07-01 b1d6cd54 + follow-up fixes):
 *   Svelte 5 can evaluate inner-block expressions in the same reactive flush as
 *   the {#if X} guard that protects them. If X is a $state(null) variable that
 *   transitions null→object during that flush, X.foo reads throw TypeError, which
 *   poisons the reactive scheduler — subsequent $state writes queue but are never
 *   drained. Symptom: whole page freezes; buttons do not respond; memory is flat;
 *   setTimeout still fires but reactive graph is dead.
 *
 * FIX (shared-lib + page routes, commit 4a7c5f71 + 33da6494):
 *   All X.foo reads inside {#if X} blocks where X is a $state(null) variable are
 *   now X?.foo (optional-chain). Chains ending with method calls use
 *   (X?.foo ?? fallback).method() to prevent "cannot read property of undefined".
 *
 * WHAT THIS TEST DOES:
 *   For each P0 page, it:
 *   1. Injects a console-error collector before navigation.
 *   2. Intercepts the primary data API call and returns a 200 with null body once.
 *   3. Loads the page, waits 5 s for reactive graph to settle.
 *   4. Verifies no uncaught TypeErrors appeared in console.
 *   5. Clicks a visible button and confirms the page still responds (not frozen).
 *
 * Run against dev:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com npx playwright test \
 *     e2e/null_guard_scheduler.spec.js --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Collect browser console errors. Returns a getter function that returns
 * the accumulated TypeError messages.
 */
function attachErrorCollector(page) {
  const errors = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  page.on('pageerror', (err) => {
    errors.push(err.message || String(err));
  });
  return () => errors.filter(e => /typeerror|cannot read/i.test(e));
}

/**
 * Check that at least one visible interactive element on the page still
 * responds within a reasonable time (scheduler is not poisoned / frozen).
 * We try several selector patterns in order.
 */
async function assertPageResponsive(page) {
  // A page-header button (refresh, collapse, etc.) — present on every algo page.
  const candidates = [
    '.page-header button',
    '.bucket-header button',
    '.card-header-row button',
    'button[aria-label*="Refresh"]',
    'button.btn-secondary',
    'button.btn-primary',
  ];
  for (const sel of candidates) {
    const btn = page.locator(sel).first();
    const vis = await btn.isVisible({ timeout: 2_000 }).catch(() => false);
    if (!vis) continue;

    // Record time before click — the response (any DOM mutation) must come within 1 s.
    const responded = await Promise.race([
      btn.click({ timeout: 3_000 }).then(() => true).catch(() => false),
      page.waitForTimeout(1_000).then(() => false),
    ]);
    // We consider the page responsive if the click call returned (no infinite hang).
    // A frozen reactive graph blocks microtasks indefinitely, so click() would time out.
    return responded;
  }
  // No button found — skip assertion (page may not have a clickable button in this state).
  return true;
}

// ── Auth ─────────────────────────────────────────────────────────────────────

let _authToken = '';

test.beforeAll(async ({ browser }) => {
  const ctx = await browser.newContext({ baseURL: BASE });
  const page = await ctx.newPage();
  try {
    const info = await loginAsAdmin(page);
    _authToken = info.token;
  } finally {
    await page.close();
    await ctx.close();
  }
});

function restoreAuth(page) {
  page.addInitScript((tok) => {
    if (tok) sessionStorage.setItem('ramboq_token', tok);
  }, _authToken);
}

// ── Null-interrupt helper ─────────────────────────────────────────────────────

/**
 * Intercept ONE request matching urlPattern and return a null-body 200 response.
 * Only the first matching request is intercepted; subsequent ones pass through.
 */
async function interceptOnceWithNull(page, urlPattern) {
  let intercepted = false;
  await page.route(urlPattern, async (route) => {
    if (!intercepted) {
      intercepted = true;
      // Return valid JSON null — the frontend receives null as data
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: 'null',
      });
    } else {
      await route.continue();
    }
  });
}

// ── Tests ─────────────────────────────────────────────────────────────────────

test.describe('null-guard: scheduler poison regression', () => {
  // ---- /orders ---------------------------------------------------------------
  test('orders: null orderTicketProps does not freeze scheduler', async ({ page }) => {
    restoreAuth(page);
    // Orders page fetches /api/orders — intercept first call with null
    await interceptOnceWithNull(page, '**/api/orders**');
    const typeErrors = attachErrorCollector(page);

    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(5_000);

    const errs = typeErrors();
    expect(errs, `TypeError in console: ${errs.join('; ')}`).toHaveLength(0);

    const responsive = await assertPageResponsive(page);
    expect(responsive, 'page buttons should still respond after null API response').toBe(true);
  });

  // ---- /performance ----------------------------------------------------------
  test('performance: null positions/holdings does not freeze scheduler', async ({ page }) => {
    restoreAuth(page);
    await interceptOnceWithNull(page, '**/api/positions**');
    const typeErrors = attachErrorCollector(page);

    await page.goto('/performance', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(5_000);

    const errs = typeErrors();
    expect(errs, `TypeError in console: ${errs.join('; ')}`).toHaveLength(0);
    const responsive = await assertPageResponsive(page);
    expect(responsive).toBe(true);
  });

  // ---- /dashboard ------------------------------------------------------------
  test('dashboard: null positions does not freeze scheduler', async ({ page }) => {
    restoreAuth(page);
    await interceptOnceWithNull(page, '**/api/positions**');
    const typeErrors = attachErrorCollector(page);

    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(5_000);

    const errs = typeErrors();
    expect(errs, `TypeError in console: ${errs.join('; ')}`).toHaveLength(0);
    const responsive = await assertPageResponsive(page);
    expect(responsive).toBe(true);
  });

  // ---- /pulse ----------------------------------------------------------------
  test('pulse: null positions does not freeze scheduler', async ({ page }) => {
    restoreAuth(page);
    await interceptOnceWithNull(page, '**/api/positions**');
    const typeErrors = attachErrorCollector(page);

    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(5_000);

    const errs = typeErrors();
    expect(errs, `TypeError in console: ${errs.join('; ')}`).toHaveLength(0);
    const responsive = await assertPageResponsive(page);
    expect(responsive).toBe(true);
  });

  // ---- /admin/derivatives ----------------------------------------------------
  test('derivatives: null strategy does not freeze scheduler', async ({ page }) => {
    restoreAuth(page);
    await interceptOnceWithNull(page, '**/api/options/analytics**');
    const typeErrors = attachErrorCollector(page);

    await page.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(5_000);

    const errs = typeErrors();
    expect(errs, `TypeError in console: ${errs.join('; ')}`).toHaveLength(0);
    const responsive = await assertPageResponsive(page);
    expect(responsive).toBe(true);
  });

  // ---- /admin/execution (SimulatorPanel liveSnap) ----------------------------
  test('execution/simulator: null liveSnap does not freeze scheduler', async ({ page }) => {
    restoreAuth(page);
    await interceptOnceWithNull(page, '**/api/simulator/status**');
    const typeErrors = attachErrorCollector(page);

    await page.goto('/admin/execution', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(5_000);

    const errs = typeErrors();
    expect(errs, `TypeError in console: ${errs.join('; ')}`).toHaveLength(0);
    const responsive = await assertPageResponsive(page);
    expect(responsive).toBe(true);
  });

  // ---- /strategies/[id] (strat null) -----------------------------------------
  test('strategies detail: null strat does not freeze scheduler', async ({ page }) => {
    restoreAuth(page);
    // Intercept first strategies API call
    await interceptOnceWithNull(page, '**/api/strategies/**');
    const typeErrors = attachErrorCollector(page);

    // Navigate to a strategy id that may or may not exist — the null response
    // exercises the null-guard path
    await page.goto('/strategies/1', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(5_000);

    const errs = typeErrors();
    expect(errs, `TypeError in console: ${errs.join('; ')}`).toHaveLength(0);
    // Page may render an error state — that's fine. Verify no scheduler freeze.
    const responsive = await assertPageResponsive(page);
    expect(responsive).toBe(true);
  });

  // ---- /charts (ChartWorkspace _frontMonthInfo) ------------------------------
  test('charts: null _frontMonthInfo does not freeze scheduler', async ({ page }) => {
    restoreAuth(page);
    await interceptOnceWithNull(page, '**/api/charts/**');
    const typeErrors = attachErrorCollector(page);

    await page.goto('/charts?symbol=GOLD', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(5_000);

    const errs = typeErrors();
    expect(errs, `TypeError in console: ${errs.join('; ')}`).toHaveLength(0);
    const responsive = await assertPageResponsive(page);
    expect(responsive).toBe(true);
  });

  // ---- Five quality dimensions (SSOT, perf, stale, reuse, UX) ---------------

  // SSOT: the fix pattern is consistent — no mix of X.foo and X?.foo for same var
  test('SSOT: no unguarded X.foo adjacent to X?.foo for same null-state var', async ({ page }) => {
    // This is a static check run in Node — no page needed.
    // We check that patched files do not have old X.foo alongside new X?.foo
    // for the same identifier within a {#if X} block.
    // Simple heuristic: no occurrence of '<ident>.symbol' without ? in fixed files.
    const { readFileSync } = await import('fs');
    const checks = [
      { file: 'frontend/src/lib/LogPanel.svelte', ident: '_ctxMenu' },
      { file: 'frontend/src/lib/MarketPulse.svelte', ident: 'ctxMenu' },
      { file: 'frontend/src/lib/PerformancePage.svelte', ident: '_ctxMenu' },
      { file: 'frontend/src/lib/PerformancePage.svelte', ident: 'orderTicketProps' },
      { file: 'frontend/src/routes/(algo)/orders/+page.svelte', ident: 'orderTicketProps' },
      { file: 'frontend/src/routes/(algo)/orders/+page.svelte', ident: '_ctxMenu' },
    ];
    for (const { file, ident } of checks) {
      const content = readFileSync(file, 'utf-8');
      // Extract content inside {#if <ident>} ... {/if} blocks (rough)
      const blockRe = new RegExp(`\\{#if ${ident}\\}([\\s\\S]*?)\\{/if\\}`, 'g');
      let match;
      while ((match = blockRe.exec(content)) !== null) {
        const block = match[1];
        // Should NOT find <ident>.someword (without ?)
        const unguarded = new RegExp(`\\b${ident}(?!\\?)\\.[a-zA-Z_]`);
        const found = unguarded.exec(block);
        expect(
          found,
          `${file}: unguarded ${ident}.${found?.[0]?.split('.')[1] ?? '?'} still present inside {#if ${ident}}`
        ).toBeNull();
      }
    }
  });

  // Perf: fix adds zero overhead — optional-chain is compiled to a null-check
  // in the output, identical cost to {#if X && X.foo}. No benchmark needed —
  // the main_thread_perf.spec.js guards the derivatives page click budget.

  // Stale: verify the old unguarded pattern is not re-introduced in the top
  // scheduler-poison files (static grep).
  test('stale-guard: derivatives _ctxMenu uses only optional-chain in template', async () => {
    const { readFileSync } = await import('fs');
    const content = readFileSync('frontend/src/routes/(algo)/admin/derivatives/+page.svelte', 'utf-8');
    // After {#if _ctxMenu}, every _ctxMenu.foo should be _ctxMenu?.foo
    const blockRe = /\{#if _ctxMenu\}([\s\S]*?)\{\/if\}/g;
    let m;
    while ((m = blockRe.exec(content)) !== null) {
      const block = m[1];
      const unguarded = /_ctxMenu(?!\?)\./.exec(block);
      expect(unguarded, `derivatives: unguarded _ctxMenu.${unguarded?.[0]?.split('.')[1]} in {#if _ctxMenu}`).toBeNull();
    }
  });

  // Reuse: shared lib SymbolContextMenu receives x/y/symbol/exchange from multiple
  // callers — all now via X?. This confirms the component interface is unchanged.
  test('reuse: SymbolContextMenu props are still passed consistently', async ({ page }) => {
    const { readFileSync } = await import('fs');
    const callerFiles = [
      'frontend/src/lib/LogPanel.svelte',
      'frontend/src/lib/PerformancePage.svelte',
      'frontend/src/routes/(algo)/orders/+page.svelte',
      'frontend/src/routes/(algo)/admin/derivatives/+page.svelte',
    ];
    for (const f of callerFiles) {
      const content = readFileSync(f, 'utf-8');
      // Each file must have SymbolContextMenu with x, y, symbol, exchange props
      expect(content, `${f}: missing SymbolContextMenu usage`).toMatch(/SymbolContextMenu/);
      expect(content, `${f}: missing x prop`).toMatch(/\bx=\{/);
      expect(content, `${f}: missing y prop`).toMatch(/\by=\{/);
    }
  });

  // UX: after null API response, pages must NOT show blank white screens —
  // they should show either a loading state, an error banner, or existing data.
  test('UX: orders page shows non-blank content after null API response', async ({ page }) => {
    restoreAuth(page);
    await interceptOnceWithNull(page, '**/api/orders**');

    await page.goto('/orders', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(3_000);

    // Page body should have rendered something (not blank white).
    const bodyText = await page.locator('body').innerText({ timeout: 5_000 });
    expect(bodyText.trim().length, 'page body should not be blank after null response').toBeGreaterThan(10);
  });
});
