/**
 * Simulator end-to-end suite (post-Option B).
 *
 * Workflow under Option B:
 *   - Navbar dropdown carries persistent master toggles only
 *     (PAPER / LIVE on prod; PAPER on dev). SHADOW removed
 *     from the dropdown to prevent accidental master-toggle flips.
 *   - SIM and REPLAY are transient workspaces accessed via the
 *     "Execution" navbar link → tab strip on /admin/execution.
 *   - Mode chip in navbar reflects the persistent master OR an
 *     active sim/replay driver — read-only when the driver is
 *     running; pickable from the dropdown otherwise.
 *
 * Auth: API-cached login (one /api/auth/login call per process)
 * to stay under prod's 5/min rate limit.
 */

import { test, expect } from '@playwright/test';

// ── auth (cached) ────────────────────────────────────────────────────
const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedAuth = null;

async function authOnce(page) {
  if (!_cachedAuth) {
    let tok = null;
    for (const delay of [0, 20000, 65000]) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      const resp = await page.request.post('/api/auth/login', {
        data: { username: _AUTH_USER, password: _AUTH_PASS },
      });
      if (resp.ok()) { tok = (await resp.json()).access_token; break; }
      if (resp.status() !== 429) throw new Error(`authOnce: /api/auth/login ${resp.status()}`);
    }
    if (!tok) {
      // Rate-limited after 3 attempts — skip rather than hard-fail.
      // Callers don't need to check a return value; test.skip throws internally.
      test.skip(true, 'rate-limited — run in isolation for clean pass');
      return;  // unreachable, but prevents TypeScript complaints
    }
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

/** Navigate to the Simulator workspace via the new Option B path. */
async function gotoSimWorkspace(page) {
  await page.goto('/admin/execution?tab=sim');
  await page.waitForLoadState('domcontentloaded');
  // SimulatorPanel mounts when the Simulator tab is active.
  await expect(page.locator('.exec-tab-active', { hasText: /scenario/i })).toBeVisible({ timeout: 8000 });
}

async function waitForStatus(page, predicate, timeoutMs = 90_000) {
  const start = Date.now();
  let last = null;
  while (Date.now() - start < timeoutMs) {
    const r = await page.request.get('/api/simulator/status');
    if (r.ok()) {
      last = await r.json();
      if (predicate(last)) return last;
    }
    await new Promise((res) => setTimeout(res, 2000));
  }
  throw new Error(`waitForStatus timeout after ${timeoutMs}ms · last=${JSON.stringify(last)}`);
}

// ─────────────────────────────────────────────────────────────────────

test.describe.configure({ mode: 'serial' });
test.setTimeout(200_000);

test.describe('Sim end-to-end — Option B workspace', () => {

  // 1. Login flows through the /signin form (no API shortcut here —
  //    proves the form wiring works for a real visitor).
  test('1: signin form drives /signin → /dashboard', async ({ page }) => {
    await page.goto('/signin');
    const userI = page.locator('#s-user, input[name="username"]').first();
    const passI = page.locator('#s-pass, input[name="password"]').first();
    // The signin page has no <form> tag and the button has no type="submit".
    // The submit button is .btn-primary (the tab-strip Sign In link lacks that class).
    const subBtn = page.locator('button.btn-primary').first();
    await expect(userI).toBeVisible({ timeout: 8000 });

    let ok = false;
    let lastSigninBanner = '';
    for (const delay of [0, 8000, 65000]) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      if (!/\/signin/.test(page.url())) { ok = true; break; }
      await userI.fill(_AUTH_USER);
      await passI.fill(_AUTH_PASS);
      await subBtn.click();
      try {
        await page.waitForURL(/\/(dashboard|performance|auth\/change-password)$/, { timeout: 12000 });
        ok = true; break;
      } catch (_) {
        lastSigninBanner = await page.locator('.pub-banner-error, .error, [role="alert"]').first()
          .textContent().catch(() => '');
      }
    }
    // Rate-limited during back-to-back CI runs — skip rather than fail.
    if (!ok && /(demo mode|feature unavailable)/i.test(lastSigninBanner)) {
      test.skip(true, 'rate-limited — run in isolation for clean pass');
      return;
    }
    expect(ok, `signin form did not redirect within 3 retries — last banner: "${lastSigninBanner}"`).toBeTruthy();
    expect(page.url()).toMatch(/\/dashboard$/);
  });

  // 2. Navbar dropdown shows ONLY persistent master modes.
  //    Prod: PAPER · LIVE. Dev: PAPER.
  //    SHADOW removed (rare-use diagnostic, settings-page toggle only).
  //    SIM + REPLAY removed (transient workspaces, accessed via tab).
  test('2: navbar dropdown shows only master-toggle modes (no SIM/REPLAY/SHADOW)', async ({ page }) => {
    await authOnce(page);
    const modeR = await page.request.get('/api/admin/execution/mode');
    expect(modeR.ok()).toBeTruthy();
    const md = await modeR.json();
    expect(Array.isArray(md.allowed_modes)).toBeTruthy();
    const allowed = md.allowed_modes.map((s) => s.toLowerCase());
    // Hard expectation: SIM and REPLAY MUST be absent.
    expect(allowed).not.toContain('sim');
    expect(allowed).not.toContain('replay');
    // SHADOW removed from dropdown post 25aab45 follow-up.
    expect(allowed).not.toContain('shadow');
    // PAPER always present (the safe default).
    expect(allowed).toContain('paper');
    // LIVE present only on main.
    if (md.branch === 'main') expect(allowed).toContain('live');
    console.log(`[sim_run_end_to_end] dropdown allowed_modes on branch=${md.branch}: ${allowed.join(',')}`);
  });

  // 3. Execution navbar link → /admin/execution → Simulator tab active.
  test('3: Execution navbar → Simulator tab is the default workspace', async ({ page }) => {
    await authOnce(page);
    await page.goto('/dashboard');
    await page.waitForLoadState('domcontentloaded');
    const execLink = page.locator('.algo-nav-btn', { hasText: /^Lab$/ }).first();
    await expect(execLink).toBeVisible({ timeout: 8000 });
    await execLink.click();
    await expect(page).toHaveURL(/\/admin\/execution/, { timeout: 8000 });

    // Tab strip should be visible with Simulator + Replay tabs.
    const simTab = page.locator('.exec-tab', { hasText: /scenario/i }).first();
    const replayTab = page.locator('.exec-tab', { hasText: /backtest/i }).first();
    await expect(simTab).toBeVisible();
    await expect(replayTab).toBeVisible();

    // Simulator tab is active by default (post-25aab45 the page lands on sim).
    await expect(page.locator('.exec-tab-active', { hasText: /scenario/i })).toBeVisible({ timeout: 4000 });
  });

  // 4. Switching to the Replay tab loads ReplayPanel + the explainer chip.
  test('4: Replay tab loads ReplayPanel + explainer chip', async ({ page }) => {
    await authOnce(page);
    await page.goto('/admin/execution');
    await page.waitForLoadState('domcontentloaded');
    const replayTab = page.locator('.exec-tab', { hasText: /backtest/i }).first();
    await replayTab.click();
    await expect(page.locator('.exec-tab-active', { hasText: /backtest/i })).toBeVisible({ timeout: 4000 });
    // The Replay tab's subtitle "historical backtest" distinguishes it from
    // Re-run-iteration. The .exec-tab-subtitle span is always rendered inside
    // every tab button; scope to the active tab so the locator is unique.
    const activeSubtitle = page.locator('.exec-tab-active .exec-tab-subtitle');
    await expect(activeSubtitle).toBeVisible({ timeout: 4000 });
  });

  // 5. SimulatorPanel renders: Inputs MultiSelect with Positions preselected,
  //    Accounts MultiSelect (from /defaults.available_accounts), correlation chip.
  test('5: Simulator form has Inputs · Accounts · correlation chip', async ({ page }) => {
    await authOnce(page);
    await gotoSimWorkspace(page);

    // Inputs MultiSelect — Positions preselected by default.
    const inputsSelect = page.locator('#iter-inputs');
    await expect(inputsSelect).toBeVisible({ timeout: 8000 });
    const inputsText = await inputsSelect.textContent();
    expect((inputsText || '').toLowerCase()).toContain('positions');

    // Accounts MultiSelect — option list comes from /defaults.available_accounts.
    // Empty selection means "all loaded accounts" — placeholder is "(all loaded)".
    const accountsSelect = page.locator('#iter-accounts');
    await expect(accountsSelect).toBeVisible({ timeout: 4000 });

    // Correlation chip — beta pairs from SimDriver._DEFAULT_BETAS.
    const corrChip = page.locator('.iter-corr-chip').first();
    await expect(corrChip).toBeVisible();
    const corrTxt = await corrChip.textContent();
    expect((corrTxt || '').toLowerCase()).toContain('correlation');
    expect(corrTxt || '').toMatch(/β\s*=\s*\d/);
  });

  // 6. iv-crush regime appears in /defaults + Regimes MultiSelect.
  test('6: iv-crush regime is selectable', async ({ page }) => {
    await authOnce(page);
    const dR = await page.request.get('/api/simulator/defaults');
    expect(dR.ok()).toBeTruthy();
    const d = await dR.json();
    const slugs = (d.available_regimes || []).map((r) => r.slug);
    expect(slugs, 'iv-crush regime missing from /defaults').toContain('iv-crush');
  });

  // 7. Section structure always renders (with .sim-empty placeholders
  //    when no live data).
  test('7: Sim panel section headers always visible (or with empty placeholders)', async ({ page }) => {
    await authOnce(page);
    await gotoSimWorkspace(page);

    // The 5 section headers must always be in the DOM, regardless of
    // whether a sim has run — even when empty they render a .sim-empty
    // placeholder below the header.
    for (const label of ['Indices', 'Live activity', 'Underlyings', 'Positions summary', 'Past simulations']) {
      const heading = page.getByText(label, { exact: true });
      const count = await heading.count();
      console.log(`[sim_run_end_to_end] section "${label}" count: ${count}`);
      expect(count, `section "${label}" not visible`).toBeGreaterThan(0);
    }
  });

  // 8. Iteration run end-to-end: API start-run → SIMULATOR banner → completes.
  //    Skipped if the live book has no positions (degenerate run).
  test('8: iteration run completes with finite fees', async ({ page }) => {
    await authOnce(page);

    // Stop / clear any prior sim.
    await page.request.post('/api/simulator/stop').catch(() => null);
    await page.request.post('/api/simulator/clear').catch(() => null);

    const dR = await page.request.get('/api/simulator/defaults');
    expect(dR.ok()).toBeTruthy();
    const d = await dR.json();
    const regimes = d.available_regimes || [];
    expect(regimes.length).toBeGreaterThan(0);
    const firstSlug = regimes[0].slug ?? regimes[0];

    const startR = await page.request.post('/api/simulator/start-run', {
      data: {
        iterations: 1, max_minutes: 2, regimes: [firstSlug],
        agent_ids: null, seed: 42, force_close_on_timeout: true,
        seed_mode: 'live', rate_ms: 2000, spread_pct: 0.10,
      },
    });
    expect(startR.ok(), `start-run returned ${startR.status()}`).toBe(true);
    const startBody = await startR.json();
    if ((startBody.positions_count ?? 0) === 0) {
      test.skip(true, 'Live broker book empty — iteration run degenerate');
      return;
    }

    const final = await waitForStatus(page, (s) => !s.active && !s.run_active, 180_000);
    console.log('[sim_run_end_to_end] iteration final:', JSON.stringify({
      iteration_index: final.iteration_index, iterations_total: final.iterations_total,
    }));

    const itR = await page.request.get('/api/simulator/iterations?limit=5');
    const its = await itR.json();
    const latest = (Array.isArray(its) ? its : its.iterations || [])[0];
    expect(latest, 'no iteration row after run').toBeTruthy();
    expect(latest.end_reason, 'end_reason should be set').toBeTruthy();
    const fees = latest.summary?.total_fees;
    if (fees != null) {
      expect(Number.isFinite(Number(fees)), `fees not finite: ${fees}`).toBeTruthy();
    }
  });

  // 9. Past simulations table + inline Re-run button after a run.
  test('9: Past simulations table renders with Re-run button', async ({ page }) => {
    await authOnce(page);
    await gotoSimWorkspace(page);

    // Either the past-sims table renders (because run 8 just landed an
    // iteration row) OR a .sim-empty placeholder is shown. Both are
    // valid — only check the structural rendering.
    const grid = page.locator('.sim-past-grid');
    const empty = page.locator('.sim-empty');
    const gridCount = await grid.count();
    const emptyCount = await empty.count();
    expect(gridCount + emptyCount, 'no past-sims grid or empty placeholder').toBeGreaterThan(0);

    if (gridCount > 0) {
      const rerunBtns = page.locator('.sim-past-rerun');
      const btnCount = await rerunBtns.count();
      console.log(`[sim_run_end_to_end] Re-run buttons in past-sims grid: ${btnCount}`);
      expect(btnCount, 'no Re-run buttons in past-sims grid').toBeGreaterThan(0);
    }
  });

  // 10. Iteration detail page has "Re-run iteration" button (not "Replay this").
  test('10: iteration detail button reads "Re-run iteration"', async ({ page }) => {
    await authOnce(page);
    const itR = await page.request.get('/api/simulator/iterations?limit=1');
    if (!itR.ok()) {
      test.skip(true, 'iterations API unreachable');
      return;
    }
    const its = await itR.json();
    const row = (Array.isArray(its) ? its : its.iterations || [])[0];
    if (!row) {
      test.skip(true, 'no iterations to inspect');
      return;
    }
    await page.goto(`/admin/simulator/iterations/${row.slug}`);
    await page.waitForLoadState('domcontentloaded');
    const rerunBtn = page.locator('button:has-text("Re-run iteration")').first();
    await expect(rerunBtn).toBeVisible({ timeout: 8000 });
    const oldText = await page.locator('button:has-text("Replay this iteration")').count();
    expect(oldText, 'old "Replay this iteration" text still present').toBe(0);
  });

});
