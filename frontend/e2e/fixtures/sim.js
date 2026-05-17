/**
 * Simulator helpers — start an agent in the sim, poll for fire, clean up.
 *
 * Every spec under e2e/ that exercises the agent engine in paper-mode
 * style end-to-end uses these. They live in fixtures/ alongside the
 * shared auth helper so the per-spec setup stays one or two lines.
 *
 * All calls go through `page.request` so they ride the Playwright
 * session cookies + the JWT planted by loginAsAdmin. Any non-2xx is a
 * test failure — the simulator is admin-only and should never 401 in a
 * green run.
 */

/**
 * Stop + clear any in-flight sim state. Safe to call from beforeEach
 * AND afterEach without checking — both routes are idempotent.
 *
 * @param {import('@playwright/test').APIRequestContext} request
 */
export async function resetSim(request) {
  await request.post('/api/simulator/stop').catch(() => {});
  await request.post('/api/simulator/clear').catch(() => {});
}

/**
 * Start the per-agent synthesized scenario for `agentId`.
 *
 * Backend route: POST /api/simulator/start-for-agent/{id}
 *   - walks the agent's condition tree
 *   - synthesizes the right SimDriver primitive (target_pnl /
 *     set_margin / etc.) for the leaf metric
 *   - starts the sim with bypass_schedule=True and only_agent_ids=[id]
 *
 * Returns the JSON status the backend hands back.
 *
 * @param {import('@playwright/test').APIRequestContext} request
 * @param {number} agentId
 */
export async function startForAgent(request, agentId) {
  const r = await request.post(`/api/simulator/start-for-agent/${agentId}`);
  if (!r.ok()) {
    throw new Error(`start-for-agent ${agentId} failed: ${r.status()} ${await r.text()}`);
  }
  return r.json();
}

/**
 * Poll /api/simulator/status + /events/recent until the sim has
 * ticked at least once AND the target agent has fired (or the
 * timeout expires).
 *
 * The /events/recent payload is a flat list of AgentEvent rows. The
 * filter that matters is `agent_id` — `sim_mode=True` is already
 * applied server-side by the route, and `agent_slug` isn't projected
 * into the response. We additionally try to force a run_cycle after
 * the sim auto-stops, since the synthesized scenarios run only 3
 * ticks and the engine may have batched run_cycle calls.
 *
 * Returns the matching event row on success; throws on timeout.
 *
 * @param {import('@playwright/test').APIRequestContext} request
 * @param {number} agentId
 * @param {number} timeoutMs
 */
export async function waitForAgentFire(request, agentId, timeoutMs = 30_000) {
  const start = Date.now();
  let lastStatus = null;
  let nudgedAfterStop = false;
  while (Date.now() - start < timeoutMs) {
    const r = await request.get('/api/simulator/status');
    if (!r.ok()) throw new Error(`status: ${r.status()}`);
    const s = await r.json();
    lastStatus = s;
    if (s.tick_index >= 1) {
      const ev = await request.get('/api/simulator/events/recent?limit=20');
      if (ev.ok()) {
        const body = await ev.json();
        const events = Array.isArray(body) ? body : (body.events ?? []);
        const hit = events.find(/** @param {any} e */ (e) => Number(e.agent_id) === Number(agentId));
        if (hit) return { status: s, event: hit };
      }
    }
    // Synthesized scenarios run only `total_ticks` ticks then halt.
    // If the sim has hit its tick budget without firing, give run_cycle
    // a final nudge — the rate-window agents in particular need an
    // explicit final evaluation to see the full window. One-shot nudge.
    if (!nudgedAfterStop && s.tick_index >= (s.total_ticks ?? 999)) {
      nudgedAfterStop = true;
      await request.post('/api/simulator/run-cycle').catch(() => {});
    }
    await new Promise((res) => setTimeout(res, 700));
  }
  throw new Error(
    `waitForAgentFire timeout (${timeoutMs}ms) for agent_id=${agentId} — last status: `
    + JSON.stringify(lastStatus).slice(0, 200),
  );
}

/**
 * GET /api/simulator/orders/recent and filter to rows tagged with the
 * target agent id.
 *
 * @param {import('@playwright/test').APIRequestContext} request
 * @param {number} agentId
 */
export async function findSimOrders(request, agentId) {
  const r = await request.get('/api/simulator/orders/recent?limit=50');
  if (!r.ok()) return [];
  const body = await r.json();
  const orders = Array.isArray(body) ? body : (body.orders ?? []);
  return orders.filter(/** @param {any} o */ (o) => Number(o.agent_id) === Number(agentId));
}

/**
 * Fetch the loss agents list once and return a slug → id map. Used
 * by every Part-A test to avoid hard-coding numeric IDs (which can
 * shift across reseed runs).
 *
 * @param {import('@playwright/test').APIRequestContext} request
 */
export async function lossAgentIds(request) {
  const r = await request.get('/api/agents/');
  if (!r.ok()) throw new Error(`agents fetch: ${r.status()}`);
  const body = await r.json();
  const agents = Array.isArray(body) ? body : (body.agents ?? []);
  /** @type {Record<string, number>} */
  const map = {};
  for (const a of agents) {
    if (typeof a?.slug === 'string' && a.slug.startsWith('loss-')) {
      map[a.slug] = a.id;
    }
  }
  return map;
}
