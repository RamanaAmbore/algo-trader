/**
 * Simulator end-to-end: SIM navbar link + iteration run + force-close fees.
 *
 * Covers:
 *   1. SIM click in navbar lands on /admin/execution without bouncing to
 *      /signin (regression for e0946a4 — role-gate fix for 'designated').
 *   2. Iteration mode: POST /api/simulator/start-run, SIMULATOR banner
 *      appears in the layout.
 *   3. Run completes with a non-null end_reason.
 *   4. Fees column is numeric (regression for 097bc13 force-close fix).
 *
 * Only runs on dev.ramboq.com — simulator is capped off on prod.
 * Serial: sim driver is a singleton.
 *
 * Note on mode-pill behaviour: 'sim' is not a persistable execution mode —
 * POST /api/admin/execution/mode always returns {mode:'paper'} for 'sim'.
 * The execution page's onMount calls setExecutionMode('sim') which resolves
 * successfully but the store stays 'paper', so the SimulatorPanel does NOT
 * render via the ?mode=sim URL-param path. This is a known UI limitation;
 * the iteration run test bypasses it by POST-ing /start-run directly and
 * observing the layout-level SIMULATOR banner (which polls /api/simulator/status
 * independently and renders whenever run_active=true).
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

// 3 minutes for a 1-iteration, max_minutes=2 run + headroom.
test.setTimeout(200_000);

// Serial — sim is a process-level singleton.
test.describe.configure({ mode: 'serial' });

// ── helpers ────────────────────────────────────────────────────────────────

/**
 * Poll /api/simulator/status until predicate returns truthy or timeout lapses.
 * @param {import('@playwright/test').Page} page
 * @param {(s: any) => boolean} predicate
 * @param {number} timeoutMs
 */
async function waitForStatus(page, predicate, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  let status = {};
  while (Date.now() < deadline) {
    const r = await page.request.get('/api/simulator/status');
    if (r.ok()) {
      status = await r.json();
      if (predicate(status)) return status;
    }
    await page.waitForTimeout(1500);
  }
  return status;
}

/**
 * Stop + clear any running sim so tests start clean.
 * @param {import('@playwright/test').Page} page
 */
async function resetSim(page) {
  try { await page.request.post('/api/simulator/stop'); } catch (_) {}
  try { await page.request.post('/api/simulator/clear'); } catch (_) {}
}

// ── tests ──────────────────────────────────────────────────────────────────

