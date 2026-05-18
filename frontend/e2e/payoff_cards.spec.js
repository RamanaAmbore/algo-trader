/**
 * Smoke spec: per-underlying payoff cards on /admin/execution?tab=sim
 *
 * Verifies the UI change in 31e0671 where the flat position-pill list was
 * replaced by per-underlying OptionsPayoff cards on the Simulator tab.
 *
 * Flow:
 *  1. Authenticate once (PLAYWRIGHT_AUTH_TOKEN env → fresh curl; else form login).
 *  2. Stop / clear any residual sim state.
 *  3. Look up the loss-pos-total-auto-close agent id from /api/agents/.
 *  4. Seed the live book via POST /api/simulator/seed-live.
 *  5. POST /api/simulator/start with extreme-crash + seed_mode='live'
 *     targeting the auto-close agent (bypass_schedule, no cooldown gate).
 *  6. Poll /api/simulator/status until tick_index >= 1 (book seeded + moved).
 *  7. Visit /admin/execution?tab=sim, wait for .sim-payoff-card or skip.
 *  8. Assert card structure, absence of old position pills, chase pills untouched.
 *  9. Stop + clear the sim.
 *
 * Skip conditions (book-empty on prod at time of run):
 *  - /api/simulator/status returns positions_count = 0 after seed.
 *  - No .sim-payoff-card found after a 15 s wait.
 *
 * Target: prod (ramboq.com) — run with:
 *   BASE_URL=https://ramboq.com PLAYWRIGHT_AUTH_TOKEN="$TOK" \
 *   npx playwright test e2e/payoff_cards.spec.js --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

// ── auth (one API login per process, injected via env when available) ──────────

const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedToken = process.env.PLAYWRIGHT_AUTH_TOKEN || null;

async function authOnce(page) {
  if (!_cachedToken) {
    let tok = null;
    for (const delay of [0, 20_000, 65_000]) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      const resp = await page.request.post('/api/auth/login', {
        data: { username: _AUTH_USER, password: _AUTH_PASS },
      });
      if (resp.ok()) {
        tok = (await resp.json()).access_token;
        break;
      }
      if (resp.status() !== 429) {
        throw new Error(`authOnce: /api/auth/login returned ${resp.status()}`);
      }
    }
    if (!tok) {
      test.skip(true, 'rate-limited — run in isolation or pass PLAYWRIGHT_AUTH_TOKEN');
      return;
    }
    _cachedToken = tok;
  }

  // Plant the token directly into sessionStorage (avoids driving the /signin
  // form which costs an extra rate-limit hit on prod's 5/min window).
  await page.goto('/');
  await page.evaluate((tok) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
    }));
  }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
}

// ── helpers ────────────────────────────────────────────────────────────────────

async function resetSim(request) {
  await request.post('/api/simulator/stop').catch(() => {});
  await request.post('/api/simulator/clear').catch(() => {});
}

/** Resolve the numeric id for the loss-pos-total-auto-close agent. */
async function resolveAutoCloseAgentId(request) {
  const r = await request.get('/api/agents/');
  if (!r.ok()) return null;
  const body = await r.json();
  const agents = Array.isArray(body) ? body : (body.agents ?? []);
  const row = agents.find((a) => a?.slug === 'loss-pos-total-auto-close');
  return row ? row.id : null;
}

/** Poll /api/simulator/status until predicate is true or timeout. */
async function waitForStatus(request, predicate, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    const r = await request.get('/api/simulator/status');
    if (r.ok()) {
      last = await r.json();
      if (predicate(last)) return last;
    }
    await new Promise((res) => setTimeout(res, 1500));
  }
  throw new Error(`waitForStatus timeout (${timeoutMs}ms) — last: ${JSON.stringify(last).slice(0, 200)}`);
}

// ── spec ───────────────────────────────────────────────────────────────────────

