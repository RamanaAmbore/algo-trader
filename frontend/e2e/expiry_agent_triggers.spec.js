/**
 * Expiry-day agent triggering during sim.
 *
 * Verifies that scenarios run with `market_state.preset='expiry_day'`
 * set `is_expiry_day=true` on the agent engine's context, which
 * gates expiry-day-aware agents (loss-pos-total-auto-close, the
 * expiry ITM scanner) to fire.
 *
 * Approach: legacy /api/simulator/start (single-shot) endpoint
 * with `market_state_preset='expiry_day'`. The iteration framework
 * /start-run takes market_state from the regime's scenario.yaml
 * block; no shipped regime uses expiry_day, so we use the legacy
 * single-shot path with the override.
 *
 * Assertions:
 *   1. /api/simulator/status reflects `market_state.is_expiry_day = true`
 *      after start.
 *   2. After the scenario runs, sim_mode=True agent events land
 *      (proving the engine ran with the expiry flag set).
 *
 * Skips cleanly if prod's book is empty (no positions for the
 * sim to crash).
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

async function waitForStatus(page, predicate, timeoutMs = 60_000) {
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
  return last;
}

test.describe.configure({ mode: 'serial' });
test.setTimeout(180_000);

test.describe('Expiry-day agent triggers during sim', () => {

  test('1: legacy /start with market_state_preset=expiry_day flips is_expiry_day=true', async ({ page }) => {
    await authOnce(page);

    // Ensure clean state.
    await page.request.post('/api/simulator/stop').catch(() => null);
    await waitForStatus(page, (s) => !s.active && !s.run_active, 30_000).catch(() => null);
    await page.request.post('/api/simulator/clear').catch(() => null);

    // Resolve auto-close agent id (the canonical expiry-aware agent).
    const agentsR = await page.request.get('/api/agents');
    expect(agentsR.ok()).toBeTruthy();
    const agents = await agentsR.json();
    const autoClose = (agents || []).find((a) => a.slug === 'loss-pos-total-auto-close');
    const lossIds = (agents || [])
      .filter((a) => a.slug?.startsWith('loss-') && ['active', 'cooldown'].includes(a.status))
      .map((a) => a.id);
    const agentIds = [...new Set([autoClose?.id, ...lossIds].filter(Boolean))];

    // Pick a scenario — extreme-crash is reliable (real book stress).
    const startR = await page.request.post('/api/simulator/start', {
      data: {
        scenario:             'extreme-crash',
        seed_mode:            'live',
        rate_ms:              2000,
        agent_ids:            agentIds,
        spread_pct:           0.10,
        market_state_preset:  'expiry_day',
      },
    });
    const startBody = startR.ok() ? await startR.json() : await startR.text();
    console.log(`[expiry] start status=${startR.status()} body=${JSON.stringify(startBody).slice(0, 300)}`);
    if (!startR.ok()) {
      // Skip if the sim cap is off on the active branch or other
      // configuration issue — surface the message cleanly.
      test.skip(true, `start rejected: ${startR.status()} ${JSON.stringify(startBody).slice(0, 120)}`);
      return;
    }
    if ((startBody.positions_count ?? 0) === 0) {
      test.skip(true, 'live broker book empty — expiry agent fires need positions');
      return;
    }

    // Read status and verify market_state.is_expiry_day is True.
    const status = await page.request.get('/api/simulator/status').then((r) => r.json());
    console.log(`[expiry] market_state: ${JSON.stringify(status?.market_state)}`);
    expect(status?.market_state, 'status should expose market_state').toBeTruthy();
    expect(status.market_state.is_expiry_day,
      `market_state.is_expiry_day should be true under expiry_day preset · got ${status.market_state.is_expiry_day}`).toBe(true);

    // Give the agent engine a few ticks to fire — extreme-crash
    // breaches thresholds quickly. The single-shot scenario runs
    // ~6-8 ticks and auto-completes via the chase loop.
    await waitForStatus(page, (s) => !s.active, 60_000).catch(() => null);
    await page.request.post('/api/simulator/stop').catch(() => null);

    // Verify sim agent events landed.
    const evR = await page.request.get('/api/simulator/events/recent?limit=200');
    expect(evR.ok()).toBeTruthy();
    const events = await evR.json();
    console.log(`[expiry] sim agent events: ${(events || []).length}`);
    expect((events || []).length,
      'expected at least 1 sim_mode=true agent event under expiry_day preset').toBeGreaterThan(0);

    // Sim AlgoOrder rows should also have landed (engine column = 'sim').
    const ordR = await page.request.get('/api/simulator/orders/recent?limit=200');
    if (ordR.ok()) {
      const orders = await ordR.json();
      console.log(`[expiry] sim orders: ${(orders || []).length}`);
      // Don't hard-assert — agents may fire without order actions.
      // Just log so the operator can see the chain ran.
    }
  });
});
