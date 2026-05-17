/**
 * Agents in paper-trading mode — exercised end-to-end via the Simulator.
 *
 * The same `run_cycle` that drives Mode-2 paper trading on prod also
 * drives the Simulator on dev. The only material differences are the
 * quote source (fabricated vs. live broker) and the row's `mode` tag
 * (`sim` vs. `paper`). So firing an agent through the simulator
 * validates the full Mode-2 path without needing live Kite quotes.
 *
 * We synthesize per-agent scenarios via /api/simulator/start-for-agent
 * (no scenarios.yaml edit needed — the backend walks the agent's
 * condition tree and picks the right primitive: target_pnl / set_margin
 * / etc.). Each spec runs ONE agent so failures localise.
 *
 * Serial mode: the simulator is a singleton, so this entire file runs
 * sequentially to avoid one test's start-for-agent stomping on
 * another's run.
 *
 * Cleanup: afterEach POSTs /stop + /clear so a flaky test doesn't leak
 * a running sim into the next case.
 *
 * Scope notes:
 *   - We deliberately target TOTAL-scope agents (loss-pos-total-*,
 *     loss-funds-*). The per-account synthesizer (`positions.any_acct`
 *     scope) splits the synthetic target P&L across both scripted
 *     accounts, which divides each below the threshold — the agent
 *     doesn't fire. That's a synthesizer limitation worth filing
 *     separately; not what these tests exercise.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin }  from './fixtures/auth.js';
import {
  resetSim, startForAgent, waitForAgentFire, findSimOrders, lossAgentIds,
} from './fixtures/sim.js';

// Whole file serial — sim is a process-level singleton.
test.describe.configure({ mode: 'serial' });

// Sim cases take ~15-30 s each (3 scripted ticks at 2 s + run_cycle
// + occasional retry). Bump the per-test timeout so the helper's
// poll loop has headroom.
test.setTimeout(90_000);

const FIRE_TIMEOUT = 45_000;

test.describe('Agents · paper trading via simulator', () => {

  /** @type {Record<string, number>} */
  let agentMap = {};

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await loginAsAdmin(page);
    const probe = await page.request.get('/api/simulator/status');
    expect(probe.ok(), 'simulator status probe failed').toBe(true);
    const status = await probe.json();
    if (!status.enabled) {
      test.skip(true, `simulator disabled on branch=${status.branch}`);
    }
    agentMap = await lossAgentIds(page.request);
    await ctx.close();
  });

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await resetSim(page.request);
  });

  test.afterEach(async ({ page }) => {
    await resetSim(page.request).catch(() => {});
  });

  // ── Case 1: total positions absolute P&L threshold (₹) ──────────────
  test('loss-pos-total-static-abs fires via target_pnl', async ({ page }) => {
    const id = agentMap['loss-pos-total-static-abs'];
    test.skip(!id, 'agent not seeded');

    await startForAgent(page.request, id);
    const { event } = await waitForAgentFire(page.request, id, FIRE_TIMEOUT);

    expect(event.agent_id).toBe(id);
    expect(event.event_type).toBe('triggered');
    // Trigger condition string carries the metric + threshold + actual.
    expect(event.trigger_condition).toMatch(/positions\.total\s+pnl/);
    expect(event.trigger_condition).toMatch(/<=/);

    // This agent has no actions configured (alerting-only) → no
    // AlgoOrder row written. If a future seed adds actions, the
    // assertion auto-tightens (length > 0).
    const orders = await findSimOrders(page.request, id);
    expect(orders.length, 'unexpected algo_orders rows for alerting agent').toBe(0);
  });

  // ── Case 2: funds — cash negative (set_margin primitive) ────────────
  test('loss-funds-cash-negative fires via set_margin', async ({ page }) => {
    const id = agentMap['loss-funds-cash-negative'];
    test.skip(!id, 'agent not seeded');

    await startForAgent(page.request, id);
    const { event } = await waitForAgentFire(page.request, id, FIRE_TIMEOUT);

    expect(event.agent_id).toBe(id);
    expect(event.trigger_condition).toMatch(/cash/i);
    // Funds agents are alerting-only — no AlgoOrder row expected.
    const orders = await findSimOrders(page.request, id);
    expect(orders.length, 'funds agent should not write AlgoOrder').toBe(0);
  });

  // ── Case 3: funds — avail_margin negative (set_margin primitive) ────
  test('loss-funds-margin-negative fires via set_margin', async ({ page }) => {
    const id = agentMap['loss-funds-margin-negative'];
    test.skip(!id, 'agent not seeded');

    await startForAgent(page.request, id);
    const { event } = await waitForAgentFire(page.request, id, FIRE_TIMEOUT);

    expect(event.agent_id).toBe(id);
    expect(event.trigger_condition).toMatch(/margin/i);
  });

  // ── Case 4: total positions percentage P&L threshold ────────────────
  // Validates the synthesizer's `_margin_rows_with_total` + scope-aware
  // denominator scaling. Earlier the TOTAL margin row was missing so
  // `ctx.used_margin_for('TOTAL')` returned None and the agent never
  // fired (commit 944cfa1 fixed this).
  test('loss-pos-total-static-pct fires via target_pnl sized to TOTAL util margin', async ({ page }) => {
    const id = agentMap['loss-pos-total-static-pct'];
    test.skip(!id, 'agent not seeded');

    await startForAgent(page.request, id);
    const { event } = await waitForAgentFire(page.request, id, FIRE_TIMEOUT);

    expect(event.agent_id).toBe(id);
    expect(event.trigger_condition).toMatch(/pnl_pct/);
  });

  // ── Case 5: per-account positions absolute P&L threshold ────────────
  // Validates the synthesizer's `any_acct`-aware per-account share —
  // earlier the target was divided across both scripted accounts, so
  // each ended up at half the threshold and the agent never crossed.
  test('loss-pos-acct-static-abs fires with the FULL target on each account', async ({ page }) => {
    const id = agentMap['loss-pos-acct-static-abs'];
    test.skip(!id, 'agent not seeded');

    await startForAgent(page.request, id);
    const { event } = await waitForAgentFire(page.request, id, FIRE_TIMEOUT);

    expect(event.agent_id).toBe(id);
    expect(event.trigger_condition).toMatch(/positions\.any_acct\s+pnl/);
  });

  // ── Case 6: total positions ₹/min rate metric ───────────────────────
  // Validates the engine's pnl_history writer (commit 944cfa1).
  // Before that fix, alert_state.pnl_history was never populated by
  // the v2 engine, so rate metric resolvers returned None on every
  // tick and every rate agent silently never fired — both in prod and
  // in sim.
  test('loss-pos-total-rate-abs fires once history accumulates', async ({ page }) => {
    const id = agentMap['loss-pos-total-rate-abs'];
    test.skip(!id, 'agent not seeded');

    await startForAgent(page.request, id);
    // 60 s — rate scenarios run window_min+1 ticks at 2 s spacing and
    // need run_cycle to accumulate ≥2 history samples before the rate
    // evaluator can compute a slope.
    const { event } = await waitForAgentFire(page.request, id, 60_000);

    expect(event.agent_id).toBe(id);
    expect(event.trigger_condition).toMatch(/pnl_rate_abs/);
  });

  // ── UI smoke: /agents page surfaces the fired event in its panel ────
  test('agents page shows the fired event in its scoped event panel', async ({ page }) => {
    const id = agentMap['loss-pos-total-static-abs'];
    test.skip(!id, 'agent not seeded');

    await startForAgent(page.request, id);
    await waitForAgentFire(page.request, id, FIRE_TIMEOUT);

    await page.goto('/agents');
    // /agents renders the agent row with the slug visible AND a
    // scoped event panel below. The slug is the most stable text;
    // it surfaces in both the agent name and the per-agent badge.
    // Poll for visibility — the page hydrates over several render
    // passes and the strict-equality locator can race the mount.
    await page.waitForLoadState('domcontentloaded');
    await expect(
      page.getByText('loss-pos-total-static-abs').first()
    ).toBeVisible({ timeout: 60_000 });
  });
});