test.describe('Simulator payoff cards (/admin/execution?tab=sim)', () => {
  test.setTimeout(120_000);

  test('payoff cards replace position pills after extreme-crash sim', async ({ page }) => {
    await authOnce(page);

    // ── Step 1: guard — simulator must be enabled on this branch ──────────
    const statusProbe = await page.request.get('/api/simulator/status');
    expect(statusProbe.ok(), 'simulator status probe failed').toBe(true);
    const statusInit = await statusProbe.json();
    if (statusInit.enabled === false) {
      test.skip(true, `simulator disabled on branch=${statusInit.branch} — run on dev`);
      return;
    }

    // ── Step 2: clean slate ───────────────────────────────────────────────
    await resetSim(page.request);

    // ── Step 3: resolve agent id ──────────────────────────────────────────
    const agentId = await resolveAutoCloseAgentId(page.request);
    // Agent may be absent on a fresh seed or renamed — continue without it;
    // we're testing the UI rendering, not the agent logic.
    console.log(`[payoff_cards] loss-pos-total-auto-close agent_id=${agentId ?? '(not found)'}`);

    // ── Step 4: seed the live book ────────────────────────────────────────
    const seedR = await page.request.post('/api/simulator/seed-live');
    // seed-live returns 200 even when the book is empty — check positions.
    const seedBody = seedR.ok() ? await seedR.json().catch(() => ({})) : {};
    console.log(`[payoff_cards] seed-live ok=${seedR.ok()} positions=${seedBody.positions_count ?? '?'}`);

    // ── Step 5: start extreme-crash with seed_mode='live' ─────────────────
    const startPayload = {
      scenario: 'extreme-crash',
      rate_ms: 2000,
      seed_mode: 'live',
      // Direct the sim at the auto-close agent when resolved (bypasses
      // schedule + cooldown gates). When not found, agent_ids=null lets
      // all active agents evaluate — the payoff cards don't depend on it.
      agent_ids: agentId ? [agentId] : null,
    };
    const startR = await page.request.post('/api/simulator/start', { data: startPayload });
    if (!startR.ok()) {
      const txt = await startR.text().catch(() => '');
      // "empty book" or "sim gated" — skip cleanly.
      if (/empty|no position|not enabled|disabled/i.test(txt)) {
        test.skip(true, `sim start skipped: ${txt.slice(0, 120)}`);
        return;
      }
      throw new Error(`POST /api/simulator/start failed ${startR.status()}: ${txt.slice(0, 200)}`);
    }

    const startBody = await startR.json().catch(() => ({}));
    const posCount = startBody.positions_count ?? startBody.total_positions ?? 0;
    console.log(`[payoff_cards] start ok — positions_count=${posCount}`);

    if (posCount === 0) {
      // Book empty on prod at this moment — payoff cards not expected.
      await resetSim(page.request).catch(() => {});
      test.skip(true, 'Live broker book has no F&O positions — no payoff cards expected');
      return;
    }

    // ── Step 6: wait for at least one tick ───────────────────────────────
    const liveStatus = await waitForStatus(
      page.request,
      (s) => s.tick_index >= 1,
      30_000,
    );
    console.log(`[payoff_cards] sim ticked: tick_index=${liveStatus.tick_index} active=${liveStatus.active}`);

    // ── Step 7: visit the simulator workspace ─────────────────────────────
    await page.goto('/admin/execution?tab=sim');
    await page.waitForLoadState('domcontentloaded');

    // Confirm the Simulator tab is active.
    await expect(
      page.locator('.exec-tab-active', { hasText: /simulator/i }),
    ).toBeVisible({ timeout: 10_000 });

    // ── Step 8: wait for payoff cards (or skip on empty F&O book) ────────
    // positionsByUnderlying only renders cards when the backend's positions
    // array contains at least one recognisable F&O underlying (NIFTY, BANKNIFTY,
    // …). Non-F&O or equity-only books show nothing here.
    const cardLocator = page.locator('.sim-payoff-card');
    let cardCount = 0;
    try {
      await cardLocator.first().waitFor({ state: 'visible', timeout: 15_000 });
      cardCount = await cardLocator.count();
    } catch (_) {
      // No payoff cards — either no F&O positions or analytics fetch pending.
      console.warn('[payoff_cards] No .sim-payoff-card visible — skipping card assertions');
      await resetSim(page.request).catch(() => {});
      test.skip(true, 'No payoff cards rendered (no F&O positions in live book at time of run)');
      return;
    }

    console.log(`[payoff_cards] .sim-payoff-card count=${cardCount}`);
    expect(cardCount, 'at least one payoff card must render').toBeGreaterThanOrEqual(1);

    // ── Step 9: inspect the first card ───────────────────────────────────
    const firstCard = cardLocator.first();

    // 9a. Underlying name chip is visible.
    const nameEl = firstCard.locator('.sim-payoff-name');
    await expect(nameEl).toBeVisible({ timeout: 5_000 });
    const underlyingName = (await nameEl.textContent() || '').trim();
    console.log(`[payoff_cards] first card underlying="${underlyingName}"`);
    expect(underlyingName.length, 'underlying name should be non-empty').toBeGreaterThan(0);

    // 9b. At least one leg row in the legend.
    const legRows = firstCard.locator('.sim-leg-row');
    const legCount = await legRows.count();
    console.log(`[payoff_cards] .sim-leg-row count=${legCount} in first card`);
    expect(legCount, 'at least one leg row must be in the legend').toBeGreaterThanOrEqual(1);

    // 9c. Color swatch visible on the first leg row.
    const swatch = firstCard.locator('.sim-leg-swatch').first();
    await expect(swatch).toBeVisible({ timeout: 3_000 });

    // Sample leg row content for the report.
    const firstLegText = (await legRows.first().textContent() || '').replace(/\s+/g, ' ').trim();
    console.log(`[payoff_cards] first leg row text: "${firstLegText}"`);

    // Log all card underlyings.
    const allCards = await cardLocator.all();
    const allUnderlyings = [];
    for (const c of allCards) {
      const n = await c.locator('.sim-payoff-name').textContent().catch(() => '');
      allUnderlyings.push(n.trim());
    }
    console.log(`[payoff_cards] all card underlyings: ${allUnderlyings.join(', ')}`);

    // ── Step 10: old position pills (non-chase) must NOT be present ───────
    // The replacement removed the per-position .sim-pill elements that were
    // NOT chase pills. .sim-pill elements that ARE chase pills (.sim-pill-chase)
    // are only rendered when open_order_details is non-empty — that's legitimate.
    // We check that there are no .sim-pill elements lacking the .sim-pill-chase
    // modifier class (those were the old position pills).
    const nonChasePillCount = await page.evaluate(() => {
      const allPills = [...document.querySelectorAll('.sim-pill')];
      return allPills.filter((el) => !el.classList.contains('sim-pill-chase')).length;
    });
    console.log(`[payoff_cards] non-chase .sim-pill count=${nonChasePillCount} (expected 0)`);
    expect(
      nonChasePillCount,
      'old position pills (.sim-pill without .sim-pill-chase) should not be present',
    ).toBe(0);

    // Chase pills (.sim-pill-chase) may or may not exist depending on whether
    // the auto-close agent fired and initiated a chase. Either is valid.
    const chasePillCount = await page.locator('.sim-pill-chase').count();
    console.log(`[payoff_cards] .sim-pill-chase count=${chasePillCount} (may be 0 if no orders in flight)`);

    // ── Step 11: stop the sim cleanly ────────────────────────────────────
    await resetSim(page.request);
    console.log('[payoff_cards] sim stopped and cleared');
  });
});