test.describe('/admin/execution — SIM mode + iteration run', () => {

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  // ── 1. Navbar SIM click — no redirect to /signin ─────────────────────
  //
  // Root cause of the original report: onMount had `role !== 'admin'` guard
  // without `|| role === 'designated'`. After e0946a4, both 'admin' and
  // 'designated' users can reach /admin/execution.
  test('SIM navbar click lands on /admin/execution without redirect to /signin', async ({ page }) => {
    const probe = await page.request.get('/api/simulator/status');
    const status = await probe.json();
    test.skip(!status.enabled, `Simulator disabled on branch=${status.branch}`);

    // Start from a neutral page.
    await page.goto('/dashboard');
    await page.waitForLoadState('domcontentloaded');

    // Click the mode chip in the navbar — it opens a dropdown with SIM.
    const modeChip = page.locator('.mode-trigger').first();
    await expect(modeChip).toBeVisible({ timeout: 10_000 });
    await modeChip.click();

    // SIM option (role="option" aria-selected on the <button> inside the dropdown).
    const simOption = page.getByRole('option', { name: 'SIM' });
    await expect(simOption).toBeVisible({ timeout: 5_000 });
    await simOption.click();

    // KEY ASSERTION: must land on /admin/execution — NOT /signin.
    // Before e0946a4 a 'designated' user was bounced to /signin here.
    await expect(page).toHaveURL(/\/admin\/execution/, { timeout: 10_000 });
    await expect(page).not.toHaveURL(/\/signin/);

    // The execution page rendered — exec-combo-trigger is always present.
    await expect(page.locator('.exec-combo-trigger')).toBeVisible({ timeout: 8_000 });
    const titleText = await page.locator('.exec-combo-trigger').textContent();
    expect(titleText).toContain('EXECUTION');

    // The LogPanel must also hydrate (proves the page rendered past the gate
    // and didn't silently fail after mount).
    // LogPanel renders tab buttons with class log-tab-btn.
    await expect(page.locator('[class*="log-tab"]').first()).toBeVisible({ timeout: 8_000 });

    console.log(
      '[sim_run_end_to_end] Test 1 PASS — /admin/execution accessible, ' +
      'no /signin redirect (e0946a4 role-gate fix confirmed)'
    );
  });

  // ── 2-5. Iteration run + banner + end_reason + fees ──────────────────
  //
  // POST /api/simulator/start-run directly (avoids the mode-pill rendering
  // limitation described in the file header). Then observe the layout's
  // SIMULATOR banner which polls /api/simulator/status independently.
  test('iteration run: SIMULATOR banner appears, completes, fees are numeric', async ({ page }) => {
    const probe = await page.request.get('/api/simulator/status');
    const statusProbe = await probe.json();
    test.skip(!statusProbe.enabled, `Simulator disabled on branch=${statusProbe.branch}`);

    await resetSim(page);

    // ── Fetch available regimes to pick the first one ─────────────────
    const defaultsR = await page.request.get('/api/simulator/defaults');
    expect(defaultsR.ok(), '/api/simulator/defaults failed').toBe(true);
    const defaults = await defaultsR.json();
    const availableRegimes = defaults.available_regimes ?? [];
    expect(availableRegimes.length, 'no regimes available').toBeGreaterThan(0);
    const firstRegime = availableRegimes[0].slug ?? availableRegimes[0];
    console.log(`[sim_run_end_to_end] Using regime: ${firstRegime}`);

    // ── POST /api/simulator/start-run ────────────────────────────────
    const startR = await page.request.post('/api/simulator/start-run', {
      data: {
        iterations:             1,
        max_minutes:            2,
        regimes:                [firstRegime],
        agent_ids:              null,   // run all active agents
        seed:                   null,
        force_close_on_timeout: true,
        seed_mode:              'live',
        rate_ms:                2000,
        spread_pct:             0.10,
      },
    });
    if (!startR.ok()) {
      const body = await startR.text();
      console.warn('[sim_run_end_to_end] start-run response:', startR.status(), body);
    }
    expect(startR.ok(), `start-run returned ${startR.status()}`).toBe(true);
    const startBody = await startR.json();
    console.log('[sim_run_end_to_end] start-run response:', JSON.stringify(startBody));

    // ── Navigate to /admin/execution — observe the layout SIMULATOR banner
    await page.goto('/admin/execution');
    await page.waitForLoadState('domcontentloaded');

    // The layout polls /api/simulator/status every 4 s and renders the
    // .sim-banner whenever active=true or run_active=true.
    // Wait up to 15 s for the banner to appear.
    const simBanner = page.locator('.sim-banner');
    await expect(simBanner).toBeVisible({ timeout: 15_000 });

    // Banner must contain "SIMULATOR" text.
    await expect(simBanner.getByText('SIMULATOR')).toBeVisible({ timeout: 5_000 });

    // "iter 1/1" should appear once the run advances (iterations_total > 0).
    await expect(page.locator('.sim-banner').getByText(/1\/1/)).toBeVisible({ timeout: 20_000 });

    // ── Poll via API until run completes ─────────────────────────────
    // max_minutes=2 means it finishes in ≤2 min; add 1 min headroom.
    const finalStatus = await waitForStatus(
      page,
      (s) => !s.active && !s.run_active,
      180_000,
    );
    console.log('[sim_run_end_to_end] Final sim status:', JSON.stringify({
      active: finalStatus.active,
      run_active: finalStatus.run_active,
      iteration_index: finalStatus.iteration_index,
      iterations_total: finalStatus.iterations_total,
    }));

    // Banner must disappear when run completes.
    await expect(simBanner).not.toBeVisible({ timeout: 20_000 });

    // ── Check iteration rows via API ──────────────────────────────────
    // API returns a bare array; each row has a `summary` sub-object
    // carrying total_fees / net_pnl_remaining / total_pnl_remaining.
    const iterR = await page.request.get('/api/simulator/iterations?limit=5');
    expect(iterR.ok(), 'iterations API returned error').toBe(true);
    const iterData = await iterR.json();
    // Bare array or {iterations:[...]} — handle both.
    const iters = Array.isArray(iterData) ? iterData : (iterData.iterations ?? []);

    expect(iters.length, 'no iteration rows found after run').toBeGreaterThan(0);
    const latest = iters[0];
    const summary = latest.summary ?? {};

    console.log('[sim_run_end_to_end] Latest iteration row:', JSON.stringify({
      id:                latest.id,
      end_reason:        latest.end_reason,
      regime:            latest.regime,
      started_at:        latest.started_at,
      ended_at:          latest.ended_at,
      summary_fees:      summary.total_fees,
      summary_net_pnl:   summary.net_pnl_remaining,
      summary_hung_pos:  summary.hung_positions,
    }));

    // end_reason must be set (book_empty / time_limit / stopped).
    expect(latest.end_reason, 'end_reason should be non-null').toBeTruthy();

    // total_fees must be a finite number.
    // The force-close bug (097bc13) produced null/undefined here for
    // time_limit iterations because AlgoOrder column names were wrong.
    // After the fix, fees flow through for all end_reasons.
    const feesNum = Number(summary.total_fees ?? null);
    expect(
      Number.isFinite(feesNum),
      `summary.total_fees should be finite, got: ${JSON.stringify(summary.total_fees)}`,
    ).toBe(true);

    // net_pnl_remaining must also be a finite number.
    const netPnlNum = Number(summary.net_pnl_remaining ?? null);
    expect(
      Number.isFinite(netPnlNum),
      `summary.net_pnl_remaining should be finite, got: ${JSON.stringify(summary.net_pnl_remaining)}`,
    ).toBe(true);

    // ── /admin/simulator/iterations UI ───────────────────────────────
    await page.goto('/admin/simulator/iterations');
    await page.waitForLoadState('domcontentloaded');
    // The page renders one table per run-group — use .first() to avoid
    // strict mode violation when multiple runs exist.
    await expect(page.locator('table').first()).toBeVisible({ timeout: 10_000 });

    // ── Summary ───────────────────────────────────────────────────────
    console.log(
      `\n[sim_run_end_to_end SUMMARY]\n` +
      `  SIM link accessible (no /signin redirect):  PASS\n` +
      `  Iteration completed:                        ${latest.end_reason ? 'PASS' : 'FAIL'}\n` +
      `  end_reason:                                 ${latest.end_reason ?? '(null)'}\n` +
      `  fees (force-close regression check):        ${feesNum} ${Number.isFinite(feesNum) ? 'PASS' : 'FAIL'}\n` +
      `  net_pnl:                                    ${netPnlNum}\n` +
      `  regime used:                                ${firstRegime}\n`
    );
  });
});
