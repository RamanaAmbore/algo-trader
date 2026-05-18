/**
 * Agent triggering during sim → AlgoOrder(mode='sim') chain.
 *
 * Verifies the full chain that the recent prod sweep proved out:
 *   - Iteration sim runs with `loss-pos-total-auto-close` in
 *     `agent_ids` (so the engine treats it as active for the run).
 *   - The extreme-crash regime breaches the agent's threshold.
 *   - run_cycle fires the agent; its chase_close_positions /
 *     close_position actions write AlgoOrder rows.
 *   - sim_mode=True → _resolve_mode returns 'sim' regardless of
 *     master mode, so the rows land as mode='sim'.
 *
 * Two assertions on the iteration outcome (data confirmed by the
 * sweep landed earlier in this session — 200 events + 117 sim
 * orders across 6 regimes):
 *   1. At least one AlgoOrder(mode='sim') row is created during
 *      the iteration window.
 *   2. At least one agent_event with sim_mode=True for any agent
 *      that fired (target slug not asserted — prod's current book
 *      may breach different thresholds than expected).
 *
 * Uses iteration mode so the run self-terminates cleanly. Live
 * book seed is required (extreme-crash needs positions to crash).
 * Skips gracefully when prod's book is empty.
 */

import { test, expect } from '@playwright/test';

const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedAuth = null;

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

async function waitForStatus(page, predicate, timeoutMs = 180_000) {
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
  throw new Error(`waitForStatus timeout · last=${JSON.stringify(last)}`);
}

test.describe.configure({ mode: 'serial' });
test.setTimeout(300_000);   // 5 min — iteration (≤2 min) + setup/teardown headroom

test.describe('Agent triggering during sim', () => {

  test('extreme-crash sim with auto-close agent → mode=sim orders land', async ({ page }) => {
    await authOnce(page);

    // Ensure no prior sim is running; wipe past sim rows so we have a
    // clean assertion window. POST /stop returns immediately but the
    // driver's run_active flag may stay True for a few seconds — poll
    // until the engine reports idle before proceeding.
    await page.request.post('/api/simulator/stop').catch(() => null);
    await waitForStatus(page, (s) => !s.active && !s.run_active, 30_000).catch(() => null);
    await page.request.post('/api/simulator/clear').catch(() => null);

    // Find the auto-close agent's id — needed to include in agent_ids
    // so the engine treats it as active during the sim (the agent
    // ships with status='inactive' by default).
    const agentsR = await page.request.get('/api/agents');
    expect(agentsR.ok(), `GET /api/agents ${agentsR.status()}`).toBeTruthy();
    const agents = await agentsR.json();
    const autoClose = (agents || []).find((a) => a.slug === 'loss-pos-total-auto-close');
    if (!autoClose) {
      test.skip(true, 'loss-pos-total-auto-close agent not seeded');
      return;
    }

    // Also include the active loss-* agents so we don't depend on
    // exactly one threshold breach. Empty positions on prod won't
    // fire anything; we'll skip cleanly in that case.
    const activeLossIds = (agents || [])
      .filter((a) => a.slug?.startsWith('loss-') && ['active', 'cooldown'].includes(a.status))
      .map((a) => a.id);
    const agentIds = [...new Set([autoClose.id, ...activeLossIds])];
    console.log(`[agent_triggering] agent_ids=${agentIds.join(',')}`);

    // Kick off the iteration. extreme-crash hits hardest; the live
    // book is the source so we need real positions to crash.
    const startR = await page.request.post('/api/simulator/start-run', {
      data: {
        iterations: 1, max_minutes: 2,
        regimes: ['extreme-crash'],
        agent_ids: agentIds,
        seed: 42,
        force_close_on_timeout: true,
        seed_mode: 'live',
        rate_ms: 2000,
        spread_pct: 0.10,
      },
    });
    const startBody = await startR.json();
    console.log(`[agent_triggering] start-run status=${startR.status()} positions_count=${startBody.positions_count ?? 'N/A'}`);
    expect(startR.ok(), `start-run ${startR.status()}: ${JSON.stringify(startBody)}`).toBe(true);
    if ((startBody.positions_count ?? 0) === 0) {
      test.skip(true, 'live broker book empty — agent fires need positions');
      return;
    }

    // Wait for the iteration to fully finish.
    const final = await waitForStatus(page, (s) => !s.active && !s.run_active, 240_000);
    console.log(`[agent_triggering] iteration finished · end_reason=${final.iteration_end_reason}`);

    // 1. Sim-mode AlgoOrder rows should have been created. The sweep
    //    earlier in this session generated 117 from 6 iterations;
    //    a single extreme-crash run should produce at least a few.
    const ordersR = await page.request.get('/api/simulator/orders/recent?limit=200');
    expect(ordersR.ok(), `orders/recent ${ordersR.status()}`).toBeTruthy();
    const orders = await ordersR.json();
    console.log(`[agent_triggering] sim orders this run: ${(orders || []).length}`);
    expect((orders || []).length, 'expected at least 1 mode=sim AlgoOrder row').toBeGreaterThan(0);

    // Validate the row shape: AlgoOrderInfo exposes `engine: "sim"` (the
    // DB mode column is serialised as `engine` by the simulator routes).
    // Side must be SELL for long-position close, BUY for short.
    const sample = (orders || [])[0];
    expect(sample.engine, 'AlgoOrder.engine should be "sim"').toBe('sim');
    expect(['BUY', 'SELL']).toContain(sample.transaction_type);
    expect(sample.symbol, 'AlgoOrder.symbol should be non-empty').toBeTruthy();

    // 2. Sim-mode agent events should also have landed.
    const evR = await page.request.get('/api/simulator/events/recent?limit=200');
    expect(evR.ok(), `events/recent ${evR.status()}`).toBeTruthy();
    const events = await evR.json();
    console.log(`[agent_triggering] sim agent events: ${(events || []).length}`);
    expect((events || []).length, 'expected at least 1 sim agent event').toBeGreaterThan(0);
  });
});
